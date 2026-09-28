"""Compose the /api/pulse snapshot: jobs + daemon liveness + heartbeat.

Reads workspace state read-only. All OS-facing details live in
pulse_launchd.py (plist + logs) and pulse_freshness.py (cadence + status);
this module just walks the plists, folds in the freshness verdict, and
returns the response payload.

Snapshots are cached under a short TTL (PULSE_CACHE_TTL_SECONDS) so a burst
of tabs / an autoscroll poll doesn't re-fork launchctl and re-walk memory/
on every hit. The daemon PID is intentionally omitted from the wire output:
downstream consumers only need the `pid_alive` bool, and echoing the numeric
pid is a needless information disclosure.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from config import LAUNCHD_LABEL_PREFIX, MINERU_HOME
from pulse_freshness import (
    cadence_seconds,
    classify_status,
    is_neutral,
    signal_timestamp,
)
from pulse_launchd import (
    cadence_bucket,
    discover_plist_files,
    display_name_for_label,
    humanize_schedule,
    load_plist,
    log_mtime,
)


logger = logging.getLogger(__name__)


HEARTBEAT_PATH = MINERU_HOME / "memory" / "heartbeat-state.json"
# The daemon's launchd label. Derives from the shared prefix so it tracks a
# downstream LAUNCHD_LABEL_PREFIX; MINERU_DAEMON_LABEL overrides it outright.
DAEMON_LABEL = os.environ.get("MINERU_DAEMON_LABEL", f"{LAUNCHD_LABEL_PREFIX}telegram-daemon")

PID_RE = re.compile(r'"PID"\s*=\s*(\d+);')

# Per-signal freshness classification for the heartbeat payload.
# A dead signal is not a stale signal: after HEARTBEAT_OFF_AFTER_DAYS the
# frontend paints the row in the muted channel ("off since Feb 3") instead of
# the warning channel, so a 6-month-dead producer stops reading as an active
# problem it is not.
HEARTBEAT_STALE_AFTER_SECONDS = 2 * 24 * 3600     # > 2 days → stale
HEARTBEAT_OFF_AFTER_SECONDS = 30 * 24 * 3600      # > 30 days → off (dead)
HEARTBEAT_STATUS_FRESH = "fresh"
HEARTBEAT_STATUS_STALE = "stale"
HEARTBEAT_STATUS_OFF = "off"

# Snapshot cache. `build_pulse_snapshot` reads every plist under
# ~/Library/LaunchAgents, forks launchctl, and rglobs multiple memory/
# subtrees; a 5-second TTL is invisible to a human but absorbs the tab-burst
# case where several browsers hit /api/pulse within one refresh window.
PULSE_CACHE_TTL_SECONDS = 5.0
_pulse_cache: Dict[str, object] = {"ts": 0.0, "snapshot": None}
_pulse_cache_lock = threading.Lock()


def build_job_row(plist_path: Path, plist: Dict) -> Dict:
    """One /api/pulse jobs[] entry.

    Freshness comes from the job's REAL artifact (see pulse_freshness.
    JOB_SIGNAL_TABLE), not the plist's stdout/stderr log file — log
    rotation freezes those mtimes for weeks even while the job runs fine.
    Log mtime is retained as a secondary display field only.
    Jobs marked neutral in the table are surfaced as status "scheduled"
    so the UI shows them without a red dot.

    Emits humanized fields the frontend groups by:
      - `display_name`: the friendly title ("Morning Briefing").
      - `short_label`: the label with LAUNCHD_LABEL_PREFIX stripped, so the
        frontend never hardcodes the prefix to derive a fallback name.
      - `schedule_human`: the free-text schedule phrase.
      - `cadence`: the closed-set bucket for section grouping.
    """
    label = plist.get("Label") or plist_path.stem
    short_label = label[len(LAUNCHD_LABEL_PREFIX):] if label.startswith(LAUNCHD_LABEL_PREFIX) else label
    schedule_human = humanize_schedule(plist)
    last_output_ts = signal_timestamp(label)
    last_log_ts = log_mtime(plist)
    if is_neutral(label):
        status = "scheduled"
    else:
        status = classify_status(cadence_seconds(plist), last_output_ts)
    return {
        "label": label,
        "display_name": display_name_for_label(label),
        "short_label": short_label,
        "cadence": cadence_bucket(plist),
        "schedule_human": schedule_human,
        "last_output_ts": last_output_ts,
        "last_log_ts": last_log_ts,
        "status": status,
    }


def daemon_pid_alive() -> Dict:
    """Report the daemon's label and liveness.

    Runs launchctl to find the daemon's PID, then probes it with kill(pid, 0).
    The numeric PID is deliberately NOT included in the wire response — the
    UI only needs the bool, and echoing PIDs is a needless disclosure.
    """
    label = DAEMON_LABEL
    try:
        result = subprocess.run(
            ["/bin/launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as launch_error:
        logger.warning("launchctl list failed: %s", launch_error)
        return {"label": label, "pid_alive": False}

    if result.returncode != 0:
        return {"label": label, "pid_alive": False}

    match = PID_RE.search(result.stdout)
    if not match:
        return {"label": label, "pid_alive": False}
    pid = int(match.group(1))

    # kill(pid, 0) is the standard "does this process exist?" probe on POSIX.
    try:
        os.kill(pid, 0)
        alive = True
    except (ProcessLookupError, PermissionError):
        alive = False
    return {"label": label, "pid_alive": alive}


def read_heartbeat() -> Optional[Dict]:
    """Raw contents of memory/heartbeat-state.json, or None if unreadable."""
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        with open(HEARTBEAT_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as read_error:
        logger.warning("heartbeat read failed: %s", read_error)
        return None


def classify_heartbeat_age(epoch_seconds: float, now: Optional[float] = None) -> str:
    """`fresh` / `stale` / `off` for a single heartbeat entry's age.

    Boundaries: <= 2 days fresh; <= 30 days stale; > 30 days off (dead). The
    "off" bucket is what stops a 6-month-dead producer from lighting up the
    same warning channel as a 3-day-behind producer.
    """
    reference_now = time.time() if now is None else now
    age_seconds = reference_now - float(epoch_seconds)
    if age_seconds <= HEARTBEAT_STALE_AFTER_SECONDS:
        return HEARTBEAT_STATUS_FRESH
    if age_seconds <= HEARTBEAT_OFF_AFTER_SECONDS:
        return HEARTBEAT_STATUS_STALE
    return HEARTBEAT_STATUS_OFF


def build_heartbeat_signals(heartbeat: Optional[Dict]) -> List[Dict]:
    """Flatten the heartbeat dict into `[{key, group?, epoch, status}]` rows.

    Frontend consumes THIS list (not the raw dict) so it can render "off"
    entries in the muted channel while keeping fresh + stale in the warning
    channel. The raw `heartbeat` dict is still emitted on the wire for
    backward compatibility with any consumer already reading it.

    Handles the two shapes that show up on disk today:
      - flat top-level scalars (`{"lastPing": 1770160620}`)
      - one-level-deep nested groups (`{"lastChecks": {"socialPulse": ...}}`)
    Non-numeric leaves are dropped — they carry no timestamp to classify.
    """
    if not isinstance(heartbeat, dict):
        return []
    now = time.time()
    signals: List[Dict] = []
    for top_key, top_value in heartbeat.items():
        if isinstance(top_value, dict):
            for sub_key, sub_value in top_value.items():
                signal_row = coerce_heartbeat_signal(sub_key, sub_value, group=top_key, now=now)
                if signal_row is not None:
                    signals.append(signal_row)
        else:
            signal_row = coerce_heartbeat_signal(top_key, top_value, group=None, now=now)
            if signal_row is not None:
                signals.append(signal_row)
    return signals


def coerce_heartbeat_signal(
    key: str, value: object, group: Optional[str], now: float,
) -> Optional[Dict]:
    """Turn one leaf `(key, value)` into a signal row, or None if not a timestamp.

    Recognizes numeric epochs in seconds (10-digit range) and milliseconds
    (13-digit, down-scaled). Anything outside that window is treated as
    non-timestamp state and skipped — the raw `heartbeat` dict still carries
    those bytes for anyone who wants them.
    """
    if not isinstance(value, (int, float)):
        return None
    numeric_value = float(value)
    if numeric_value > 1e12:
        numeric_value = numeric_value / 1000.0
    if numeric_value < 1e9 or numeric_value >= 1e11:
        return None
    row: Dict = {
        "key": key,
        "epoch": numeric_value,
        "status": classify_heartbeat_age(numeric_value, now=now),
    }
    if group is not None:
        row["group"] = group
    return row


def compute_pulse_snapshot() -> Dict:
    """The uncached snapshot builder. Walks all plists + forks launchctl."""
    plist_paths = discover_plist_files()
    jobs: List[Dict] = []
    for plist_path in plist_paths:
        plist = load_plist(plist_path)
        if plist is None:
            continue
        label = plist.get("Label") or plist_path.stem
        if label == DAEMON_LABEL:
            # Daemon has its own block below with pid liveness.
            continue
        jobs.append(build_job_row(plist_path, plist))
    jobs.sort(key=lambda row: row["label"])

    heartbeat_raw = read_heartbeat()
    return {
        "jobs": jobs,
        "daemon": daemon_pid_alive(),
        "heartbeat": heartbeat_raw,
        "heartbeat_signals": build_heartbeat_signals(heartbeat_raw),
        "generated_ts": time.time(),
    }


def build_pulse_snapshot() -> Dict:
    """Full /api/pulse response, cached for PULSE_CACHE_TTL_SECONDS.

    A cache hit inside the TTL returns the previous snapshot without any
    filesystem or subprocess work. Cache is thread-safe (the http server is
    threaded) and the compute happens outside the lock so a slow launchctl
    on a cold hit doesn't block other pulse readers indefinitely.
    """
    now = time.time()
    with _pulse_cache_lock:
        cached_snapshot = _pulse_cache.get("snapshot")
        cached_ts = _pulse_cache.get("ts", 0.0)
        if cached_snapshot is not None and (now - cached_ts) < PULSE_CACHE_TTL_SECONDS:
            return cached_snapshot

    snapshot = compute_pulse_snapshot()
    with _pulse_cache_lock:
        _pulse_cache["snapshot"] = snapshot
        _pulse_cache["ts"] = time.time()
    return snapshot
