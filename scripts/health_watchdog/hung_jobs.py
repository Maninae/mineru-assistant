"""Hung-job selector and killer for the workspace's scheduled launchd jobs.

Python 3.9 compatible (see config.py).

Selection is deliberately conservative: a false kill (an interactive session, the
Telegram daemon, the browser) is far worse than leaving a hung job for the next run.

A process tree is a kill candidate when EITHER
  (a) `launchctl list` shows a non-service `<prefix>.*` label running with that
      root pid (trigger scripts, export/sweep/publish Python jobs), OR
  (b) it is an orphan (ppid 1, no TTY, our user) whose command line names a
      workspace script, an imsg firewall binary, or a headless `claude --print`
      carrying the run_cc_job "recurring/" prompt marker,
AND its root is older than MAX_JOB_AGE (3 h; every plist TimeOut is <= 2 h).

A tree is NEVER a candidate when its root, or any ancestor, is
  - a KeepAlive/allowlisted service pid or matches a service command fragment,
  - attached to a TTY (an interactive terminal or Claude session),
  - the watchdog itself,
  - owned by another user.
"""

import os
import signal
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from health_watchdog.config import (
    CC_BINARY_NAMES,
    CC_JOB_PROMPT_MARKER,
    JOB_COMMAND_MARKERS,
    JOB_MAX_AGE_OVERRIDES_SECONDS,
    KILL_KILL_GRACE_SECONDS,
    KILL_TERM_GRACE_SECONDS,
    LAUNCHD_PREFIX,
    MAX_JOB_AGE_SECONDS,
    NO_TTY,
    SERVICE_COMMAND_ALLOWLIST,
)
from health_watchdog.plist_inventory import LaunchdJob, service_labels
from health_watchdog.process_table import ProcessInfo, ancestors, children_by_ppid, descendants, index_by_pid
from health_watchdog.subprocess_deadline import wait_for_exit

INTERACTIVE_CC_FLAGS = ("--resume", "--continue")
LAUNCHD_PID = 1


@dataclass
class HungJobCandidate:
    """A job tree judged hung: its root plus every descendant pid."""

    root_pid: int
    age_seconds: int
    command: str
    label: Optional[str] = None
    descendant_pids: List[int] = field(default_factory=list)

    @property
    def all_pids(self) -> List[int]:
        """Root first, then descendants (the order kills are delivered)."""
        return [self.root_pid] + list(self.descendant_pids)

    @property
    def name(self) -> str:
        """Label suffix when launchd knows the job, else a short command."""
        if self.label:
            return self.label.split(".", 2)[-1]
        return self.command[:60]


@dataclass
class KillReport:
    """What the killer did. survivors are pids alive after SIGKILL + grace."""

    candidates: List[HungJobCandidate]
    dry_run: bool
    killed: List[int] = field(default_factory=list)
    survivors: List[int] = field(default_factory=list)


def is_service_command(command: str) -> bool:
    """Command-line match against the service allowlist."""
    return any(fragment in command for fragment in SERVICE_COMMAND_ALLOWLIST)


def is_cc_job_command(command: str) -> bool:
    """Headless `claude --print` running a recurring job (never an interactive session)."""
    tokens = command.split()
    if not tokens or tokens[0].rsplit("/", 1)[-1] not in CC_BINARY_NAMES:
        return False
    if "--print" not in tokens or any(flag in tokens for flag in INTERACTIVE_CC_FLAGS):
        return False
    return CC_JOB_PROMPT_MARKER in command


def is_job_command(command: str) -> bool:
    """Command line that belongs to a scheduled job (script path, imsg firewall, CC job)."""
    return any(marker in command for marker in JOB_COMMAND_MARKERS) or is_cc_job_command(command)


def max_age_for(label: Optional[str]) -> int:
    """Per-job age limit (label suffix override) or the default."""
    if label:
        suffix = label.split(".", 2)[-1]
        return JOB_MAX_AGE_OVERRIDES_SECONDS.get(suffix, MAX_JOB_AGE_SECONDS)
    return MAX_JOB_AGE_SECONDS


