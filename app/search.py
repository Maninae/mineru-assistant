"""Substring search across briefs and library files (backing /api/search).

Given a query string, scope, and limit, walks the relevant file trees
(briefs for scope=briefs, plus reports/creations for scope=all), scores
each hit by where the match appeared (title > tldr > filename > body),
and returns a newest-first, capped list of results with a short context
snippet around the first body match.

Match semantics are **literal substring** (casefold both sides): the query
is never compiled as a regex, so a value like `.*` matches only files that
literally contain ".*". This is the ReDoS defense — Python's C `str.find`
is O(n*m) worst-case but has no catastrophic backtracking.

Body scans are bounded to `SEARCH_BODY_MAX_BYTES` (~16 KB from the head of
each file) so a huge file never turns one request into a full-tree read.
The total number of files scanned per request is capped at
`SEARCH_MAX_FILES_SCANNED`; hitting either cap sets `truncated=True`.

A short TTL cache keyed by (query, scope, limit) keeps repeated queries
(pagination, quick backspace-retype) from re-walking the tree — same
rhythm as the sidebar/Today caches in `feeds.py`.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from config import (
    ALLOWED_EXTENSIONS,
    FEED_REGISTRY,
    LIBRARY_SOURCES,
    TEXT_EXTENSIONS,
)
import feeds as feeds_module


logger = logging.getLogger(__name__)


# --- Contract knobs ----------------------------------------------------------

# Longest accepted query. Longer than this almost certainly isn't a real
# search (a paste of a whole brief, an injection payload); rejecting up
# front keeps a huge string from being smeared across every file scan.
SEARCH_QUERY_MAX_LEN = 200

# Result count clamp. The frontend paginates by re-issuing with a larger
# limit rather than by cursor, so this is a hard ceiling.
SEARCH_LIMIT_DEFAULT = 40
SEARCH_LIMIT_MAX = 100
SEARCH_LIMIT_MIN = 1

# Head bytes read from each candidate file for the body scan. Twice the
# feed sidebar's TLDR scan (`feeds.TLDR_SCAN_BYTES` = 8 KB), so titles and
# tldrs are still recoverable while the body window catches more real prose.
# Kept explicitly separate from TLDR_SCAN_BYTES so bumping this doesn't
# change how the sidebar renders.
SEARCH_BODY_MAX_BYTES = 16384

# Total files scanned per request. Well above the current brief+library
# corpus (~1k files today); a runaway workspace trips `truncated=True`
# instead of hanging the request.
SEARCH_MAX_FILES_SCANNED = 5000

# Snippet window (chars) around a body match. Enough context to orient
# the reader without ballooning the payload.
SEARCH_SNIPPET_CHARS = 160

# Same TTL as the sidebar/Today caches — a fresh brief becomes searchable
# within a couple of seconds without any explicit invalidation.
SEARCH_CACHE_TTL_SECONDS = 5.0

# Match ranking. Lower = better. Sorted primarily by tier, then by -mtime.
MATCH_TIER_TITLE = 0
MATCH_TIER_TLDR = 1
MATCH_TIER_FILENAME = 2
MATCH_TIER_BODY = 3
MATCH_TIER_LABEL: Dict[int, str] = {
    MATCH_TIER_TITLE: "title",
    MATCH_TIER_TLDR: "tldr",
    MATCH_TIER_FILENAME: "filename",
    MATCH_TIER_BODY: "body",
}

# Body scans only touch text-ish files; binary/HTML files can still match
# on filename or title but their contents are never read.
LIBRARY_BODY_EXTENSIONS = set(TEXT_EXTENSIONS)


# --- Result cache -------------------------------------------------------------

_search_cache: Dict[Tuple[str, str, int], Tuple[float, Dict]] = {}
_search_cache_lock = threading.Lock()


def invalidate_search_cache() -> None:
    """Drop the whole result cache. Rarely needed; TTL handles most churn."""
    with _search_cache_lock:
        _search_cache.clear()


# --- Library file iteration ---------------------------------------------------

def iter_library_files(source: str) -> Iterable[Tuple[Path, str]]:
    """Yield (path, relpath) for every non-hidden, non-symlink, allowed-extension
    file under a library source root, recursively.

    Mirrors `feeds.iter_brief_files`: dotfiles at ANY segment are skipped,
    symlinks are skipped so a listed-but-unclickable escape can't creep in.
    The extension allowlist matches `library.list_directory`, so search
    candidates line up exactly with what the browsing UI already exposes.
    """
    root = LIBRARY_SOURCES.get(source)
    if root is None:
        return
    root_resolved = root.resolve()
    try:
        if not root_resolved.exists() or not root_resolved.is_dir():
            return
    except OSError:
        return
    try:
        walker = root_resolved.rglob("*")
    except OSError as walk_error:
        logger.warning("cannot walk library source %s: %s", source, walk_error)
        return
    for path in walker:
        try:
            rel = path.relative_to(root_resolved)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            if path.is_symlink():
                continue
            if not path.is_file():
                continue
        except OSError:
            continue
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        yield path, str(rel)


# --- Snippet extraction -------------------------------------------------------

def collapse_whitespace(text: str) -> str:
    """Fold runs of whitespace into a single space, strip both ends."""
    return " ".join(text.split())


def build_snippet_around(needle_casefold: str, haystack: str) -> str:
    """Return a ~SEARCH_SNIPPET_CHARS window of `haystack` centered on the
    first case-insensitive occurrence of `needle_casefold`.

    The matched region is kept in the haystack's original case; the
    surrounding text is whitespace-collapsed for readability. If the needle
    isn't actually present (shouldn't happen once a body match has fired),
    falls back to the head of the file so the response still has SOMETHING.
    """
    haystack_casefold = haystack.casefold()
    match_at = haystack_casefold.find(needle_casefold)
    if match_at < 0:
        return collapse_whitespace(haystack[:SEARCH_SNIPPET_CHARS])
    half = SEARCH_SNIPPET_CHARS // 2
    start = max(0, match_at - half)
    end = min(len(haystack), start + SEARCH_SNIPPET_CHARS)
    # Re-anchor start so the tail can extend when we clipped against 0.
    start = max(0, end - SEARCH_SNIPPET_CHARS)
    snippet = collapse_whitespace(haystack[start:end])
    if start > 0:
        snippet = "… " + snippet
    if end < len(haystack):
        snippet = snippet + " …"
    return snippet


# --- Match evaluation ---------------------------------------------------------

def evaluate_match(
    query_casefold: str,
    filename: str,
    title: str,
    tldr: str,
    body_head: str,
) -> Tuple[Optional[int], str]:
    """Return `(tier, snippet)` for the best match on this file, or `(None, "")`.

    Tier ordering (best first): title > tldr > filename > body. Snippet is
    the matched field (whitespace-collapsed) for title/tldr/filename, or a
    window around the first body occurrence for body.
    """
    if title and query_casefold in title.casefold():
        return MATCH_TIER_TITLE, collapse_whitespace(title)
    if tldr and query_casefold in tldr.casefold():
        return MATCH_TIER_TLDR, collapse_whitespace(tldr)
    if query_casefold in filename.casefold():
        return MATCH_TIER_FILENAME, filename
    if body_head and query_casefold in body_head.casefold():
        return MATCH_TIER_BODY, build_snippet_around(query_casefold, body_head)
    return None, ""


def read_body_head(path: Path) -> str:
    """Read up to SEARCH_BODY_MAX_BYTES from the head of `path` as text.

    Returns "" on any I/O error; callers treat "no body" as "no body match",
    which is the same handling as a genuinely-empty file.
    """
    try:
        with open(path, "rb") as fh:
            head_bytes = fh.read(SEARCH_BODY_MAX_BYTES)
    except OSError as read_error:
        logger.warning("search: skipping unreadable file %s (errno %s)", path, read_error.errno)
        return ""
    return head_bytes.decode("utf-8", errors="replace")


# --- Candidate scans ----------------------------------------------------------

def scan_briefs_for_query(
    query_casefold: str,
    files_budget: int,
) -> Tuple[List[Dict], int, bool]:
    """Walk every brief feed and collect hits.

    Returns `(hits, remaining_budget, truncated)`. `truncated` is True when
    the file-scan budget ran out mid-walk.
    """
    hits: List[Dict] = []
    truncated = False
    for feed in FEED_REGISTRY:
        feed_id = feed["id"]
        dirs = feeds_module.feed_directories(feed_id)
        if not dirs:
            continue
        for brief_path, brief_mtime in feeds_module.collect_brief_files_sorted(feed_id):
            if files_budget <= 0:
                truncated = True
                return hits, 0, truncated
            files_budget -= 1
            filename = feeds_module.brief_display_filename(brief_path, dirs)
            body_head = read_body_head(brief_path)
            title, tldr = feeds_module.extract_title_and_tldr(body_head)
            tier, snippet = evaluate_match(
                query_casefold, filename, title, tldr, body_head
            )
            if tier is None:
                continue
            hits.append({
                "kind": "brief",
                "feed_id": feed_id,
                "filename": filename,
                "title": title,
                "snippet": snippet,
                "mtime": brief_mtime,
                "matched_in": MATCH_TIER_LABEL[tier],
                "_tier": tier,
            })
    return hits, files_budget, truncated


def scan_library_for_query(
    query_casefold: str,
    files_budget: int,
) -> Tuple[List[Dict], int, bool]:
    """Walk reports/ + creations/ and collect hits.

    Text-ish files (md/txt/csv/log/json) get body-scanned; binary and HTML
    files match only on filename or title (title is the filename stem when
    we can't read a real one from bytes).
    """
    hits: List[Dict] = []
    truncated = False
    for source in LIBRARY_SOURCES:
        for path, relpath in iter_library_files(source):
            if files_budget <= 0:
                truncated = True
                return hits, 0, truncated
            files_budget -= 1
            extension = path.suffix.lower()
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if extension in LIBRARY_BODY_EXTENSIONS:
                body_head = read_body_head(path)
                title, tldr = feeds_module.extract_title_and_tldr(body_head)
                if not title:
                    title = path.stem
            else:
                body_head = ""
                title = path.stem
                tldr = ""
            tier, snippet = evaluate_match(
                query_casefold, relpath, title, tldr, body_head
            )
            if tier is None:
                continue
            hits.append({
                "kind": "library",
                "source": source,
                "relpath": relpath,
                "title": title,
                "snippet": snippet,
                "mtime": mtime,
                "matched_in": MATCH_TIER_LABEL[tier],
                "_tier": tier,
            })
    return hits, files_budget, truncated


# --- Public entry point -------------------------------------------------------

def run_search(query: str, scope: str, limit: int) -> Dict:
    """Execute one search and return the response payload dict.

    Args:
        query: caller-provided string, already length-validated at the handler.
        scope: 'briefs' (default) or 'all' (adds reports + creations).
        limit: caller-provided int, clamped to [SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX].

    Returns the response dict shaped exactly as the frontend expects
    ({query, scope, truncated, results}). Symlink escapes, dotfiles, and
    non-allowed extensions are all filtered before matching.
    """
    query = query.strip()
    query_casefold = query.casefold()
    cache_key = (query, scope, limit)

    now = time.time()
    with _search_cache_lock:
        entry = _search_cache.get(cache_key)
        if entry and (now - entry[0]) < SEARCH_CACHE_TTL_SECONDS:
            return entry[1]

    files_budget = SEARCH_MAX_FILES_SCANNED
    briefs_hits, files_budget, truncated_briefs = scan_briefs_for_query(
        query_casefold, files_budget
    )
    if scope == "all":
        library_hits, _, truncated_library = scan_library_for_query(
            query_casefold, files_budget
        )
    else:
        library_hits, truncated_library = [], False

    all_hits = briefs_hits + library_hits
    # Sort by tier ascending (title=0 best), then by newest mtime.
    all_hits.sort(key=lambda h: (h["_tier"], -h["mtime"]))

    truncated = truncated_briefs or truncated_library
    if len(all_hits) > limit:
        truncated = True
        all_hits = all_hits[:limit]

    # Strip the sort-only tier field before it hits the wire.
    for hit in all_hits:
        hit.pop("_tier", None)

    payload = {
        "query": query,
        "scope": scope,
        "truncated": truncated,
        "results": all_hits,
    }
    with _search_cache_lock:
        _search_cache[cache_key] = (time.time(), payload)
    return payload
