"""User-owned launchd job registry: display names + freshness signal sources.

The Pulse tab needs two pieces of per-job metadata that are inherently
instance-specific (which jobs a given install runs, what to call them, and
where each job's real output lands): a display-name map and a freshness-signal
table. Baking those into code hardcodes one operator's job set, so they live in
a JSON registry a downstream user owns instead.

Load order (first hit wins):
  1. The user file at `config.LAUNCHD_JOBS_FILE`
     (`$MINERU_HOME/config/launchd-jobs.json` by default, override via
     `MINERU_LAUNCHD_JOBS_FILE`). This is where a real install customizes.
  2. The bundled `launchd-jobs.default.json` next to this module, so the Pulse
     tab works out-of-the-box and the test suite has a populated registry
     without any external file.
  3. Neither present → empty registry + a warning. `display_name_for_label`
     then falls back to the prefix-stripped label suffix.

File schema (see launchd-jobs.default.json for a worked example):
    {
      "jobs": [
        {"label_suffix": "morning-brief",
         "display_name": "Morning Briefing",
         "freshness": {"kind": "newest_in_dir", "dir": "briefs_morning"}},
        ...
      ]
    }
  - `label_suffix`: the part after `config.LAUNCHD_LABEL_PREFIX`. The full
    launchd label is `LAUNCHD_LABEL_PREFIX + label_suffix`.
  - `display_name`: human title for the Pulse card.
  - `freshness` (optional): the job's real-output signal source. `kind` is one
    of SignalKind; `newest_in_dir` needs `dir` (+ optional `exclude` list),
    `file_mtime` needs `path`, `neutral` needs nothing. All dir/path/exclude
    values are relative to MINERU_HOME. A job with no `freshness` block falls
    back to its plist log mtime (the always-on daemons work this way).

Python 3.9-compatible (system /usr/bin/python3 is 3.9.6). Stdlib only.
"""

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config


logger = logging.getLogger(__name__)


# Bundled fallback registry, shipped alongside this module.
BUNDLED_DEFAULT_JOBS_FILE = config.APP_DIR / "launchd-jobs.default.json"


class SignalKind(str, Enum):
    """Closed set of freshness signal-source kinds a job entry may declare.

    Inherits str so `SignalKind.NEUTRAL == "neutral"` stays True — every
    comparison-by-string site (tests, JSON round-trips) keeps working, but an
    unknown kind coming out of the JSON is rejected at the parse boundary
    (logged + freshness dropped for that job) instead of silently misclassifying.
    """
    NEWEST_IN_DIR = "newest_in_dir"
    FILE_MTIME = "file_mtime"
    NEUTRAL = "neutral"


# Parsed registry cache: (display_names, freshness_signals), both keyed by the
# FULL launchd label. Populated lazily on first access; `reload_registry`
# clears it (used by tests that point the loader at a different file).
_REGISTRY_CACHE: Optional[Tuple[Dict[str, str], Dict[str, Dict]]] = None


def registry_source_path() -> Path:
    """The file the registry loads from: the user file if it exists, else the
    bundled default. (The bundled default is returned even when it too is
    missing, so callers can log a single 'no registry' path.)"""
    user_file = config.LAUNCHD_JOBS_FILE
    if user_file.exists():
        return user_file
    return BUNDLED_DEFAULT_JOBS_FILE


def read_jobs_list() -> List[Dict]:
    """Read and JSON-parse the `jobs` list from the active registry file.

    Missing file → warning + empty list (a fresh install with no jobs yet).
    Malformed JSON or wrong top-level shape → warning + empty list (fail soft:
    a bad metadata file must not take down the read-only web app).
    """
    source = registry_source_path()
    if not source.exists():
        logger.warning(
            "no launchd job registry found (looked at %s and the bundled default); "
            "Pulse will show prefix-stripped labels", config.LAUNCHD_JOBS_FILE,
        )
        return []
    try:
        with open(source, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as read_error:
        logger.warning("launchd job registry %s unreadable, treating as empty: %s", source, read_error)
        return []
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        logger.warning("launchd job registry %s has wrong shape (need {'jobs': [...]}); ignoring", source)
        return []
    return data["jobs"]


def parse_freshness(label: str, freshness: Dict) -> Optional[Dict]:
    """Validate one job's freshness block into a signal spec, or None to skip it.

    Returns a spec dict shaped exactly like the old in-code JOB_SIGNAL_TABLE
    entries — `{"kind": SignalKind, ...}` with `dir`(+`exclude`) / `path` — so
    `pulse_freshness.signal_timestamp` consumes it unchanged. Any structural
    problem (unknown kind, missing dir/path) logs a warning and drops freshness
    for that job rather than crashing the whole registry load.
    """
    kind_raw = freshness.get("kind")
    try:
        kind = SignalKind(kind_raw)
    except ValueError:
        logger.warning("job %s: unknown freshness kind %r; ignoring its freshness", label, kind_raw)
        return None
    if kind == SignalKind.NEUTRAL:
        return {"kind": kind}
    if kind == SignalKind.NEWEST_IN_DIR:
        directory = freshness.get("dir")
        if not directory:
            logger.warning("job %s: newest_in_dir freshness missing `dir`; ignoring", label)
            return None
        spec: Dict = {"kind": kind, "dir": directory}
        exclude = freshness.get("exclude")
        if isinstance(exclude, list) and exclude:
            spec["exclude"] = exclude
        return spec
    if kind == SignalKind.FILE_MTIME:
        path = freshness.get("path")
        if not path:
            logger.warning("job %s: file_mtime freshness missing `path`; ignoring", label)
            return None
        return {"kind": kind, "path": path}
    return None


def load_registry(force_reload: bool = False) -> Tuple[Dict[str, str], Dict[str, Dict]]:
    """Return `(display_names, freshness_signals)`, both keyed by full label.

    Cached after the first call. Full labels are built as
    `config.LAUNCHD_LABEL_PREFIX + label_suffix`, so a downstream install under
    a different prefix maps to its own labels automatically.
    """
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None and not force_reload:
        return _REGISTRY_CACHE

    prefix = config.LAUNCHD_LABEL_PREFIX
    display_names: Dict[str, str] = {}
    freshness_signals: Dict[str, Dict] = {}
    for job in read_jobs_list():
        if not isinstance(job, dict):
            continue
        suffix = job.get("label_suffix")
        display_name = job.get("display_name")
        if not suffix or not display_name:
            logger.warning("launchd job registry entry missing label_suffix/display_name: %r; skipping", job)
            continue
        label = prefix + suffix
        display_names[label] = display_name
        freshness = job.get("freshness")
        if isinstance(freshness, dict):
            spec = parse_freshness(label, freshness)
            if spec is not None:
                freshness_signals[label] = spec

    _REGISTRY_CACHE = (display_names, freshness_signals)
    return _REGISTRY_CACHE


def load_display_names() -> Dict[str, str]:
    """`{full_label: display_name}` for every registered job."""
    return load_registry()[0]


def load_freshness_signals() -> Dict[str, Dict]:
    """`{full_label: freshness_spec}` for every job that declares one."""
    return load_registry()[1]


def reload_registry() -> Tuple[Dict[str, str], Dict[str, Dict]]:
    """Drop the cache and re-read from disk. For tests that swap the file."""
    return load_registry(force_reload=True)
