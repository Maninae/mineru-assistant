#!/usr/bin/env python3
"""
gog_cleaner.py - Clean gog output to reduce token usage.

Removes noisy metadata fields while preserving essential information.
Configuration is loaded from gog_cleaner_config.json.

Usage:
    # As module (called by gog-firewall):
    from gog_cleaner import clean
    cleaned_output = clean(["gmail", "search", "test"], raw_json_output)

    # As CLI (for testing):
    echo '{"huge": "json"}' | python3 gog_cleaner.py gmail search
"""
import base64
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

# Load config once at module import.
# Fail SAFE (block) with a clear message if the config is missing or corrupt —
# otherwise a bare traceback at import time crashes every firewall call with
# exit 78 and no explanation, silently breaking every cron delivery.
CONFIG_PATH = Path(__file__).parent / "gog_cleaner_config.json"
try:
    with open(CONFIG_PATH) as _cfg_f:
        CONFIG = json.load(_cfg_f)
except FileNotFoundError:
    print(
        f"FATAL: gog_cleaner config not found at {CONFIG_PATH}. "
        f"Firewall cannot screen output — blocking for safety.",
        file=sys.stderr,
    )
    sys.exit(78)  # EXIT_ERROR (fail-safe block)
except (json.JSONDecodeError, OSError) as _e:
    print(
        f"FATAL: gog_cleaner config at {CONFIG_PATH} is unreadable/corrupt: {_e}. "
        f"Firewall cannot screen output — blocking for safety.",
        file=sys.stderr,
    )
    sys.exit(78)  # EXIT_ERROR (fail-safe block)


# ---------------------------------------------------------------------------
# HTML → plain text (stdlib only, Python 3.9 safe).
#
# Real email bodies are ~96% HTML markup. For LLM screening we keep the
# *readable* text and throw away the tags. Be defensive: any parse error
# falls back to a regex tag-strip, then to "" — never raises to the caller.
# ---------------------------------------------------------------------------

# Block-level tags get a newline before/after so paragraphs and list items
# don't smash together into a wall of text after tag-stripping.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
    "footer", "nav", "aside", "blockquote", "pre", "hr",
}

# Tags whose *contents* should be dropped entirely. Container tags with real
# end tags ONLY — a void element (meta, link, br, img...) here would increment
# the drop depth without ever decrementing it, suppressing the entire body.
_DROP_TAG_CONTENT = {"script", "style", "head", "title"}

# Body safety cap: avoid a pathological huge email re-bloating context.
# 10K chars keeps essentially every real email whole.
BODY_MAX_CHARS = 10_000


class _HTMLToText(HTMLParser):
    """Minimal HTML→text extractor. Stdlib only."""

    def __init__(self):
        # convert_charrefs=True so &amp; / &nbsp; / etc. arrive as their
        # unescaped Unicode in handle_data().
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []
        # Stack of currently-open drop-content tags so nested matches still
        # suppress correctly.
        self._drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        # <body> means head is over, whatever tags went unclosed inside it.
        if tag == "body":
            self._drop_depth = 0
        if tag in _DROP_TAG_CONTENT:
            self._drop_depth += 1
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag, attrs):
        # Self-closing like <br/>: still want the newline for <br/>.
        tag = tag.lower()
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _DROP_TAG_CONTENT:
            if self._drop_depth > 0:
                self._drop_depth -= 1
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._drop_depth > 0:
            return
        if data:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


