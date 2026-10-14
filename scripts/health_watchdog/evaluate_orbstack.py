"""OrbStack threshold logic: OrbstackHealth in, Findings and a restart decision out.

Python 3.9 compatible (see config.py).

Pure like evaluate.py (only `state["streaks"]` and the restart timestamp are touched),
kept in its own module so evaluate.py stays a readable size.

- Wedged = helper CPU at or above the bar for N runs, or the liveness probe hung for
  N runs. With auto-restart enabled (profile key `orbstack_auto_restart`), off
  cooldown, and no stuck-exit helper, the caller restarts instead of only warning.
- Stuck exit = a helper with STAT "E" for N runs: CRITICAL, never a restart (orb
  stop/start cannot clear a process the kernel will not let die).
- Stopped / not installed are deliberate states: no finding.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from health_watchdog.config import (
    ORBSTACK_CPU_SUSTAINED_RUNS,
    ORBSTACK_HELPER_CPU_WARN_PERCENT,
    ORBSTACK_HUNG_SUSTAINED_RUNS,
    ORBSTACK_RESTART_COOLDOWN_S,
    ORBSTACK_STUCK_EXIT_SUSTAINED_RUNS,
)
from health_watchdog.findings import Finding, Severity
from health_watchdog.orbstack_health import (
    PROBE_HUNG,
    PROBE_NOT_INSTALLED,
    PROBE_NOT_RUNNING,
    OrbstackHealth,
    RestartReport,
)
from health_watchdog.process_table import outcome_summary
from health_watchdog.state import bump_streak

CHECK_ORBSTACK = "orbstack"
CHECK_ORBSTACK_STUCK = "orbstack_stuck_exit"
STREAK_ORBSTACK_CPU = "orbstack_cpu"
STREAK_ORBSTACK_HUNG = "orbstack_hung"
STREAK_ORBSTACK_STUCK = "orbstack_stuck_exit"
LAST_RESTART_KEY = "orbstack_last_restart_ts"
KILOBYTES_PER_GIGABYTE = 1024 * 1024
MANUAL_FIX_TEXT = "Fix: `orb stop && orb start` restarts the VM (a Mac reboot only if that fails)."
STUCK_EXIT_TITLE = "OrbStack helper stuck exiting (STAT E): unkillable; the Mac needs a reboot."


@dataclass
class OrbstackVerdict:
    """Findings to report, plus the reason to restart now (None = do not restart)."""

    findings: List[Finding] = field(default_factory=list)
    restart_reason: Optional[str] = None


def restart_allowed(state: Dict[str, Any], now_ts: float) -> bool:
    """At most one automatic restart per ORBSTACK_RESTART_COOLDOWN_S."""
    last = state.get(LAST_RESTART_KEY)
    return last is None or now_ts - float(last) >= ORBSTACK_RESTART_COOLDOWN_S


def record_restart(state: Dict[str, Any], now_ts: float) -> None:
    """Start the restart cooldown (the caller skips this on a dry run)."""
    state[LAST_RESTART_KEY] = now_ts


def helper_stats_line(health: OrbstackHealth) -> str:
    """"OrbStack Helper: 340% CPU, 1.2 GB RSS (pids 1, 2)" for alert details."""
    return "OrbStack Helper: %.0f%% CPU, %.1f GB RSS (pids %s)" % (
        health.helper_cpu_percent, health.helper_rss_kb / KILOBYTES_PER_GIGABYTE,
        ", ".join(str(row.pid) for row in health.helpers) or "none")


def evaluate_orbstack(
    health: OrbstackHealth, state: Dict[str, Any], now_ts: float, auto_restart_enabled: bool,
) -> OrbstackVerdict:
    """Bump the three streaks and decide what to report or do this run."""
    verdict = OrbstackVerdict()
    installed = health.liveness != PROBE_NOT_INSTALLED
    running = installed and health.liveness != PROBE_NOT_RUNNING
    stuck_streak = bump_streak(state, STREAK_ORBSTACK_STUCK, installed and bool(health.stuck_exiting_pids))
    cpu_streak = bump_streak(
        state, STREAK_ORBSTACK_CPU, running and health.helper_cpu_percent >= ORBSTACK_HELPER_CPU_WARN_PERCENT)
    hung_streak = bump_streak(state, STREAK_ORBSTACK_HUNG, health.liveness == PROBE_HUNG)

    if stuck_streak >= ORBSTACK_STUCK_EXIT_SUSTAINED_RUNS:
        verdict.findings.append(Finding(
            CHECK_ORBSTACK_STUCK, Severity.CRITICAL, STUCK_EXIT_TITLE,
            ["Stuck helper pid(s) %s for %d checks in a row; no automatic restart, since "
             "`orb stop/start` cannot clear a process stuck in exit." % (
                 ", ".join(str(pid) for pid in health.stuck_exiting_pids), stuck_streak)],
        ))
        return verdict

    reasons = []  # type: List[str]
    if cpu_streak >= ORBSTACK_CPU_SUSTAINED_RUNS:
        reasons.append("helper at %.0f%% CPU for %d checks in a row" % (health.helper_cpu_percent, cpu_streak))
    if hung_streak >= ORBSTACK_HUNG_SUSTAINED_RUNS:
        reasons.append("%s (%d checks in a row)" % (health.detail, hung_streak))
    if not reasons:
        return verdict
    reason = "; ".join(reasons)

    if auto_restart_enabled and restart_allowed(state, now_ts):
        verdict.restart_reason = reason
        return verdict

    details = [helper_stats_line(health)]
    if auto_restart_enabled:
        next_allowed = float(state.get(LAST_RESTART_KEY) or 0) + ORBSTACK_RESTART_COOLDOWN_S
        details.append("Auto-restart already used; next one allowed after %s." % (
            dt.datetime.fromtimestamp(next_allowed).strftime("%H:%M")))
    details.append(MANUAL_FIX_TEXT)
    verdict.findings.append(Finding(CHECK_ORBSTACK, Severity.WARN, "OrbStack looks wedged: %s." % reason, details))
    return verdict


def restart_finding(reason: str, report: Optional[RestartReport], dry_run: bool) -> Finding:
    """Report a restart (or the dry-run intent); a VM that did not come back is CRITICAL."""
    if dry_run or report is None:
        return Finding(CHECK_ORBSTACK, Severity.WARN, "Would restart OrbStack: %s." % reason,
                       ["dry run, not restarted"], action_taken=True)
    steps = "stop: %s; start: %s" % (
        outcome_summary(report.stop) if report.stop else "skipped",
        outcome_summary(report.start) if report.start else "skipped")
    if report.came_back:
        return Finding(CHECK_ORBSTACK, Severity.WARN, "Restarted OrbStack: %s." % reason,
                       [steps, "After restart: " + report.detail_after], action_taken=True)
    return Finding(
        CHECK_ORBSTACK, Severity.CRITICAL, "OrbStack did not come back after restart; reboot the Mac.",
        ["Restarted because: " + reason, steps, "After restart: %s (%s)" % (report.liveness_after, report.detail_after)],
        action_taken=True,
    )
