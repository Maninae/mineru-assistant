"""Browser hygiene: tab piles, idle tabs, runaway renderers, orphaned stealth Chromium.

Python 3.9 compatible (see config.py).

The browser server (browser/server.py, port 9471) exposes unauthenticated GET
/health and /tabs; the documented safe stop is `bin/browser action=stop` (profile
and logins persist, the server auto-starts on next use). When the server is down
the tab snapshot at cache/browser-tabs.json is stale, so it is only consulted
while /health answers.
"""

import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from health_watchdog.config import (
    BROWSER_CLI,
    BROWSER_SERVER_COMMAND_MARKER,
    BROWSER_HTTP_DEADLINE_SECONDS,
    BROWSER_PORT,
    BROWSER_PROFILE_DIR,
    BROWSER_STOP_DEADLINE_SECONDS,
    BROWSER_TAB_SUMMARY_LIMIT,
    BROWSER_TABS_SNAPSHOT,
)
from health_watchdog.process_table import ProcessInfo
from health_watchdog.subprocess_deadline import CommandOutcome, run_with_deadline

RENDERER_MARKER = "--type=renderer"
TAB_TITLE_MAX_CHARS = 40


@dataclass
class BrowserStatus:
    """What the watchdog learned about the browser this run."""

    server_running: bool
    tab_count: int = 0
    tab_summaries: List[str] = field(default_factory=list)
    tabs: List[Dict[str, Any]] = field(default_factory=list)
    renderers: List[ProcessInfo] = field(default_factory=list)

    @property
    def hottest_renderer_cpu(self) -> float:
        """Highest %cpu among this profile's renderer processes."""
        return max((row.cpu_percent for row in self.renderers), default=0.0)


def http_get_json(url: str, deadline: float = BROWSER_HTTP_DEADLINE_SECONDS) -> Optional[Dict[str, Any]]:
    """GET a local JSON endpoint; None on any failure or timeout."""
    try:
        with urllib.request.urlopen(url, timeout=deadline) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def load_tab_snapshot(path: Path = BROWSER_TABS_SNAPSHOT) -> List[Dict[str, Any]]:
    """Tabs from the on-disk snapshot; empty when missing or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    tabs = data.get("tabs", []) if isinstance(data, dict) else []
    return [tab for tab in tabs if isinstance(tab, dict)]


def summarize_tabs(tabs: List[Dict[str, Any]], limit: int = BROWSER_TAB_SUMMARY_LIMIT) -> List[str]:
    """"host - title" lines, hosts only (no paths/queries), capped at `limit`."""
    lines = []  # type: List[str]
    for tab in tabs[:limit]:
        host = urlparse(str(tab.get("url", ""))).netloc or str(tab.get("url", ""))[:30]
        title = str(tab.get("title", "")).strip()[:TAB_TITLE_MAX_CHARS]
        lines.append("%s - %s" % (host, title) if title else host)
    if len(tabs) > limit:
        lines.append("... and %d more" % (len(tabs) - limit))
    return lines


def parse_tab_timestamp(text: Any) -> Optional[float]:
    """Epoch seconds from the server's ISO 8601 stamp ("2026-10-13T22:01:05-07:00").

    3.9's fromisoformat handles the "+HH:MM" offset the server writes; a stamp with
    no offset is read as local time. None for anything unparseable.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        return dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def format_age(seconds: float) -> str:
    """Coarse age for alert lines: 45m, 7h, 3d."""
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return "%dd" % (seconds // 86400)
    if seconds >= 3600:
        return "%dh" % (seconds // 3600)
    return "%dm" % (seconds // 60)


def compact_tab_url(url: Any, path_tail_chars: int) -> str:
    """"host…/tail/of/path": host plus the last `path_tail_chars` of the path, no query.

    The end of a path is what tells tabs on one site apart, and dropping the query keeps
    tokens out of Telegram.
    """
    parsed = urlparse(str(url or ""))
    if not parsed.netloc:
        return str(url or "")[:path_tail_chars]
    path = parsed.path if parsed.path != "/" else ""
    if len(path) > path_tail_chars:
        path = "…" + path[-path_tail_chars:]
    return parsed.netloc + path


def profile_processes(table: List[ProcessInfo], profile_dir: Path = BROWSER_PROFILE_DIR) -> List[ProcessInfo]:
    """Every Chromium process (browser, renderers, helpers) running on the stealth profile."""
    marker = str(profile_dir)
    return [row for row in table if marker in row.command]


def renderer_processes(table: List[ProcessInfo], profile_dir: Path = BROWSER_PROFILE_DIR) -> List[ProcessInfo]:
    """Chromium renderer processes belonging to the stealth profile."""
    return [row for row in profile_processes(table, profile_dir) if RENDERER_MARKER in row.command]


def browser_server_processes(table: List[ProcessInfo]) -> List[ProcessInfo]:
    """The browser server itself (browser/server.py), by command line."""
    return [row for row in table if BROWSER_SERVER_COMMAND_MARKER in row.command]


def orphaned_profile_processes(table: List[ProcessInfo]) -> List[ProcessInfo]:
    """Profile Chromium processes left behind with NO browser-server process alive.

    Gated on the process table only: a slow or unreachable /health (plausible under
    incident load) must never make a live server's Chromium look orphaned. With the
    server truly gone nothing will ever close them, so the caller may terminate them.
    """
    if not table or browser_server_processes(table):
        return []
    return profile_processes(table)


def inspect_browser(table: List[ProcessInfo], port: int = BROWSER_PORT) -> BrowserStatus:
    """Probe /health and /tabs (falling back to the snapshot) and find renderers."""
    base = "http://127.0.0.1:%d" % port
    health = http_get_json(base + "/health")
    renderers = renderer_processes(table)
    if health is None:
        return BrowserStatus(server_running=False, renderers=renderers)

    tabs_payload = http_get_json(base + "/tabs")
    tabs = tabs_payload.get("tabs", []) if isinstance(tabs_payload, dict) else None
    if not isinstance(tabs, list):
        tabs = load_tab_snapshot()
    tab_count = len(tabs) if tabs else int(health.get("tabs", 0) or 0)
    return BrowserStatus(
        server_running=True, tab_count=tab_count,
        tab_summaries=summarize_tabs(tabs or []), tabs=list(tabs or []), renderers=renderers,
    )


def stop_browser(dry_run: bool) -> Optional[CommandOutcome]:
    """Documented safe stop via the CLI, bounded; None when dry-running."""
    if dry_run:
        return None
    return run_with_deadline([sys.executable, str(BROWSER_CLI), "action=stop"], BROWSER_STOP_DEADLINE_SECONDS)
