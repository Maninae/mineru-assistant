"""launchd plist discovery + parsing + schedule-string humanizer.

Isolates every OS-facing read for the Pulse tab so pulse.py can compose the
final snapshot without knowing about plist internals.

Exports three humanizers that pulse.py drops onto every job row:
  - `humanize_schedule` — the existing free-text phrase (e.g. "Sunday at 21:00").
  - `display_name_for_label` — human title for the plist label
    (`com.mineru.morning-brief` → "Morning Briefing"). Falls back to the
    stripped basename for anything unmapped so a new job never renders as an
    empty string.
  - `cadence_bucket` — closed-set cadence tag (`daily | weekly | monthly |
    interval | always_on | unknown`) so the frontend can group jobs into
    Daily / Weekly / Monthly / Always-on sections.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import logging
import plistlib
from pathlib import Path
from typing import Dict, List, Optional

from config import LAUNCHD_LABEL_PREFIX
from launchd_jobs import load_display_names


logger = logging.getLogger(__name__)


LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# Weekday mapping used to humanize StartCalendarInterval Weekday.
WEEKDAY_NAMES: Dict[int, str] = {
    0: "Sunday",
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
    7: "Sunday",  # launchd accepts both 0 and 7 for Sunday
}

# Humanized names for each installed job plist, loaded from the user-owned
# launchd job registry (see launchd_jobs.py) and keyed by full launchd label,
# so the Pulse grid shows titles instead of kebab-case slugs. Fall-through for
# an unmapped label is the prefix-stripped suffix (see display_name_for_label),
# so a newly added plist still renders as something readable.
PULSE_JOB_DISPLAY_NAMES: Dict[str, str] = load_display_names()

# Closed set of cadence buckets. String enum via bare constants so the API
# contract is a plain string on the wire — the frontend groups Daily / Weekly /
# Monthly / Always-on sections by value equality.
CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"
CADENCE_MONTHLY = "monthly"
CADENCE_INTERVAL = "interval"
CADENCE_ALWAYS_ON = "always_on"
CADENCE_UNKNOWN = "unknown"


def discover_plist_files() -> List[Path]:
    """Every launchd agent installed under LAUNCHD_LABEL_PREFIX for the user."""
    if not LAUNCH_AGENTS_DIR.exists():
        return []
    return sorted(LAUNCH_AGENTS_DIR.glob(f"{LAUNCHD_LABEL_PREFIX}*.plist"))


def load_plist(plist_path: Path) -> Optional[Dict]:
    """Read one plist; None on parse failure (logged, not raised)."""
    try:
        with open(plist_path, "rb") as fh:
            return plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError) as parse_error:
        logger.warning("plist parse failed for %s: %s", plist_path, parse_error)
        return None


def humanize_schedule(plist: Dict) -> str:
    """Turn StartCalendarInterval / StartInterval into a human phrase.

    Handles the shapes actually used here: single-dict calendar entry, list of
    dict entries (multi-fire), StartInterval (seconds), and KeepAlive daemons.
    """
    if plist.get("KeepAlive"):
        return "always-on (daemon)"

    calendar = plist.get("StartCalendarInterval")
    if calendar is not None:
        entries = calendar if isinstance(calendar, list) else [calendar]
        return humanize_calendar_entries(entries)

    interval_seconds = plist.get("StartInterval")
    if isinstance(interval_seconds, int) and interval_seconds > 0:
        return humanize_interval_seconds(interval_seconds)

    return "not scheduled"


def humanize_calendar_entries(entries: List[Dict]) -> str:
    """Compact phrase for one or more StartCalendarInterval dicts."""
    times: List[str] = []
    weekdays: List[str] = []
    day_of_month: Optional[int] = None
    for entry in entries:
        hour = entry.get("Hour")
        minute = entry.get("Minute", 0)
        weekday = entry.get("Weekday")
        day = entry.get("Day")
        if isinstance(hour, int):
            times.append(f"{hour:02d}:{minute:02d}")
        if isinstance(weekday, int):
            weekdays.append(WEEKDAY_NAMES.get(weekday, str(weekday)))
        if isinstance(day, int):
            day_of_month = day

    times = sorted(set(times))
    weekdays = sorted(set(weekdays))

    parts: List[str] = []
    if weekdays:
        parts.append("/".join(weekdays))
    elif day_of_month is not None:
        parts.append(f"day {day_of_month} of month")
    else:
        parts.append("daily")

    if times:
        parts.append("at " + " · ".join(times))

    return " ".join(parts)


def humanize_interval_seconds(seconds: int) -> str:
    """Turn a repeating interval into "every N min/hour"."""
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"every {hours}h" if hours != 1 else "hourly"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"every {minutes} min"
    return f"every {seconds}s"


def find_log_path(plist: Dict) -> Optional[Path]:
    """StandardOutPath or StandardErrorPath, whichever the plist declared."""
    for key in ("StandardOutPath", "StandardErrorPath"):
        path_str = plist.get(key)
        if path_str:
            return Path(path_str)
    return None


def log_mtime(plist: Dict) -> Optional[float]:
    """Newest of the plist's declared stdout/stderr log paths."""
    best: Optional[float] = None
    for key in ("StandardOutPath", "StandardErrorPath"):
        path_str = plist.get(key)
        if not path_str:
            continue
        path = Path(path_str)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best:
            best = mtime
    return best


def display_name_for_label(label: str) -> str:
    """Human title for a Pulse job row.

    First tries the registry `PULSE_JOB_DISPLAY_NAMES` map; failing that, falls
    back to the plist label with the `LAUNCHD_LABEL_PREFIX` stripped. The
    fall-through means a newly installed job plist still renders something
    readable before its friendly name lands in the registry.
    """
    if label in PULSE_JOB_DISPLAY_NAMES:
        return PULSE_JOB_DISPLAY_NAMES[label]
    if label.startswith(LAUNCHD_LABEL_PREFIX):
        return label[len(LAUNCHD_LABEL_PREFIX):]
    return label


def cadence_bucket(plist: Dict) -> str:
    """Which cadence section a job belongs in on the Pulse grid.

    - `always_on`: KeepAlive daemons (webapp, telegram-daemon).
    - `interval`: `StartInterval` in seconds — think daemon-watchdog fanning
      every 5 minutes; not calendar-aligned so it deserves its own bucket.
    - `monthly`: any StartCalendarInterval entry with a `Day` key
      (day-of-month firing, e.g. cleanup-retention on day 2).
    - `weekly`: `Weekday` present without `Day` — Sunday / Mon-Wed-Fri jobs.
    - `daily`:   `Hour` (± `Minute`) present, no `Weekday`, no `Day`.
    - `unknown`: none of the above — plist parses but no schedule shape we
      recognize (rare; treated as an ungrouped miscellaneous row).
    """
    if plist.get("KeepAlive"):
        return CADENCE_ALWAYS_ON
    interval_seconds = plist.get("StartInterval")
    if isinstance(interval_seconds, int) and interval_seconds > 0:
        return CADENCE_INTERVAL

    calendar = plist.get("StartCalendarInterval")
    if calendar is None:
        return CADENCE_UNKNOWN
    entries = calendar if isinstance(calendar, list) else [calendar]

    has_day = any(isinstance(entry.get("Day"), int) for entry in entries)
    if has_day:
        return CADENCE_MONTHLY
    has_weekday = any(isinstance(entry.get("Weekday"), int) for entry in entries)
    if has_weekday:
        return CADENCE_WEEKLY
    has_hour = any(isinstance(entry.get("Hour"), int) for entry in entries)
    if has_hour:
        return CADENCE_DAILY
    return CADENCE_UNKNOWN
