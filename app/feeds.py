"""Feed listing, TLDR extraction, and pagination.

Given a feed id, walk its registered dirs, collect every .md file, sort
newest-first, and page over that list. The seen ledger decorates each item
with a seen flag so the UI can render unread badges.

Title/TLDR extraction goes through `lib.tldr.extract_title_and_tldr`, which
is the single source of truth shared with `scripts/deliver-output.py`. The
web wrapper returns an empty tldr whenever it would repeat the title, so the
Inbox card can hide the redundant row.

Pagination uses an opaque `(mtime, filename)` cursor encoded as URL-safe
base64 JSON, so briefs that share an mtime aren't dropped between pages.
Cursors survive through the frontend as an opaque string; the backend is the
only side that parses them.

`summarize_feed` results are cached under a short TTL and invalidated on
`POST /api/seen` for the touched feed, so the sidebar isn't recomputed on
every keystroke while still going stale within a couple of seconds on writes.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import base64
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from config import (
    FEED_BY_ID,
    FEED_PAGE_SIZE_DEFAULT,
    FEED_PAGE_SIZE_MAX,
    FEED_REGISTRY,
    MINERU_HOME,
    TODAY_ITEMS_MAX,
)

# `lib/` lives one level above `app/`. To share the TLDR helper with
# `scripts/deliver-output.py` we bootstrap sys.path here, but anchoring on
# THIS file's location (not `MINERU_HOME`) so a worktree checkout finds its
# own `lib/`, not the live `$MINERU_HOME/lib/`.
#
# `MINERU_HOME` in `config.py` points at the live workspace ($MINERU_HOME, or
# `Path.home() / ".mineru"` by default). If we insert that at sys.path[0], any
# worktree checkout of this repo silently shadows its own peer packages
# (notably `browser/`) with the live workspace copy the moment `feeds` is
# imported. The `test_browser_lifecycle` /
# `test_browser_sse` collection errors traced back to exactly this line.
_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))
from lib.tldr import (  # noqa: E402
    clean_display_line,
    extract_title_and_tldr,
    is_title_skip_line,
    strip_yaml_frontmatter,
    TLDR_LINE_RE,
)


# --- Preview snippet extraction ---------------------------------------------
#
# The message-row `.row-snippet` (spec §4, matches the reference screenshot's
# 2-line preview under each subject) needs a plain-prose sentence pulled from
# the brief body. That's NOT the TLDR: the tldr is a strict `TL;DR:` line when
# present and otherwise falls back to the title, so an inbox-triage brief with
# no TLDR ends up with tldr==title==headline (the frontend hides that
# duplicate). Preview scans past the leading heading (and any TLDR line) and
# grabs the next real content chunk, so every row gets useful body text.

# Preview cap — 160 chars ≈ 2 lines of the 13px snippet at the current list
# width. The message-row CSS clamps to 2 lines with a hard ellipsis, so an
# overshoot doesn't render, but we cap here anyway to keep JSON responses tight.
PREVIEW_MAX_CHARS = 160

# `--- ... ---` and `***` are horizontal rules, not content. Skip these when
# hunting for preview text so a brief that opens with a decorative divider
# doesn't have an empty preview.
_HORIZONTAL_RULE_RE = re.compile(r"^\s*(?:-\s*){3,}\s*$|^\s*(?:\*\s*){3,}\s*$")


def extract_preview(content: str) -> str:
    """First real body sentence(s) from a brief, minus the title and TLDR.

    Distinct from `extract_title_and_tldr`:
      - Title comes from the first heading; preview STARTS after that.
      - Tldr fires only when a brief carries an explicit `TL;DR:` line; when
        it's absent, tldr collapses to the title and the frontend hides it —
        so preview is the only source of "what does this brief actually say"
        for a large fraction of briefs (morning brief body, news lede, etc.).

    Returns an empty string when nothing useful is left after skipping.
    """
    lines = strip_yaml_frontmatter(content.splitlines())
    saw_first_heading = False
    parts = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            # A blank line AFTER we've collected content ends the preview.
            if parts:
                break
            continue
        if is_title_skip_line(stripped):
            continue
        if _HORIZONTAL_RULE_RE.match(stripped):
            continue
        if not saw_first_heading and stripped.startswith("#"):
            saw_first_heading = True
            continue
        if TLDR_LINE_RE.match(stripped):
            # Never surface the TLDR itself in the preview slot; the TLDR is
            # already carried by the `tldr` field for callers that want it.
            continue
        parts.append(stripped)
        if sum(len(p) for p in parts) + len(parts) - 1 >= PREVIEW_MAX_CHARS:
            break
    if not parts:
        return ""
    joined = " ".join(parts)
    cleaned = clean_display_line(joined)
    # clean_display_line truncates at TLDR_MAX_CHARS (200); tighten to our own
    # cap so the preview never wraps past 2 rendered lines.
    if len(cleaned) > PREVIEW_MAX_CHARS:
        cleaned = cleaned[: PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return cleaned


logger = logging.getLogger(__name__)


BRIEF_EXTENSION = ".md"
# Read enough head bytes to find the TLDR without slurping big files.
TLDR_SCAN_BYTES = 8192

# Sidebar summary cache. `summarize_feed` re-walks every feed directory on
# each hit; without this the sidebar amplifies filesystem load 40-50x under
# concurrent tabs. Invalidated per-feed on POST /api/seen.
FEED_SUMMARY_CACHE_TTL_SECONDS = 5.0
_feed_summary_cache: Dict[str, Tuple[float, Dict]] = {}
_feed_summary_cache_lock = threading.Lock()

# Cross-feed Today cache. `list_today_items` walks EVERY feed and head-scans
# each in-window brief for its title/tldr, so the same tab hitting /api/today
# on refresh would repeat that work otherwise. Cached value is seen-flag-free
# so a mark-seen never has to invalidate — the flag is stamped fresh from the
# live ledger at response time. TTL matches the sidebar cache so a new brief
# is visible within a couple of seconds without an explicit invalidation.
TODAY_ITEMS_CACHE_TTL_SECONDS = 5.0
_today_items_cache: Dict[int, Tuple[float, List[Dict], bool]] = {}
_today_items_cache_lock = threading.Lock()


def invalidate_feed_summary_cache(feed_id: Optional[str] = None) -> None:
    """Drop cached summaries so the next /api/feeds hit re-walks the disk.

    Called from handle_seen_post after a successful write, and safe to call
    with an unknown feed_id (no-ops).
    """
    with _feed_summary_cache_lock:
        if feed_id is None:
            _feed_summary_cache.clear()
        else:
            _feed_summary_cache.pop(feed_id, None)


def invalidate_today_items_cache() -> None:
    """Drop the Today cross-feed cache. Rarely needed; the TTL handles most churn."""
    with _today_items_cache_lock:
        _today_items_cache.clear()


def encode_page_cursor(mtime: float, filename: str) -> str:
    """Pack `(mtime, filename)` into a URL-safe opaque cursor string.

    Frontend echoes the cursor verbatim into `?before=<cursor>`; only this
    module reads it. base64(JSON) keeps the format extensible and avoids
    ambiguity with filenames that contain any punctuation.
    """
    payload = json.dumps({"mtime": mtime, "filename": filename}, ensure_ascii=False)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_page_cursor(cursor: str) -> Tuple[float, str]:
    """Reverse of encode_page_cursor. Raises ValueError on any malformed input."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii") + b"==")
    except (ValueError, TypeError) as decode_error:
        raise ValueError("bad cursor encoding") from decode_error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as json_error:
        raise ValueError("cursor is not valid JSON") from json_error
    if not isinstance(payload, dict):
        raise ValueError("cursor must be a JSON object")
    mtime = payload.get("mtime")
    filename = payload.get("filename")
    if not isinstance(mtime, (int, float)) or not isinstance(filename, str):
        raise ValueError("cursor fields must be number+string")
    return float(mtime), filename