_RE_TAG_STRIP = re.compile(r"<[^>]+>")
# Well-formed drop-content blocks, removable with their contents by regex.
_RE_DROP_BLOCKS = re.compile(
    r"<(script|style|head|title)[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_RE_WHITESPACE_RUN = re.compile(r"[ \t\f\v]+")
_RE_BLANK_LINES = re.compile(r"\n[ \t]*\n[\s\n]*")


def _collapse_whitespace(text: str) -> str:
    """Collapse spaces/tabs to single space; collapse runs of blank lines to one."""
    # Per-line: collapse internal whitespace runs.
    lines = []
    for line in text.split("\n"):
        line = _RE_WHITESPACE_RUN.sub(" ", line).strip()
        lines.append(line)
    out = "\n".join(lines)
    # Collapse 3+ blank lines down to a single blank line.
    out = _RE_BLANK_LINES.sub("\n\n", out)
    return out.strip()


def _regex_strip_fallback(content: str) -> str:
    """Crude but coverage-safe HTML→text: remove well-formed script/style/head
    blocks with their contents, strip remaining tags, collapse whitespace.
    Unclosed drop-block contents leak through as noise — acceptable, since this
    path exists to preserve content the parser lost."""
    without_blocks = _RE_DROP_BLOCKS.sub(" ", _RE_HTML_COMMENT.sub(" ", content))
    return _collapse_whitespace(_RE_TAG_STRIP.sub(" ", without_blocks))


def _parser_lost_most_content(parsed_text: str, content: str) -> str:
    """Return the fallback text when the parser output looks like silent loss.

    HTMLParser enters CDATA mode at <script>/<style> and, when the closing tag
    never comes (common in sloppy email templates), consumes everything to EOF
    — the parser output is then just the pre-tag prefix. Detect by comparing
    against a crude tag-strip of the same input; when the parser kept under
    half of it, prefer the noisy-but-complete fallback. Returns "" when the
    parser output is fine.
    """
    fallback = _regex_strip_fallback(content)
    # Fallback fires on a big RELATIVE loss with a real ABSOLUTE loss behind it.
    # Worst case of firing wrongly is CSS/JS noise alongside complete content;
    # worst case of not firing is silent content loss — bias toward firing.
    if (len(parsed_text) < len(fallback) * 0.5
            and len(fallback) - len(parsed_text) > 10):
        return fallback
    return ""


def html_to_text(content: str) -> str:
    """
    Convert HTML to readable plain text.

    Robust: never raises. On any parse error, falls back to a regex tag-strip;
    if that also fails, returns "". If the input has no tags, it passes
    through (still whitespace-collapsed).
    """
    if not isinstance(content, str) or not content:
        return ""
    # Fast path: pure plain text (no '<' at all) — just collapse whitespace.
    if "<" not in content:
        return _collapse_whitespace(content)
    try:
        parser = _HTMLToText()
        parser.feed(content)
        parser.close()
        text = _collapse_whitespace(parser.get_text())
        recovered = _parser_lost_most_content(text, content)
        return recovered if recovered else text
    except Exception:
        # Fallback: regex tag-strip.
        try:
            return _regex_strip_fallback(content)
        except Exception:
            return ""


def _truncate_body(text: str) -> str:
    """Cap a body at BODY_MAX_CHARS and append a clear truncation marker."""
    if len(text) <= BODY_MAX_CHARS:
        return text
    over = len(text) - BODY_MAX_CHARS
    return text[:BODY_MAX_CHARS] + (
        "\n…[truncated {} chars — use --raw for full body]".format(over)
    )


# ---------------------------------------------------------------------------
# Gmail MIME body extraction (for `gmail thread get`).
# ---------------------------------------------------------------------------

def _b64url_decode(data: str) -> str:
    """
    Decode Gmail's URL-safe base64 body.data into a UTF-8 string.

    Gmail uses URL-safe alphabet without padding. Always pad, decode tolerantly,
    and replace undecodable bytes rather than raise.
    """
    if not isinstance(data, str) or not data:
        return ""
    try:
        # urlsafe_b64decode wants bytes. Fix padding (multiple of 4).
        s = data
        # Gmail occasionally returns padded URL-safe; harmless to over-pad and let
        # the decoder strip — but Python's b64decode is strict on excess '='
        # only at the *start*, so this is safe.
        s = s + "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode(s.encode("ascii", errors="ignore"))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_parts_for_body(payload: Dict[str, Any]) -> str:
    """
    Find the message body inside a Gmail payload tree.

    Preference order: text/plain (no conversion) > text/html (HTML→text).
    Walks nested multipart trees. If no parts present, falls back to
    payload.body.data. Returns plain text, possibly empty.
    """
    if not isinstance(payload, dict):
        return ""

    # First pass: search for text/plain anywhere in the tree.
    plain = _find_part_data(payload, "text/plain")
    if plain:
        return _b64url_decode(plain)

    # Second pass: text/html anywhere → convert.
    html = _find_part_data(payload, "text/html")
    if html:
        decoded = _b64url_decode(html)
        return html_to_text(decoded)

    # Fallback: payload-level body.
    body = payload.get("body")
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, str) and data:
            decoded = _b64url_decode(data)
            mime = payload.get("mimeType", "")
            if isinstance(mime, str) and mime.lower() == "text/html":
                return html_to_text(decoded)
            # Could be text/plain or unknown — if it looks like HTML, convert.
            if "<html" in decoded[:1000].lower() or "<body" in decoded[:1000].lower():
                return html_to_text(decoded)
            return decoded

    # Gmail returns attachmentId instead of inline data for very large text
    # parts. Surface a marker so the agent knows the body is off-band, not empty.
    if _has_attachment_only_text_part(payload):
        return ("[body not inlined by Gmail (attachmentId only): fetch via "
                "'gog gmail attachment' or --raw]")

    return ""


