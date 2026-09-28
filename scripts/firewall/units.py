#!/usr/bin/env python3
"""
units.py - Split command output into screenable units and reassemble with
per-unit redactions.

The whole point of unit-level screening: one suspicious message gets redacted
to a stub while the other 19 flow through, instead of one false positive
blocking the entire read.

Three output shapes are recognized:

  json   - the whole output is one JSON value (gog style). Each item of every
           top-level list-of-dicts becomes a unit; everything else is the
           "envelope" unit. A blocked item is replaced by a stub.
  jsonl  - one JSON object per line (imsg style), separator lines allowed.
           Each line is a unit; blocked JSON lines become stub objects.
  text   - anything else. Single unit, all-or-nothing.

Redaction stubs never include attacker-controllable strings unless those
strings are individually pattern-clean.

Python 3.9 compatible. Stdlib only.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import patterns

# Structural metadata copied into redaction stubs. Free-text, attacker-
# controllable fields (sender_name, subject, body, text, snippet) are
# deliberately EXCLUDED — they can't be safely shown in a "blocked" record.
# Only identifiers, booleans/numbers, and strictly-validated timestamps survive.
_STUB_SCALAR_KEYS = ["id", "chat_id", "is_from_me", "messageCount"]
_STUB_TIMESTAMP_KEYS = ["created_at", "date", "start", "end"]

# Strict shapes for values allowed into a stub. Anything not matching is dropped.
_RE_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?([.\d]*)?"
                              r"([+-]\d{2}:?\d{2}|Z)?)?$")
_RE_ID_TOKEN = re.compile(r"^[A-Za-z0-9_.:#/-]{1,80}$")


class Redaction:
    """One redacted unit, for the stderr report."""

    __slots__ = ("label", "reason", "location", "detector")

    def __init__(self, label: str, reason: Optional[str],
                 location: Optional[str], detector: Optional[str]):
        self.label = label
        self.reason = reason
        self.location = location
        self.detector = detector


def _safe_timestamp(value: Any) -> Optional[Any]:
    """Return value only if it's a strict ISO date/datetime (or a {dateTime|date} dict)."""
    if isinstance(value, str) and _RE_ISO_DATETIME.match(value):
        return value
    if isinstance(value, dict):  # calendar start/end objects
        out = {}
        for k in ("dateTime", "date", "timeZone"):
            v = value.get(k)
            if k == "timeZone" and isinstance(v, str) and _RE_ID_TOKEN.match(v):
                out[k] = v
            elif isinstance(v, str) and _RE_ISO_DATETIME.match(v):
                out[k] = v
        return out or None
    return None


