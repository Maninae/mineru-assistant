"""Probe for the missed-run check: when did each scheduled launchd job last show life?

Python 3.9 compatible (see config.py). Reads plists, the Pulse job registry, pause
files, and file mtimes; its only command is one bounded `launchctl list`.

A job is in scope when its plist (LaunchAgents, then any extra plist dirs) has a
watched label prefix, a schedule (`job_schedule.expected_cadence_seconds`), and its
label is LOADED in launchd. That last rule is what keeps retired jobs quiet: turning
a job off means booting it out and removing its plist, so it is simply not listed.

Skipped as well: KeepAlive services, labels in the skip list, registry/override
`neutral` jobs (no visible artifact), and jobs paused through `bin/pause-job`.

The newest sign of life is the max of:
  - the registry/override freshness signal (newest file in a dir, or one file's mtime),
  - the newest file under `logs/<suffix>/` (cc-job-lib writes there every run),
  - the plist's StandardOutPath / StandardErrorPath mtimes,
  - floors that mean "do not judge before this": the plist's own mtime (just
    installed or re-enabled) and the day after an expired pause ended.
Signals are read cheapest first and stop as soon as one is recent enough, so the
directory walks only happen for jobs that are actually behind.
"""

import datetime as dt
import json
import logging
import os
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from health_watchdog.config import (
    JOB_LOGS_DIR,
    LAUNCH_AGENTS_DIR,
    LAUNCHD_JOBS_REGISTRY_FILE,
    LAUNCHD_PREFIX,
    MINERU_HOME,
    MISSED_RUN_EXTRA_PLIST_DIRS,
    MISSED_RUN_FRESHNESS_OVERRIDES,
    MISSED_RUN_GRACE_FACTOR,
    MISSED_RUN_LABEL_PREFIXES,
    MISSED_RUN_MIN_GRACE_SECONDS,
    MISSED_RUN_SKIP_LABELS,
    PAUSED_JOBS_DIR,
    QUICK_COMMAND_DEADLINE_SECONDS,
)
from health_watchdog.job_schedule import expected_cadence_seconds
from health_watchdog.process_table import LAUNCHCTL_LIST_ARGV
from health_watchdog.subprocess_deadline import run_with_deadline

logger = logging.getLogger("health_watchdog")

KIND_NEWEST_IN_DIR = "newest_in_dir"
KIND_FILE_MTIME = "file_mtime"
KIND_NEUTRAL = "neutral"


@dataclass
class ScheduledJobActivity:
    """One in-scope job: its cadence, its allowed silence, and its newest sign of life."""

    label: str
    display_name: str
    cadence_seconds: int
    allowed_silence_seconds: int
    last_activity_ts: Optional[float]
    last_activity_source: str


def allowed_silence_seconds(cadence_seconds: int) -> int:
    """Cadence plus grace: max(cadence x factor, cadence + minimum grace)."""
    return int(max(cadence_seconds * MISSED_RUN_GRACE_FACTOR, cadence_seconds + MISSED_RUN_MIN_GRACE_SECONDS))


def parse_loaded_labels(launchctl_text: str, prefixes: Iterable[str]) -> Set[str]:
    """Labels in `launchctl list` output ("<pid>\\t<status>\\t<label>") under any prefix."""
    wanted = tuple(prefix + "." for prefix in prefixes)
    loaded = set()  # type: Set[str]
    for line in launchctl_text.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[2].startswith(wanted):
            loaded.add(fields[2])
    return loaded


def read_loaded_labels(prefixes: Iterable[str]) -> Optional[Set[str]]:
    """Loaded labels, or None when launchctl fails (the check then skips: no false alarms)."""
    outcome = run_with_deadline(LAUNCHCTL_LIST_ARGV, QUICK_COMMAND_DEADLINE_SECONDS)
    if not outcome.succeeded:
        return None
    return parse_loaded_labels(outcome.stdout, prefixes)


