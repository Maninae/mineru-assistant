"""Threshold logic: raw probe data in, Findings out.

Python 3.9 compatible (see config.py).

Every function here is pure apart from `state["streaks"]`, which the sustained
checks (tccd CPU, load, renderer CPU) update through bump_streak. OrbStack's
threshold logic lives in evaluate_orbstack.py under the same rules. Nothing in this
module runs a command, so the whole threshold surface is unit-testable with fakes.
"""

from typing import Any, Dict, List, Optional, Tuple

from health_watchdog.browser_hygiene import BrowserStatus, compact_tab_url, format_age, parse_tab_timestamp
from health_watchdog.config import (
    BROWSER_IDLE_TAB_DAYS,
    BROWSER_IDLE_TABS_COOLDOWN_S,
    BROWSER_ORPHAN_SUSTAINED_RUNS,
    BROWSER_RENDERER_CPU_PERCENT,
    BROWSER_RENDERER_CPU_SUSTAINED_RUNS,
    BROWSER_TAB_PATH_TAIL_CHARS,
    BROWSER_TAB_SUMMARY_LIMIT,
    BROWSER_TABS_HEADSUP,
    BROWSER_TABS_HEADSUP_COOLDOWN_S,
    DISK_FREE_CRITICAL_GB,
    DISK_FREE_WARN_GB,
    LANDLINE_SILENT_WARN_COUNT,
    LANDLINE_SILENT_WINDOW_SECONDS,
    LOAD_PER_CORE_CLEAR,
    LOAD_PER_CORE_CRITICAL,
    LOAD_PER_CORE_WARN,
    LOAD_SUSTAINED_RUNS,
    MEMORY_FREE_CLEAR_PERCENT,
    MEMORY_FREE_CRITICAL_PERCENT,
    MEMORY_FREE_WARN_PERCENT,
    MEMORY_SUSTAINED_RUNS,
    OFFLOAD_VOLUME_HINT,
    SWAP_USED_CLEAR_FRACTION,
    SWAP_USED_CRITICAL_FRACTION,
    SWAP_USED_WARN_FRACTION,
    TCCD_CPU_SUSTAINED_RUNS,
    TCCD_CPU_WARN_PERCENT,
    TCC_FIX_TEXT,
)
from health_watchdog.findings import Finding, Severity
from health_watchdog.hung_jobs import KillReport
from health_watchdog.state import bump_streak, hysteresis_flag
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
CHECK_SURVIVORS = "hung_jobs_survivor"
CHECK_BROWSER_ORPHANS = "browser_orphans"
CHECK_BROWSER_TABS = "browser_tabs"
CHECK_BROWSER_IDLE_TABS = "browser_idle_tabs"
IDLE_TABS_REPLY_HINT = 'Reply to Mineru: "close idle tabs", or the tab ids to close.'
SECONDS_PER_DAY = 86400

STREAK_TCCD_CPU = "tccd_cpu"
STREAK_MEMORY = "memory"
STREAK_LOAD_WARN = "load_warn"
STREAK_LOAD_CRITICAL = "load_critical"
STREAK_RENDERER_CPU = "renderer_cpu"
STREAK_BROWSER_ORPHANS = "browser_orphans"
FLAG_MEMORY = "memory_bad"
FLAG_LOAD = "load_bad"


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


def evaluate_memory(snapshot: SystemSnapshot, state: Dict[str, Any], top_rss: List[str]) -> Optional[Finding]:
    """Swap fraction and free-memory percent, latched with hysteresis and sustained.

    The check turns on at the WARN bars, stays on until BOTH metrics are under their
    CLEAR bars, and alerts only after MEMORY_SUSTAINED_RUNS consecutive latched runs.
    """
    swap = snapshot.swap_used_fraction
    free = snapshot.memory_free_percent
    swap_bad = swap is not None and swap >= SWAP_USED_WARN_FRACTION
    free_bad = free is not None and free <= MEMORY_FREE_WARN_PERCENT
    critical = (swap is not None and swap >= SWAP_USED_CRITICAL_FRACTION) or (
        free is not None and free <= MEMORY_FREE_CRITICAL_PERCENT)
    clear = (swap is None or swap < SWAP_USED_CLEAR_FRACTION) and (free is None or free > MEMORY_FREE_CLEAR_PERCENT)
    latched = hysteresis_flag(state, FLAG_MEMORY, swap_bad or free_bad, clear)
    streak = bump_streak(state, STREAK_MEMORY, latched)
    if not latched or streak < MEMORY_SUSTAINED_RUNS:
        return None

    reasons = []  # type: List[str]
    if swap is not None:
        reasons.append("swap %.0f%% used (%.1f of %.1f GB)" % (
            swap * 100, snapshot.swap_used_mb / 1024, snapshot.swap_total_mb / 1024))
    if free is not None:
        reasons.append("%d%% memory free" % free)
    return Finding(
        CHECK_MEMORY, Severity.CRITICAL if critical else Severity.WARN,
        "Memory pressure for %d checks in a row: %s." % (streak, "; ".join(reasons)),
        ["Top RSS: " + ", ".join(top_rss)] if top_rss else [],
    )