def feed_directories(feed_id: str) -> List[Path]:
    """Resolved on-disk dirs backing a feed id (each may or may not exist)."""
    feed = FEED_BY_ID.get(feed_id)
    if feed is None:
        return []
    dirs: List[Path] = []
    for feed_dir_name in feed["dirs"]:
        candidate = (MINERU_HOME / feed_dir_name).resolve()
        if candidate.exists() and candidate.is_dir():
            dirs.append(candidate)
    return dirs


def iter_brief_files(feed_dirs: Iterable[Path]) -> Iterable[Path]:
    """Yield every non-hidden, non-symlink .md brief under the feed dirs (recursive).

    Dotfiles (`.hidden.md`, `._DS_Store.md`) are filtered here to match the
    library listing's behavior — a hidden brief is the user's own scratch or
    editor cruft, never something the sidebar should surface.

    Symlinks are filtered here so listings, /api/today, and bulk mark-all-read
    all agree with the per-brief read path: `resolve_brief_path` +
    `is_path_inside_allowlist` reject symlinks that resolve outside their
    feed dir, so a listed-but-unclickable brief would just 404 on open and
    still land in the seen-ledger on bulk-seen. Cron writes real .md files
    into these dirs, never symlinks; anything symlinked was placed by hand
    and doesn't belong in the sidebar.

    Recursion matters: briefs_news stores its actual briefs under
    briefs_news/briefs/*.md.
    """
    for feed_dir in feed_dirs:
        try:
            for path in feed_dir.rglob("*" + BRIEF_EXTENSION):
                if any(part.startswith(".") for part in path.relative_to(feed_dir).parts):
                    continue
                try:
                    if path.is_symlink():
                        continue
                except OSError:
                    # Broken symlink or transient FS error — treat as absent.
                    continue
                yield path
        except OSError as walk_error:
            logger.warning("cannot walk feed dir %s: %s", feed_dir, walk_error)


