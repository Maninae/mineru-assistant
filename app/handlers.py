"""Route handlers for the Mineru web app.

Every handler returns (status:int, headers:Dict[str,str], body:bytes). The
dispatcher in server.py just runs them. Security-sensitive helpers all live
in http_helpers.py so no handler re-implements path/extension checks.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import json
import logging
import math
import re
import urllib.parse
from typing import Dict, Tuple

from config import (
    ALLOWED_EXTENSIONS,
    FEED_BY_ID,
    FEED_PAGE_SIZE_DEFAULT,
    FEED_REGISTRY,
    HTML_EXTENSIONS,
    MAX_INLINE_MARKDOWN_BYTES,
    MIME_TYPES,
    STATIC_DIR,
    TODAY_HOURS_DEFAULT,
    TODAY_HOURS_MAX,
    TODAY_HOURS_MIN,
)
import feeds as feeds_module
import library as library_module
import pulse as pulse_module
import search as search_module
import seen_ledger
import unlock_gate
from http_helpers import (
    DEFAULT_TEXT_ENCODING,
    MAX_JSON_BODY_BYTES,
    apply_security_headers,
    error_response,
    is_path_inside_allowlist,
    json_response,
    safe_exists,
    safe_is_dir,
    safe_is_file,
    safe_serve_file,
)


logger = logging.getLogger(__name__)


# --- Static asset serving -----------------------------------------------------

STATIC_MIME_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def serve_static_asset(rel_static_path: str) -> Tuple[int, Dict[str, str], bytes]:
    """Serve one file under app/static/, denying any escape.

    Security headers are attached via apply_security_headers so the shell
    and every static asset carry the same strict CSP as the API responses.
    """
    rel_static_path = rel_static_path.lstrip("/")
    candidate = (STATIC_DIR / rel_static_path).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return error_response(404, "not found")
    if not candidate.is_file():
        return error_response(404, "not found")
    extension = candidate.suffix.lower()
    content_type = STATIC_MIME_TYPES.get(extension, "application/octet-stream")
    body = candidate.read_bytes()
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Cache-Control": "private, max-age=60",
    }
    return 200, apply_security_headers(headers), body


def handle_root(_match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET / — the SPA shell."""
    return serve_static_asset("index.html")


