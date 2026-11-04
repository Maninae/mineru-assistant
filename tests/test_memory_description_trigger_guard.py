"""The memory-description trigger runs CC at most once per local day.

On the DST fall-back night launchd fires the 1:30 AM job twice. The trigger touches
`cache/job-markers/memory-description.done` after a successful run, and
`idempotent_guard` skips any later fire the same day. Each case runs the real
trigger in bash with WORKSPACE at tmp_path and CC_BIN at a stub that counts calls.
"""

import stat
import subprocess
from pathlib import Path

TRIGGER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "trigger-memory-description-claude-code.sh"


def write_executable_script(path: Path, body: str) -> Path:
    """Write a bash script and mark it executable."""
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def run_trigger(tmp_path: Path, cc_exit_code: int) -> subprocess.CompletedProcess:
    """Run the trigger once against a stub CC that logs a line and exits with cc_exit_code."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cc_stub = write_executable_script(
        tmp_path / "cc-stub.sh", f"echo call >> '{tmp_path}/cc-calls.txt'\nexit {cc_exit_code}\n"
    )
    deliver_stub = write_executable_script(tmp_path / "deliver-stub.sh", "exit 0\n")
    return subprocess.run(
        ["/bin/bash", str(TRIGGER_PATH)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "WORKSPACE": str(workspace),
            "CC_BIN": str(cc_stub),
            "DELIVER_BIN": str(deliver_stub),
        },
    )


def count_cc_calls(tmp_path: Path) -> int:
    calls_path = tmp_path / "cc-calls.txt"
    return len(calls_path.read_text().splitlines()) if calls_path.exists() else 0


def test_second_fire_same_day_is_skipped(tmp_path: Path) -> None:
    first_run = run_trigger(tmp_path, cc_exit_code=0)
    assert first_run.returncode == 0, first_run.stderr
    assert (tmp_path / "workspace" / "cache" / "job-markers" / "memory-description.done").exists()

    second_run = run_trigger(tmp_path, cc_exit_code=0)
    assert second_run.returncode == 0, second_run.stderr
    assert "Already ran today" in second_run.stdout
    assert count_cc_calls(tmp_path) == 1


def test_failed_run_leaves_no_marker_so_a_retry_still_runs(tmp_path: Path) -> None:
    failed_run = run_trigger(tmp_path, cc_exit_code=3)
    assert failed_run.returncode == 3
    assert not (tmp_path / "workspace" / "cache" / "job-markers" / "memory-description.done").exists()

    retry_run = run_trigger(tmp_path, cc_exit_code=0)
    assert retry_run.returncode == 0, retry_run.stderr
    assert count_cc_calls(tmp_path) == 2