def collect_brief_files_sorted(feed_id: str) -> List[Tuple[Path, float]]:
    """All briefs under this feed with their mtimes, sorted newest-first.

    Sort key is `(-mtime, path.name)` so briefs with tied mtimes have a
    deterministic order and the `(mtime, filename)` cursor never revisits
    or skips a tie group.
    """
    dirs = feed_directories(feed_id)
    if not dirs:
        return []
    briefs: List[Tuple[Path, float]] = []
    for path in iter_brief_files(dirs):
        try:
            if path.is_file():
                briefs.append((path, path.stat().st_mtime))
        except OSError:
            continue
    briefs.sort(key=lambda item: (-item[1], item[0].name))
    return briefs


def brief_display_filename(brief_path: Path, feed_dirs: List[Path]) -> str:
    """The path we hand back to /api/brief/<feed_id>/<filename>.

    Relative to whichever backing dir contains the file (so briefs from
    briefs_news/briefs/ round-trip as `briefs/2026-08-16.md`).
    """
    for feed_dir in feed_dirs:
        try:
            return str(brief_path.relative_to(feed_dir))
        except ValueError:
            continue
    return brief_path.name


def summarize_feed(feed_id: str, seen_set: Set[str]) -> Dict:
    """{unread_count, latest_ts, total_count} for the /api/feeds landing view.

    Cached under FEED_SUMMARY_CACHE_TTL_SECONDS to spare a full rglob per hit.
    Because unread_count depends on the seen set (which mutates via POST
    /api/seen), invalidation is explicit — see invalidate_feed_summary_cache.
    """
    now = time.time()
    with _feed_summary_cache_lock:
        entry = _feed_summary_cache.get(feed_id)
        if entry and (now - entry[0]) < FEED_SUMMARY_CACHE_TTL_SECONDS:
            return entry[1]

    briefs = collect_brief_files_sorted(feed_id)
    dirs = feed_directories(feed_id)
    total = len(briefs)
    unread = 0
    for brief_path, _ in briefs:
        display = brief_display_filename(brief_path, dirs)
        if display not in seen_set:
            unread += 1
    latest_ts = briefs[0][1] if briefs else 0.0
    summary = {
        "unread_count": unread,
        "latest_ts": latest_ts,
        "total_count": total,
    }
    with _feed_summary_cache_lock:
        _feed_summary_cache[feed_id] = (time.time(), summary)
    return summary


