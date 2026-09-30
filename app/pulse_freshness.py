"""Freshness classification + workspace signal lookup for Pulse jobs.

For each launchd job, we pick a signal source that reflects the job's REAL
output, not just its stdout log (log rotation freezes log-file mtimes for
weeks even while the job is running fine). The signal source is one of:

  - A directory: the newest file under it is the "last output" mtime.
    (Most briefs jobs work this way — e.g. morning writes to briefs_morning.)
  - A directory with excluded subpaths: same as above, but skip children
    matching a prefix. (memory-description walks memory/ but skips daily/
    and monthly/ which are populated by other jobs.)
  - A single file: use its mtime. (house-scan appends to
    logs/house-scan/house-scan.log on every run, so that file's mtime is
    the last-run signal.)
  - Neutral: job produces no artifact the app can see. Classify as
    "scheduled" so the UI shows a neutral status, not red-failed.

JOB_SIGNAL_TABLE is loaded from the user-owned launchd job registry (see
launchd_jobs.py); everything not in it falls back to the plist's
stdout/stderr log mtime, same as before.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from config import MINERU_HOME
from launchd_jobs import SignalKind, load_freshness_signals


logger = logging.getLogger(__name__)


# Per-label freshness signal specifications, loaded from the user-owned launchd
# job registry (see launchd_jobs.py). Keyed by full launchd label; each value
# is `{"kind": SignalKind, ...}`:
#   NEWEST_IN_DIR — `dir` (relative to MINERU_HOME) whose newest file mtime is
#                   the signal, plus optional `exclude` path-prefixes to skip.
#   FILE_MTIME    — `path` (relative to MINERU_HOME) whose mtime is the signal.
#   NEUTRAL       — job produces no artifact the app can see; classify as
#                   "scheduled" rather than red-failed.
# A label absent from the table falls back to the plist stdout/stderr log mtime.
JOB_SIGNAL_TABLE: Dict[str, Dict] = load_freshness_signals()


def path_starts_with(path: Path, prefix: Path) -> bool:
    """True iff `path` lives at or under `prefix`, matched by path components.

    Prevents the string-startswith over-match where `memory/daily` also
    swallowed `memory/daily-tags`. Uses Path.relative_to, which succeeds
    exactly when `prefix` is a component-wise ancestor of `path`.
    """
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


def newest_mtime_in_dir(dir_path: Path, exclude_prefixes: Optional[List[Path]] = None) -> Optional[float]:
    """Walk dir_path and return the max mtime of any regular file.

    exclude_prefixes: Path prefixes to skip, matched by path components. A
    file is skipped if any exclude is one of its ancestor dirs.
    """
    if not dir_path.exists() or not dir_path.is_dir():
        return None
    excludes = exclude_prefixes or []
    best: Optional[float] = None
    try:
        for path in dir_path.rglob("*"):
            if not path.is_file():
                continue
            if excludes and any(path_starts_with(path, prefix) for prefix in excludes):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best:
                best = mtime
    except OSError as walk_error:
        logger.warning("newest_mtime_in_dir walk failed (errno %s)", walk_error.errno)
    return best


def signal_timestamp(label: str) -> Optional[float]:
    """Newest mtime of the label's real artifact, or None if there's no
    mapping / no artifact yet / the job is neutral.
    """
    spec = JOB_SIGNAL_TABLE.get(label)
    if not spec:
        return None
    kind = spec.get("kind")
    if kind == SignalKind.NEUTRAL:
        return None
    if kind == SignalKind.FILE_MTIME:
        target = MINERU_HOME / spec["path"]
        try:
            return target.stat().st_mtime
        except OSError:
            return None
    if kind == SignalKind.NEWEST_IN_DIR:
        directory = MINERU_HOME / spec["dir"]
        excludes = [MINERU_HOME / p for p in spec.get("exclude", [])]
        return newest_mtime_in_dir(directory, exclude_prefixes=excludes)
    return None


def is_neutral(label: str) -> bool:
    """True for jobs the app can't meaningfully classify (they still run,
    they just don't leave an artifact we can see)."""
    spec = JOB_SIGNAL_TABLE.get(label)
    return bool(spec and spec.get("kind") == SignalKind.NEUTRAL)


def cadence_seconds(plist: Dict) -> Optional[int]:
    """Approximate expected max seconds between runs.

    - Monthly (Day key): 31 days
    - Weekly (Weekday keys): 7 / N days when N weekdays fire
    - Multi-fire per day: 24h / N hours (floor 1h)
    - StartInterval: the interval itself
    - KeepAlive daemon or unknown: None
    """
    if plist.get("KeepAlive"):
        return None
    interval_seconds = plist.get("StartInterval")
    if isinstance(interval_seconds, int) and interval_seconds > 0:
        return interval_seconds

    calendar = plist.get("StartCalendarInterval")
    if calendar is None:
        return None
    entries = calendar if isinstance(calendar, list) else [calendar]
    weekdays = {e.get("Weekday") for e in entries if isinstance(e.get("Weekday"), int)}
    days_of_month = {e.get("Day") for e in entries if isinstance(e.get("Day"), int)}
    hours = [e.get("Hour") for e in entries if isinstance(e.get("Hour"), int)]

    if days_of_month:
        return 31 * 24 * 3600
    if weekdays:
        return max(1, 7 // len(weekdays)) * 24 * 3600
    if hours:
        return max(3600, 24 * 3600 // max(1, len(hours)))
    return 24 * 3600


def classify_status(cadence: Optional[int], last_activity: Optional[float]) -> str:
    """fresh | stale | failed | not-scheduled.

    fresh:   age <= 1.5 x cadence
    stale:   1.5x < age <= 3.0x
    failed:  age > 3.0x, OR last_activity is None (never fired).
    not-scheduled: no cadence at all (KeepAlive daemon).

    Multiplicative thresholds scale correctly across daily/weekly/monthly:
    a Sunday-weekly job (cadence 7d) is fresh through Wednesday of the
    following week (age 10.5d); a daily job (cadence 24h) is fresh through
    36 hours out.
    """
    if cadence is None:
        return "not-scheduled"
    if last_activity is None:
        return "failed"
    age = time.time() - last_activity
    if age <= cadence * 1.5:
        return "fresh"
    if age <= cadence * 3.0:
        return "stale"
    return "failed"