def evaluate_load(snapshot: SystemSnapshot, state: Dict[str, Any], top_cpu: List[str]) -> Optional[Finding]:
    """5-min load per core, latched (on at WARN, off under CLEAR) and sustained.

    LOAD_SUSTAINED_RUNS latched runs -> WARN; the same number of runs at or above the
    CRITICAL bar -> CRITICAL.
    """
    per_core = snapshot.load_per_core
    latched = hysteresis_flag(
        state, FLAG_LOAD,
        per_core is not None and per_core >= LOAD_PER_CORE_WARN,
        per_core is None or per_core < LOAD_PER_CORE_CLEAR,
    )
    warn_streak = bump_streak(state, STREAK_LOAD_WARN, latched)
    critical_streak = bump_streak(
        state, STREAK_LOAD_CRITICAL, per_core is not None and per_core >= LOAD_PER_CORE_CRITICAL)
    if not latched or warn_streak < LOAD_SUSTAINED_RUNS or per_core is None:
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
    """Reason to stop the browser (a renderer hot for N runs), or None.

    Tab count is deliberately NOT a reason: tabs may sit open for days, so piles are
    surfaced by evaluate_tab_pile / evaluate_idle_tabs instead of reaped.
    """
    hot = status.server_running and status.hottest_renderer_cpu >= BROWSER_RENDERER_CPU_PERCENT
    renderer_streak = bump_streak(state, STREAK_RENDERER_CPU, hot)
    if not status.server_running:
        return None
    if renderer_streak >= BROWSER_RENDERER_CPU_SUSTAINED_RUNS:
        return "a renderer at %.0f%% CPU for %d checks in a row" % (status.hottest_renderer_cpu, renderer_streak)
    return None


def split_idle_tabs(tabs: List[Dict[str, Any]], now_ts: float) -> Tuple[List[Tuple[Dict[str, Any], float]], List[Dict[str, Any]]]:
    """(idle tabs with their last-activity ts, the rest); idle = quiet BROWSER_IDLE_TAB_DAYS or more."""
    idle_cutoff = now_ts - BROWSER_IDLE_TAB_DAYS * SECONDS_PER_DAY
    idle, active = [], []  # type: Tuple[List[Tuple[Dict[str, Any], float]], List[Dict[str, Any]]]
    for tab in tabs:
        last_ts = parse_tab_timestamp(tab.get("lastActivityAt"))
        if last_ts is not None and last_ts < idle_cutoff:
            idle.append((tab, last_ts))
        else:
            active.append(tab)
    return idle, active


def capped_lines(lines: List[str], limit: int = BROWSER_TAB_SUMMARY_LIMIT) -> List[str]:
    """First `limit` lines plus a "… and N more" line when there are more."""
    if len(lines) <= limit:
        return lines
    return lines[:limit] + ["… and %d more" % (len(lines) - limit)]


def evaluate_tab_pile(status: BrowserStatus, now_ts: float) -> Optional[Finding]:
    """More than BROWSER_TABS_HEADSUP tabs -> a daily WARN. No action.

    Idle tabs are counted here but listed only by evaluate_idle_tabs, so no tab is
    listed twice in one message.
    """
    if not status.server_running or status.tab_count <= BROWSER_TABS_HEADSUP:
        return None
    idle, active = split_idle_tabs(status.tabs, now_ts)
    details = []  # type: List[str]
    if idle:
        details.append("%d of them idle over a week (listed separately); the others:" % len(idle))
    tab_lines = []  # type: List[str]
    for tab in active:
        opened_ts = parse_tab_timestamp(tab.get("openedAt"))
        age = format_age(now_ts - opened_ts) if opened_ts is not None else "?"
        tab_lines.append("%s · %s" % (age, compact_tab_url(tab.get("url"), BROWSER_TAB_PATH_TAIL_CHARS)))
    details.extend(capped_lines(tab_lines))
    return Finding(CHECK_BROWSER_TABS, Severity.WARN, "%d stealth-browser tabs open." % status.tab_count,
                   details, cooldown_seconds=BROWSER_TABS_HEADSUP_COOLDOWN_S)


