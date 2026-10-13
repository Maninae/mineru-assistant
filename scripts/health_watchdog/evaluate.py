"""Threshold logic: raw probe data in, Findings out.

Python 3.9 compatible (see config.py).

Every function here is pure apart from `state["streaks"]`, which the sustained
checks (tccd CPU, load, renderer CPU) update through bump_streak. Nothing in this
module runs a command, so the whole threshold surface is unit-testable with fakes.
"""

from typing import Any, Dict, List, Optional

from health_watchdog.browser_hygiene import BrowserStatus
from health_watchdog.config import (
    BROWSER_RENDERER_CPU_PERCENT,
    BROWSER_RENDERER_CPU_SUSTAINED_RUNS,
    DISK_FREE_CRITICAL_GB,
    DISK_FREE_WARN_GB,
    LANDLINE_SILENT_WARN_COUNT,
    LANDLINE_SILENT_WINDOW_SECONDS,
    LOAD_PER_CORE_CRITICAL,
    LOAD_PER_CORE_WARN,
    LOAD_SUSTAINED_RUNS,
    MAX_BROWSER_TABS,
    MEMORY_FREE_CRITICAL_PERCENT,
    MEMORY_FREE_WARN_PERCENT,
    OFFLOAD_VOLUME_HINT,
    SWAP_USED_CRITICAL_FRACTION,
    SWAP_USED_WARN_FRACTION,
    TCCD_CPU_SUSTAINED_RUNS,
    TCCD_CPU_WARN_PERCENT,
    TCC_FIX_TEXT,
)
from health_watchdog.findings import Finding, Severity
from health_watchdog.hung_jobs import KillReport
from health_watchdog.state import bump_streak
from health_watchdog.system_probes import SystemSnapshot, health_snapshot_line
from health_watchdog.tcc_probe import STATUS_ERROR, STATUS_HUNG, TccProbeResult

CHECK_TCC = "tcc"
CHECK_TCCD_CPU = "tccd_cpu"
CHECK_MEMORY = "memory"
CHECK_LOAD = "load"
CHECK_DISK = "disk"
CHECK_LANDLINE = "landline"
CHECK_BROWSER = "browser"
CHECK_HUNG_JOBS = "hung_jobs"

STREAK_TCCD_CPU = "tccd_cpu"
STREAK_LOAD_WARN = "load_warn"
STREAK_LOAD_CRITICAL = "load_critical"
STREAK_RENDERER_CPU = "renderer_cpu"


def evaluate_tcc(result: TccProbeResult) -> Optional[Finding]:
    """Hung probe -> CRITICAL with the fix; probe error -> WARN (config problem)."""
    if result.status == STATUS_HUNG:
        return Finding(
            CHECK_TCC, Severity.CRITICAL,
            "macOS permission daemon (tccd) is stuck; protected file reads are freezing.",
            [result.detail, TCC_FIX_TEXT],
        )
    if result.status == STATUS_ERROR:
        return Finding(
            CHECK_TCC, Severity.WARN,
            "TCC probe cannot read its protected file (permission/config issue, not a freeze).",
            [result.detail],
        )
    return None


def evaluate_tccd_cpu(snapshot: SystemSnapshot, state: Dict[str, Any]) -> Optional[Finding]:
    """Either tccd above the CPU threshold for N consecutive runs -> WARN."""
    hottest = max(snapshot.tccd_root_cpu or 0.0, snapshot.tccd_user_cpu or 0.0)
    streak = bump_streak(state, STREAK_TCCD_CPU, hottest >= TCCD_CPU_WARN_PERCENT)
    if streak < TCCD_CPU_SUSTAINED_RUNS:
        return None
    return Finding(
        CHECK_TCCD_CPU, Severity.WARN,
        "tccd has been burning %.0f%% CPU for %d checks in a row." % (hottest, streak),
        ["root tccd %.0f%%, user tccd %.0f%%" % (snapshot.tccd_root_cpu or 0.0, snapshot.tccd_user_cpu or 0.0),
         TCC_FIX_TEXT],
    )


def evaluate_memory(snapshot: SystemSnapshot, top_rss: List[str]) -> Optional[Finding]:
    """Swap fraction and free-memory percent; worst of the two sets the severity."""
    severity = None  # type: Optional[Severity]
    reasons = []  # type: List[str]
    swap = snapshot.swap_used_fraction
    if swap is not None:
        if swap >= SWAP_USED_CRITICAL_FRACTION:
            severity = Severity.CRITICAL
        elif swap >= SWAP_USED_WARN_FRACTION:
            severity = Severity.WARN
        if severity is not None:
            reasons.append("swap %.0f%% used (%.1f of %.1f GB)" % (
                swap * 100, snapshot.swap_used_mb / 1024, snapshot.swap_total_mb / 1024))
    free = snapshot.memory_free_percent
    if free is not None:
        free_severity = None  # type: Optional[Severity]
        if free <= MEMORY_FREE_CRITICAL_PERCENT:
            free_severity = Severity.CRITICAL
        elif free <= MEMORY_FREE_WARN_PERCENT:
            free_severity = Severity.WARN
        if free_severity is not None:
            reasons.append("only %d%% memory free" % free)
            if severity is None or free_severity.rank > severity.rank:
                severity = free_severity
    if severity is None:
        return None
    return Finding(CHECK_MEMORY, severity, "Memory pressure: " + "; ".join(reasons) + ".",
                   ["Top RSS: " + ", ".join(top_rss)] if top_rss else [])


