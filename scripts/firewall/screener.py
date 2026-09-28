#!/usr/bin/env python3
"""
screener.py - Prompt-injection screening engine.

Pipeline per unit of text (one message / email / event):

    cache -> patterns (Tier A block / Tier B escalate) -> LLM (only if escalated)

Design points:
  * Patterns scan the FULL text. Nothing is truncated before scanning.
  * The LLM is local Ollama over HTTP (not `ollama run`): thinking disabled,
    JSON-schema-constrained output, temperature 0, model kept warm.
  * For very large units the LLM screens windows around the pattern matches
    instead of a blind prefix, so attacks past any size limit are still seen.
  * LLM verdicts are cached by content hash (sqlite) — repeat reads are free.
  * Fail-closed: if the LLM is needed and unreachable, the unit is blocked.
    Pattern-clean units never need the LLM, so an Ollama outage no longer
    blocks ordinary reads.

Modes (env MINERU_FIREWALL_MODE):
  gated  (default) LLM only for Tier-B escalations
  always           LLM for every non-trivial unit, even pattern-clean ones
  off              patterns only; Tier-B matches pass without LLM review

Exit codes (CLI): 0 safe, 77 blocked, 78 screening error.

Python 3.9 compatible. Stdlib only.
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

import patterns

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_FILE = SCRIPT_DIR / "prompts" / "screen.md"

OLLAMA_URL = os.environ.get("MINERU_FIREWALL_OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("MINERU_FIREWALL_MODEL", "qwen3:8b")
MODE = os.environ.get("MINERU_FIREWALL_MODE", "gated")  # gated | always | off

LLM_TIMEOUT = 60          # per-call; first call may include model cold-load
LLM_RETRIES = 1           # retry once on connection error / timeout
TOTAL_LLM_BUDGET = 150.0  # seconds across all units in one wrapper invocation
KEEP_ALIVE = "2h"

MAX_LLM_CHARS = 7000      # units larger than this are screened via windows
WINDOW_RADIUS = 1200      # chars of context around each pattern match
MAX_WINDOWS = 4
MIN_LLM_LENGTH = 25       # "always" mode skips trivially short units

_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
_DEFAULT_CACHE = _MINERU_HOME / "cache" / "firewall-verdicts.sqlite3"
_MINERU_ROOT = _MINERU_HOME.resolve()


def _resolve_cache_path() -> Path:
    """
    Resolve the cache path, refusing locations outside $MINERU_HOME.

    MINERU_FIREWALL_CACHE is honored only if it stays under $MINERU_HOME — without
    this guard the env var is an arbitrary-file `chmod 0600` / `mkdir` primitive
    for anyone who can set it before a cron/launchd-invoked firewall runs.
    """
    raw = os.environ.get("MINERU_FIREWALL_CACHE")
    if not raw:
        return _DEFAULT_CACHE
    try:
        candidate = Path(raw).expanduser().resolve()
        candidate.relative_to(_MINERU_ROOT)  # raises if outside the tree
        return candidate
    except (ValueError, OSError):
        return _DEFAULT_CACHE


CACHE_PATH = _resolve_cache_path()
CACHE_TTL_DAYS = 30

LOG_PATH = _MINERU_HOME / "logs" / "firewall" / "screen.jsonl"

EXIT_BLOCKED = 77
EXIT_ERROR = 78


class ScreenResult(NamedTuple):
    safe: bool
    reason: Optional[str] = None
    location: Optional[str] = None
    severity: Optional[str] = None
    detector: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self._asdict().items() if v is not None}


# ============================================================================
# PROMPT
# ============================================================================

def _load_prompt_template() -> str:
    content = PROMPT_FILE.read_text()
    lines = content.split("\n")
    if lines and lines[0].startswith("#"):
        lines = lines[1:]
    return "\n".join(lines).strip()


_PROMPT_TEMPLATE = _load_prompt_template()
# Cache key namespace: changing the prompt or model invalidates old verdicts
_CACHE_NS = hashlib.sha256(
    (MODEL + "\x00" + _PROMPT_TEMPLATE).encode("utf-8")).hexdigest()[:16]


# ============================================================================
# VERDICT CACHE (sqlite). Cache failures never block screening — we just
# screen again. Only definitive LLM verdicts are cached, never errors.
# ============================================================================

def _cache_conn() -> Optional[sqlite3.Connection]:
    try:
        parent = CACHE_PATH.parent
        parent.mkdir(parents=True, exist_ok=True)
        # 0700 on the parent dir is the load-bearing control: it covers the DB
        # AND sqlite's -journal/-wal/-shm sidecars (which a per-file chmod
        # misses, leaving recent verdict text world-readable).
        os.chmod(str(parent), 0o700)
        conn = sqlite3.connect(str(CACHE_PATH), timeout=2.0)
        # DELETE journal (not WAL): cache writes are infrequent, and DELETE
        # avoids long-lived sidecar files entirely.
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS verdicts ("
            " hash TEXT PRIMARY KEY, safe INTEGER NOT NULL,"
            " reason TEXT, created REAL NOT NULL)")
        if CACHE_PATH.is_file():
            os.chmod(str(CACHE_PATH), 0o600)
        return conn
    except Exception:
        return None


def _cache_key(text: str) -> str:
    return hashlib.sha256((_CACHE_NS + "\x00" + text).encode("utf-8")).hexdigest()


def _cache_get(conn: Optional[sqlite3.Connection], text: str) -> Optional[ScreenResult]:
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT safe, reason FROM verdicts WHERE hash = ? AND created > ?",
            (_cache_key(text), time.time() - CACHE_TTL_DAYS * 86400)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    if row[0]:
        return ScreenResult(safe=True, detector="llm-cached")
    return ScreenResult(safe=False, reason=row[1] or "previously flagged",
                        severity="high", detector="llm-cached")


def _cache_put(conn: Optional[sqlite3.Connection], text: str, result: ScreenResult) -> None:
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO verdicts (hash, safe, reason, created) VALUES (?,?,?,?)",
            (_cache_key(text), 1 if result.safe else 0, result.reason, time.time()))
        conn.commit()
    except Exception:
        pass


def _cache_prune(conn: Optional[sqlite3.Connection]) -> None:
    if conn is None:
        return
    try:
        conn.execute("DELETE FROM verdicts WHERE created < ?",
                     (time.time() - CACHE_TTL_DAYS * 86400,))
        conn.commit()
    except Exception:
        pass


# ============================================================================
# LLM CALL (Ollama HTTP, structured output, no thinking)
# ============================================================================

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"safe": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["safe"],
}


def _llm_verdict(text: str) -> ScreenResult:
    """One LLM call. Fail-closed: any unrecoverable error blocks the unit."""
    prompt = _PROMPT_TEMPLATE.replace("{text}", text)
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "format": _RESPONSE_SCHEMA,
        "options": {"temperature": 0, "num_ctx": 8192},
        "keep_alive": KEEP_ALIVE,
    }).encode("utf-8")

    last_error = "unknown"
    for attempt in range(LLM_RETRIES + 1):
        try:
            req = urllib.request.Request(
                OLLAMA_URL + "/api/chat", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                payload = json.load(resp)
            parsed = json.loads(payload["message"]["content"])
            if parsed.get("safe", False):
                return ScreenResult(safe=True, detector="llm")
            return ScreenResult(
                safe=False,
                reason=parsed.get("reason", "flagged by LLM screener"),
                severity="high", detector="llm")
        except Exception as e:  # noqa: BLE001 - fail-closed boundary
            last_error = "{}: {}".format(type(e).__name__, e)
            if attempt < LLM_RETRIES:
                time.sleep(1.0)

    return ScreenResult(
        safe=False,
        reason="LLM screening unavailable ({}) - blocked for safety".format(last_error),
        severity="medium", detector="llm-error")


def _build_windows(text: str, spans: List[Tuple[int, int]]) -> Tuple[List[str], bool]:
    """
    Build LLM windows for a unit larger than MAX_LLM_CHARS.

    Coverage is the contract in BOTH branches: every char of the unit must land
    in some window, or fully_covered comes back False and the caller fails
    closed. Span windows only anchor match context on top of that coverage —
    they never REPLACE it. (The old spans branch screened only ±RADIUS around
    matches, so one decoy Tier-B keyword at char 20 left a vocabulary-free
    payload at char 10,000 entirely unscreened while claiming full coverage.)
    """
    if not spans:
        size = MAX_LLM_CHARS
        chunks = [text[i:i + size] for i in range(0, len(text), size)]
        fully_covered = len(chunks) <= MAX_WINDOWS
        if not fully_covered:
            # Sample head, tail, and evenly-spaced middle chunks.
            picks = [0, len(chunks) - 1]
            step = max(1, (len(chunks) - 1) // (MAX_WINDOWS - 1))
            for j in range(1, len(chunks) - 1, step):
                picks.append(j)
            chunks = [chunks[k] for k in sorted(set(picks))[:MAX_WINDOWS]]
        return chunks, fully_covered

    spans = sorted(spans)
    merged: List[List[int]] = []
    for start, end in spans:
        lo = max(0, start - WINDOW_RADIUS)
        hi = min(len(text), end + WINDOW_RADIUS)
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    # Full coverage: span windows plus the gaps between/around them, gaps
    # chunked at MAX_LLM_CHARS. Intervals are contiguous over [0, len).
    intervals: List[List[int]] = []
    cursor = 0
    for lo, hi in merged:
        while cursor < lo:
            intervals.append([cursor, min(cursor + MAX_LLM_CHARS, lo)])
            cursor = intervals[-1][1]
        intervals.append([lo, hi])
        cursor = hi
    while cursor < len(text):
        intervals.append([cursor, min(cursor + MAX_LLM_CHARS, len(text))])
        cursor = intervals[-1][1]

    # Pack adjacent intervals into windows up to MAX_LLM_CHARS. Packing merges
    # whole intervals only, so a span window is never split mid-match.
    packed: List[List[int]] = []
    for lo, hi in intervals:
        if packed and packed[-1][1] == lo and (hi - packed[-1][0]) <= MAX_LLM_CHARS:
            packed[-1][1] = hi
        else:
            packed.append([lo, hi])

    fully_covered = len(packed) <= MAX_WINDOWS
    return [text[lo:hi] for lo, hi in packed[:MAX_WINDOWS]], fully_covered


def _llm_screen_unit(text: str, spans: List[Tuple[int, int]],
                     conn: Optional[sqlite3.Connection]) -> ScreenResult:
    """LLM-screen one unit, using windows when it exceeds MAX_LLM_CHARS."""
    cached = _cache_get(conn, text)
    if cached is not None:
        return cached

    if len(text) <= MAX_LLM_CHARS:
        result = _llm_verdict(text)
    else:
        windows, fully_covered = _build_windows(text, spans)
        result = ScreenResult(safe=True, detector="llm")
        for window in windows:
            verdict = _llm_verdict(window)
            if not verdict.safe:
                result = verdict
                break
        if result.safe and not fully_covered:
            # Couldn't screen the whole oversized unit — don't claim it's clean.
            result = ScreenResult(
                safe=False,
                reason="unit too large to fully screen ({} chars) - blocked for safety".format(len(text)),
                severity="medium", detector="llm-error")

    if result.detector == "llm":  # definitive verdict, not an error
        _cache_put(conn, text, result)
    return result


# ============================================================================
# PUBLIC API
# ============================================================================

def screen_units(texts: List[str]) -> List[ScreenResult]:
    """
    Screen a list of text units. Returns one ScreenResult per unit, in order.
    LLM calls run on a small thread pool with a global time budget; if the
    budget is exhausted, remaining escalated units are blocked (fail-closed).
    """
    started = time.time()
    results: List[Optional[ScreenResult]] = [None] * len(texts)
    needs_llm: List[Tuple[int, List[Tuple[int, int]]]] = []

    for i, text in enumerate(texts):
        if not text.strip():
            results[i] = ScreenResult(safe=True, detector="pattern")
            continue
        verdict = patterns.scan(text)
        if verdict.blocked:
            results[i] = ScreenResult(safe=False, reason=verdict.reason,
                                      location=verdict.location,
                                      severity="high", detector="pattern")
        elif MODE == "off":
            results[i] = ScreenResult(safe=True, detector="pattern")
        elif verdict.escalate:
            needs_llm.append((i, verdict.match_spans))
        elif MODE == "always" and len(text.strip()) >= MIN_LLM_LENGTH:
            needs_llm.append((i, []))
        else:
            results[i] = ScreenResult(safe=True, detector="pattern")

    if needs_llm:
        conn = _cache_conn()
        _cache_prune(conn)

        def worker(item: Tuple[int, List[Tuple[int, int]]]) -> Tuple[int, ScreenResult]:
            idx, spans = item
            if time.time() - started > TOTAL_LLM_BUDGET:
                return idx, ScreenResult(
                    safe=False, reason="screening time budget exhausted - blocked for safety",
                    severity="medium", detector="llm-error")
            # each thread gets its own sqlite connection
            local_conn = _cache_conn()
            try:
                return idx, _llm_screen_unit(texts[idx], spans, local_conn)
            finally:
                if local_conn is not None:
                    local_conn.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            for idx, result in pool.map(worker, needs_llm):
                results[idx] = result
        if conn is not None:
            conn.close()

    final = [r if r is not None else ScreenResult(safe=True, detector="pattern")
             for r in results]
    _log_event(len(texts), len(needs_llm), sum(1 for r in final if not r.safe),
               time.time() - started)
    return final


def screen_text(text: str) -> ScreenResult:
    """Screen a single blob of text."""
    return screen_units([text])[0]


def _log_event(units: int, escalated: int, blocked: int, elapsed: float) -> None:
    """
    Append a telemetry line for tuning. Counts only — never message content.
    Never breaks the pipeline.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(str(LOG_PATH.parent), 0o700)
        with open(str(LOG_PATH), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "mode": MODE, "units": units, "escalated": escalated,
                "blocked": blocked, "ms": int(elapsed * 1000),
            }) + "\n")
        os.chmod(str(LOG_PATH), 0o600)
    except Exception:
        pass


# ============================================================================
# CLI:  echo "content" | python3 screener.py
# ============================================================================

def main() -> None:
    text = sys.stdin.read()
    if not text.strip():
        print(json.dumps({"safe": True}))
        sys.exit(0)
    result = screen_text(text)
    print(json.dumps(result.to_dict()))
    sys.exit(0 if result.safe else EXIT_BLOCKED)


if __name__ == "__main__":
    main()