def _stub_fields(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Copy ONLY non-attacker-controllable structural metadata into a stub.
    Identifiers must look like id tokens; timestamps must be strict ISO;
    free-text fields are never copied. This guarantees no attacker payload
    survives in a record the agent will read as 'blocked'.
    """
    fields: Dict[str, Any] = {}
    for key in _STUB_SCALAR_KEYS:
        if key not in obj:
            continue
        value = obj[key]
        if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
            fields[key] = value
        elif isinstance(value, str) and _RE_ID_TOKEN.match(value):
            fields[key] = value
    for key in _STUB_TIMESTAMP_KEYS:
        if key in obj:
            ts = _safe_timestamp(obj[key])
            if ts is not None:
                fields[key] = ts
    return fields


def _stub(obj: Optional[Dict[str, Any]], reason: Optional[str],
          detector: Optional[str] = None) -> Dict[str, Any]:
    stub: Dict[str, Any] = {"firewall_blocked": True,
                            "reason": patterns.sanitize_reason(reason, detector)}
    if obj:
        stub.update(_stub_fields(obj))
    return stub


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class UnitizedOutput:
    """
    Holds the split units of one command's output.

    Usage:
        uo = split(raw_output)
        verdicts = screener.screen_units(uo.unit_texts())
        output, redactions, all_blocked = uo.reassemble(verdicts)
    """

    def __init__(self, kind: str, raw: str):
        self.kind = kind          # "json" | "jsonl" | "text"
        self.raw = raw
        self._texts: List[str] = []
        # json mode bookkeeping: (key_path, index) per item unit, then envelope.
        # key_path is a tuple: ("threads",) for a top-level list, or
        # ("thread", "messages") for a list one dict-level down.
        self._json_value: Any = None
        self._wrapped_list = False  # raw was a bare JSON array we boxed
        self._item_refs: List[Tuple[Tuple[str, ...], int]] = []
        # jsonl mode bookkeeping: parsed dict (or None) per line
        self._lines: List[str] = []
        self._line_objs: List[Optional[Dict[str, Any]]] = []

    def unit_texts(self) -> List[str]:
        return self._texts

    def _unit_label(self, i: int) -> str:
        if self.kind == "json":
            if i < len(self._item_refs):
                key_path, idx = self._item_refs[i]
                return "{}[{}]".format(".".join(key_path), idx)
            return "envelope"
        if self.kind == "jsonl":
            return "line {}".format(i + 1)
        return "output"

    def reassemble(self, verdicts: List[Any]) -> Tuple[str, List[Redaction], bool]:
        """
        Apply per-unit verdicts (objects with .safe/.reason/.location/.detector).
        Returns (output, redactions, all_blocked). When all_blocked is True the
        output string must not be shown — block the read entirely.
        """
        redactions = [
            Redaction(self._unit_label(i), v.reason, v.location, v.detector)
            for i, v in enumerate(verdicts) if not v.safe
        ]
        if not redactions:
            return self.raw, [], False
        if len(redactions) == len(verdicts):
            return "", redactions, True

        if self.kind == "text":
            return (self.raw, [], False) if not redactions else ("", redactions, True)

        if self.kind == "json":
            envelope_verdict = verdicts[-1]
            if not envelope_verdict.safe:
                return "", redactions, True
            value = self._json_value
            for unit_index, (key_path, idx) in enumerate(self._item_refs):
                v = verdicts[unit_index]
                if not v.safe:
                    container = value
                    for path_key in key_path:
                        container = container[path_key]
                    container[idx] = _stub(container[idx], v.reason, v.detector)
            if self._wrapped_list:
                value = value["items"]
            return _dumps(value), redactions, False

        # jsonl
        out_lines: List[str] = []
        for i, line in enumerate(self._lines):
            v = verdicts[i]
            if v.safe:
                out_lines.append(line)
            elif self._line_objs[i] is not None:
                out_lines.append(_dumps(_stub(self._line_objs[i], v.reason, v.detector)))
            else:
                out_lines.append("[firewall: line redacted - {}]".format(
                    patterns.sanitize_reason(v.reason, v.detector)))
        return "\n".join(out_lines), redactions, False


def split(raw: str) -> UnitizedOutput:
    """Split raw command output into screenable units."""
    stripped = raw.strip()

    # Whole-output JSON value (gog style)
    try:
        value = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        value = None

    if isinstance(value, dict):
        uo = UnitizedOutput("json", raw)
        uo._json_value = value

        def _is_item_list(candidate: Any) -> bool:
            return (isinstance(candidate, list) and candidate
                    and all(isinstance(x, dict) for x in candidate))

        # Item lists at the top level AND one dict-level down. The nested case
        # is load-bearing: `gmail thread get` nests its messages under
        # thread.messages, and without descending, the whole thread screens as
        # ONE unit — a single flagged message then blocks the entire read
        # instead of being stubbed (the per-unit redaction promise).
        for key in sorted(value.keys()):
            child = value[key]
            if _is_item_list(child):
                for idx, item in enumerate(child):
                    uo._item_refs.append(((key,), idx))
                    uo._texts.append(_dumps(item))
            elif isinstance(child, dict):
                for sub_key in sorted(child.keys()):
                    if _is_item_list(child[sub_key]):
                        for idx, item in enumerate(child[sub_key]):
                            uo._item_refs.append(((key, sub_key), idx))
                            uo._texts.append(_dumps(item))

        # Envelope: the dict with covered item lists elided (their metadata
        # siblings still get screened as part of the envelope).
        covered_paths = {r[0] for r in uo._item_refs}
        envelope: Dict[str, Any] = {}
        for k, v in value.items():
            if (k,) in covered_paths:
                envelope[k] = "..."
            elif isinstance(v, dict):
                envelope[k] = {k2: ("..." if (k, k2) in covered_paths else v2)
                               for k2, v2 in v.items()}
            else:
                envelope[k] = v
        uo._texts.append(_dumps(envelope))
        return uo
    if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
        wrapped = UnitizedOutput("json", raw)
        wrapped._json_value = {"items": value}
        wrapped._wrapped_list = True
        for idx, item in enumerate(value):
            wrapped._item_refs.append((("items",), idx))
            wrapped._texts.append(_dumps(item))
        wrapped._texts.append("{}")
        return wrapped

    # JSON-lines (imsg style)?
    lines = raw.splitlines()
    parsed: List[Optional[Dict[str, Any]]] = []
    dict_count = 0
    for line in lines:
        obj: Optional[Dict[str, Any]] = None
        candidate = line.strip()
        if candidate.startswith("{"):
            try:
                loaded = json.loads(candidate)
                if isinstance(loaded, dict):
                    obj = loaded
                    dict_count += 1
            except (json.JSONDecodeError, ValueError):
                obj = None
        parsed.append(obj)

    if dict_count >= 1 and dict_count >= len([l for l in lines if l.strip()]) // 2:
        uo = UnitizedOutput("jsonl", raw)
        uo._lines = lines
        uo._line_objs = parsed
        uo._texts = list(lines)
        return uo

    # Plain text fallback
    uo = UnitizedOutput("text", raw)
    uo._texts = [raw]
    return uo
