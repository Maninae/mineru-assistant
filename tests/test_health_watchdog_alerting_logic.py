"""Tests for scripts/health_watchdog thresholds (evaluate.py), cooldown/recovery
bookkeeping (state.py), and message formatting (alerting.py), all on fake data."""

import datetime as dt
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from health_watchdog import config, evaluate  # noqa: E402
from health_watchdog.alerting import format_alert_message  # noqa: E402
from health_watchdog.browser_hygiene import BrowserStatus  # noqa: E402
from health_watchdog.findings import Finding, Severity  # noqa: E402
from health_watchdog.hung_jobs import HungJobCandidate, KillReport  # noqa: E402
from health_watchdog.process_table import ProcessInfo  # noqa: E402
from health_watchdog.state import decide_alerts, default_state, load_state, save_state  # noqa: E402
from health_watchdog.system_probes import SystemSnapshot  # noqa: E402
from health_watchdog.tcc_probe import TccProbeResult  # noqa: E402

NOW = 1_000_000.0


def snapshot(**overrides):
    base = dict(swap_total_mb=10240, swap_used_mb=1024, memory_free_percent=60,
                load_1m=1.0, load_5m=1.0, load_15m=1.0, ncpu=10, disk_free_gb=50.0,
                tccd_root_cpu=2.0, tccd_user_cpu=1.0)
    base.update(overrides)
    return SystemSnapshot(**base)


# --- thresholds -----------------------------------------------------------------

def test_tcc_hung_is_critical_with_fix_text():
    finding = evaluate.evaluate_tcc(TccProbeResult(status="hung", detail="20s exceeded"))
    assert finding.severity == Severity.CRITICAL
    assert "tccd" in finding.title and config.TCC_FIX_TEXT in finding.details


def test_tcc_error_is_warn_and_ok_is_none():
    assert evaluate.evaluate_tcc(TccProbeResult(status="error", detail="exit 1")).severity == Severity.WARN
    assert evaluate.evaluate_tcc(TccProbeResult(status="ok", detail="")) is None


def test_tccd_cpu_needs_sustained_runs():
    state = default_state()
    hot = snapshot(tccd_root_cpu=130.0)
    results = [evaluate.evaluate_tccd_cpu(hot, state) for _ in range(config.TCCD_CPU_SUSTAINED_RUNS)]
    assert results[:-1] == [None] * (config.TCCD_CPU_SUSTAINED_RUNS - 1)
    assert results[-1].severity == Severity.WARN and "130%" in results[-1].title
    # One quiet run resets the streak.
    assert evaluate.evaluate_tccd_cpu(snapshot(), state) is None
    assert state["streaks"][evaluate.STREAK_TCCD_CPU] == 0


def test_memory_incident_numbers_are_critical_with_top_rss():
    finding = evaluate.evaluate_memory(snapshot(swap_total_mb=10240, swap_used_mb=9420, memory_free_percent=8),
                                       ["OrbStack Helper (pid 5) 8.5 GB"])
    assert finding.severity == Severity.CRITICAL
    assert "swap 92%" in finding.title and "8% memory free" in finding.title
    assert finding.details == ["Top RSS: OrbStack Helper (pid 5) 8.5 GB"]


def test_memory_warn_band_and_quiet():
    assert evaluate.evaluate_memory(snapshot(swap_used_mb=8300), []).severity == Severity.WARN
    assert evaluate.evaluate_memory(snapshot(swap_used_mb=7000), []) is None
    assert evaluate.evaluate_memory(SystemSnapshot(), []) is None


def test_load_sustained_warn_then_critical():
    state = default_state()
    warn = snapshot(load_5m=12.0)
    for _ in range(config.LOAD_SUSTAINED_RUNS - 1):
        assert evaluate.evaluate_load(warn, state, []) is None
    assert evaluate.evaluate_load(warn, state, ["x (pid 1) 300%"]).severity == Severity.WARN
    critical = snapshot(load_5m=17.0)
    for _ in range(config.LOAD_SUSTAINED_RUNS - 1):
        assert evaluate.evaluate_load(critical, state, []).severity == Severity.WARN
    assert evaluate.evaluate_load(critical, state, []).severity == Severity.CRITICAL


def test_disk_thresholds():
    assert evaluate.evaluate_disk(snapshot(disk_free_gb=20)) is None
    assert evaluate.evaluate_disk(snapshot(disk_free_gb=8)).severity == Severity.WARN
    assert evaluate.evaluate_disk(snapshot(disk_free_gb=3)).severity == Severity.CRITICAL


def test_landline_count_threshold():
    assert evaluate.evaluate_landline(1, snapshot()) is None
    finding = evaluate.evaluate_landline(3, snapshot())
    assert finding.severity == Severity.WARN and "3 times" in finding.title
    assert finding.details[0].startswith("Snapshot: load5")


