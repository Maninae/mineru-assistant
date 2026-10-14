"""Tests for the watchdog's OrbStack check: ps parsing, probe ordering (docker must never
run unless `orb status` says Running), and the pure wedge / stuck-exit / restart logic."""

import sys
from pathlib import Path
from typing import List

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import health_watchdog.__main__ as watchdog_main  # noqa: E402
from health_watchdog import config  # noqa: E402
from health_watchdog.evaluate_orbstack import (  # noqa: E402
    CHECK_ORBSTACK,
    CHECK_ORBSTACK_STUCK,
    LAST_RESTART_KEY,
    evaluate_orbstack,
    record_restart,
    restart_finding,
)
from health_watchdog.findings import Severity  # noqa: E402
from health_watchdog.orbstack_health import (  # noqa: E402
    PROBE_HUNG,
    PROBE_NOT_INSTALLED,
    PROBE_NOT_RUNNING,
    PROBE_OK,
    PROBE_UNKNOWN,
    OrbstackHealth,
    OrbstackHelperRow,
    RestartReport,
    parse_helper_rows,
    probe_liveness,
    restart_orbstack,
)
from health_watchdog.state import default_state  # noqa: E402
from health_watchdog.subprocess_deadline import CommandOutcome  # noqa: E402

ORB = "/fake/orb"
DOCKER = "/fake/docker"
NOW = 1_800_000_000.0

HELPER_PS_TEXT = """\
 3738 ?Es    0.0      0 (OrbStack Helper)
15542 S      3.2 122368 /Applications/OrbStack.app/Contents/MacOS/OrbStack
 4100 R    210.5 900000 /Applications/OrbStack.app/Contents/Frameworks/OrbStack Helper.app/Contents/MacOS/OrbStack Helper vmgr
"""


