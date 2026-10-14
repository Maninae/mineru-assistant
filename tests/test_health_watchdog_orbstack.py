"""Tests for the watchdog's OrbStack check: ps parsing, probe ordering (docker must never
run unless `orb status` says Running), and the pure wedge / stuck-exit / restart logic."""

import sys
from pathlib import Path
from typing import List

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

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
    OrbstackHealth,
    OrbstackHelperRow,
    RestartReport,
    parse_helper_rows,
    probe_liveness,
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
