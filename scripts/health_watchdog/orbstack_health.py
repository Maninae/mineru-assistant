"""OrbStack health: helper-process stats, a bounded liveness probe, and the restart action.

Python 3.9 compatible (see config.py).

A wedged OrbStack looks like this: the VM's memory was swapped out by the host, its
`OrbStack Helper` spins at 300-400% CPU doing no real work, and every `docker` call
hangs. `orb stop` + `orb start` clears it. A helper stuck in exit state (ps STAT "E",
rss 0) ignores SIGKILL and only a reboot clears it, so nothing here acts on that case.

- Probe order is load-bearing: any `docker` call auto-launches a stopped OrbStack, so
  docker is only invoked after `orb status` reports Running. Stopped is a deliberate
  state (or the app simply was not started at login), never a finding.
- Every command goes through run_with_deadline; `runner` is injectable for tests.
- No orb binary and no OrbStack.app -> `not_installed`, and the caller skips the check.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from health_watchdog.config import (
    DOCKER_BIN_CANDIDATES,
    ORB_BIN_CANDIDATES,
    ORBSTACK_APP_PATH,
    ORBSTACK_DOCKER_PROBE_DEADLINE_S,
    ORBSTACK_HELPER_COMMAND_MARKER,
    ORBSTACK_RESTART_DEADLINE_S,
    ORBSTACK_STATUS_DEADLINE_S,
    QUICK_COMMAND_DEADLINE_SECONDS,
)
from health_watchdog.subprocess_deadline import STATUS_OK, STATUS_TIMEOUT, CommandOutcome, run_with_deadline

PROBE_OK = "ok"
PROBE_HUNG = "hung"
PROBE_NOT_RUNNING = "not_running"
PROBE_NOT_INSTALLED = "not_installed"
# orb answered but said something other than Running/Stopped, or docker failed fast:
# not evidence of a wedge, so no finding (logged by the caller).
PROBE_UNKNOWN = "unknown"

ORB_STATUS_RUNNING = "running"
ORB_STATUS_STOPPED = "stopped"
STUCK_EXIT_STAT_FLAG = "E"
DOCKER_VERSION_ARGS = ["version", "--format", "{{.Server.Version}}"]
HELPER_PS_ARGV = ["/bin/ps", "-axo", "pid=,stat=,%cpu=,rss=,command="]
HELPER_PS_FIXED_FIELD_COUNT = 4

Runner = Callable[[List[str], float], CommandOutcome]


@dataclass
class OrbstackHelperRow:
    """One `OrbStack Helper` process: pid, ps STAT flags, %cpu, resident KB."""

    pid: int
    stat: str
    cpu_percent: float
    rss_kb: int


@dataclass
class OrbstackHealth:
    """Everything the evaluator needs about OrbStack this run."""

    liveness: str
    helpers: List[OrbstackHelperRow] = field(default_factory=list)
    detail: str = ""

    @property
    def helper_cpu_percent(self) -> float:
        """Summed %cpu across helper processes (the VM helper can exceed 100%)."""
        return sum(row.cpu_percent for row in self.helpers)

    @property
    def helper_rss_kb(self) -> int:
        """Summed resident memory across helper processes."""
        return sum(row.rss_kb for row in self.helpers)

    @property
    def stuck_exiting_pids(self) -> List[int]:
        """Helpers whose STAT carries the exit flag: unkillable until reboot."""
        return [row.pid for row in self.helpers if STUCK_EXIT_STAT_FLAG in row.stat]


def first_existing(candidates: Sequence[str]) -> Optional[str]:
    """First path in `candidates` that exists and is executable, else None."""
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def orbstack_installed(orb_bin: Optional[str], app_path: Path = ORBSTACK_APP_PATH) -> bool:
    """True when either the orb CLI or the app bundle is present."""
    return orb_bin is not None or app_path.exists()


def parse_helper_rows(text: str, marker: str = ORBSTACK_HELPER_COMMAND_MARKER) -> List[OrbstackHelperRow]:
    """Helper rows from header-less `ps -axo pid,stat,%cpu,rss,command` output.

    A helper stuck exiting shows as `(OrbStack Helper)` (no path, rss 0), so the match
    is a substring on the command, not a path prefix. The OrbStack GUI app itself
    (`.../MacOS/OrbStack`) does not match.
    """
    rows = []  # type: List[OrbstackHelperRow]
    for line in text.splitlines():
        fields = line.split(None, HELPER_PS_FIXED_FIELD_COUNT)
        if len(fields) < HELPER_PS_FIXED_FIELD_COUNT + 1 or marker not in fields[4]:
            continue
        try:
            rows.append(OrbstackHelperRow(
                pid=int(fields[0]), stat=fields[1], cpu_percent=float(fields[2]), rss_kb=int(fields[3])))
        except ValueError:
            continue
    return rows


def read_helper_rows(runner: Runner = run_with_deadline) -> List[OrbstackHelperRow]:
    """Bounded ps read of the helper processes; empty when ps fails."""
    outcome = runner(HELPER_PS_ARGV, QUICK_COMMAND_DEADLINE_SECONDS)
    if not outcome.succeeded:
        return []
    return parse_helper_rows(outcome.stdout)


def classify_orb_status(outcome: CommandOutcome) -> str:
    """`orb status` outcome -> running | stopped | hung | unknown.

    `orb status` prints "Running" (exit 0) or "Stopped" (exit 1); the exit code alone
    is not enough, so the first word decides.
    """
    if outcome.status == STATUS_TIMEOUT:
        return PROBE_HUNG
    if outcome.status != STATUS_OK:
        return PROBE_UNKNOWN
    words = outcome.stdout.strip().lower().split()
    first_word = words[0] if words else ""
    if first_word == ORB_STATUS_RUNNING:
        return ORB_STATUS_RUNNING
    if first_word == ORB_STATUS_STOPPED:
        return ORB_STATUS_STOPPED
    return PROBE_UNKNOWN


def probe_liveness(
    orb_bin: Optional[str],
    docker_bin: Optional[str],
    runner: Runner = run_with_deadline,
    app_path: Path = ORBSTACK_APP_PATH,
) -> Tuple[str, str]:
    """(liveness, detail): ok | hung | not_running | not_installed | unknown.

    `orb status` first; docker only when it says Running (docker auto-starts a
    stopped OrbStack). `orb status` hanging past its deadline counts as hung.
    """
    if not orbstack_installed(orb_bin, app_path):
        return PROBE_NOT_INSTALLED, "no orb CLI and no OrbStack.app"
    if orb_bin is None:
        return PROBE_UNKNOWN, "OrbStack.app present but no orb CLI found"

    status_outcome = runner([orb_bin, "status"], ORBSTACK_STATUS_DEADLINE_S)
    orb_status = classify_orb_status(status_outcome)
    if orb_status == PROBE_HUNG:
        return PROBE_HUNG, "`orb status` did not answer within %ds" % ORBSTACK_STATUS_DEADLINE_S
    if orb_status == ORB_STATUS_STOPPED:
        return PROBE_NOT_RUNNING, "orb status: Stopped"
    if orb_status != ORB_STATUS_RUNNING:
        return PROBE_UNKNOWN, "orb status unreadable: %r" % (status_outcome.stdout.strip() or status_outcome.error)
    if docker_bin is None:
        return PROBE_OK, "orb status: Running (no docker CLI to probe)"

    docker_outcome = runner([docker_bin] + DOCKER_VERSION_ARGS, ORBSTACK_DOCKER_PROBE_DEADLINE_S)
    if docker_outcome.status == STATUS_TIMEOUT:
        return PROBE_HUNG, "`docker version` did not answer within %ds" % ORBSTACK_DOCKER_PROBE_DEADLINE_S
    if docker_outcome.succeeded:
        return PROBE_OK, "docker server %s" % docker_outcome.stdout.strip()
    return PROBE_UNKNOWN, "docker version failed: %s" % (
        (docker_outcome.stderr.strip() or docker_outcome.error)[-200:])


def inspect_orbstack(runner: Runner = run_with_deadline) -> OrbstackHealth:
    """Locate the binaries, probe liveness, read helper stats (skipped when not installed)."""
    orb_bin = first_existing(ORB_BIN_CANDIDATES)
    docker_bin = first_existing(DOCKER_BIN_CANDIDATES)
    liveness, detail = probe_liveness(orb_bin, docker_bin, runner)
    if liveness == PROBE_NOT_INSTALLED:
        return OrbstackHealth(liveness=liveness, detail=detail)
    return OrbstackHealth(liveness=liveness, helpers=read_helper_rows(runner), detail=detail)


@dataclass
class RestartReport:
    """Outcome of one `orb stop` + `orb start` cycle and the liveness probe after it."""

    stop: Optional[CommandOutcome]
    start: Optional[CommandOutcome]
    liveness_after: str
    detail_after: str

    @property
    def came_back(self) -> bool:
        """True when docker answered after the restart."""
        return self.liveness_after == PROBE_OK


def restart_orbstack(runner: Runner = run_with_deadline) -> RestartReport:
    """`orb stop`, `orb start` (each bounded), then re-probe. Caller handles DRY_RUN."""
    orb_bin = first_existing(ORB_BIN_CANDIDATES)
    docker_bin = first_existing(DOCKER_BIN_CANDIDATES)
    if orb_bin is None:
        return RestartReport(None, None, PROBE_UNKNOWN, "no orb CLI found")
    stop = runner([orb_bin, "stop"], ORBSTACK_RESTART_DEADLINE_S)
    start = runner([orb_bin, "start"], ORBSTACK_RESTART_DEADLINE_S)
    liveness, detail = probe_liveness(orb_bin, docker_bin, runner)
    return RestartReport(stop, start, liveness, detail)
