"""Landline daemon health: how often it had to kill a silent Claude recently.

Python 3.9 compatible (see config.py).

The daemon log line "[YYYY-MM-DD HH:MM:SS] Claude silent for 600s, killing process"
means a CC turn produced nothing for its silence budget. Two or more within the
window is the daemon telling us the machine is wedged, not the model.
"""

import datetime as dt
import re
from pathlib import Path
from typing import Optional

from health_watchdog.config import LANDLINE_DAEMON_LOG, LANDLINE_LOG_TAIL_BYTES, LANDLINE_SILENT_WINDOW_SECONDS

# Both daemon kill lines: "Claude silent for Ns, killing process" (no output at all)
# and "Claude owned-progress silent for Ns (...) — killing process" (heartbeats only).
SILENT_LINE_PATTERN = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] Claude (?:owned-progress )?silent for"
)
LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def read_log_tail(log_path: Path, max_bytes: int = LANDLINE_LOG_TAIL_BYTES) -> str:
    """Last `max_bytes` of the log as text; empty when the file is missing."""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def count_silent_kills(
    log_text: str,
    now: dt.datetime,
    window_seconds: int = LANDLINE_SILENT_WINDOW_SECONDS,
) -> int:
    """Number of silent-kill lines whose local timestamp falls within the window."""
    cutoff = now - dt.timedelta(seconds=window_seconds)
    count = 0
    for line in log_text.splitlines():
        match = SILENT_LINE_PATTERN.match(line)
        if not match:
            continue
        try:
            stamp = dt.datetime.strptime(match.group(1), LOG_TIMESTAMP_FORMAT)
        except ValueError:
            continue
        if stamp >= cutoff:
            count += 1
    return count


def recent_silent_kill_count(now: Optional[dt.datetime] = None, log_path: Path = LANDLINE_DAEMON_LOG) -> int:
    """Read the daemon log tail and count recent silent kills (0 when the log is absent)."""
    return count_silent_kills(read_log_tail(log_path), now or dt.datetime.now())
