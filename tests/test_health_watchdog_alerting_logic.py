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


def test_memory_incident_numbers_are_critical_with_top_rss_after_sustained_runs():
    state = default_state()
    incident = snapshot(swap_total_mb=10240, swap_used_mb=9420, memory_free_percent=8)
    for _ in range(config.MEMORY_SUSTAINED_RUNS - 1):
        assert evaluate.evaluate_memory(incident, state, []) is None
    finding = evaluate.evaluate_memory(incident, state, ["OrbStack Helper (pid 5) 8.5 GB"])
    assert finding.severity == Severity.CRITICAL
    assert "swap 92%" in finding.title and "8% memory free" in finding.title
    assert finding.details == ["Top RSS: OrbStack Helper (pid 5) 8.5 GB"]


def test_memory_hysteresis_holds_between_clear_and_warn_bars():
    state = default_state()
    warn = snapshot(swap_used_mb=8300)        # 81%: above WARN
    between = snapshot(swap_used_mb=7700)     # 75%: between CLEAR (70) and WARN (80)
    clear = snapshot(swap_used_mb=7000)       # 68%: below CLEAR
    for _ in range(config.MEMORY_SUSTAINED_RUNS):
        finding = evaluate.evaluate_memory(warn, state, [])
    assert finding.severity == Severity.WARN
    assert evaluate.evaluate_memory(between, state, []).severity == Severity.WARN  # still latched
    assert evaluate.evaluate_memory(clear, state, []) is None
    assert evaluate.evaluate_memory(between, state, []) is None  # released, not re-latched
    assert evaluate.evaluate_memory(SystemSnapshot(), state, []) is None


def test_swap_flapping_across_warn_line_sends_one_message():
    state = default_state()
    messages = 0
    for run, fraction in enumerate((0.79, 0.81, 0.79, 0.81, 0.79, 0.81)):
        snap = SystemSnapshot(swap_total_mb=4096, swap_used_mb=4096 * fraction, memory_free_percent=60)
        finding = evaluate.evaluate_memory(snap, state, [])
        decision = decide_alerts([finding] if finding else [], state, NOW + run * 300)
        messages += 1 if (decision.to_send or decision.recovered) else 0
    assert messages == 1


def test_load_sustained_warn_then_critical_with_hysteresis():
    state = default_state()
    warn = snapshot(load_5m=12.0)
    for _ in range(config.LOAD_SUSTAINED_RUNS - 1):
        assert evaluate.evaluate_load(warn, state, []) is None
    assert evaluate.evaluate_load(warn, state, ["x (pid 1) 300%"]).severity == Severity.WARN
    critical = snapshot(load_5m=17.0)
    for _ in range(config.LOAD_SUSTAINED_RUNS - 1):
        assert evaluate.evaluate_load(critical, state, []).severity == Severity.WARN
    assert evaluate.evaluate_load(critical, state, []).severity == Severity.CRITICAL
    assert evaluate.evaluate_load(snapshot(load_5m=9.0), state, []).severity == Severity.WARN  # 0.9x: latched
    assert evaluate.evaluate_load(snapshot(load_5m=7.0), state, []) is None  # 0.7x: released


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


def test_tab_count_never_stops_the_browser_but_sustained_renderer_does():
    state = default_state()
    assert evaluate.browser_needs_stop(BrowserStatus(server_running=False, tab_count=40), state) is None
    for _ in range(5):
        assert evaluate.browser_needs_stop(BrowserStatus(server_running=True, tab_count=60), state) is None
    hot = BrowserStatus(server_running=True, tab_count=3, renderers=[renderer(97.0)])
    for _ in range(config.BROWSER_RENDERER_CPU_SUSTAINED_RUNS - 1):
        assert evaluate.browser_needs_stop(hot, state) is None
    assert "renderer at 97%" in evaluate.browser_needs_stop(hot, state)


TAB_NOW = dt.datetime(2026, 10, 13, 22, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))


def tab(index, opened_days_ago, idle_days_ago, url=None):
    return {
        "targetId": "tab_%04d" % index,
        "url": url or "https://example.com/page/%d" % index,
        "openedAt": (TAB_NOW - dt.timedelta(days=opened_days_ago)).isoformat(timespec="seconds"),
        "lastActivityAt": (TAB_NOW - dt.timedelta(days=idle_days_ago)).isoformat(timespec="seconds"),
        "openedBy": "api",
    }


def tab_status(tabs):
    return BrowserStatus(server_running=True, tab_count=len(tabs), tabs=tabs)


def test_tab_pile_headsup_only_above_threshold_lists_age_and_truncated_url():
    now_ts = TAB_NOW.timestamp()
    at_limit = [tab(i, 1, 0) for i in range(config.BROWSER_TABS_HEADSUP)]
    assert evaluate.evaluate_tab_pile(tab_status(at_limit), now_ts) is None
    long_url = "https://example.com/" + "x" * 200
    pile = [tab(i, 2, 0) for i in range(29)] + [tab(99, 0.25, 0, url=long_url)]
    finding = evaluate.evaluate_tab_pile(tab_status(pile), now_ts)
    assert finding.check_id == evaluate.CHECK_BROWSER_TABS and finding.severity == Severity.WARN
    assert finding.title == "30 stealth-browser tabs open." and not finding.action_taken
    assert finding.cooldown_seconds == config.BROWSER_TABS_HEADSUP_COOLDOWN_S
    assert finding.details[0] == "2d · https://example.com/page/0"
    assert len(finding.details) == config.BROWSER_TAB_LIST_LIMIT + 1
    assert finding.details[-1] == "... and 5 more"
    assert evaluate.evaluate_tab_pile(BrowserStatus(server_running=False, tab_count=50), now_ts) is None


