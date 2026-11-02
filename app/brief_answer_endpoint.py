"""HTTP handlers for the two note-append routes (storage rules in brief_answers.py).

  POST /api/brief/<feed_id>/<filename>/answer     a note on a feed brief
  POST /api/library/<source>/<relpath>/answer     a note on a library `.md`

`relpath` is ONE URL-encoded segment (`a%2Fb.md`), unquoted the same way the
`GET /api/library/<source>/<relpath>` route does. This is the app's one write
into the workspace, so every check fails closed and most failures are a bland 404:

  body > MAX_ANSWER_BODY_BYTES                     -> 413
  bad JSON / non-object body                       -> 400
  missing / empty / oversized text                 -> 400
  unknown feed or source, path that does not
  resolve, escapes its root or the allowlist,
  is not a regular file, is not `.md`, or has a
  dot-prefixed name                                -> 404
  file is not valid UTF-8                          -> 409 not answerable

The dispatcher's write-guards (Content-Type, Origin, OPTIONS 405, Host
allowlist, passphrase gate) run before these handlers because both routes are
`wants_body=True`. Logs carry the path only, never the note text.

Python 3.9-compatible. Stdlib only.
"""

import json
import logging
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from config import FEED_BY_ID, LIBRARY_SOURCES
import brief_answers
import feeds as feeds_module
import library as library_module
import search as search_module
from brief_answers import MAX_ANSWER_BODY_BYTES
from http_helpers import (
    DEFAULT_TEXT_ENCODING,
    error_response,
    is_path_inside_allowlist,
    json_response,
    safe_is_file,
)


logger = logging.getLogger(__name__)

Response = Tuple[int, Dict[str, str], bytes]


def parse_answer_text_from_body(body: bytes) -> Tuple[Optional[str], Optional[Response]]:
    """Return `(answer_text, None)` for a usable body, else `(None, error_response)`."""
    if len(body) > MAX_ANSWER_BODY_BYTES:
        return None, error_response(413, "body too large")
    try:
        payload = json.loads(body.decode(DEFAULT_TEXT_ENCODING) or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, error_response(400, "bad json")
    if not isinstance(payload, dict):
        return None, error_response(400, "expected object body")
    answer_text = brief_answers.validate_answer_text(payload.get("text"))
    if answer_text is None:
        return None, error_response(400, "bad answer text")
    return answer_text, None


# Files larger than this never take a note: the append is a read-modify-write of
# the whole file under one lock, so this bounds both memory and unbounded growth.
NOTE_TARGET_MAX_BYTES = 2 * 1024 * 1024


def is_writable_markdown_target(target: Optional[Path], roots: Tuple[Path, ...]) -> bool:
    """Allowlisted, regular, size-capped `.md` file with no hidden path component.

    `roots` are the resolved directories the caller allows (a feed's dirs, or the
    library source root); the dot check runs on the RESOLVED path relative to
    whichever root contains it, so a symlink or a leading space in the raw
    request cannot smuggle a note into a hidden directory the listings never show.
    """
    if target is None or not is_path_inside_allowlist(target):
        return False
    if not safe_is_file(target):
        return False
    if target.suffix.lower() != ".md":
        return False
    try:
        if target.stat().st_size > NOTE_TARGET_MAX_BYTES:
            return False
    except OSError:
        return False
    for root in roots:
        try:
            relative_parts = target.relative_to(root).parts
        except ValueError:
            continue
        return not any(part.startswith(".") for part in relative_parts)
    return False


def append_note_and_respond(target: Path, answer_text: str, identity: Dict[str, Any]) -> Response:
    """Append the note, drop every cache that may hold the old text, answer 200/409.

    `identity` is echoed into the 200 body (`feed_id`+`filename` or `source`+`relpath`).
    """
    try:
        answer_status = brief_answers.append_answer_note(
            target, answer_text, datetime.now().astimezone(),
        )
    except brief_answers.BriefNotAnswerableError:
        return json_response({"error": "not answerable"}, status=409, cache_seconds=0)

    # A feed dir may sit under reports/ or creations/, so clear all of them.
    feeds_module.invalidate_feed_summary_cache()
    feeds_module.invalidate_today_items_cache()
    search_module.invalidate_search_cache()
    logger.info("note appended: %s", target)
    return json_response({"ok": True, "answer_status": answer_status, **identity}, cache_seconds=0)


def handle_brief_answer_post(
    match: re.Match, _query: Dict, body: bytes, host: str = None,
) -> Response:
    """POST /api/brief/<feed_id>/<filename>/answer with `{"text": "..."}`."""
    answer_text, error = parse_answer_text_from_body(body)
    if error is not None:
        return error

    feed_id = match.group("feed_id")
    filename = urllib.parse.unquote(match.group("filename"))
    if feed_id not in FEED_BY_ID:
        return error_response(404, "unknown feed")
    brief_path = feeds_module.resolve_brief_path(feed_id, filename)
    feed_roots = tuple(path.resolve() for path in feeds_module.feed_directories(feed_id))
    if not is_writable_markdown_target(brief_path, feed_roots):
        return error_response(404, "not found")
    return append_note_and_respond(brief_path, answer_text, {"feed_id": feed_id, "filename": filename})


def handle_library_answer_post(
    match: re.Match, _query: Dict, body: bytes, host: str = None,
) -> Response:
    """POST /api/library/<source>/<relpath>/answer with `{"text": "..."}`."""
    answer_text, error = parse_answer_text_from_body(body)
    if error is not None:
        return error

    source = match.group("source")
    relpath = urllib.parse.unquote(match.group("relpath"))
    if not relpath.strip():
        return error_response(404, "not found")
    try:
        target = library_module.resolve_library_path(source, relpath)
    except ValueError:
        return error_response(404, "not found")
    source_root = LIBRARY_SOURCES[source].resolve()
    if not is_writable_markdown_target(target, (source_root,)):
        return error_response(404, "not found")
    return append_note_and_respond(target, answer_text, {"source": source, "relpath": relpath})


# Per-route body cap read by server.MineruRequestHandler.dispatch (default is
# http_helpers.MAX_JSON_BODY_BYTES).
handle_brief_answer_post.max_body_bytes = MAX_ANSWER_BODY_BYTES
handle_library_answer_post.max_body_bytes = MAX_ANSWER_BODY_BYTES