def evaluate_load(snapshot: SystemSnapshot, state: Dict[str, Any], top_cpu: List[str]) -> Optional[Finding]:
    """5-min load per core sustained for N runs -> WARN; the higher bar -> CRITICAL."""
    per_core = snapshot.load_per_core
    warn_streak = bump_streak(state, STREAK_LOAD_WARN, per_core is not None and per_core >= LOAD_PER_CORE_WARN)
    critical_streak = bump_streak(
        state, STREAK_LOAD_CRITICAL, per_core is not None and per_core >= LOAD_PER_CORE_CRITICAL)
    if warn_streak < LOAD_SUSTAINED_RUNS or per_core is None:
        return None
    severity = Severity.CRITICAL if critical_streak >= LOAD_SUSTAINED_RUNS else Severity.WARN
    return Finding(
        CHECK_LOAD, severity,
        "Load average %.1f on %d cores (%.1fx) for %d checks in a row." % (
            snapshot.load_5m, snapshot.ncpu, per_core, warn_streak),
        ["Top CPU: " + ", ".join(top_cpu)] if top_cpu else [],
    )


def evaluate_disk(snapshot: SystemSnapshot) -> Optional[Finding]:
    """Free space on / under the thresholds."""
    free = snapshot.disk_free_gb
    if free is None or free >= DISK_FREE_WARN_GB:
        return None
    severity = Severity.CRITICAL if free < DISK_FREE_CRITICAL_GB else Severity.WARN
    return Finding(CHECK_DISK, severity, "Only %.1f GB free on /." % free,
                   [OFFLOAD_VOLUME_HINT] if OFFLOAD_VOLUME_HINT else [])


def evaluate_landline(silent_kill_count: int, snapshot: SystemSnapshot) -> Optional[Finding]:
    """Repeated daemon-side silent kills -> WARN with a health snapshot."""
    if silent_kill_count < LANDLINE_SILENT_WARN_COUNT:
        return None
    return Finding(
        CHECK_LANDLINE, Severity.WARN,
        "Landline killed a silent Claude %d times in the last %dh." % (
            silent_kill_count, LANDLINE_SILENT_WINDOW_SECONDS // 3600),
        ["Snapshot: " + health_snapshot_line(snapshot)],
    )


def browser_needs_stop(status: BrowserStatus, state: Dict[str, Any]) -> Optional[str]:
    """Reason to stop the browser (tab leak or sustained hot renderer), or None."""
    hot = status.server_running and status.hottest_renderer_cpu >= BROWSER_RENDERER_CPU_PERCENT
    renderer_streak = bump_streak(state, STREAK_RENDERER_CPU, hot)
    if not status.server_running:
        return None
    if status.tab_count > MAX_BROWSER_TABS:
        return "%d tabs open (limit %d)" % (status.tab_count, MAX_BROWSER_TABS)
    if renderer_streak >= BROWSER_RENDERER_CPU_SUSTAINED_RUNS:
        return "a renderer at %.0f%% CPU for %d checks in a row" % (status.hottest_renderer_cpu, renderer_streak)
    return None


def browser_finding(status: BrowserStatus, reason: str, stop_summary: str) -> Finding:
    """Report a browser stop (or the dry-run intent) with what was open."""
    return Finding(
        CHECK_BROWSER, Severity.WARN,
        "Stopped the stealth browser: %s." % reason,
        ["Stop: " + stop_summary] + (["Open: " + "; ".join(status.tab_summaries)] if status.tab_summaries else []),
        action_taken=True,
    )


def hung_jobs_finding(report: KillReport) -> Finding:
    """Describe what was killed; survivors of SIGKILL escalate to CRITICAL (tccd evidence)."""
    killed_lines = [
        "%s (pid %d, %dh%02dm old, %d child procs)" % (
            candidate.name, candidate.root_pid, candidate.age_seconds // 3600,
            (candidate.age_seconds % 3600) // 60, len(candidate.descendant_pids))
        for candidate in report.candidates
    ]
    verb = "Would kill" if report.dry_run else "Killed"
    details = ["%s: %s" % (verb, "; ".join(killed_lines))]
    severity = Severity.WARN
    if report.survivors:
        severity = Severity.CRITICAL
        details.append(
            "%d process(es) survived SIGKILL (pids %s): strong evidence tccd is frozen. %s" % (
                len(report.survivors), ", ".join(str(pid) for pid in report.survivors), TCC_FIX_TEXT))
    return Finding(
        CHECK_HUNG_JOBS, severity,
        "%s %d hung scheduled job(s)." % (verb, len(report.candidates)),
        details, action_taken=True,
    )
