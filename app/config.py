"""Central configuration for the Mineru web app.

Owns the allowlist, feed registry, library sources, and network binding.
Every other module reads from here. Instance-specific data (the feed list, the
Pulse job registry) loads from JSON under $MINERU_HOME/config, never code.

Python 3.9-compatible (system /usr/bin/python3 is 3.9.6). Stdlib only.
"""

import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


logger = logging.getLogger(__name__)


# --- Filesystem roots ---------------------------------------------------------

# Workspace root. Every subsystem derives its paths from this seam, so a
# downstream install with a custom location just exports MINERU_HOME. Default
# is the conventional per-user workspace under $HOME.
MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
# Runtime state (push subscriptions, seen ledger, unlock tokens, VAPID public
# key) lives OUTSIDE the code tree: in a deployed install `app/` is a symlink
# into the engine checkout, so writing under it would dirty the engine repo.
STATE_DIR = Path(
    os.environ.get("MINERU_APP_STATE_DIR", str(MINERU_HOME / "app-state"))
)
SEEN_LEDGER_PATH = STATE_DIR / "seen.json"
# Passphrase-gate state (see unlock_gate.py). Both files stay 0600.
UNLOCK_TOKENS_PATH = STATE_DIR / "unlock-tokens.json"
UNLOCK_LOCKOUT_PATH = STATE_DIR / "unlock-lockout.json"

# The only place the server is ever allowed to write.
WRITE_ROOT = STATE_DIR.resolve()


# --- launchd / Pulse job registry ---------------------------------------------

# Prefix every launchd job label shares (e.g. "com.mineru.morning-brief"). The
# `com.mineru.` default is the framework namespace; a downstream install running
# under a different profile overrides it via LAUNCHD_LABEL_PREFIX. The Pulse tab
# uses this both to discover job plists and to strip the prefix for display.
LAUNCHD_LABEL_PREFIX = os.environ.get("LAUNCHD_LABEL_PREFIX", "com.mineru.")

# User-owned registry that maps each launchd job to a display name + a freshness
# signal source (see launchd_jobs.py). Keeps instance-specific job metadata out
# of code: a downstream user drops their own file here. Defaults under
# MINERU_HOME/config; a bundled fallback ships in the app so Pulse works
# out-of-the-box.
LAUNCHD_JOBS_FILE = Path(
    os.environ.get("MINERU_LAUNCHD_JOBS_FILE", str(MINERU_HOME / "config" / "launchd-jobs.json"))
)


# --- Passphrase gate ----------------------------------------------------------

# Cookie name used to carry the unlock bearer token. HttpOnly / Secure /
# SameSite=Strict, minted by POST /api/unlock, validated per-request against
# the on-disk token store. Random 256-bit opaque token (secrets.token_urlsafe).
UNLOCK_COOKIE_NAME = "mineru_unlock"
# 7-day session; deliberately long so household devices stay unlocked between
# uses. Any device the operator wants to drop early is handled by removing the
# token from the store (POST /api/lock, not yet implemented) or letting it expire.
UNLOCK_COOKIE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


# --- Network binding ----------------------------------------------------------

# Loopback ONLY. Tailnet exposure comes via `tailscale serve`, never by binding wider.
HOST = "127.0.0.1"


def _resolve_port() -> int:
    """Return the webapp bind port.

    Reads `MINERU_WEBAPP_PORT` from the environment (default 5195) so a
    second profile on the same machine can bind a different port instead
    of colliding with the framework's 5195 slot. Step-5 audit, Finding 8:
    without this env seam, every profile's `com.<name>.webapp` plist
    launched a server on 5195 and the second one silently failed to bind.
    """
    raw = os.environ.get("MINERU_WEBAPP_PORT", "").strip()
    if not raw:
        return 5195
    try:
        candidate = int(raw)
    except ValueError:
        logger.warning(
            "MINERU_WEBAPP_PORT=%r is not an integer; falling back to 5195",
            raw,
        )
        return 5195
    if not (1 <= candidate <= 65535):
        logger.warning(
            "MINERU_WEBAPP_PORT=%d is out of range; falling back to 5195",
            candidate,
        )
        return 5195
    return candidate


PORT = _resolve_port()

# The dedicated Tailscale-service name this app publishes under (a security
# boundary: it sits in the Host allowlist for the real HTTPS deployment). No
# default — a downstream install MUST set MINERU_TAILNET_HOSTNAME to serve over
# the tailnet. Empty means "not configured"; the app still runs loopback-only
# (server.main logs a loud warning), and the empty value is filtered out of the
# allowlist below so it never widens it.
TAILNET_HOSTNAME = os.environ.get("MINERU_TAILNET_HOSTNAME", "")