def _has_attachment_only_text_part(payload: Dict[str, Any]) -> bool:
    """True if a text/* part carries body.attachmentId with no inline data."""
    if not isinstance(payload, dict):
        return False
    mime = payload.get("mimeType", "")
    body = payload.get("body")
    if (isinstance(mime, str) and mime.lower().startswith("text/")
            and isinstance(body, dict)
            and body.get("attachmentId") and not body.get("data")):
        return True
    parts = payload.get("parts")
    if isinstance(parts, list):
        return any(_has_attachment_only_text_part(p) for p in parts
                   if isinstance(p, dict))
    return False


def _find_part_data(payload: Dict[str, Any], wanted_mime: str) -> Optional[str]:
    """
    Depth-first search for the first part with mimeType == wanted_mime that
    has non-empty body.data. Returns the raw base64 string, or None.
    """
    if not isinstance(payload, dict):
        return None
    mime = payload.get("mimeType", "")
    if isinstance(mime, str) and mime.lower() == wanted_mime.lower():
        body = payload.get("body")
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, str) and data:
                return data
    parts = payload.get("parts")
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, dict):
                found = _find_part_data(p, wanted_mime)
                if found:
                    return found
    return None


def is_read_command(args: List[str]) -> bool:
    """
    Determine if command reads external data (needs cleaning + screening)
    or writes data (pass through, no screening needed).
    
    Supports nested commands like 'gmail labels list' -> check 'gmail.labels.list'.
    
    Defaults to True (read) for safety - if we can't classify, assume it
    might contain external data that needs screening.
    """
    if len(args) < 2:
        return True  # Fail safe
    
    classification = CONFIG.get("command_classification", {})
    read_commands = classification.get("read_commands", {})
    write_commands = classification.get("write_commands", {})
    
    # Build possible lookup keys from most specific to least specific
    # e.g., ["gmail", "labels", "list"] -> ["gmail.labels.list", "gmail.labels", "gmail"]
    args_lower = [a.lower() for a in args]
    
    # Try nested keys first (for commands like 'gmail labels list')
    for depth in range(min(3, len(args_lower)), 0, -1):
        nested_key = ".".join(args_lower[:depth])
        action = args_lower[depth] if depth < len(args_lower) else None
        
        if action:
            # Check write commands first
            if nested_key in write_commands and action in write_commands[nested_key]:
                return False
            
            # Check read commands
            if nested_key in read_commands and action in read_commands[nested_key]:
                return True
    
    # Fall back to simple service.action check
    service = args_lower[0]
    action = args_lower[1] if len(args_lower) > 1 else None
    
    if action:
        # Check if it's explicitly a write command
        if service in write_commands and action in write_commands[service]:
            return False
        
        # Check if it's explicitly a read command
        if service in read_commands and action in read_commands[service]:
            return True
    
    # Unknown: default to read (fail safe)
    return True


