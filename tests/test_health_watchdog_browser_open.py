"""Tests for the page-open probe schedule and verdict (evaluate_browser_open.py)."""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from health_watchdog import config, evaluate_browser_open as probe_logic  # noqa: E402
from health_watchdog.browser_hygiene import BrowserStatus, PageOpenProbe  # noqa: E402
from health_watchdog.findings import Severity  # noqa: E402
from health_watchdog.state import decide_alerts, default_state  # noqa: E402

NOW = 1_000_000.0
IDLE_SERVER = BrowserStatus(server_running=True, tab_count=0)
FAILED = PageOpenProbe(False, "open failed: HTTP 500: No module named 'playwright'")


def test_first_run_probes_then_waits_for_interval_after_a_pass():
    state = default_state()
    assert probe_logic.probe_is_due(IDLE_SERVER, state, NOW)
    probe_logic.record_probe(state, PageOpenProbe(True, "opened"), NOW)
    assert not probe_logic.probe_is_due(IDLE_SERVER, state, NOW + 60)
    assert probe_logic.probe_is_due(IDLE_SERVER, state, NOW + config.BROWSER_OPEN_PROBE_INTERVAL_SECONDS)


def test_never_probes_with_server_down_or_tabs_open_and_clears_old_failure():
    state = default_state()
    probe_logic.record_probe(state, FAILED, NOW)
    assert not probe_logic.probe_is_due(BrowserStatus(server_running=True, tab_count=2), state, NOW)
    assert state[probe_logic.STATE_KEY] is None
    probe_logic.record_probe(state, FAILED, NOW)
    assert not probe_logic.probe_is_due(BrowserStatus(server_running=False), state, NOW)
    assert probe_logic.evaluate_page_open(state, None) is None


def test_alerts_critical_only_after_sustained_failures_and_keeps_alerting_between_probes():
    state = default_state()
    probe_logic.record_probe(state, FAILED, NOW)
    assert probe_logic.evaluate_page_open(state, "/py") is None
    assert probe_logic.probe_is_due(IDLE_SERVER, state, NOW + 300)  # failing: re-probe every run
    probe_logic.record_probe(state, FAILED, NOW + 300)
    finding = probe_logic.evaluate_page_open(state, "/opt/homebrew/bin/python3.15")
    assert finding.severity == Severity.CRITICAL
    assert any("playwright" in line for line in finding.details)
    assert any("python3.15" in line for line in finding.details)
    # A run that did not probe still re-emits the stored failure (no false recovery).
    assert probe_logic.evaluate_page_open(state, None) is not None


def test_pass_after_alert_sends_recovered_line():
    state = default_state()
    for offset in (0, 300):
        probe_logic.record_probe(state, FAILED, NOW + offset)
    decide_alerts([probe_logic.evaluate_page_open(state, None)], state, NOW + 300)
    probe_logic.record_probe(state, PageOpenProbe(True, "opened"), NOW + 600)
    assert probe_logic.evaluate_page_open(state, None) is None
    decision = decide_alerts([], state, NOW + 600)
    assert decision.recovered == [probe_logic.CHECK_BROWSER_OPEN]