def load_plists(directories: Iterable[Path], prefixes: Iterable[str]) -> Dict[str, Tuple[Path, Dict[str, Any]]]:
    """label -> (plist path, parsed dict); the first directory to define a label wins."""
    plists = {}  # type: Dict[str, Tuple[Path, Dict[str, Any]]]
    for directory in directories:
        for prefix in prefixes:
            for plist_path in sorted(directory.glob(prefix + ".*.plist")):
                try:
                    with open(plist_path, "rb") as handle:
                        parsed = plistlib.load(handle)
                except (OSError, plistlib.InvalidFileException, ValueError):
                    continue
                label = str(parsed.get("Label", ""))
                if label and label not in plists:
                    plists[label] = (plist_path, parsed)
    return plists


def load_registry(registry_path: Path, workspace_prefix: str) -> Dict[str, Dict[str, Any]]:
    """Full label -> registry entry from the Pulse job registry ({} when absent or malformed)."""
    try:
        with open(registry_path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    entries = loaded.get("jobs", []) if isinstance(loaded, dict) else []
    registry = {}  # type: Dict[str, Dict[str, Any]]
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("label_suffix"):
            registry["%s.%s" % (workspace_prefix, entry["label_suffix"])] = entry
    return registry


def label_suffix(label: str, prefixes: Iterable[str]) -> str:
    """The label with its watched prefix removed ("com.x.morning-brief" -> "morning-brief")."""
    for prefix in sorted(prefixes, key=len, reverse=True):
        if label.startswith(prefix + "."):
            return label[len(prefix) + 1:]
    return label


def resolve_path(text: str, home: Path) -> Path:
    """Absolute, "~/"-relative, or workspace-relative path."""
    expanded = os.path.expanduser(text)
    return Path(expanded) if os.path.isabs(expanded) else home / expanded


def file_mtime(path: Path) -> Optional[float]:
    """mtime, or None when the file is missing."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def is_under(path: Path, ancestors: List[Path]) -> bool:
    """True when `path` sits at or below any of `ancestors` (component-wise)."""
    for ancestor in ancestors:
        try:
            path.relative_to(ancestor)
            return True
        except ValueError:
            continue
    return False


def newest_file_mtime(directory: Path, excludes: Optional[List[Path]] = None) -> Optional[float]:
    """Newest regular-file mtime under `directory` (recursive), skipping excluded subtrees."""
    if not directory.is_dir():
        return None
    newest = None  # type: Optional[float]
    try:
        for path in directory.rglob("*"):
            if excludes and is_under(path, excludes):
                continue
            if not path.is_file():
                continue
            mtime = file_mtime(path)
            if mtime is not None and (newest is None or mtime > newest):
                newest = mtime
    except OSError:
        pass
    return newest


def freshness_signal(spec: Dict[str, Any], home: Path) -> Optional[float]:
    """Timestamp from a registry-shaped spec (newest_in_dir / file_mtime); None otherwise."""
    kind = spec.get("kind")
    if kind == KIND_FILE_MTIME and spec.get("path"):
        return file_mtime(resolve_path(str(spec["path"]), home))
    if kind == KIND_NEWEST_IN_DIR and spec.get("dir"):
        excludes = [resolve_path(str(item), home) for item in list(spec.get("exclude", []) or [])]
        return newest_file_mtime(resolve_path(str(spec["dir"]), home), excludes)
    return None


def pause_status(job_name: str, paused_dir: Path, today: dt.date) -> Tuple[bool, Optional[float]]:
    """(paused now, end-of-pause timestamp for an expired pause) from `<job>.until`."""
    try:
        text = (paused_dir / (job_name + ".until")).read_text(encoding="utf-8").strip()
        until = dt.date.fromisoformat(text)
    except (OSError, ValueError):
        return False, None
    if until >= today:
        return True, None
    resumed_at = dt.datetime.combine(until + dt.timedelta(days=1), dt.time())
    return False, resumed_at.timestamp()


def newest_activity(
    candidates: List[Tuple[str, Callable[[], Optional[float]]]], fresh_after_ts: float
) -> Tuple[Optional[float], str]:
    """Evaluate signals in order, stopping at the first one newer than `fresh_after_ts`."""
    newest_ts, newest_source = None, "no artifact found"  # type: Tuple[Optional[float], str]
    for source, read_signal in candidates:
        signal_ts = read_signal()
        if signal_ts is None:
            continue
        if newest_ts is None or signal_ts > newest_ts:
            newest_ts, newest_source = signal_ts, source
        if signal_ts > fresh_after_ts:
            break
    return newest_ts, newest_source


def collect_job_activity(now: dt.datetime) -> List[ScheduledJobActivity]:
    """Every in-scope scheduled job with its newest sign of life; [] when launchctl fails."""
    loaded = read_loaded_labels(MISSED_RUN_LABEL_PREFIXES)
    if loaded is None:
        logger.warning("missed runs: launchctl list failed; check skipped this run")
        return []
    plists = load_plists((LAUNCH_AGENTS_DIR,) + MISSED_RUN_EXTRA_PLIST_DIRS, MISSED_RUN_LABEL_PREFIXES)
    registry = load_registry(LAUNCHD_JOBS_REGISTRY_FILE, LAUNCHD_PREFIX)
    return build_job_activity(plists, loaded, registry, MISSED_RUN_FRESHNESS_OVERRIDES, now)


def build_job_activity(
    plists: Dict[str, Tuple[Path, Dict[str, Any]]],
    loaded: Set[str],
    registry: Dict[str, Dict[str, Any]],
    overrides: Dict[str, Dict[str, Any]],
    now: dt.datetime,
    home: Path = MINERU_HOME,
    logs_dir: Path = JOB_LOGS_DIR,
    paused_dir: Path = PAUSED_JOBS_DIR,
    prefixes: Tuple[str, ...] = MISSED_RUN_LABEL_PREFIXES,
    skip_labels: FrozenSet[str] = MISSED_RUN_SKIP_LABELS,
) -> List[ScheduledJobActivity]:
    """Apply the scope rules and read each job's signals (paths injectable for tests)."""
    rows = []  # type: List[ScheduledJobActivity]
    for label in sorted(plists):
        plist_path, plist = plists[label]
        cadence = expected_cadence_seconds(plist)
        if cadence is None or label in skip_labels or label not in loaded:
            continue
        entry = registry.get(label, {})
        spec = overrides.get(label) or entry.get("freshness") or {}
        if spec.get("kind") == KIND_NEUTRAL:
            continue
        suffix = label_suffix(label, prefixes)
        paused, pause_ended_ts = pause_status(suffix, paused_dir, now.date())
        if paused:
            continue
        silence = allowed_silence_seconds(cadence)
        fresh_after_ts = now.timestamp() - silence
        candidates = [
            ("plist installed", lambda path=plist_path: file_mtime(path)),
            ("pause ended", lambda ts=pause_ended_ts: ts),
        ]  # type: List[Tuple[str, Callable[[], Optional[float]]]]
        for key in ("StandardOutPath", "StandardErrorPath"):
            if plist.get(key):
                log_path = Path(str(plist[key]))
                candidates.append((log_path.name, lambda path=log_path: file_mtime(path)))
        candidates.append(("logs/%s/" % suffix, lambda directory=logs_dir / suffix: newest_file_mtime(directory)))
        if spec:
            source = str(spec.get("dir") or spec.get("path") or spec.get("kind"))
            candidates.append((source, lambda job_spec=spec: freshness_signal(job_spec, home)))
        last_ts, last_source = newest_activity(candidates, fresh_after_ts)
        rows.append(ScheduledJobActivity(
            label=label,
            display_name=str(entry.get("display_name") or suffix),
            cadence_seconds=cadence,
            allowed_silence_seconds=silence,
            last_activity_ts=last_ts,
            last_activity_source=last_source,
        ))
    return rows