# The actual hostname(s) `tailscale serve` publishes this app under, kept in the
# Host allowlist so the DNS-rebinding defense doesn't 421 the real deployment.
# When the app is served on a machine name at a non-443 HTTPS port, both the
# bare name and the name:PORT form are trusted (tailscale may forward the Host
# either way). This stays a CLOSED set of the operator's own tailnet names —
# never a wildcard — so an external rebinding Host (evil.com, etc.) is still
# rejected. Comma-separated via MINERU_SERVE_HOSTNAMES; default is empty.
SERVE_HOSTNAMES = set(filter(None, os.environ.get("MINERU_SERVE_HOSTNAMES", "").split(",")))


def build_allowed_hosts(port: int) -> Set[str]:
    """Host-header allowlist. Anything not in this set is DNS rebinding.

    Loopback lives on the CLI-selected port; the tailnet names are served by
    `tailscale serve` (bare name on 443, or name:PORT on a non-443 port). An
    unset (empty) TAILNET_HOSTNAME is filtered out so it can't add "" to the set.
    """
    hosts = {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
    } | SERVE_HOSTNAMES
    if TAILNET_HOSTNAME:
        hosts.add(TAILNET_HOSTNAME)
    return hosts


def build_allowed_origins(port: int) -> Set[str]:
    """Origin allowlist for state-changing requests (POST /api/seen).

    A same-origin browser POST sends Origin matching where the page loaded from;
    anything else (or a missing scheme match) is a cross-site attempt. An unset
    (empty) TAILNET_HOSTNAME is filtered out so it can't add "https://" to the set.
    """
    origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    } | {f"https://{host}" for host in SERVE_HOSTNAMES}
    if TAILNET_HOSTNAME:
        origins.add(f"https://{TAILNET_HOSTNAME}")
    return origins


# --- Feed registry ------------------------------------------------------------

# Which brief feeds the sidebar shows is instance-specific (one operator runs a
# pet-summary job, another does not), so the list is data, not code. Load order
# mirrors launchd_jobs.py (first hit wins):
#   1. $MINERU_FEEDS_FILE, default $MINERU_HOME/config/feeds.json (operator-owned).
#   2. app/feeds.default.json, the generic engine feeds, bundled next to this module.
# Schema: {"feeds": [{"id", "dirs", "display_name", "emoji", "accent", "group"}]}.
# See engine/config/feeds.example.json.
# - The user file REPLACES the default list; UI order = file order.
# - `dirs` are relative to MINERU_HOME and feed READ_ALLOWLIST, so each must sit
#   under an allowed output tree (briefs_*, reports, creations, inbox, outbox);
#   anything else raises UnsafeFeedDirError at load time, naming the entry.
# - Sidebar groups appear in the order they first occur; a feed with no
#   `group` lands under "Feeds".
FEEDS_FILE = Path(
    os.environ.get("MINERU_FEEDS_FILE", str(MINERU_HOME / "config" / "feeds.json"))
)
BUNDLED_DEFAULT_FEEDS_FILE = APP_DIR / "feeds.default.json"
FEED_DEFAULT_GROUP = "Feeds"


# Feed dirs are served to the tailnet UI, so each must sit inside a known
# output tree. The first path segment must match one of these (fnmatch).
FEED_DIR_ALLOWED_TOP_LEVEL_PATTERNS = (
    "briefs_*",
    "reports",
    "creations",
    "inbox",
    "outbox",
)
# Never servable at any depth, even under an allowed top level.
FEED_DIR_DENIED_SEGMENTS = frozenset(
    {
        "cache",
        "logs",
        "profiles",
        "app-state",
        "config",
        "engine",
    }
)


class UnsafeFeedDirError(ValueError):
    """A feed registry entry names a dir outside the servable output trees."""