def handle_static(match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /static/<rel_path> — CSS, JS, icons, vendored marked."""
    return serve_static_asset(match.group("rel"))


def handle_manifest(_match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /manifest.webmanifest — PWA manifest."""
    return serve_static_asset("manifest.webmanifest")


def handle_service_worker(_match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /sw.js — service worker (shell caching only)."""
    return serve_static_asset("sw.js")


# --- API handlers -------------------------------------------------------------

def handle_feeds_index(_match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /api/feeds — landing view for the sidebar."""
    ledger = seen_ledger.load_ledger()
    rows = []
    for feed in FEED_REGISTRY:
        seen_set = seen_ledger.get_seen_set_for_feed(ledger, feed["id"])
        summary = feeds_module.summarize_feed(feed["id"], seen_set=seen_set)
        rows.append({
            "id": feed["id"],
            "display_name": feed["display_name"],
            "emoji": feed["emoji"],
            "accent": feed["accent"],
            "dirs": feed["dirs"],
            # Sidebar section label; config.parse_feed_entry defaults it.
            "group": feed.get("group", "Feeds"),
            **summary,
        })
    return json_response({"feeds": rows}, cache_seconds=5)


def handle_feed_page(match: re.Match, query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /api/feed/<id>?before=<cursor>&limit=N — newest-first page.

    `before` is the opaque cursor string returned as `next_before` in the
    previous page (base64 `(mtime, filename)`). Tied mtimes are handled by
    the filename tiebreaker, so a batch-touch or bulk restore no longer
    causes items to be silently skipped between pages.
    """
    feed_id = match.group("feed_id")
    if feed_id not in FEED_BY_ID:
        return error_response(404, "unknown feed")

    before_raw = (query.get("before") or [None])[0]
    limit_raw = (query.get("limit") or [str(FEED_PAGE_SIZE_DEFAULT)])[0]
    try:
        limit = int(limit_raw)
    except ValueError:
        return error_response(400, "bad limit")

    before_cursor = None
    if before_raw:
        try:
            before_cursor = feeds_module.decode_page_cursor(before_raw)
        except ValueError:
            return error_response(400, "bad before cursor")
        # NaN / Infinity mtimes make comparisons undefined; reject at the door.
        if not math.isfinite(before_cursor[0]):
            return error_response(400, "bad before cursor")

    ledger = seen_ledger.load_ledger()
    seen_set = seen_ledger.get_seen_set_for_feed(ledger, feed_id)
    page = feeds_module.list_feed_page(
        feed_id,
        seen_set=seen_set,
        before_cursor=before_cursor,
        limit=limit,
    )
    return json_response({"feed_id": feed_id, **page}, cache_seconds=5)


def handle_brief_detail(match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /api/brief/<feed_id>/<filename> — raw markdown + metadata."""
    feed_id = match.group("feed_id")
    filename = urllib.parse.unquote(match.group("filename"))
    brief_path = feeds_module.resolve_brief_path(feed_id, filename)
    if brief_path is None or not is_path_inside_allowlist(brief_path):
        return error_response(404, "not found")
    if brief_path.suffix.lower() != ".md":
        return error_response(404, "not found")
    try:
        text = brief_path.read_text(encoding="utf-8", errors="replace")
        stat = brief_path.stat()
    except OSError as read_error:
        logger.warning("brief read refused (errno %s)", read_error.errno)
        return error_response(404, "not found")
    title, tldr = feeds_module.extract_title_and_tldr(text)
    return json_response({
        "feed_id": feed_id,
        "filename": filename,
        "title": title,
        "tldr": tldr,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "markdown": text,
    })


def handle_library(match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /api/library/<source>[/<relpath>] — dir listing or file metadata."""
    source = match.group("source")
    relpath = match.group("relpath") or ""
    relpath = urllib.parse.unquote(relpath)
    try:
        target = library_module.resolve_library_path(source, relpath)
    except ValueError:
        return error_response(404, "not found")
    if not is_path_inside_allowlist(target):
        return error_response(404, "not found")
    if not safe_exists(target):
        return error_response(404, "not found")

    if safe_is_dir(target):
        try:
            listing = library_module.list_directory(source, relpath)
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return error_response(404, "not found")
        return json_response(listing, cache_seconds=5)

    extension = target.suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return error_response(404, "not found")
    try:
        stat = target.stat()
    except OSError as stat_error:
        logger.warning("library stat refused (errno %s)", stat_error.errno)
        return error_response(404, "not found")
    kind = library_module.classify_extension(target)
    metadata = {
        "source": source,
        "relpath": relpath,
        "kind": "file",
        "media_kind": kind,
        "extension": extension,
        "mime": MIME_TYPES.get(extension, "application/octet-stream"),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }
    if kind == "markdown":
        # Client renders markdown; inline the text so the UI stays snappy.
        # Oversized files fall back to the raw URL (same shape as pdf/html/
        # image/text below) so a stray multi-MB .md never spikes process RAM
        # or the JSON body — the reader loads it via a normal fetch instead.
        if stat.st_size <= MAX_INLINE_MARKDOWN_BYTES:
            try:
                metadata["markdown"] = target.read_text(encoding="utf-8", errors="replace")
            except OSError as read_error:
                logger.warning("library markdown read refused (errno %s)", read_error.errno)
                return error_response(404, "not found")
            # Prefer the document's own H1 as the display title (matches how
            # humans think about a report); fall back to the filename stem so a
            # heading-less scratch doc still shows something friendlier than
            # "papers.md".
            document_title, _ = feeds_module.extract_title_and_tldr(metadata["markdown"])
            metadata["title"] = document_title or target.stem
        else:
            logger.info(
                "library: markdown %s exceeds inline cap (%d > %d), serving via raw_url",
                target.name, stat.st_size, MAX_INLINE_MARKDOWN_BYTES,
            )
            metadata["raw_url"] = f"/raw/library/{source}/{urllib.parse.quote(relpath)}"
            metadata["oversize"] = True
            # Head-scan the same 8 KB the sidebar uses so an oversized doc
            # still gets a real title instead of just the filename stem.
            try:
                with open(target, "rb") as head_fh:
                    head_bytes = head_fh.read(feeds_module.TLDR_SCAN_BYTES)
                head_text = head_bytes.decode("utf-8", errors="replace")
                document_title, _ = feeds_module.extract_title_and_tldr(head_text)
            except OSError as head_error:
                logger.warning("library markdown head-scan refused (errno %s)", head_error.errno)
                document_title = ""
            metadata["title"] = document_title or target.stem
    else:
        metadata["raw_url"] = f"/raw/library/{source}/{urllib.parse.quote(relpath)}"
        if kind == "html":
            metadata["sandbox_url"] = f"/sandbox/library/{source}/{urllib.parse.quote(relpath)}"
    return json_response(metadata, cache_seconds=5)


def handle_raw_library(match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /raw/library/<source>/<relpath> — bytes with correct MIME.

    HTML files 404 here on purpose; they must go through /sandbox/library/*.
    """
    source = match.group("source")
    relpath = urllib.parse.unquote(match.group("relpath"))
    try:
        target = library_module.resolve_library_path(source, relpath)
    except ValueError:
        return error_response(404, "not found")
    if not safe_is_file(target):
        return error_response(404, "not found")
    if target.suffix.lower() in HTML_EXTENSIONS:
        return error_response(404, "html served via /sandbox/library/*")
    return safe_serve_file(target)


def handle_sandbox_library(match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /sandbox/library/<source>/<relpath> — HTML only, strict CSP."""
    source = match.group("source")
    relpath = urllib.parse.unquote(match.group("relpath"))
    try:
        target = library_module.resolve_library_path(source, relpath)
    except ValueError:
        return error_response(404, "not found")
    if not safe_is_file(target):
        return error_response(404, "not found")
    if target.suffix.lower() not in HTML_EXTENSIONS:
        return error_response(404, "not html")
    return safe_serve_file(target)


def handle_pulse(_match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /api/pulse — job status, daemon liveness, heartbeat."""
    return json_response(pulse_module.build_pulse_snapshot(), cache_seconds=2)


SEARCH_ALLOWED_SCOPES = {"briefs", "all"}


def handle_search(_match: re.Match, query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /api/search?q=<q>&scope=<briefs|all>&limit=<N> — substring search.

    `q` is required and treated as a literal substring (casefold both sides);
    the query is never compiled as a regex, so a value like `.*` matches
    only files that literally contain ".*". Length is capped at
    `search_module.SEARCH_QUERY_MAX_LEN` to keep pathological pastes from
    smearing across every file scan.

    `scope` defaults to "briefs" (all `briefs_*` feeds). "all" additionally
    walks reports/ + creations/. Unknown values fall back to "briefs" rather
    than 400 so a stray typo in the URL still returns something useful.

    `limit` defaults to `SEARCH_LIMIT_DEFAULT` and is clamped to
    `[SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX]`. Anything non-integer 400s.

    Ranking, snippet extraction, and truncation live in `search.run_search`;
    this handler is the argument-parsing + validation layer only.
    """
    q_raw = (query.get("q") or [""])[0]
    q_stripped = q_raw.strip()
    if not q_stripped:
        return error_response(400, "empty query")
    if len(q_stripped) > search_module.SEARCH_QUERY_MAX_LEN:
        return error_response(400, "query too long")

    scope_raw = (query.get("scope") or ["briefs"])[0]
    scope = scope_raw if scope_raw in SEARCH_ALLOWED_SCOPES else "briefs"

    limit_raw = (query.get("limit") or [str(search_module.SEARCH_LIMIT_DEFAULT)])[0]
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        return error_response(400, "bad limit")
    limit = max(search_module.SEARCH_LIMIT_MIN, min(limit, search_module.SEARCH_LIMIT_MAX))

    payload = search_module.run_search(q_stripped, scope, limit)
    # cache_seconds=2 matches /api/today — search results churn on every new
    # brief, and the module already has a 5s TTL on the underlying walk so
    # the HTTP cache adds a tiny extra dampening without going stale.
    return json_response(payload, cache_seconds=2)


def handle_today(_match: re.Match, query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """GET /api/today?hours=<N> — cross-feed "what landed recently".

    Merges every feed in FEED_REGISTRY into one newest-first list of briefs
    with mtime within the last N hours (default TODAY_HOURS_DEFAULT, clamped
    to [TODAY_HOURS_MIN, TODAY_HOURS_MAX]). Caps the list at TODAY_ITEMS_MAX
    and sets `truncated: true` when clipped, so the frontend can send the
    reader to the per-feed view when there's more history to see.
    """
    hours_raw = (query.get("hours") or [str(TODAY_HOURS_DEFAULT)])[0]
    try:
        hours = int(hours_raw)
    except (TypeError, ValueError):
        return error_response(400, "bad hours")
    hours = max(TODAY_HOURS_MIN, min(hours, TODAY_HOURS_MAX))

    ledger = seen_ledger.load_ledger()
    seen_by_feed = {
        feed_id: seen_ledger.get_seen_set_for_feed(ledger, feed_id)
        for feed_id in FEED_BY_ID
    }
    items, truncated = feeds_module.list_today_items(
        hours_window=hours,
        seen_by_feed=seen_by_feed,
    )
    payload: Dict = {"hours": hours, "items": items}
    if truncated:
        payload["truncated"] = True
    return json_response(payload, cache_seconds=2)


SEEN_FIELD_MAX_LEN = 200  # cap identifier length so the ledger stays bounded.
# Below macOS NAME_MAX (255) so we reject with a clean 400 before any FS syscall
# would raise ENAMETOOLONG. Real brief filenames are all well under 100 chars.


def is_valid_seen_field(value) -> bool:
    """Every seen-ledger field must be a non-empty string <= SEEN_FIELD_MAX_LEN.

    Arrays, dicts, ints, and oversized strings are all rejected so a caller
    cannot smuggle non-string junk into the ledger via str()-coercion.
    """
    return isinstance(value, str) and 0 < len(value) <= SEEN_FIELD_MAX_LEN


def handle_seen_post(_match: re.Match, _query: Dict, body: bytes, host: str = None) -> Tuple[int, Dict[str, str], bytes]:
    """POST /api/seen — mark one brief, one library file, or a whole feed as seen.

    Accepts one of:
      - {feed_id, filename}    single-brief mark (existing shape)
      - {source, relpath}      single-library-item mark (existing shape)
      - {feed_id, all: true}   bulk "mark every brief in this feed" (v1.1)

    Every shape must be strings (bool `true` for the `all` flag), the target
    must resolve inside the read allowlist, and CSRF-hardening lives in the
    dispatcher (Content-Type / Origin write-guards + OPTIONS 405). Any other
    shape is rejected without touching the ledger.
    """
    if len(body) > MAX_JSON_BODY_BYTES:
        return error_response(413, "body too large")
    try:
        payload = json.loads(body.decode(DEFAULT_TEXT_ENCODING) or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response(400, "bad json")
    if not isinstance(payload, dict):
        return error_response(400, "expected object body")

    feed_id = payload.get("feed_id")
    filename = payload.get("filename")
    source = payload.get("source")
    relpath = payload.get("relpath")
    all_flag = payload.get("all")

    # Bulk mark: {feed_id, all: true}. Matched before the per-brief branch so
    # a caller that passes both `filename` and `all` gets a clear error rather
    # than a silent fallthrough — but in practice `all` is only ever paired
    # with `feed_id`.
    if all_flag is not None:
        # Strict `is True` — reject truthy strings/ints/lists that JSON might
        # let through. `False` and non-bool values both land here.
        if all_flag is not True:
            return error_response(400, "all must be boolean true")
        if not is_valid_seen_field(feed_id):
            return error_response(400, "feed_id required and must be a string <= 200 chars")
        if feed_id not in FEED_BY_ID:
            return error_response(404, "unknown feed")
        # `filename` / `source` / `relpath` are ignored in this branch, but
        # nothing bad happens if they were passed alongside `all: true`.
        filenames = feeds_module.all_display_filenames_for_feed(feed_id)
        marked = seen_ledger.mark_feed_items_seen(feed_id, filenames)
        # Drop the summary cache so /api/feeds shows unread=0 immediately.
        feeds_module.invalidate_feed_summary_cache(feed_id)
        return json_response({"ok": True, "feed_id": feed_id, "marked": marked})

    if feed_id is not None or filename is not None:
        if not (is_valid_seen_field(feed_id) and is_valid_seen_field(filename)):
            return error_response(400, "feed_id and filename must be strings <= 200 chars")
        if feed_id not in FEED_BY_ID:
            return error_response(404, "unknown feed")
        brief_path = feeds_module.resolve_brief_path(feed_id, filename)
        if brief_path is None or not is_path_inside_allowlist(brief_path):
            return error_response(404, "brief not found")
        seen_ledger.mark_feed_item_seen(feed_id, filename)
        # Drop the summary cache for this feed so the sidebar's unread badge
        # reflects the new seen state on the next /api/feeds hit.
        feeds_module.invalidate_feed_summary_cache(feed_id)
        return json_response({"ok": True, "feed_id": feed_id, "filename": filename})

    if source is not None or relpath is not None:
        if not (is_valid_seen_field(source) and is_valid_seen_field(relpath)):
            return error_response(400, "source and relpath must be strings <= 200 chars")
        try:
            resolved = library_module.resolve_library_path(source, relpath)
        except ValueError:
            return error_response(404, "unknown library item")
        if not is_path_inside_allowlist(resolved) or not safe_is_file(resolved):
            return error_response(404, "library item not found")
        seen_ledger.mark_library_item_seen(source, relpath)
        return json_response({"ok": True, "source": source, "relpath": relpath})

    return error_response(400, "need feed_id+filename, feed_id+all, or source+relpath")


# --- Passphrase gate: unlock + lock screen -----------------------------------

def handle_lock_screen(_match, _query) -> Tuple[int, Dict[str, str], bytes]:
    """GET /lock — the passphrase entry screen.

    Serves a small standalone HTML page (its own tiny JS + CSS + the shared
    tokens.css theme file). No app bundle, no feed/library data reaches it —
    the shell only loads after a successful POST /api/unlock sets the cookie.

    Returns 404 when the gate is OFF (option A): option A shouldn't advertise
    the gate at all, and a locked-shape lookup on an open deployment is just
    scanner noise.
    """
    if not unlock_gate.is_gate_enabled():
        return error_response(404, "not found")
    return serve_static_asset("lock.html")


def parse_unlock_body(body: bytes) -> Tuple[str, bool]:
    """Extract (submitted_passphrase_or_dummy, was_valid_shape) from a POST body.

    On ANY invalid shape (oversized body, bad JSON, wrong shape, missing or
    non-string or empty or oversized `passphrase` field), returns
    `(DUMMY_PASSPHRASE_FOR_TIMING_UNIFORMITY, False)`. The caller then still
    runs the full Keychain-read + hmac.compare_digest pipeline against the
    dummy, so wrong-passphrase and bad-shape take the same code path and the
    same time. This is the timing-oracle fix the audit called out.
    """
    if len(body) > MAX_JSON_BODY_BYTES:
        return unlock_gate.DUMMY_PASSPHRASE_FOR_TIMING_UNIFORMITY, False
    try:
        payload = json.loads(body.decode(DEFAULT_TEXT_ENCODING) or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return unlock_gate.DUMMY_PASSPHRASE_FOR_TIMING_UNIFORMITY, False
    if not isinstance(payload, dict):
        return unlock_gate.DUMMY_PASSPHRASE_FOR_TIMING_UNIFORMITY, False
    submitted = payload.get("passphrase")
    if not isinstance(submitted, str) or not submitted:
        return unlock_gate.DUMMY_PASSPHRASE_FOR_TIMING_UNIFORMITY, False
    if len(submitted) > unlock_gate.MAX_PASSPHRASE_LENGTH:
        return unlock_gate.DUMMY_PASSPHRASE_FOR_TIMING_UNIFORMITY, False
    return submitted, True


def handle_unlock(_match, _query, body: bytes, host: str = None) -> Tuple[int, Dict[str, str], bytes]:
    """POST /api/unlock — verify the passphrase, mint a session cookie (uniform timing).

    `host` is the request Host header (already Host-allowlist-validated by the
    dispatcher). Passed through to `build_unlock_set_cookie_header` so the
    Set-Cookie's `Secure` attribute is on for the tailnet (HTTPS) and off for
    plain-HTTP loopback (dev/QA). Defaults to None (Secure on) so a test or
    direct caller that omits it fails safe.

    ORDER (load-bearing):

      1. Gate-OFF 404. When no passphrase hash is set (option A) the endpoint
         does not exist for callers — no state file is created, no lockout
         counter moves, and the app doesn't advertise the gate.

      2. Persistent lockout check. Runs BEFORE any hash / body work so a
         locked-out caller can't keep burning compute — 429 + Retry-After.
         The audit accepted this short-circuit; the timing differential the
         audit cares about is the wrong-passphrase-vs-bad-shape gap AFTER the
         lockout check, not the 429 path itself.

      3. Parse body → (passphrase-or-dummy, was_valid_shape). Every bad-shape
         path collapses to the dummy so the pipeline below runs to completion
         in every case. This closes the ~35x timing oracle the audit flagged
         between "reached hash compare" and "rejected at parse".

      4. Fresh Keychain read (not the cached gate flag — the user may have
         rotated the hash mid-run, and a fresh read means the new passphrase
         works immediately). Any error becomes `expected_hash = None`, then
         the compare still runs against a fixed dummy hash so the response
         time doesn't leak "gate on with hash" vs "gate on but hash gone".

      5. hmac.compare_digest always runs (same-length hex inputs → no length
         oracle either).

      6. Success requires ALL of: valid body shape, real Keychain hash, and
         constant-time-matched compare. Anything less → record ONE failed
         attempt and return the uniform `401 {"error":"incorrect"}`. Never
         reveal whether the shape was wrong vs the passphrase wrong.

    The dispatcher's write-guards (Content-Type=application/json, Origin
    allowlist, OPTIONS→405, Host allowlist) run BEFORE this handler; a
    text/plain CSRF POST or an off-origin fetch never reaches step 1.
    """
    # 1. Gate-OFF 404: option A never advertises the gate.
    if not unlock_gate.is_gate_enabled():
        return error_response(404, "not found")

    # 2. Lockout gate — no compute for locked-out callers.
    retry_after = unlock_gate.current_lockout_retry_after()
    if retry_after is not None:
        status, headers, body_bytes = error_response(429, "too many attempts, try again later")
        headers["Retry-After"] = str(retry_after)
        return status, headers, body_bytes

    # 3. Body parse (never returns early; bad shape becomes the dummy).
    submitted_passphrase, body_was_valid = parse_unlock_body(body)

    # 4. Fresh Keychain read. Torn-out hash or transient error → dummy hash;
    #    the compare still runs so timing stays uniform, but success then
    #    requires expected_hash to actually be present, so the outcome is
    #    still fail-closed.
    try:
        expected_hash = unlock_gate.read_passphrase_hash_from_keychain()
    except unlock_gate.GateReadError:
        logger.error("passphrase-gate: keychain unreadable during unlock; denying")
        expected_hash = None
    hash_for_compare = (
        expected_hash
        if expected_hash is not None
        else unlock_gate.DUMMY_HASH_FOR_TIMING_UNIFORMITY
    )

    # 5. Constant-time compare — always executed, uniform timing.
    compare_matched = unlock_gate.verify_passphrase(submitted_passphrase, hash_for_compare)

    # 6. Outcome. Every negative case is one recorded failure + generic 401.
    if body_was_valid and expected_hash is not None and compare_matched:
        token = unlock_gate.mint_and_store_token()
        unlock_gate.reset_lockout()
        body_bytes = json.dumps({"ok": True}).encode(DEFAULT_TEXT_ENCODING)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body_bytes)),
            "Cache-Control": "no-store",
            "Set-Cookie": unlock_gate.build_unlock_set_cookie_header(token, host=host),
        }
        return 200, apply_security_headers(headers), body_bytes

    unlock_gate.record_failed_unlock()
    return error_response(401, "incorrect")