def renderer(cpu):
    return ProcessInfo(pid=1, ppid=1, pgid=1, tty="??", user="u", etime_seconds=10, cpu_percent=cpu,
                       rss_kb=1, command="Chromium Helper --type=renderer")


def test_browser_tab_leak_and_sustained_renderer():
    state = default_state()
    assert evaluate.browser_needs_stop(BrowserStatus(server_running=False, tab_count=40), state) is None
    assert "26 tabs" in evaluate.browser_needs_stop(BrowserStatus(server_running=True, tab_count=26), state)
    hot = BrowserStatus(server_running=True, tab_count=3, renderers=[renderer(97.0)])
    for _ in range(config.BROWSER_RENDERER_CPU_SUSTAINED_RUNS - 1):
        assert evaluate.browser_needs_stop(hot, state) is None
    assert "renderer at 97%" in evaluate.browser_needs_stop(hot, state)


def test_hung_jobs_finding_escalates_on_sigkill_survivors():
    candidate = HungJobCandidate(root_pid=100, age_seconds=9 * 3600 + 120, command="bash x",
                                 label="p.morning-brief", descendant_pids=[101])
    clean = evaluate.hung_jobs_finding(KillReport([candidate], dry_run=False, killed=[100, 101]))
    assert clean.severity == Severity.WARN and clean.action_taken
    assert "Killed 1 hung" in clean.title and "morning-brief (pid 100, 9h02m old, 1 child procs)" in clean.details[0]
    stuck = evaluate.hung_jobs_finding(KillReport([candidate], dry_run=False, killed=[100], survivors=[101]))
    assert stuck.severity == Severity.CRITICAL and "survived SIGKILL" in stuck.details[1]
    dry = evaluate.hung_jobs_finding(KillReport([candidate], dry_run=True))
    assert dry.title.startswith("Would kill")


# --- cooldown + recovery ----------------------------------------------------------

def warn(check="memory"):
    return Finding(check, Severity.WARN, "w")


def critical(check="memory"):
    return Finding(check, Severity.CRITICAL, "c")


def test_first_alert_sends_then_cooldown_suppresses_then_expires():
    state = default_state()
    assert decide_alerts([warn()], state, NOW).to_send == [warn()]
    later = NOW + config.WARN_COOLDOWN_SECONDS - 1
    decision = decide_alerts([warn()], state, later)
    assert decision.to_send == [] and decision.suppressed == [warn()]
    expired = NOW + config.WARN_COOLDOWN_SECONDS
    assert decide_alerts([warn()], state, expired).to_send == [warn()]


def test_escalation_bypasses_cooldown_and_critical_cooldown_is_shorter():
    state = default_state()
    decide_alerts([warn()], state, NOW)
    assert decide_alerts([critical()], state, NOW + 60).to_send == [critical()]
    assert decide_alerts([critical()], state, NOW + 60 + config.CRITICAL_COOLDOWN_SECONDS - 1).to_send == []
    assert decide_alerts([critical()], state, NOW + 60 + config.CRITICAL_COOLDOWN_SECONDS).to_send == [critical()]


def test_recovery_fires_once_and_resets_cooldown():
    state = default_state()
    decide_alerts([warn()], state, NOW)
    recovered = decide_alerts([], state, NOW + 300)
    assert recovered.recovered == ["memory"] and recovered.to_send == []
    assert decide_alerts([], state, NOW + 600).recovered == []
    # A fresh occurrence after recovery alerts immediately even inside the old cooldown.
    assert decide_alerts([warn()], state, NOW + 900).to_send == [warn()]


def test_action_findings_always_send_and_never_recover():
    state = default_state()
    kill = Finding("hung_jobs", Severity.WARN, "killed", action_taken=True)
    assert decide_alerts([kill], state, NOW).to_send == [kill]
    assert decide_alerts([kill], state, NOW + 1).to_send == [kill]
    assert decide_alerts([], state, NOW + 2).recovered == []


def test_state_roundtrip_and_corruption(tmp_path):
    path = tmp_path / "state.json"
    assert load_state(path) == default_state()
    state = default_state()
    state["streaks"]["load_warn"] = 2
    state["tcc_probe_pid"] = 4242
    save_state(path, state)
    assert load_state(path)["streaks"]["load_warn"] == 2 and load_state(path)["tcc_probe_pid"] == 4242
    path.write_text("{not json")
    assert load_state(path) == default_state()


# --- formatting ---------------------------------------------------------------------

def test_message_has_header_findings_details_and_recoveries():
    message = format_alert_message(
        [Finding("tcc", Severity.CRITICAL, "tccd stuck", ["probe 20s", "Fix: x"])],
        ["load"], dt.datetime(2026, 10, 13, 16, 25),
    )
    lines = message.splitlines()
    assert lines[0].startswith("🩺 health-watchdog on ") and lines[0].endswith("16:25")
    assert lines[1] == "🔴 CRITICAL tccd stuck"
    assert lines[2:4] == ["   · probe 20s", "   · Fix: x"]
    assert lines[4] == "✅ recovered: load average"