def feed_dir_rejection_reason(feed_dir: object) -> Optional[str]:
    """Why `feed_dir` may not be a feed dir, or None when it is safe.

    - Must be a non-empty relative path with no `..` segment.
    - After normalization its first segment must match
      FEED_DIR_ALLOWED_TOP_LEVEL_PATTERNS; `.` and `memory` never qualify.
    - No segment may be dot-prefixed (`.git`, `.env`) or in FEED_DIR_DENIED_SEGMENTS.
    """
    if not isinstance(feed_dir, str) or not feed_dir.strip():
        return "must be a non-empty string"
    candidate = Path(feed_dir)
    if candidate.is_absolute():
        return "must be relative to MINERU_HOME"
    if ".." in candidate.parts:
        return "must not contain `..`"
    normalized_parts = Path(os.path.normpath(feed_dir)).parts
    if not normalized_parts or normalized_parts == (".",):
        return "must name a subdirectory, not the workspace root"
    for segment in normalized_parts:
        if segment.startswith("."):
            return f"segment {segment!r} is dot-prefixed"
        if segment in FEED_DIR_DENIED_SEGMENTS:
            return f"segment {segment!r} is never servable"
    top_level = normalized_parts[0]
    if not any(fnmatch.fnmatchcase(top_level, pattern) for pattern in FEED_DIR_ALLOWED_TOP_LEVEL_PATTERNS):
        allowed = ", ".join(FEED_DIR_ALLOWED_TOP_LEVEL_PATTERNS)
        return f"top-level dir {top_level!r} is not one of: {allowed}"
    return None


def is_safe_feed_dir(feed_dir: object) -> bool:
    """True when `feed_dir` passes feed_dir_rejection_reason."""
    return feed_dir_rejection_reason(feed_dir) is None


def parse_feed_entry(raw_entry: object) -> Optional[Dict[str, Any]]:
    """Validate one feed entry into the registry shape, or None to skip it.

    Required: `id` (str) and `dirs` (non-empty list of servable relative paths).
    Raises UnsafeFeedDirError when any dir fails feed_dir_rejection_reason.
    Optional with defaults: display_name (= id), emoji (""), accent ("muted"),
    group (FEED_DEFAULT_GROUP).
    """
    if not isinstance(raw_entry, dict):
        logger.warning("feed registry entry is not an object: %r; skipping", raw_entry)
        return None
    feed_id = raw_entry.get("id")
    feed_dirs = raw_entry.get("dirs")
    if not isinstance(feed_id, str) or not feed_id:
        logger.warning("feed registry entry missing `id`: %r; skipping", raw_entry)
        return None
    if not isinstance(feed_dirs, list) or not feed_dirs:
        logger.warning("feed %s: `dirs` must be a non-empty list; skipping", feed_id)
        return None
    for feed_dir in feed_dirs:
        rejection_reason = feed_dir_rejection_reason(feed_dir)
        if rejection_reason is not None:
            # Fail loud: a mistyped dir would expose workspace state to the tailnet UI.
            raise UnsafeFeedDirError(
                f"feed {feed_id!r}: dir {feed_dir!r} is not servable ({rejection_reason})"
            )
    return {
        "id": feed_id,
        "dirs": list(feed_dirs),
        "display_name": str(raw_entry.get("display_name") or feed_id),
        "emoji": str(raw_entry.get("emoji") or ""),
        "accent": str(raw_entry.get("accent") or "muted"),
        "group": str(raw_entry.get("group") or FEED_DEFAULT_GROUP),
    }