def clean_dict(data: Dict[str, Any], keep: List[str], drop: List[str], 
               nested_rules: Optional[Dict[str, Dict]] = None) -> Dict[str, Any]:
    """
    Clean a dictionary based on keep/drop rules.
    
    - If field is in 'drop': remove it
    - If field is in 'keep': keep it  
    - If field is in neither: keep it (allowlist is optional)
    - Apply nested_rules recursively for nested objects
    """
    result = {}
    
    for key, value in data.items():
        # Explicit drop
        if key in drop:
            continue
        
        # Check for nested rules
        if nested_rules and key in nested_rules:
            nested = nested_rules[key]
            if isinstance(value, dict):
                value = clean_dict(
                    value,
                    nested.get("keep", []),
                    nested.get("drop", [])
                )
            elif isinstance(value, list):
                value = [
                    clean_dict(item, nested.get("keep", []), nested.get("drop", []))
                    if isinstance(item, dict) else item
                    for item in value
                ]
        
        result[key] = value
    
    return result


def clean_gmail_search(data: Dict[str, Any]) -> Dict[str, Any]:
    """Clean gmail search output."""
    rules = CONFIG.get("gmail", {}).get("search", {})
    return clean_dict(
        data,
        rules.get("keep", []),
        rules.get("drop", [])
    )


