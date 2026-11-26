"""The curiosity question pauses while the open backlog is at or above the operator's limit.

Unit cases cover the counting and the decision in `scripts/curiosity_backlog.py`. Trigger
cases run the real `trigger-curiosity-question-claude-code.sh` in bash with WORKSPACE at
tmp_path, CC_BIN at a stub that records its arguments, and DELIVER_BIN at a stub that
records each notice.
"""

import importlib.util
import json
import stat
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
TRIGGER_PATH = SCRIPTS_DIR / "trigger-curiosity-question-claude-code.sh"

gate_spec = importlib.util.spec_from_file_location("curiosity_backlog", SCRIPTS_DIR / "curiosity_backlog.py")
curiosity_backlog = importlib.util.module_from_spec(gate_spec)
gate_spec.loader.exec_module(curiosity_backlog)

OPEN_LINE = "> **⬜ UNANSWERED**\n"
PENDING_LINE = "> **📝 ANSWERED · to file**\n"
FILED_LINE = "> **✅ ANSWERED** · saved to memory/people/example.md\n"


def make_workspace(tmp_path: Path, open_count: int, pending_count: int = 0, filed_count: int = 0,
                   threshold=None) -> Path:
    """A workspace with question files in each state and, optionally, a backlog limit."""
    workspace = tmp_path / "workspace"
    briefs_dir = workspace / "briefs_curiosity"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    day = 0
    for status_line, count in ((OPEN_LINE, open_count), (PENDING_LINE, pending_count), (FILED_LINE, filed_count)):
        for _ in range(count):
            day += 1
            # The body repeats the markers on purpose: only line 1 may be counted.
            (briefs_dir / f"question-2020-01-{day:02d}.md").write_text(
                status_line + "\nA question?\n\n**💬 Answer** it was UNANSWERED, now ANSWERED · to file\n")
    if threshold is not None:
        (workspace / "config").mkdir(exist_ok=True)
        (workspace / "config" / "curiosity.json").write_text(json.dumps({"pause_at_open_backlog": threshold}))
    return workspace


def test_counts_read_only_the_status_line(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, open_count=3, pending_count=2, filed_count=4)
    assert curiosity_backlog.count_open_and_pending(workspace / "briefs_curiosity") == (3, 2)


def test_choose_action_boundaries() -> None:
    choose = curiosity_backlog.choose_action
    assert choose(19, 0, 20) == curiosity_backlog.GateAction.RUN
    assert choose(20, 0, 20) == curiosity_backlog.GateAction.SKIP
    assert choose(55, 2, 20) == curiosity_backlog.GateAction.FILE_ONLY
    assert choose(500, 3, 0) == curiosity_backlog.GateAction.RUN  # gate off


def test_no_config_means_gate_off_and_no_state_file(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, open_count=40)
    decision = curiosity_backlog.decide_and_record(workspace, {})
    assert decision["action"] == "run" and decision["transition"] == "none"
    assert not (workspace / "briefs_curiosity" / "backlog-pause-state.json").exists()


def test_pause_and_resume_transitions_fire_once_each(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, open_count=21, threshold=20)
    briefs_dir = workspace / "briefs_curiosity"
    first = curiosity_backlog.decide_and_record(workspace, {})
    second = curiosity_backlog.decide_and_record(workspace, {})
    assert (first["action"], first["transition"]) == ("skip", "paused")
    assert (second["action"], second["transition"]) == ("skip", "none")
    assert json.loads((briefs_dir / "backlog-pause-state.json").read_text())["paused"] is True

    for answered in sorted(briefs_dir.glob("question-*.md"))[:2]:   # 21 open -> 19 open
        answered.write_text(FILED_LINE + "\nA question?\n")
    third = curiosity_backlog.decide_and_record(workspace, {})
    fourth = curiosity_backlog.decide_and_record(workspace, {})
    assert (third["action"], third["transition"]) == ("run", "resumed")
    assert (fourth["action"], fourth["transition"]) == ("run", "none")