def evaluate_idle_tabs(status: BrowserStatus, now_ts: float) -> Optional[Finding]:
    """Tabs with no activity for BROWSER_IDLE_TAB_DAYS -> a daily WARN with their ids. No action."""
    if not status.server_running:
        return None
    idle, _active = split_idle_tabs(status.tabs, now_ts)
    if not idle:
        return None
    tab_lines = [
        "%s · idle %s · %s" % (tab.get("targetId", "?"), format_age(now_ts - last_ts),
                               compact_tab_url(tab.get("url"), BROWSER_TAB_PATH_TAIL_CHARS))
        for tab, last_ts in idle
    ]
    return Finding(CHECK_BROWSER_IDLE_TABS, Severity.WARN, "%d tabs idle for over a week." % len(idle),
                   capped_lines(tab_lines) + [IDLE_TABS_REPLY_HINT],
                   cooldown_seconds=BROWSER_IDLE_TABS_COOLDOWN_S)


def browser_finding(status: BrowserStatus, reason: str, stop_summary: str) -> Finding:
    """Report a browser stop (or the dry-run intent) with what was open."""
    return Finding(
        CHECK_BROWSER, Severity.WARN,
        "Stopped the stealth browser: %s." % reason,
        ["Stop: " + stop_summary] + (["Open: " + "; ".join(status.tab_summaries)] if status.tab_summaries else []),
        action_taken=True,
    )


def hung_jobs_finding(report: KillReport) -> Optional[Finding]:
    """Describe what was signalled this run; NEW SIGKILL survivors escalate to CRITICAL.

    Returns None when every pid was an already-reported survivor (nothing was done);
    those are covered by survivors_finding under the CRITICAL cooldown instead.
    """
    if not report.signalled_anything:
        return None
    known = set(report.known_survivors)
    killed_lines = [
        "%s (pid %d, %dh%02dm old, %d child procs)" % (
            candidate.name, candidate.root_pid, candidate.age_seconds // 3600,
            (candidate.age_seconds % 3600) // 60, len(candidate.descendant_pids))
        for candidate in report.candidates
        if any(pid not in known for pid in candidate.all_pids)
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
        "%s %d hung scheduled job(s)." % (verb, len(killed_lines)),
        details, action_taken=True,
        # This message already covers the survivors; start their standing check's
        # cooldown so it does not repeat the news five minutes later.
        also_cools=[CHECK_SURVIVORS] if report.survivors else [],
    )


def survivors_finding(survivor_pids: List[int]) -> Optional[Finding]:
    """Standing CRITICAL while previously reported SIGKILL survivors are still alive.

    Not an action finding, so the CRITICAL cooldown applies and it recovers once the
    processes finally disappear (after `sudo killall tccd` or a reboot).
    """
    if not survivor_pids:
        return None
    return Finding(
        CHECK_SURVIVORS, Severity.CRITICAL,
        "%d hung-job process(es) still alive after SIGKILL (pids %s); not re-signalling." % (
            len(survivor_pids), ", ".join(str(pid) for pid in survivor_pids)),
        ["Unkillable processes mean tccd is frozen. " + TCC_FIX_TEXT],
    )


def browser_orphans_confirmed(orphan_count: int, state: Dict[str, Any]) -> bool:
    """Orphaned profile Chromium must be seen on consecutive runs before it is terminated."""
    streak = bump_streak(state, STREAK_BROWSER_ORPHANS, orphan_count > 0)
    return orphan_count > 0 and streak >= BROWSER_ORPHAN_SUSTAINED_RUNS


def orphaned_browser_finding(process_count: int, killed: List[int], survivors: List[int], dry_run: bool) -> Finding:
    """Stealth-profile Chromium processes found with the browser server down."""
    verb = "Would terminate" if dry_run else "Terminated"
    details = []  # type: List[str]
    if not dry_run:
        details.append("killed %d, survived SIGKILL %d" % (len(killed), len(survivors)))
    return Finding(
        CHECK_BROWSER_ORPHANS, Severity.CRITICAL if survivors else Severity.WARN,
        "%s %d orphaned stealth-browser process(es) (server not running)." % (verb, process_count),
        details, action_taken=True,
    )
