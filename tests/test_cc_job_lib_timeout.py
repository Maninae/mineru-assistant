"""Tests for `run_with_timeout` and the `run_cc_job` ceiling in scripts/cc-job-lib.sh.

Every case runs the real bash library in a child `/bin/bash` (bash 3.2 on macOS)
with `set -euo pipefail`, exactly as the trigger scripts source it. Nothing real
runs: `CC_BIN` points at a stub, `DELIVER_BIN` at a script that appends its argv
to a capture file, and `WORKSPACE` at `tmp_path`, so no Telegram message is sent
and no log lands in a live workspace.

Covered:
  * a normal exit returns the command's own code (0 and nonzero) with no alert
  * the deadline fires one alert naming the timeout, kills the whole process
    tree (parent shell and its sleep), returns 124, sets RUN_WITH_TIMEOUT_FIRED
  * a child that ignores SIGTERM is SIGKILLed after the grace period
  * nothing is left running afterwards (checked with pgrep on a unique marker)
  * usage errors return 2
"""

from __future__ import annotations

import os
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Dict, Optional

import pytest

LIB = Path(__file__).resolve().parents[1] / "scripts" / "cc-job-lib.sh"

# Short grace periods keep each timeout case to a few seconds.
FAST_GRACE_ENV = {
    "TIMEOUT_TERM_GRACE_SECONDS": "2",
    "TIMEOUT_KILL_GRACE_SECONDS": "2",
}


def _write_executable(path: Path, body: str) -> Path:
    """Write a bash script and mark it executable."""
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _deliver_stub(tmp_path: Path) -> Dict[str, Path]:
    """Return paths for a DELIVER_BIN stub that appends its argv to a capture file."""
    capture = tmp_path / "alerts.txt"
    capture.write_text("")
    stub = _write_executable(tmp_path / "deliver-stub.sh", f"printf '%s\\n' \"$*\" >> '{capture}'\n")
    return {"stub": stub, "capture": capture}


def _run_lib(
    tmp_path: Path,
    snippet: str,
    cc_bin: str = "/usr/bin/false",
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Source the library in a fresh bash and run `snippet`, the way trigger scripts do."""
    deliver = _deliver_stub(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "WORKSPACE": str(workspace),
        "DELIVER_BIN": str(deliver["stub"]),
        "CC_BIN": cc_bin,
    }
    env.update(FAST_GRACE_ENV)
    if extra_env:
        env.update(extra_env)
    script = f'set -euo pipefail\n. "{LIB}"\n{snippet}\n'
    return subprocess.run(
        ["/bin/bash", "-c", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _alerts(tmp_path: Path) -> list:
    return [line for line in (tmp_path / "alerts.txt").read_text().splitlines() if line]


def _still_running(marker: str) -> bool:
    """True if any process command line still carries `marker`."""
    probe = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return probe.returncode == 0


# Snippet that runs a command under run_with_timeout and reports the outcome on
# one parseable line, surviving `set -e`.
REPORT = 'rc=0; run_with_timeout {limit} demo-job -- {cmd} || rc=$?; echo "RESULT rc=$rc fired=$RUN_WITH_TIMEOUT_FIRED"'


def _result_line(stdout: str) -> str:
    lines = [line for line in stdout.splitlines() if line.startswith("RESULT ")]
    assert len(lines) == 1, stdout
    return lines[0]


def test_normal_exit_returns_zero_and_no_alert(tmp_path: Path) -> None:
    proc = _run_lib(tmp_path, REPORT.format(limit=10, cmd="/bin/sh -c 'exit 0'"))
    assert proc.returncode == 0, proc.stderr
    assert _result_line(proc.stdout) == "RESULT rc=0 fired=0"
    assert _alerts(tmp_path) == []


def test_nonzero_exit_is_preserved(tmp_path: Path) -> None:
    proc = _run_lib(tmp_path, REPORT.format(limit=10, cmd="/bin/sh -c 'exit 7'"))
    assert _result_line(proc.stdout) == "RESULT rc=7 fired=0"
    assert _alerts(tmp_path) == []


def test_child_stdout_reaches_caller(tmp_path: Path) -> None:
    proc = _run_lib(tmp_path, "run_with_timeout 10 demo-job -- /bin/echo hello-from-child")
    assert proc.stdout.strip() == "hello-from-child"


def test_timeout_alerts_once_kills_tree_and_returns_124(tmp_path: Path) -> None:
    marker = f"rwt-{uuid.uuid4().hex}"
    # A shell parent with a sleep child: both must be gone afterwards.
    cmd = f"/bin/sh -c ': {marker}; sleep 3607; sleep 3608'"
    proc = _run_lib(tmp_path, REPORT.format(limit=2, cmd=cmd))

    assert _result_line(proc.stdout) == "RESULT rc=124 fired=1"
    alerts = _alerts(tmp_path)
    assert len(alerts) == 1, alerts
    assert "Cron job demo-job failed (timed out after 2s and was killed" in alerts[0]
    assert "tccd" in alerts[0] and "exit 124" in alerts[0]
    assert "run_with_timeout: demo-job exceeded 2s" in proc.stdout
    assert not _still_running(marker)
    assert not _still_running("sleep 3607")


def test_child_ignoring_sigterm_is_sigkilled(tmp_path: Path) -> None:
    marker = f"rwt-{uuid.uuid4().hex}"
    # trap '' TERM is inherited by the sleep (SIG_IGN survives exec), so only
    # SIGKILL can end this tree.
    cmd = f"/bin/bash -c ': {marker}; trap \"\" TERM; sleep 3609 & wait'"
    proc = _run_lib(tmp_path, REPORT.format(limit=1, cmd=cmd))

    assert _result_line(proc.stdout) == "RESULT rc=124 fired=1"
    assert len(_alerts(tmp_path)) == 1
    assert not _still_running(marker)
    assert not _still_running("sleep 3609")


def test_zero_limit_disables_ceiling(tmp_path: Path) -> None:
    proc = _run_lib(tmp_path, REPORT.format(limit=0, cmd="/bin/sh -c 'exit 3'"))
    assert _result_line(proc.stdout) == "RESULT rc=3 fired=0"


@pytest.mark.parametrize(
    "call",
    [
        "run_with_timeout",
        "run_with_timeout 5",
        "run_with_timeout 5 demo-job --",
        "run_with_timeout abc demo-job -- true",
    ],
)
def test_usage_errors_return_2(tmp_path: Path, call: str) -> None:
    proc = _run_lib(tmp_path, f'rc=0; {call} || rc=$?; echo "RESULT rc=$rc"')
    assert _result_line(proc.stdout) == "RESULT rc=2"
    assert "run_with_timeout:" in proc.stderr