def load_feed_registry(user_feeds_file: Path, bundled_feeds_file: Path) -> List[Dict[str, Any]]:
    """Return the ordered feed registry from the user file, else the bundled default.

    - A present-but-broken user file (bad JSON, wrong shape) logs a warning and
      falls back to the bundled default, so a typo never blanks the sidebar.
    - Duplicate ids keep the first occurrence.
    """
    for source in (user_feeds_file, bundled_feeds_file):
        if not source.exists():
            continue
        try:
            with open(source, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as read_error:
            logger.warning("feed registry %s unreadable, trying next source: %s", source, read_error)
            continue
        if not isinstance(data, dict) or not isinstance(data.get("feeds"), list):
            logger.warning("feed registry %s has wrong shape (need {'feeds': [...]}); trying next source", source)
            continue
        registry: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        for raw_entry in data["feeds"]:
            entry = parse_feed_entry(raw_entry)
            if entry is None or entry["id"] in seen_ids:
                continue
            seen_ids.add(entry["id"])
            registry.append(entry)
        return registry
    logger.warning("no feed registry found (looked at %s and %s); sidebar will be empty",
                   user_feeds_file, bundled_feeds_file)
    return []


FEED_REGISTRY: List[Dict[str, Any]] = load_feed_registry(FEEDS_FILE, BUNDLED_DEFAULT_FEEDS_FILE)

FEED_BY_ID: Dict[str, Dict[str, Any]] = {feed["id"]: feed for feed in FEED_REGISTRY}


# --- Library sources ----------------------------------------------------------

# The Library tab exposes these two source trees only.
LIBRARY_SOURCES: Dict[str, Path] = {
    "reports": MINERU_HOME / "reports",
    "creations": MINERU_HOME / "creations",
}


# --- Allowlist ----------------------------------------------------------------

def build_read_allowlist() -> List[Path]:
    """Every directory the server is EVER allowed to read a file from.

    Derived from FEED_REGISTRY + LIBRARY_SOURCES regardless of whether the
    directory currently exists. A new feed dir created after startup (e.g. a
    fresh briefs_curiosity/ minted the first time that job runs) will still
    be reachable through the allowlist; without this, the app 404'd every
    brief inside it until the server restarted.

    Path.resolve() works on non-existent paths in 3.6+, so the resolved
    absolute root can still be used as the prefix that request paths must
    live under. Symlink escapes still fail the relative_to check downstream.
    """
    roots: List[Path] = []
    for feed in FEED_REGISTRY:
        for feed_dir in feed["dirs"]:
            roots.append((MINERU_HOME / feed_dir).resolve())
    for src_path in LIBRARY_SOURCES.values():
        roots.append(src_path.resolve())
    return roots


READ_ALLOWLIST: List[Path] = build_read_allowlist()


# --- File-serving extension allowlist -----------------------------------------

# Single source of truth for served extensions and how the client-side reader
# should render them. `library.classify_extension` reads this table; the per-
# kind sets below derive from it, so adding a new extension in one place lights
# up allowlisting, MIME lookup (below), AND the reader kind automatically. The
# .md value is "markdown" (rendered client-side), .txt/.json/.csv/.log are
# "text" (fixed-width viewer), and .html is served only via the sandbox iframe
# route in server.py (never inlined). Values are: "markdown" | "text" | "image"
# | "pdf" | "html".
EXTENSION_KIND: Dict[str, str] = {
    ".md": "markdown",
    ".txt": "text",
    ".json": "text",
    ".csv": "text",
    ".log": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".pdf": "pdf",
    ".html": "html",
}


def _extensions_of_kind(kind: str) -> Set[str]:
    """Return the set of extensions in EXTENSION_KIND that map to `kind`."""
    return {ext for ext, ext_kind in EXTENSION_KIND.items() if ext_kind == kind}


MARKDOWN_EXTENSIONS = _extensions_of_kind("markdown")
# TEXT_EXTENSIONS is every plain-text-ish extension the app knows about,
# including .md — search.py uses it to decide which library files to body-scan,
# and both markdown and text-kind files are body-scannable. The reader kind
# distinction (markdown vs text) lives in classify_extension, not here.
TEXT_EXTENSIONS = MARKDOWN_EXTENSIONS | _extensions_of_kind("text")
IMAGE_EXTENSIONS = _extensions_of_kind("image")
BINARY_EXTENSIONS = _extensions_of_kind("pdf")
HTML_EXTENSIONS = _extensions_of_kind("html")
ALLOWED_EXTENSIONS = set(EXTENSION_KIND.keys())


# Cap on markdown text inlined into /api/library/<source>/<relpath> JSON
# responses. Beyond this, the response omits `markdown` and hands back the raw
# URL instead — a multi-MB .md would otherwise be fully read into RAM and
# JSON-encoded per request (same reasoning as MAX_SERVE_BYTES + MAX_JSON_BODY,
# just applied to the inline-render path). Well above every real report today.
MAX_INLINE_MARKDOWN_BYTES = 512 * 1024


MIME_TYPES: Dict[str, str] = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".html": "text/html; charset=utf-8",
}


# --- Feed listing knobs -------------------------------------------------------

# Newest-first pagination page size for /api/feed/<id>.
FEED_PAGE_SIZE_DEFAULT = 30
FEED_PAGE_SIZE_MAX = 100


# --- Today (cross-feed triage) knobs ------------------------------------------

# Default look-back window for /api/today when the caller omits ?hours=.
# 36h covers "yesterday morning through now" from any time of day, which is
# the daily-triage rhythm the frontend is built around.
TODAY_HOURS_DEFAULT = 36
# Clamp bounds. 1h is the shortest meaningful window; 168h (=7d) is the widest
# — beyond a week the Today lane stops being a "recent" lane and turns into
# a search, which the per-feed views already cover.
TODAY_HOURS_MIN = 1
TODAY_HOURS_MAX = 168
# Hard cap on how many items /api/today returns. Anything above is truncated
# newest-first and the response carries `truncated: true` so the frontend can
# hint at more via the per-feed view instead of scrolling forever.
TODAY_ITEMS_MAX = 60