def protected_pids(
    table: List[ProcessInfo],
    launchctl_pids: Dict[str, Optional[int]],
    jobs: List[LaunchdJob],
    self_pid: int,
    prefix: str = LAUNCHD_PREFIX,
) -> Set[int]:
    """Every pid the killer must never touch: services, TTY sessions, ourselves, and their trees."""
    by_pid = index_by_pid(table)
    children = children_by_ppid(table)
    roots = set()  # type: Set[int]

    protected_labels = service_labels(jobs, prefix)
    for label, pid in launchctl_pids.items():
        if pid is not None and label in protected_labels:
            roots.add(pid)
    for row in table:
        if is_service_command(row.command) or row.tty != NO_TTY:
            roots.add(row.pid)
    roots.add(self_pid)
    roots.update(ancestors(self_pid, by_pid))

    protected = set(roots)
    for root in roots:
        protected.update(descendants(root, children))
    return protected


def select_hung_jobs(
    table: List[ProcessInfo],
    launchctl_pids: Dict[str, Optional[int]],
    jobs: List[LaunchdJob],
    self_pid: int,
    user: str,
    prefix: str = LAUNCHD_PREFIX,
) -> List[HungJobCandidate]:
    """Pure selection: the hung job trees in `table`, oldest first."""
    by_pid = index_by_pid(table)
    children = children_by_ppid(table)
    protected = protected_pids(table, launchctl_pids, jobs, self_pid, prefix)
    service_label_set = service_labels(jobs, prefix)
    candidates = {}  # type: Dict[int, HungJobCandidate]
    judged = set()  # type: Set[int]

    def consider(row: ProcessInfo, label: Optional[str]) -> None:
        judged.add(row.pid)
        if row.pid in protected or row.user != user or row.pid in candidates:
            return
        if row.etime_seconds < max_age_for(label):
            return
        tree = descendants(row.pid, children)
        if any(pid in protected for pid in tree):
            return
        candidates[row.pid] = HungJobCandidate(
            root_pid=row.pid, age_seconds=row.etime_seconds, command=row.command,
            label=label, descendant_pids=tree,
        )

    # (a) launchd-tracked scheduled jobs.
    for label, pid in launchctl_pids.items():
        if pid is None or label in service_label_set or pid not in by_pid:
            continue
        consider(by_pid[pid], label)

    # (b) orphaned job processes re-parented to launchd. Pids already judged under
    # their label keep that verdict (and its per-job age override).
    covered = set(judged)
    for candidate in candidates.values():
        covered.update(candidate.all_pids)
    for row in table:
        if row.pid in covered or row.ppid != LAUNCHD_PID or row.tty != NO_TTY:
            continue
        if not is_job_command(row.command):
            continue
        consider(row, None)

    return sorted(candidates.values(), key=lambda c: c.age_seconds, reverse=True)


def send_signal(pids: List[int], sig: int) -> None:
    """Deliver `sig` to each pid, ignoring ones that already exited."""
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


def kill_job_trees(candidates: List[HungJobCandidate], dry_run: bool) -> KillReport:
    """TERM every pid in every tree, wait, KILL the rest, wait, report survivors."""
    report = KillReport(candidates=candidates, dry_run=dry_run)
    all_pids = [pid for candidate in candidates for pid in candidate.all_pids]
    if dry_run or not all_pids:
        return report

    send_signal(all_pids, signal.SIGTERM)
    remaining = wait_for_exit(all_pids, KILL_TERM_GRACE_SECONDS)
    if remaining:
        send_signal(remaining, signal.SIGKILL)
        remaining = wait_for_exit(remaining, KILL_KILL_GRACE_SECONDS)

    report.survivors = remaining
    report.killed = [pid for pid in all_pids if pid not in remaining]
    return report
