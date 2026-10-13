"""Process table and launchd job table: `ps` and `launchctl list` parsed into data.

Python 3.9 compatible (see config.py).

Both commands read kernel/launchd state, not TCC-protected files, so they stay
responsive during a tccd freeze. Parsing is split from execution so the hung-job
selector and the probes can be tested on captured text.
"""

import getpass
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from health_watchdog.config import LAUNCHD_PREFIX, QUICK_COMMAND_DEADLINE_SECONDS
from health_watchdog.subprocess_deadline import CommandOutcome, run_with_deadline

PS_ARGV = ["/bin/ps", "-axo", "pid=,ppid=,pgid=,tty=,user=,etime=,%cpu=,rss=,command="]
PS_FIXED_FIELD_COUNT = 8
LAUNCHCTL_LIST_ARGV = ["/bin/launchctl", "list"]
KILOBYTES_PER_GIGABYTE = 1024 * 1024
SHORT_COMMAND_MAX_CHARS = 60
# Interpreters whose first argument is the script that actually names the job.
INTERPRETER_BASENAMES = ("python", "python3", "bash", "sh", "zsh", "node", "Python")


@dataclass
class ProcessInfo:
    """One `ps` row. rss_kb is resident memory; etime_seconds is wall-clock age."""

    pid: int
    ppid: int
    pgid: int
    tty: str
    user: str
    etime_seconds: int
    cpu_percent: float
    rss_kb: int
    command: str


def parse_etime(text: str) -> int:
    """Parse ps etime ([[dd-]hh:]mm:ss) into seconds."""
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        days = int(day_part)
    parts = [int(part) for part in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_ps_output(text: str) -> List[ProcessInfo]:
    """Parse header-less `ps -axo pid,ppid,pgid,tty,user,etime,%cpu,rss,command` output."""
    rows = []  # type: List[ProcessInfo]
    for line in text.splitlines():
        fields = line.split(None, PS_FIXED_FIELD_COUNT)
        if len(fields) < PS_FIXED_FIELD_COUNT + 1:
            continue
        try:
            rows.append(ProcessInfo(
                pid=int(fields[0]), ppid=int(fields[1]), pgid=int(fields[2]),
                tty=fields[3], user=fields[4], etime_seconds=parse_etime(fields[5]),
                cpu_percent=float(fields[6]), rss_kb=int(fields[7]), command=fields[8],
            ))
        except ValueError:
            continue
    return rows


def read_process_table() -> List[ProcessInfo]:
    """Run ps under a deadline; an unresponsive ps yields an empty table (callers log it)."""
    outcome = run_with_deadline(PS_ARGV, QUICK_COMMAND_DEADLINE_SECONDS)
    if not outcome.succeeded:
        return []
    return parse_ps_output(outcome.stdout)


def parse_launchctl_list(text: str, prefix: str = LAUNCHD_PREFIX) -> Dict[str, Optional[int]]:
    """Map every `<prefix>.*` label to its running pid (None when not running).

    `launchctl list` rows are "<pid or ->\t<last exit status>\t<label>".
    """
    labels = {}  # type: Dict[str, Optional[int]]
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not fields[2].startswith(prefix + "."):
            continue
        pid_text, _status, label = fields
        labels[label] = int(pid_text) if pid_text.isdigit() else None
    return labels


def read_launchctl_list(prefix: str = LAUNCHD_PREFIX) -> Dict[str, Optional[int]]:
    """Run `launchctl list` under a deadline and parse it; empty on failure."""
    outcome = run_with_deadline(LAUNCHCTL_LIST_ARGV, QUICK_COMMAND_DEADLINE_SECONDS)
    if not outcome.succeeded:
        return {}
    return parse_launchctl_list(outcome.stdout, prefix)


def index_by_pid(table: List[ProcessInfo]) -> Dict[int, ProcessInfo]:
    """pid -> row lookup."""
    return {row.pid: row for row in table}


def children_by_ppid(table: List[ProcessInfo]) -> Dict[int, List[int]]:
    """ppid -> [child pids] lookup."""
    children = {}  # type: Dict[int, List[int]]
    for row in table:
        children.setdefault(row.ppid, []).append(row.pid)
    return children


def ancestors(pid: int, by_pid: Dict[int, ProcessInfo]) -> List[int]:
    """Parent chain from `pid` upward (excluding pid itself), stopping at launchd/pid 0."""
    chain = []  # type: List[int]
    seen = set()  # type: set
    current = by_pid.get(pid)
    while current is not None and current.ppid > 1 and current.ppid not in seen:
        seen.add(current.ppid)
        chain.append(current.ppid)
        current = by_pid.get(current.ppid)
    return chain


def descendants(pid: int, children: Dict[int, List[int]]) -> List[int]:
    """Every pid below `pid` in the process tree (excluding pid itself)."""
    found = []  # type: List[int]
    frontier = list(children.get(pid, []))
    while frontier:
        child = frontier.pop()
        if child in found:
            continue
        found.append(child)
        frontier.extend(children.get(child, []))
    return found


def current_user() -> str:
    """Login name of the account the watchdog runs as."""
    return getpass.getuser()


def short_command(command: str) -> str:
    """Compact, human-readable name for a command line (for alert text)."""
    tokens = command.split()
    if not tokens:
        return ""
    name = tokens[0].rsplit("/", 1)[-1]
    if name in INTERPRETER_BASENAMES and len(tokens) > 1:
        script = next((tok for tok in tokens[1:] if not tok.startswith("-")), tokens[1])
        name = "%s %s" % (name, script.rsplit("/", 1)[-1])
        if tokens[1] == "-m" and len(tokens) > 2:
            name = "%s -m %s" % (tokens[0].rsplit("/", 1)[-1], tokens[2])
    elif len(tokens) > 1 and tokens[1].startswith("--"):
        name = "%s %s" % (name, tokens[1])
    # Chromium-style helpers carry the app name in a path with spaces; keep the .app.
    app_match = re.search(r"/([^/]+\.app)/", command)
    if app_match:
        name = app_match.group(1)[: -len(".app")]
    return name[:SHORT_COMMAND_MAX_CHARS]


def format_top_cpu(table: List[ProcessInfo], count: int) -> List[str]:
    """Top `count` rows by %cpu as "name pid cpu%" strings."""
    rows = sorted(table, key=lambda row: row.cpu_percent, reverse=True)[:count]
    return ["%s (pid %d) %.0f%%" % (short_command(r.command), r.pid, r.cpu_percent) for r in rows]


def format_top_rss(table: List[ProcessInfo], count: int) -> List[str]:
    """Top `count` rows by resident memory as "name pid X.X GB" strings."""
    rows = sorted(table, key=lambda row: row.rss_kb, reverse=True)[:count]
    return [
        "%s (pid %d) %.1f GB" % (short_command(r.command), r.pid, r.rss_kb / KILOBYTES_PER_GIGABYTE)
        for r in rows
    ]


def outcome_summary(outcome: CommandOutcome) -> str:
    """One-line description of a CommandOutcome for logs."""
    if outcome.status == "ok":
        return "exit %s in %.1fs" % (outcome.returncode, outcome.elapsed_seconds)
    if outcome.status == "timeout":
        return "timeout after %.1fs%s" % (
            outcome.elapsed_seconds, " (ABANDONED pid %s)" % outcome.pid if outcome.abandoned else "",
        )
    return "failed to start: %s" % outcome.error