def clean_gmail_get(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Clean gmail get output.

    The structure is:
    {
        "body": "...",          # KEEP as plain text (HTML stripped if needed).
                                # gog already gives this top-level body as text
                                # in practice, but we run it through html_to_text
                                # defensively in case any HTML markup leaks
                                # through. Capped at BODY_MAX_CHARS.
        "headers": {...},       # Keep - simplified headers (from/to/subject/date)
        "message": {            # Clean - remove payload and metadata
            "id": "...",
            "snippet": "...",   # Keep (Gmail's short summary; complements body)
            "payload": {...},   # Drop - huge MIME structure (body already lifted)
            "historyId": "...", # Drop
            ...
        }
    }

    Full content is still available via the --raw bypass.
    """
    rules = CONFIG.get("gmail", {}).get("get", {})
    drop = rules.get("drop", [])
    nested = rules.get("nested", {})

    result = {}

    for key, value in data.items():
        if key in drop:
            continue

        if key == "body":
            # KEEP body as plain text. Convert if any HTML markup is present;
            # html_to_text handles pure plain text via fast path.
            if isinstance(value, str) and value:
                text = html_to_text(value)
                if text:
                    result["body"] = _truncate_body(text)
            # else: empty/missing body, skip
        elif key == "headers" and isinstance(value, dict):
            # Apply header rules
            header_rules = nested.get("headers", {})
            result["headers"] = clean_dict(
                value,
                header_rules.get("keep", []),
                header_rules.get("drop", [])
            )
        elif key == "message" and isinstance(value, dict):
            # Clean the message object — keep id + snippet only.
            cleaned_msg = {}
            for mk, mv in value.items():
                if mk in drop:
                    continue
                if mk in ("id", "snippet"):
                    cleaned_msg[mk] = mv
            if cleaned_msg:
                result["message"] = cleaned_msg
        else:
            result[key] = value

    # gog "typically" hoists the body to a top-level text field — but when it
    # doesn't (unusual MIME shape, partial extraction), dropping payload above
    # would silently reduce the message to its snippet. Walk the MIME tree the
    # same way thread_get does before giving up on a body.
    if "body" not in result:
        message = data.get("message")
        payload = message.get("payload") if isinstance(message, dict) else None
        if isinstance(payload, dict):
            extracted_body = _walk_parts_for_body(payload)
            if extracted_body:
                result["body"] = _truncate_body(extracted_body)

    return result


def _headers_list_to_map(headers_list: List[Dict[str, Any]],
                        wanted: List[str]) -> Dict[str, str]:
    """
    Synthesize a compact {name: value} map from gmail's payload.headers list,
    keeping only the headers in 'wanted' (case-insensitive).

    gmail returns payload.headers as a list of {name, value} dicts containing
    every transport header (Delivered-To, ARC-Seal, DKIM-Signature, ...). For
    LLM screening we only need {from, to, subject, date} — dozens of routing
    headers per message would balloon token count for no benefit.
    """
    wanted_lower = {w.lower() for w in wanted}
    out: Dict[str, str] = {}
    if not isinstance(headers_list, list):
        return out
    for h in headers_list:
        if not isinstance(h, dict):
            continue
        name = h.get("name")
        value = h.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        key = name.lower()
        if key in wanted_lower and key not in out:
            out[key] = value
    return out


def clean_gmail_thread_get(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Clean 'gmail thread get' output.

    The structure is:
    {
        "downloaded": null,
        "thread": {
            "historyId": "...",
            "id": "...",
            "messages": [
                {
                    "id": "...",
                    "snippet": "...",                  # KEEP
                    "threadId": "...",                 # DROP (redundant)
                    "historyId": "...",                # DROP
                    "internalDate": "...",             # DROP
                    "sizeEstimate": 55754,             # DROP
                    "labelIds": [...],                 # DROP
                    "payload": {
                        "body": {"data": "<base64 HTML>", "size": ...},  # DROP
                        "headers": [ {name,value} ... 22 entries ],      # → compact map
                        "mimeType": "text/html",
                        "parts": [...]                                    # DROP
                    }
                },
                ...
            ]
        }
    }

    Per message: drop payload heavy subtrees and redundant per-message metadata.
    Synthesize a compact {from,to,subject,date,...} header map from
    payload.headers. Keep id + snippet (snippet is screened).
    """
    rules = CONFIG.get("gmail", {}).get("thread_get", {})
    msg_keep = rules.get("message_keep", ["id", "snippet", "headers"])
    msg_drop = rules.get("message_drop", [])
    wanted_headers = rules.get("_synthesize_headers_from_payload",
                               ["from", "to", "subject", "date"])

    result: Dict[str, Any] = {}

    for top_key, top_val in data.items():
        if top_key != "thread" or not isinstance(top_val, dict):
            result[top_key] = top_val
            continue

        thread_out: Dict[str, Any] = {}
        for tk, tv in top_val.items():
            if tk != "messages" or not isinstance(tv, list):
                thread_out[tk] = tv
                continue

            cleaned_messages: List[Dict[str, Any]] = []
            for msg in tv:
                if not isinstance(msg, dict):
                    cleaned_messages.append(msg)
                    continue

                # Extract body (text/plain preferred, text/html converted) AND
                # synthesize compact headers from payload.headers BEFORE we
                # drop payload.
                synthesized_headers: Dict[str, str] = {}
                extracted_body: str = ""
                payload = msg.get("payload")
                if isinstance(payload, dict):
                    raw_headers = payload.get("headers")
                    synthesized_headers = _headers_list_to_map(
                        raw_headers, wanted_headers
                    )
                    extracted_body = _walk_parts_for_body(payload)

                cleaned_msg: Dict[str, Any] = {}
                for mk, mv in msg.items():
                    if mk in msg_drop:
                        continue
                    if mk in msg_keep:
                        cleaned_msg[mk] = mv
                    # else: drop unrecognized fields. Anything not in msg_keep
                    # is excluded by default (safer token-diet); msg_drop just
                    # documents the expected-to-drop set explicitly.

                if synthesized_headers:
                    cleaned_msg["headers"] = synthesized_headers

                if extracted_body:
                    cleaned_msg["body"] = _truncate_body(extracted_body)

                cleaned_messages.append(cleaned_msg)

            thread_out[tk] = cleaned_messages

        result[top_key] = thread_out

    return result


def _clean_one_event(event: Dict[str, Any], drop: List[str],
                     nested: Dict[str, Dict]) -> Dict[str, Any]:
    """
    Clean a single calendar event dict.

    Drops top-level fields in `drop`, applies nested keep/drop rules to
    start/end, creator/organizer, and the attendees list. Pure function;
    used by both the list shape ({"events": [...]}) and the singular
    shape ({"event": {...}}) so the trimming logic stays DRY.
    """
    cleaned_event: Dict[str, Any] = {}
    for ek, ev in event.items():
        if ek in drop:
            continue

        # Apply nested rules for start/end
        if ek in ["start", "end"] and isinstance(ev, dict):
            nested_rules = nested.get(ek, {})
            ev = clean_dict(
                ev,
                nested_rules.get("keep", []),
                nested_rules.get("drop", [])
            )

        # Apply nested rules for creator/organizer
        if ek in ["creator", "organizer"] and isinstance(ev, dict):
            nested_rules = nested.get(ek, {})
            ev = clean_dict(
                ev,
                nested_rules.get("keep", []),
                nested_rules.get("drop", [])
            )

        # Apply nested rules for attendees (list of dicts)
        if ek == "attendees" and isinstance(ev, list):
            nested_rules = nested.get(ek, {})
            ev = [
                clean_dict(a, nested_rules.get("keep", []), nested_rules.get("drop", []))
                if isinstance(a, dict) else a
                for a in ev
            ]

        cleaned_event[ek] = ev

    return cleaned_event


def clean_calendar_events(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Clean calendar events output.

    Handles both shapes returned by the gog calendar API:
      - LIST:     {"events": [ {event}, ... ]}  ← `calendar events --days N`
      - SINGULAR: {"event":  {event} }          ← `calendar event <calId> <eventId>`

    Both go through the same `_clean_one_event` helper so the trimming rules
    stay identical. Sibling keys (e.g. `"downloaded"`) pass through unchanged.
    """
    rules = CONFIG.get("calendar", {}).get("events", {})
    drop = rules.get("drop", [])
    nested = rules.get("nested", {})

    result: Dict[str, Any] = {}

    for key, value in data.items():
        if key == "events" and isinstance(value, list):
            result["events"] = [
                _clean_one_event(event, drop, nested)
                if isinstance(event, dict) else event
                for event in value
            ]
        elif key == "event" and isinstance(value, dict):
            result["event"] = _clean_one_event(value, drop, nested)
        else:
            result[key] = value

    return result


def clean_drive(data: Dict[str, Any], action: str) -> Dict[str, Any]:
    """Clean drive output."""
    rules = CONFIG.get("drive", {}).get(action, CONFIG.get("drive", {}).get("search", {}))
    return clean_dict(
        data,
        rules.get("keep", []),
        rules.get("drop", [])
    )


def clean_contacts(data: Dict[str, Any], action: str) -> Dict[str, Any]:
    """Clean contacts output."""
    rules = CONFIG.get("contacts", {}).get(action, CONFIG.get("contacts", {}).get("list", {}))
    return clean_dict(
        data,
        rules.get("keep", []),
        rules.get("drop", [])
    )


def clean(args: List[str], output: str) -> str:
    """
    Main dispatcher: clean gog output based on command type.
    
    Args:
        args: Command arguments (e.g., ["gmail", "search", "from:boss"])
        output: Raw JSON output from gog
        
    Returns:
        Cleaned JSON string (or original if not cleanable)
    """
    # Don't clean write commands
    if not is_read_command(args):
        return output
    
    # Try to parse JSON
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        # Not JSON, return as-is
        return output
    
    if not isinstance(data, dict):
        return output
    
    # Dispatch based on service
    if len(args) < 2:
        return output
    
    service = args[0].lower()
    action = args[1].lower()
    sub_action = args[2].lower() if len(args) >= 3 else None

    try:
        if service == "gmail":
            if action == "search":
                cleaned = clean_gmail_search(data)
            elif action == "get":
                cleaned = clean_gmail_get(data)
            elif action == "thread" and sub_action == "get":
                cleaned = clean_gmail_thread_get(data)
            else:
                cleaned = data
        elif service == "calendar":
            # "events" and "list" return {"events": [...]}.
            # "event" (singular, `calendar event <calId> <eventId>`) returns
            # {"event": {...}}. clean_calendar_events handles both shapes.
            if action in ["events", "list", "event"]:
                cleaned = clean_calendar_events(data)
            else:
                cleaned = data
        elif service == "drive":
            cleaned = clean_drive(data, action)
        elif service == "contacts":
            cleaned = clean_contacts(data, action)
        else:
            # Unknown service, pass through
            cleaned = data
        
        return json.dumps(cleaned, separators=(',', ':'))
    
    except Exception:
        # On any error, return original
        return output


# CLI for testing
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: echo '{}' | python3 gog_cleaner.py <service> <action>", file=sys.stderr)
        print("Example: echo '{}' | python3 gog_cleaner.py gmail search", file=sys.stderr)
        sys.exit(1)
    
    args = sys.argv[1:]
    input_data = sys.stdin.read()
    
    result = clean(args, input_data)
    print(result)