def read_head_for_tldr(brief_path: Path) -> str:
    """Read just the first few KB of a brief for TLDR/title extraction."""
    with open(brief_path, "rb") as fh:
        head_bytes = fh.read(TLDR_SCAN_BYTES)
    return head_bytes.decode("utf-8", errors="replace")


def list_feed_page(
    feed_id: str,
    seen_set: Set[str],
    before_cursor: Optional[Tuple[float, str]] = None,
    limit: int = FEED_PAGE_SIZE_DEFAULT,
) -> Dict:
    """Paginated newest-first feed items.

    Args:
        feed_id: registered feed id from FEED_REGISTRY.
        seen_set: filenames the user has already opened in this feed.
        before_cursor: (mtime, filename) cursor. If set, only items strictly
            "older" in the newest-first sort key `(-mtime, filename)` are
            returned. Callers echo the encoded cursor from the previous
            response.
        limit: max items per page (capped by FEED_PAGE_SIZE_MAX).

    Returns {items, next_before, has_more}. `next_before` is either a
    URL-safe base64 cursor string, or None when there are no more pages.
    """
    limit = max(1, min(int(limit), FEED_PAGE_SIZE_MAX))
    briefs = collect_brief_files_sorted(feed_id)
    dirs = feed_directories(feed_id)

    if before_cursor is not None:
        cursor_mtime, cursor_filename = before_cursor
        cursor_key = (-cursor_mtime, cursor_filename)
        briefs = [item for item in briefs if (-item[1], item[0].name) > cursor_key]

    page = briefs[:limit]
    has_more = len(briefs) > limit
    next_before: Optional[str] = None
    if page and has_more:
        last_path, last_mtime = page[-1]
        next_before = encode_page_cursor(last_mtime, last_path.name)

    items: List[Dict] = []
    for brief_path, brief_mtime in page:
        try:
            head = read_head_for_tldr(brief_path)
        except OSError as read_error:
            logger.warning("skipping unreadable brief %s: %s", brief_path, read_error)
            continue
        title, tldr = extract_title_and_tldr(head)
        # Additive: `preview` carries a plain-prose snippet distinct from
        # `tldr` (which is often empty when it would duplicate the title).
        # See `extract_preview` for the scan rules; safe on any brief shape.
        preview = extract_preview(head)
        display = brief_display_filename(brief_path, dirs)
        try:
            size = brief_path.stat().st_size
        except OSError:
            size = 0
        items.append({
            "filename": display,
            "title": title,
            "tldr": tldr,
            "preview": preview,
            "mtime": brief_mtime,
            "size": size,
            "seen": display in seen_set,
        })

    return {
        "items": items,
        "next_before": next_before,
        "has_more": has_more,
    }


def all_display_filenames_for_feed(feed_id: str) -> List[str]:
    """Every current brief in `feed_id`, as the display filename we'd hand
    back to /api/brief/<feed_id>/<filename>.

    Used by the bulk mark-all-read path: enumerating the same set the sidebar
    counts as unread means marking-all guarantees the badge drops to zero
    (no drift from hidden-file skip differences). Errors on individual paths
    are swallowed exactly like `collect_brief_files_sorted` does, so the
    bulk mark degrades gracefully when a file vanishes mid-walk.
    """
    dirs = feed_directories(feed_id)
    if not dirs:
        return []
    filenames: List[str] = []
    for path in iter_brief_files(dirs):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        filenames.append(brief_display_filename(path, dirs))
    return filenames


