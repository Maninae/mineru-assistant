"""TCC responsiveness probe: can a protected file be opened within the deadline?

Python 3.9 compatible (see config.py).

A stuck tccd makes every open() of a TCC-protected path block in the kernel. The
probe is a read-only sqlite query against Messages' chat.db with a 20 s deadline:
  - finishes with rc 0           -> "ok"
  - deadline exceeded            -> "hung" (the CRITICAL the watchdog exists for)
  - finishes with rc != 0        -> "error" (permission / config problem, WARN once)
  - previous probe still alive   -> "hung" again, WITHOUT starting a new probe, so
                                    the watchdog never piles processes onto a
                                    frozen tccd.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from health_watchdog.config import (
    TCC_PROBE_DB,
    TCC_PROBE_DEADLINE_SECONDS,
    TCC_PROBE_QUERY,
    TCC_PROBE_SQLITE_BIN,
)
from health_watchdog.process_table import ProcessInfo, index_by_pid
from health_watchdog.subprocess_deadline import STATUS_OK, STATUS_TIMEOUT, process_alive, run_with_deadline

STATUS_HUNG = "hung"
STATUS_ERROR = "error"


@dataclass
class TccProbeResult:
    """Outcome of one TCC probe. probe_pid is set only while a stuck probe lives on."""

    status: str
    detail: str
    probe_pid: Optional[int] = None
    elapsed_seconds: float = 0.0


def previous_probe_still_alive(previous_pid: Optional[int], table: List[ProcessInfo]) -> bool:
    """True if the pid from the last run is alive AND still the sqlite3 probe (guards pid reuse)."""
    if previous_pid is None or not process_alive(previous_pid):
        return False
    row = index_by_pid(table).get(previous_pid)
    if row is None:
        # Alive per kill(0) but missing from ps: ps may have failed; stay conservative.
        return True
    return TCC_PROBE_SQLITE_BIN in row.command and str(TCC_PROBE_DB) in row.command


def run_tcc_probe(previous_pid: Optional[int], table: List[ProcessInfo]) -> TccProbeResult:
    """Run (or skip) the probe and classify the result."""
    if previous_probe_still_alive(previous_pid, table):
        return TccProbeResult(
            status=STATUS_HUNG,
            detail="previous probe (pid %d) is still blocked; not starting another" % previous_pid,
            probe_pid=previous_pid,
        )

    argv = [TCC_PROBE_SQLITE_BIN, "-readonly", str(TCC_PROBE_DB), TCC_PROBE_QUERY]
    outcome = run_with_deadline(argv, TCC_PROBE_DEADLINE_SECONDS)

    if outcome.status == STATUS_TIMEOUT:
        return TccProbeResult(
            status=STATUS_HUNG,
            detail="protected read did not return within %ds%s" % (
                TCC_PROBE_DEADLINE_SECONDS,
                " (probe survived SIGKILL)" if outcome.abandoned else "",
            ),
            probe_pid=outcome.pid if outcome.abandoned else None,
            elapsed_seconds=outcome.elapsed_seconds,
        )
    if outcome.status != STATUS_OK:
        return TccProbeResult(status=STATUS_ERROR, detail="probe could not start: %s" % outcome.error)
    if outcome.returncode != 0:
        stderr = outcome.stderr.strip().splitlines()
        return TccProbeResult(
            status=STATUS_ERROR,
            detail="sqlite3 exit %d: %s" % (outcome.returncode, stderr[-1] if stderr else "no stderr"),
            elapsed_seconds=outcome.elapsed_seconds,
        )
    return TccProbeResult(
        status=STATUS_OK,
        detail="chat count %s in %.1fs" % (outcome.stdout.strip(), outcome.elapsed_seconds),
        elapsed_seconds=outcome.elapsed_seconds,
    )


def probe_state_update(result: TccProbeResult) -> Dict[str, Optional[int]]:
    """The slice of persistent state the probe owns (the stuck pid, if any)."""
    return {"tcc_probe_pid": result.probe_pid}
