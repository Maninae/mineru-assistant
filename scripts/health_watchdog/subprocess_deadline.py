"""Bounded subprocess execution: every child gets a deadline and can be abandoned.

Python 3.9 compatible (see config.py).

The watchdog exists to notice a machine where syscalls hang (a stuck tccd makes
open() block for hours), so no child may ever block the watchdog itself:
  - run_with_deadline() polls instead of waiting, escalates TERM -> KILL, and if
    the child survives SIGKILL (stuck in the kernel) it is ABANDONED, not awaited.
  - The abandoned pid is reported so the caller can refuse to start another probe
    while it is still alive.
"""

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

ESCALATION_GRACE_SECONDS = 2.0
KILL_POLL_INTERVAL_SECONDS = 0.5
PS_BIN = "/bin/ps"
PS_STAT_DEADLINE_SECONDS = 5
ZOMBIE_STATE = "Z"

STATUS_OK = "ok"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"


@dataclass
class CommandOutcome:
    """What happened to one bounded child process.

    - status: "ok" (exited, any return code), "timeout" (deadline hit), or
      "error" (could not even start).
    - abandoned: True when the child ignored SIGKILL inside the grace period and
      was left running; `pid` then identifies the stuck process.
    """

    argv: List[str]
    status: str
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    pid: Optional[int] = None
    abandoned: bool = False
    elapsed_seconds: float = 0.0
    error: str = ""
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """True only for a clean exit with return code 0."""
        return self.status == STATUS_OK and self.returncode == 0


def process_alive(pid: int) -> bool:
    """Signal-0 liveness check; a PermissionError still means the pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _drain(proc: "subprocess.Popen[str]", timeout: float):
    """communicate() bounded by `timeout`; returns (stdout, stderr) or None on timeout."""
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _escalate_and_abandon(proc: "subprocess.Popen[str]") -> bool:
    """TERM, then KILL, each with a short grace; return True if the child still lives."""
    for signal_sender in (proc.terminate, proc.kill):
        try:
            signal_sender()
        except ProcessLookupError:
            return False
        if _drain(proc, ESCALATION_GRACE_SECONDS) is not None:
            return False
    return process_alive(proc.pid)


def run_with_deadline(
    argv: List[str],
    deadline_seconds: float,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    stdin_text: Optional[str] = None,
) -> CommandOutcome:
    """Run `argv` and return within roughly deadline + 2 escalation graces.

    Args:
        argv: command and arguments (no shell).
        deadline_seconds: wall-clock budget for the child to finish.
        env: full environment for the child (None inherits ours).
        cwd: working directory for the child.
        stdin_text: text fed to the child's stdin, then closed.
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except (OSError, ValueError) as start_error:
        return CommandOutcome(argv=argv, status=STATUS_ERROR, error=str(start_error))

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    drained = _drain(proc, deadline_seconds)
    elapsed = time.monotonic() - started
    if drained is not None:
        stdout, stderr = drained
        return CommandOutcome(
            argv=argv, status=STATUS_OK, returncode=proc.returncode,
            stdout=stdout or "", stderr=stderr or "", pid=proc.pid, elapsed_seconds=elapsed,
        )

    abandoned = _escalate_and_abandon(proc)
    return CommandOutcome(
        argv=argv, status=STATUS_TIMEOUT, pid=proc.pid, abandoned=abandoned,
        elapsed_seconds=time.monotonic() - started,
    )


def live_pids(pids: List[int]) -> List[int]:
    """Subset of `pids` still running, with zombies counted as exited.

    kill(pid, 0) says a zombie is alive, and a killed job's children sit as zombies
    of their dying parent for a moment, so one `ps -o stat` call decides instead;
    if ps itself fails, fall back to the signal check.
    """
    if not pids:
        return []
    try:
        result = subprocess.run(
            [PS_BIN, "-o", "pid=,stat=", "-p", ",".join(str(pid) for pid in pids)],
            capture_output=True, text=True, timeout=PS_STAT_DEADLINE_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return [pid for pid in pids if process_alive(pid)]
    running = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit() and not fields[1].startswith(ZOMBIE_STATE):
            running.add(int(fields[0]))
    return [pid for pid in pids if pid in running]


def wait_for_exit(pids: List[int], grace_seconds: float) -> List[int]:
    """Poll until every pid is gone (or a zombie) or the grace expires; return the survivors."""
    deadline = time.monotonic() + grace_seconds
    remaining = live_pids(list(pids))
    while remaining and time.monotonic() < deadline:
        time.sleep(KILL_POLL_INTERVAL_SECONDS)
        remaining = live_pids(remaining)
    return remaining
