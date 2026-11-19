"""Page-open probe scheduling and verdict (pure; no commands run here).

Python 3.9 compatible (see config.py).

State lives under `state["browser_open_probe"]` = {"ts", "ok", "detail"} so a
failure found on a probe run keeps alerting (under cooldown) on the runs between
probes instead of looking "recovered". The consecutive-failure count is the
`browser_open` streak, bumped only on runs that actually probed.

- Server down: nothing to probe; the record and streak are cleared (the next use
  starts a fresh server through bin/browser's Python picker).
- Tabs open: a session is using the browser, so opens demonstrably work; the
  probe is skipped and a stale failure is cleared.
"""

from typing import Any, Dict, List, Optional

from health_watchdog.browser_hygiene import BrowserStatus, PageOpenProbe
from health_watchdog.config import BROWSER_OPEN_PROBE_INTERVAL_SECONDS, BROWSER_OPEN_PROBE_SUSTAINED_RUNS
from health_watchdog.findings import Finding, Severity
from health_watchdog.state import bump_streak

CHECK_BROWSER_OPEN = "browser_open"
STATE_KEY = "browser_open_probe"
FIX_TEXT = ("Fix: `bin/browser action=stop`, then any open restarts it; if it still fails, "
            "set MINERU_BROWSER_PYTHON to a Python with cloakbrowser + playwright_stealth.")


def clear_record(state: Dict[str, Any]) -> None:
    """Forget the last probe and reset the failure streak."""
    state[STATE_KEY] = None
    bump_streak(state, CHECK_BROWSER_OPEN, False)


def probe_is_due(status: BrowserStatus, state: Dict[str, Any], now_ts: float) -> bool:
    """True when this run should open a probe tab; clears the record when it cannot apply."""
    if not status.server_running or status.tab_count > 0:
        clear_record(state)
        return False
    record = state.get(STATE_KEY)
    if not isinstance(record, dict) or not record.get("ok"):
        return True  # never probed, or failing: re-probe every run until it passes
    return now_ts - float(record.get("ts", 0)) >= BROWSER_OPEN_PROBE_INTERVAL_SECONDS


def record_probe(state: Dict[str, Any], probe: PageOpenProbe, now_ts: float) -> None:
    """Store a fresh probe result and bump the failure streak."""
    state[STATE_KEY] = {"ts": now_ts, "ok": probe.ok, "detail": probe.detail}
    bump_streak(state, CHECK_BROWSER_OPEN, not probe.ok)


def evaluate_page_open(state: Dict[str, Any], server_python: Optional[str]) -> Optional[Finding]:
    """CRITICAL once the stored probe has failed BROWSER_OPEN_PROBE_SUSTAINED_RUNS times in a row."""
    record = state.get(STATE_KEY)
    if not isinstance(record, dict) or record.get("ok"):
        return None
    streak = int(state.get("streaks", {}).get(CHECK_BROWSER_OPEN, 0))
    if streak < BROWSER_OPEN_PROBE_SUSTAINED_RUNS:
        return None
    details = [str(record.get("detail", ""))]  # type: List[str]
    if server_python:
        details.append("Server Python: " + server_python)
    details.append(FIX_TEXT)
    return Finding(CHECK_BROWSER_OPEN, Severity.CRITICAL,
                   "Stealth browser cannot open pages (/health still answers).", details)
