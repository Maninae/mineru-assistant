"""HTTP handler for `POST /api/brief/<feed_id>/<filename>/answer`.

Appends the reader's answer to an answerable brief (storage rules live in
brief_answers.py). This is the app's one write into the workspace, so every
check below fails closed and most failures are a bland 404:

  body > MAX_ANSWER_BODY_BYTES           -> 413
  bad JSON / non-object body             -> 400
  unknown feed, or feed not answerable   -> 404 (never advertise the write path)
  missing / empty / oversized text       -> 400
  file missing, outside the allowlist,
  or not `.md`                           -> 404
  line 1 carries no marker               -> 409 not answerable
  already filed                          -> 409 already filed

The dispatcher's write-guards (Content-Type, Origin, OPTIONS 405, Host
allowlist, passphrase gate) run before this handler because the route is
`wants_body=True`. Logs carry the path only, never the answer text.

Python 3.9-compatible. Stdlib only.
"""

import json
import logging
import re
import urllib.parse
from datetime import datetime
from typing import Dict, Tuple

from config import FEED_BY_ID
import brief_answers
import feeds as feeds_module
import search as search_module
from brief_answers import MAX_ANSWER_BODY_BYTES
from http_helpers import (
    DEFAULT_TEXT_ENCODING,
    error_response,
    is_path_inside_allowlist,
    json_response,
)


logger = logging.getLogger(__name__)


def handle_brief_answer_post(
    match: re.Match, _query: Dict, body: bytes, host: str = None,
) -> Tuple[int, Dict[str, str], bytes]:
    """Append `{"text": "..."}` as an answer note; return the new answer status."""
    if len(body) > MAX_ANSWER_BODY_BYTES:
        return error_response(413, "body too large")
    try:
        payload = json.loads(body.decode(DEFAULT_TEXT_ENCODING) or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response(400, "bad json")
    if not isinstance(payload, dict):
        return error_response(400, "expected object body")

    feed_id = match.group("feed_id")
    filename = urllib.parse.unquote(match.group("filename"))
    feed_entry = FEED_BY_ID.get(feed_id)
    if feed_entry is None:
        return error_response(404, "unknown feed")
    if feed_entry.get("answerable") is not True:
        return error_response(404, "not found")

    answer_text = brief_answers.validate_answer_text(payload.get("text"))
    if answer_text is None:
        return error_response(400, "bad answer text")

    if filename.startswith("."):
        return error_response(404, "not found")
    brief_path = feeds_module.resolve_brief_path(feed_id, filename)
    if brief_path is None or not is_path_inside_allowlist(brief_path):
        return error_response(404, "not found")
    if brief_path.suffix.lower() != ".md":
        return error_response(404, "not found")

    try:
        answer_status = brief_answers.append_answer_note(
            brief_path, answer_text, datetime.now().astimezone(),
        )
    except brief_answers.BriefNotAnswerableError:
        return json_response({"error": "not answerable"}, status=409, cache_seconds=0)
    except brief_answers.BriefAlreadyFiledError:
        return json_response({"error": "already filed"}, status=409, cache_seconds=0)

    feeds_module.invalidate_feed_summary_cache(feed_id)
    feeds_module.invalidate_today_items_cache()
    search_module.invalidate_search_cache()
    logger.info("answer note appended: %s", brief_path)
    return json_response(
        {"ok": True, "answer_status": answer_status, "filename": filename, "feed_id": feed_id},
        cache_seconds=0,
    )


# Per-route body cap read by server.MineruRequestHandler.dispatch (default is
# http_helpers.MAX_JSON_BODY_BYTES).
handle_brief_answer_post.max_body_bytes = MAX_ANSWER_BODY_BYTES
