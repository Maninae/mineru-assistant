"""Alert formatting and delivery: one Telegram message per run, with a bounded fallback.

Python 3.9 compatible (see config.py).

Delivery reuses the workspace's established path, `scripts/deliver-output.py --raw`
(Keychain token + chat allowlist, also mirrors the line into the assistant's inject
queue). It runs as a bounded subprocess. If it fails, the message is already in the
local log; a macOS notification is attempted only under a deadline, because
osascript itself can hang while tccd is stuck.
"""

import datetime as dt
import socket
from typing import List

from health_watchdog.config import DELIVER_DEADLINE_SECONDS, DELIVER_SCRIPT, NOTIFY_DEADLINE_SECONDS
from health_watchdog.findings import SEVERITY_ICON, Finding
from health_watchdog.subprocess_deadline import CommandOutcome, run_with_deadline

PYTHON_BIN = "/usr/bin/python3"
OSASCRIPT_BIN = "/usr/bin/osascript"
HEADER_ICON = "🩺"
RECOVERED_ICON = "✅"
DETAIL_PREFIX = "   · "
NOTIFICATION_MAX_CHARS = 200
RECOVERED_TITLES = {
    "tcc": "tccd responsiveness",
    "tccd_cpu": "tccd CPU",
    "memory": "memory pressure",
    "load": "load average",
    "disk": "disk space",
    "landline": "Landline silent kills",
    "hung_jobs_survivor": "unkillable hung-job processes (gone now)",
    "orbstack": "OrbStack responsiveness",
    "orbstack_stuck_exit": "OrbStack helper stuck in exit (gone now)",
}
CRASH_NOTE_MAX_CHARS = 300


def short_hostname() -> str:
    """Hostname without the domain suffix."""
    return socket.gethostname().split(".", 1)[0]


def format_alert_message(to_send: List[Finding], recovered: List[str], now: dt.datetime) -> str:
    """Compose the single per-run message: header, one block per finding, recoveries."""
    lines = ["%s health-watchdog on %s, %s" % (HEADER_ICON, short_hostname(), now.strftime("%H:%M"))]
    for finding in to_send:
        lines.append("%s %s %s" % (SEVERITY_ICON[finding.severity], finding.severity.value, finding.title))
        for detail in finding.details:
            lines.append(DETAIL_PREFIX + detail)
    for check_id in recovered:
        lines.append("%s recovered: %s" % (RECOVERED_ICON, RECOVERED_TITLES.get(check_id, check_id)))
    return "\n".join(lines)


def format_crash_message(error_text: str, now: dt.datetime) -> str:
    """Short note sent when the watchdog itself crashed (details are in its log)."""
    last_line = error_text.strip().splitlines()[-1] if error_text.strip() else "unknown error"
    return "%s health-watchdog on %s CRASHED at %s: %s. See logs/health-watchdog/watchdog.log." % (
        HEADER_ICON, short_hostname(), now.strftime("%H:%M"), last_line[:CRASH_NOTE_MAX_CHARS])


def send_telegram(message: str) -> CommandOutcome:
    """Deliver via deliver-output.py --raw under a deadline."""
    return run_with_deadline([PYTHON_BIN, str(DELIVER_SCRIPT), "--raw", message], DELIVER_DEADLINE_SECONDS)


def notify_macos(message: str) -> CommandOutcome:
    """Best-effort local notification, bounded so a stuck osascript cannot hang us."""
    body = message.replace('"', "'")[:NOTIFICATION_MAX_CHARS]
    script = 'display notification "%s" with title "health-watchdog"' % body
    return run_with_deadline([OSASCRIPT_BIN, "-e", script], NOTIFY_DEADLINE_SECONDS)