class FakeRunner:
    """Records argv and replays canned outcomes keyed by the command's second word."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []  # type: List[List[str]]

    def __call__(self, argv, deadline):
        self.calls.append(list(argv))
        key = argv[1] if argv[0] == ORB else argv[0]
        return self.outcomes[key]


def ok(stdout, returncode=0):
    return CommandOutcome(argv=[], status="ok", returncode=returncode, stdout=stdout)


def timeout():
    return CommandOutcome(argv=[], status="timeout")


def helper(cpu=0.0, stat="R", pid=4100):
    return OrbstackHelperRow(pid=pid, stat=stat, cpu_percent=cpu, rss_kb=1024)


def test_parse_helper_rows_matches_helpers_only_including_stuck_exit_form():
    rows = parse_helper_rows(HELPER_PS_TEXT)
    assert [row.pid for row in rows] == [3738, 4100]
    assert rows[0].stat == "?Es" and rows[0].rss_kb == 0
    health = OrbstackHealth(liveness=PROBE_NOT_RUNNING, helpers=rows)
    assert health.stuck_exiting_pids == [3738]
    assert round(health.helper_cpu_percent, 1) == 210.5


def test_docker_is_never_invoked_when_orb_status_is_stopped():
    runner = FakeRunner({"status": ok("Stopped\n", returncode=1), DOCKER: ok("27.0")})
    liveness, _detail = probe_liveness(ORB, DOCKER, runner)
    assert liveness == PROBE_NOT_RUNNING
    assert runner.calls == [[ORB, "status"]]


def test_docker_probed_only_after_running_and_hang_is_detected():
    runner = FakeRunner({"status": ok("Running\n"), DOCKER: ok("27.3.1\n")})
    assert probe_liveness(ORB, DOCKER, runner)[0] == PROBE_OK
    assert runner.calls[1][0] == DOCKER
    hung = FakeRunner({"status": ok("Running\n"), DOCKER: timeout()})
    assert probe_liveness(ORB, DOCKER, hung)[0] == PROBE_HUNG


def test_orb_status_hanging_counts_as_hung_without_docker():
    runner = FakeRunner({"status": timeout(), DOCKER: ok("27.0")})
    assert probe_liveness(ORB, DOCKER, runner)[0] == PROBE_HUNG
    assert all(call[0] != DOCKER for call in runner.calls)


def test_not_installed_when_no_cli_and_no_app(tmp_path):
    runner = FakeRunner({})
    liveness, _ = probe_liveness(None, None, runner, app_path=tmp_path / "missing.app")
    assert liveness == PROBE_NOT_INSTALLED and runner.calls == []


def test_not_installed_and_stopped_produce_no_findings():
    state = default_state()
    for liveness in (PROBE_NOT_INSTALLED, PROBE_NOT_RUNNING):
        for _ in range(5):
            verdict = evaluate_orbstack(OrbstackHealth(liveness=liveness), state, NOW, True)
            assert verdict.findings == [] and verdict.restart_reason is None


def test_stopped_vm_with_hot_helper_is_not_a_wedge():
    state = default_state()
    for _ in range(5):
        verdict = evaluate_orbstack(OrbstackHealth(PROBE_NOT_RUNNING, [helper(cpu=350.0)]), state, NOW, True)
    assert verdict.findings == [] and verdict.restart_reason is None


def test_sustained_helper_cpu_warns_when_auto_restart_disabled():
    state = default_state()
    health = OrbstackHealth(PROBE_OK, [helper(cpu=350.0)])
    for _ in range(config.ORBSTACK_CPU_SUSTAINED_RUNS - 1):
        assert evaluate_orbstack(health, state, NOW, False).findings == []
    verdict = evaluate_orbstack(health, state, NOW, False)
    assert verdict.restart_reason is None
    [finding] = verdict.findings
    assert finding.check_id == CHECK_ORBSTACK and finding.severity == Severity.WARN
    assert "OrbStack looks wedged" in finding.title and "350% CPU" in finding.title


def test_hung_probe_restarts_when_enabled_then_cooldown_blocks_second_restart():
    state = default_state()
    health = OrbstackHealth(PROBE_HUNG, [helper(cpu=20.0)], detail="`docker version` did not answer within 30s")
    assert evaluate_orbstack(health, state, NOW, True).restart_reason is None
    verdict = evaluate_orbstack(health, state, NOW, True)
    assert verdict.restart_reason and "docker version" in verdict.restart_reason
    record_restart(state, NOW)
    later = evaluate_orbstack(health, state, NOW + 600, True)
    assert later.restart_reason is None
    assert "Auto-restart already used" in later.findings[0].details[1]
    after_cooldown = evaluate_orbstack(health, state, NOW + config.ORBSTACK_RESTART_COOLDOWN_S, True)
    assert after_cooldown.restart_reason is not None


def test_stuck_exit_is_critical_after_two_runs_and_never_restarts():
    state = default_state()
    health = OrbstackHealth(PROBE_HUNG, [helper(stat="?Es", pid=3738), helper(cpu=400.0)])
    assert evaluate_orbstack(health, state, NOW, True).findings == []
    verdict = None
    for _ in range(4):
        verdict = evaluate_orbstack(health, state, NOW, True)
    assert verdict.restart_reason is None
    [finding] = verdict.findings
    assert finding.check_id == CHECK_ORBSTACK_STUCK and finding.severity == Severity.CRITICAL
    assert "needs a reboot" in finding.title and "3738" in finding.details[0]
    assert state.get(LAST_RESTART_KEY) is None


def test_restart_findings_report_success_failure_and_dry_run():
    dry = restart_finding("x", None, dry_run=True)
    assert dry.action_taken and dry.title.startswith("Would restart OrbStack")
    good = restart_finding("x", RestartReport(ok(""), ok(""), PROBE_OK, "docker server 27"), dry_run=False)
    assert good.severity == Severity.WARN and good.title == "Restarted OrbStack: x." and good.action_taken
    bad = restart_finding("x", RestartReport(ok(""), timeout(), PROBE_HUNG, "hung"), dry_run=False)
    assert bad.severity == Severity.CRITICAL and "reboot the Mac" in bad.title and bad.action_taken


class SequenceRunner(FakeRunner):
    """FakeRunner whose canned outcomes may be lists, consumed one per call."""

    def __call__(self, argv, deadline):
        self.calls.append(list(argv))
        key = argv[1] if argv[0] == ORB else argv[0]
        value = self.outcomes[key]
        return value.pop(0) if isinstance(value, list) else value


def test_cpu_only_evidence_with_docker_answering_warns_and_never_restarts():
    state = default_state()
    health = OrbstackHealth(PROBE_OK, [helper(cpu=250.0)], detail="docker server 27")
    for _ in range(config.ORBSTACK_CPU_SUSTAINED_RUNS):
        verdict = evaluate_orbstack(health, state, NOW, True)
    assert verdict.restart_reason is None
    [finding] = verdict.findings
    assert finding.severity == Severity.WARN and "250% CPU" in finding.title
    assert any("No auto-restart" in line for line in finding.details)
    assert any("orb stop && orb start" in line for line in finding.details)


def test_unknown_liveness_does_not_count_toward_cpu_streak():
    state = default_state()
    for _ in range(5):
        verdict = evaluate_orbstack(OrbstackHealth(PROBE_UNKNOWN, [helper(cpu=400.0)]), state, NOW, True)
    assert verdict.findings == [] and state["streaks"]["orbstack_cpu"] == 0


def test_stuck_exit_critical_uses_six_hour_cooldown():
    state = default_state()
    health = OrbstackHealth(PROBE_NOT_RUNNING, [helper(stat="?Es", pid=3738)])
    evaluate_orbstack(health, state, NOW, True)
    [finding] = evaluate_orbstack(health, state, NOW, True).findings
    assert finding.cooldown_seconds == config.ORBSTACK_STUCK_EXIT_COOLDOWN_S == 6 * 3600


def test_restart_runs_stop_then_start_and_retries_probe_until_docker_answers():
    sleeps = []
    runner = SequenceRunner({
        "stop": ok(""), "start": ok(""), "status": ok("Running\n"),
        DOCKER: [timeout(), timeout(), ok("27.3.1\n")],
    })
    report = restart_orbstack(runner, orb_bin=ORB, docker_bin=DOCKER, sleeper=sleeps.append)
    assert runner.calls[0] == [ORB, "stop"] and runner.calls[1] == [ORB, "start"]
    assert report.came_back and report.detail_after.endswith("(probe 3)")
    assert sleeps == [config.ORBSTACK_RESTART_PROBE_INTERVAL_S] * 2


def test_restart_gives_up_after_max_probe_attempts():
    runner = SequenceRunner({"stop": ok(""), "start": ok(""), "status": ok("Running\n"), DOCKER: timeout()})
    sleeps = []
    report = restart_orbstack(runner, orb_bin=ORB, docker_bin=DOCKER, sleeper=sleeps.append)
    assert not report.came_back and report.liveness_after == PROBE_HUNG
    assert len(sleeps) == config.ORBSTACK_RESTART_PROBE_ATTEMPTS - 1


def hung_health():
    return OrbstackHealth(PROBE_HUNG, [helper(cpu=20.0)], detail="`docker version` did not answer within 30s")


def wire_handle_orbstack(monkeypatch, dry_run):
    """Patch __main__'s collaborators; return the call log."""
    calls = []
    monkeypatch.setattr(watchdog_main, "DRY_RUN", dry_run)
    monkeypatch.setattr(watchdog_main, "ORBSTACK_AUTO_RESTART", True)
    monkeypatch.setattr(watchdog_main.orbstack_health, "inspect_orbstack", lambda: hung_health())
    monkeypatch.setattr(watchdog_main.orbstack_health, "restart_orbstack",
                        lambda: calls.append("restart") or RestartReport(ok(""), ok(""), PROBE_OK, "docker 27"))
    real_record = watchdog_main.evaluate_orbstack.record_restart
    monkeypatch.setattr(watchdog_main.evaluate_orbstack, "record_restart",
                        lambda state, ts: calls.append("record") or real_record(state, ts))
    monkeypatch.setattr(watchdog_main, "save_state", lambda path, state: calls.append("save"))
    return calls


def test_handle_orbstack_dry_run_never_restarts_or_records(monkeypatch):
    calls = wire_handle_orbstack(monkeypatch, dry_run=True)
    state = default_state()
    findings = []
    for _ in range(config.ORBSTACK_HUNG_SUSTAINED_RUNS):
        findings = watchdog_main.handle_orbstack(state, NOW)
    assert calls == []
    assert findings[0].title.startswith("Would restart OrbStack") and state.get(LAST_RESTART_KEY) is None


def test_handle_orbstack_persists_cooldown_before_restarting(monkeypatch):
    calls = wire_handle_orbstack(monkeypatch, dry_run=False)
    state = default_state()
    findings = []
    for _ in range(config.ORBSTACK_HUNG_SUSTAINED_RUNS):
        findings = watchdog_main.handle_orbstack(state, NOW)
    assert calls == ["record", "save", "restart"]
    assert findings[0].title.startswith("Restarted OrbStack") and state[LAST_RESTART_KEY] == NOW