def test_idle_tabs_over_a_week_listed_with_ids_and_reply_hint():
    now_ts = TAB_NOW.timestamp()
    tabs = [tab(1, 10, 9), tab(2, 10, 6), tab(3, 30, 8), {"targetId": "tab_bad", "lastActivityAt": "garbage"}]
    finding = evaluate.evaluate_idle_tabs(tab_status(tabs), now_ts)
    assert finding.title == "2 tabs idle for over a week."
    assert finding.details[0] == "tab_0001 · idle 9d · https://example.com/page/1"
    assert finding.details[1].startswith("tab_0003 · idle 8d")
    assert finding.details[-1] == 'Reply to Mineru: "close idle tabs", or the tab ids to close.'
    assert not finding.action_taken and finding.cooldown_seconds == config.BROWSER_IDLE_TABS_COOLDOWN_S
    assert evaluate.evaluate_idle_tabs(tab_status([tab(1, 10, 6)]), now_ts) is None


def test_per_finding_cooldown_overrides_severity_default():
    state = default_state()
    finding = evaluate.evaluate_idle_tabs(tab_status([tab(1, 10, 9)]), TAB_NOW.timestamp())
    assert decide_alerts([finding], state, now_ts=0).to_send
    past_warn_default = config.WARN_COOLDOWN_SECONDS + 60
    assert not decide_alerts([finding], state, now_ts=past_warn_default).to_send
    assert decide_alerts([finding], state, now_ts=config.BROWSER_IDLE_TABS_COOLDOWN_S).to_send


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
    # Every pid already a known survivor: nothing happened, no action finding.
    assert evaluate.hung_jobs_finding(KillReport([candidate], dry_run=False, known_survivors=[100, 101])) is None


def test_survivor_realert_cadence_is_one_critical_per_cooldown():
    state = default_state()
    sent = []
    for run in range(6):
        standing = evaluate.survivors_finding([4242])
        decision = decide_alerts([standing], state, NOW + run * 300)
        sent.append(len(decision.to_send))
    assert sent == [1, 0, 0, 0, 0, 0]
    assert standing.severity == Severity.CRITICAL and not standing.action_taken
    # Cooldown expiry re-alerts; disappearance announces one recovery.
    assert decide_alerts([standing], state, NOW + config.CRITICAL_COOLDOWN_SECONDS).to_send == [standing]
    assert decide_alerts([], state, NOW + config.CRITICAL_COOLDOWN_SECONDS + 300).recovered == ["hung_jobs_survivor"]
    assert evaluate.survivors_finding([]) is None


def test_kill_with_survivors_seeds_the_survivor_cooldown():
    state = default_state()
    candidate = HungJobCandidate(root_pid=600, age_seconds=4 * 3600, command="claude-fda --print")
    kill = evaluate.hung_jobs_finding(KillReport([candidate], dry_run=False, killed=[], survivors=[600]))
    assert kill.also_cools == ["hung_jobs_survivor"]
    assert decide_alerts([kill], state, NOW).to_send == [kill]
    standing = evaluate.survivors_finding([600])
    assert decide_alerts([standing], state, NOW + 300).to_send == []
    assert decide_alerts([standing], state, NOW + config.CRITICAL_COOLDOWN_SECONDS).to_send == [standing]
    # A clean kill seeds nothing.
    assert evaluate.hung_jobs_finding(KillReport([candidate], dry_run=False, killed=[600])).also_cools == []


def test_orphaned_browser_finding():
    finding = evaluate.orphaned_browser_finding(3, killed=[1, 2, 3], survivors=[], dry_run=False)
    assert finding.severity == Severity.WARN and finding.action_taken and "3 orphaned" in finding.title
    assert evaluate.orphaned_browser_finding(1, [], [9], dry_run=False).severity == Severity.CRITICAL
    assert evaluate.orphaned_browser_finding(1, [], [], dry_run=True).title.startswith("Would terminate")


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


def test_warn_recovery_is_silent_and_keeps_cooldown():
    state = default_state()
    decide_alerts([warn()], state, NOW)
    recovered = decide_alerts([], state, NOW + 300)
    assert recovered.recovered == [] and recovered.to_send == []
    # Re-occurrence inside the cooldown stays quiet; after it, alerts again.
    assert decide_alerts([warn()], state, NOW + 900).to_send == []
    assert decide_alerts([warn()], state, NOW + config.WARN_COOLDOWN_SECONDS).to_send == [warn()]


def test_critical_recovery_announced_once_per_sent_alert():
    state = default_state()
    decide_alerts([critical()], state, NOW)
    assert decide_alerts([], state, NOW + 300).recovered == ["memory"]
    assert decide_alerts([], state, NOW + 600).recovered == []
    # Flap back inside the cooldown: suppressed, and its clearing is not re-announced.
    assert decide_alerts([critical()], state, NOW + 900).to_send == []
    assert decide_alerts([], state, NOW + 1200).recovered == []
    # After the cooldown a new alert is sent and its recovery is announced again.
    assert decide_alerts([critical()], state, NOW + config.CRITICAL_COOLDOWN_SECONDS).to_send == [critical()]
    assert decide_alerts([], state, NOW + config.CRITICAL_COOLDOWN_SECONDS + 300).recovered == ["memory"]


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