def collect_today_items_uncached(hours_window: int) -> Tuple[List[Dict], bool]:
    """Walk every feed once, keep briefs whose mtime is within the window.

    Returns `(items, truncated)` where each item carries feed metadata + the
    same title/tldr/mtime shape as `list_feed_page` items, but no `seen` flag
    — callers apply that from the live ledger so a stale cache entry doesn't
    pin a bad seen state. Newest-first across all feeds, capped at
    TODAY_ITEMS_MAX; `truncated` is True when we clipped items off the tail.

    Iteration reuses `collect_brief_files_sorted` (per-feed newest-first
    sort), so we can stop scanning a feed the moment its next brief falls
    below the cutoff, keeping the walk O(items-in-window) not O(all-briefs).
    """
    cutoff = time.time() - hours_window * 3600.0
    all_items: List[Dict] = []
    for feed in FEED_REGISTRY:
        feed_id = feed["id"]
        dirs = feed_directories(feed_id)
        if not dirs:
            continue
        for brief_path, brief_mtime in collect_brief_files_sorted(feed_id):
            if brief_mtime < cutoff:
                # Sorted newest-first, so the rest are older too.
                break
            try:
                head = read_head_for_tldr(brief_path)
            except OSError as read_error:
                logger.warning("today: skipping unreadable brief %s: %s", brief_path, read_error)
                continue
            title, tldr = extract_title_and_tldr(head)
            preview = extract_preview(head)
            display = brief_display_filename(brief_path, dirs)
            all_items.append({
                "feed_id": feed_id,
                "feed_display_name": feed["display_name"],
                "feed_emoji": feed["emoji"],
                "feed_accent": feed["accent"],
                "filename": display,
                "title": title,
                "tldr": tldr,
                "preview": preview,
                "mtime": brief_mtime,
            })
    # Newest-first across feeds; filename tiebreaker keeps the order stable.
    all_items.sort(key=lambda item: (-item["mtime"], item["feed_id"], item["filename"]))
    truncated = len(all_items) > TODAY_ITEMS_MAX
    if truncated:
        all_items = all_items[:TODAY_ITEMS_MAX]
    return all_items, truncated


def list_today_items(
    hours_window: int,
    seen_by_feed: Dict[str, Set[str]],
) -> Tuple[List[Dict], bool]:
    """Cached cross-feed Today list, decorated with per-feed seen flags.

    The cache holds the seen-flag-free item list keyed by `hours_window`;
    callers pass a fresh `seen_by_feed` mapping and this function stamps the
    per-item `seen` flag from that mapping. That decoupling means POST
    /api/seen never has to invalidate the Today cache.
    """
    now = time.time()
    cache_hit: Optional[Tuple[List[Dict], bool]] = None
    with _today_items_cache_lock:
        entry = _today_items_cache.get(hours_window)
        if entry and (now - entry[0]) < TODAY_ITEMS_CACHE_TTL_SECONDS:
            cache_hit = (entry[1], entry[2])

    if cache_hit is None:
        items, truncated = collect_today_items_uncached(hours_window)
        with _today_items_cache_lock:
            _today_items_cache[hours_window] = (time.time(), items, truncated)
    else:
        items, truncated = cache_hit

    decorated: List[Dict] = []
    for item in items:
        seen_set = seen_by_feed.get(item["feed_id"], set())
        decorated.append(dict(item, seen=item["filename"] in seen_set))
    return decorated, truncated


def resolve_brief_path(feed_id: str, filename: str) -> Optional[Path]:
    """Resolve a brief filename inside a feed to an on-disk path.

    Returns None if the feed is unknown, the file is missing, the resolved
    path escapes every backing dir for this feed (symlink-escape defense),
    or the filename is structurally impossible (e.g. a segment past NAME_MAX,
    or embedded NUL bytes that Pathlib rejects with ValueError). Caller must
    still range-check against the global read allowlist.
    """
    dirs = feed_directories(feed_id)
    if not dirs:
        return None
    for feed_dir in dirs:
        try:
            candidate = (feed_dir / filename).resolve()
        except (OSError, ValueError):
            # ENAMETOOLONG / embedded NUL / other structurally illegal inputs.
            continue
        try:
            candidate.relative_to(feed_dir)
        except ValueError:
            continue
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except OSError:
            continue
    return None