def test_malformed_limit_fails_loud(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, open_count=1, threshold="twenty")
    try:
        curiosity_backlog.decide_and_record(workspace, {})
    except ValueError as error:
        assert "pause_at_open_backlog" in str(error)
    else:
        raise AssertionError("a non-numeric limit must raise")


def test_env_override_wins_over_config(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, open_count=5, threshold=20)
    decision = curiosity_backlog.decide_and_record(workspace, {"MINERU_CURIOSITY_PAUSE_AT": "3"})
    assert decision["action"] == "skip" and decision["threshold"] == 3


# ---- the real trigger script ----

def write_executable_script(path: Path, body: str) -> Path:
    """Write a bash script and mark it executable."""
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def run_trigger(tmp_path: Path, workspace: Path) -> subprocess.CompletedProcess:
    """Run the trigger once. The CC stub records its arguments and touches a question file
    (a real run writes or stamps one, which the trigger's fresh-output check looks for)."""
    cc_stub = write_executable_script(
        tmp_path / "cc-stub.sh",
        f"echo \"$*\" >> '{tmp_path}/cc-calls.txt'\ntouch '{workspace}/briefs_curiosity/question-2020-01-01.md'\nexit 0\n")
    deliver_stub = write_executable_script(
        tmp_path / "deliver-stub.sh", f"echo \"$*\" >> '{tmp_path}/deliveries.txt'\nexit 0\n")
    return subprocess.run(
        ["/bin/bash", str(TRIGGER_PATH)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "WORKSPACE": str(workspace),
             "CC_BIN": str(cc_stub), "DELIVER_BIN": str(deliver_stub)},
    )


def read_lines(path: Path) -> list:
    return path.read_text().splitlines() if path.exists() else []


def test_trigger_skips_without_running_claude_and_notifies_once(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, open_count=55, threshold=20)
    first = run_trigger(tmp_path, workspace)
    second = run_trigger(tmp_path, workspace)
    assert first.returncode == 0 and second.returncode == 0, first.stderr + second.stderr
    assert "action=skip open=55" in first.stdout
    assert read_lines(tmp_path / "cc-calls.txt") == []
    deliveries = read_lines(tmp_path / "deliveries.txt")
    assert len(deliveries) == 1 and "paused: 55 open (limit 20)" in deliveries[0]
    assert (workspace / "briefs_curiosity" / "backlog-pause-state.json").exists()  # the sign of life


def test_trigger_files_waiting_answers_while_paused(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, open_count=30, pending_count=2, threshold=20)
    result = run_trigger(tmp_path, workspace)
    assert result.returncode == 0, result.stderr
    calls = read_lines(tmp_path / "cc-calls.txt")
    assert len(calls) == 1
    assert "recurring/curiosity-file-pending-answers.md" in calls[0]
    assert "daily-curiosity-question.md" not in calls[0]


def test_trigger_asks_when_under_the_limit_or_when_no_limit_is_set(tmp_path: Path) -> None:
    under_limit = make_workspace(tmp_path / "a", open_count=19, threshold=20)
    (tmp_path / "a").mkdir(exist_ok=True)
    result = run_trigger(tmp_path / "a", under_limit)
    assert result.returncode == 0, result.stderr
    assert "recurring/daily-curiosity-question.md" in read_lines(tmp_path / "a" / "cc-calls.txt")[0]

    no_limit = make_workspace(tmp_path / "b", open_count=80)
    result = run_trigger(tmp_path / "b", no_limit)
    assert result.returncode == 0, result.stderr
    assert "recurring/daily-curiosity-question.md" in read_lines(tmp_path / "b" / "cc-calls.txt")[0]
    assert read_lines(tmp_path / "b" / "deliveries.txt") == []


def test_trigger_alerts_and_fails_on_a_malformed_limit(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, open_count=5, threshold="twenty")
    result = run_trigger(tmp_path, workspace)
    assert result.returncode == 1
    assert read_lines(tmp_path / "cc-calls.txt") == []
    assert any("backlog gate failed" in line for line in read_lines(tmp_path / "deliveries.txt"))
