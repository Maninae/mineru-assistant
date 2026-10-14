"""Entry point: one watchdog run (probe, evaluate, act, alert, persist).

Python 3.9 compatible (see config.py).

Run order matters: the cheap kernel-level reads (ps, launchctl, sysctl) go first
so they are in hand even if the TCC probe hangs; the hung-job killer and browser
stop are the only actions, both honoured by DRY_RUN; the single Telegram message
goes out last. Exit code is 0 unless the run itself crashed.
"""

import datetime as dt
import logging
import logging.handlers
import os
import sys
import time
import traceback
from typing import List

from health_watchdog import alerting, browser_hygiene, evaluate, hung_jobs, landline_health
from health_watchdog.config import (
    DRY_RUN,
    LOG_DIR,
    LOG_FILE,
    LOG_ROTATE_BYTES,
    LOG_ROTATE_COUNT,
    PAUSE_FILE,
    PRIVATE_CONFIG_ERROR,
    STATE_FILE,
    TOP_PROCESS_COUNT,
)
from health_watchdog.findings import Finding
from health_watchdog.plist_inventory import load_launchd_jobs
from health_watchdog.process_table import (
    current_user,
    format_top_cpu,
    format_top_rss,
    index_by_pid,
    outcome_summary,
    read_launchctl_list,
    read_process_table,
)
from health_watchdog.state import decide_alerts, load_state, save_state
from health_watchdog.system_probes import collect_system_snapshot, health_snapshot_line
from health_watchdog.tcc_probe import run_tcc_probe

logger = logging.getLogger("health_watchdog")


def setup_logging() -> None:
    """Rotating file log plus stdout (launchd captures stdout into its own log)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(
        str(LOG_FILE), maxBytes=LOG_ROTATE_BYTES, backupCount=LOG_ROTATE_COUNT)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


def paused_until_today_or_later() -> bool:
    """Same semantics as cc-job-lib's paused_until_guard: a YYYY-MM-DD >= today pauses."""
    try:
        text = PAUSE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    try:
        until = dt.date.fromisoformat(text)
    except ValueError:
        logger.warning("ignoring malformed pause file %s (%r)", PAUSE_FILE, text)
        return False
    return until >= dt.date.today()


def collect_findings(state: dict, now: dt.datetime) -> List[Finding]:
    """Run every probe and threshold check; take the two permitted actions."""
    findings = []  # type: List[Finding]

    table = read_process_table()
    if not table:
        logger.error("ps returned nothing; process-based checks are skipped this run")
    launchctl_pids = read_launchctl_list()
    jobs = load_launchd_jobs()
    snapshot = collect_system_snapshot(table)
    for probe_error in snapshot.probe_errors:
        logger.warning("probe error: %s", probe_error)
    logger.info("snapshot: %s", health_snapshot_line(snapshot))

    tcc = run_tcc_probe(state.get("tcc_probe_pid"), table)
    state["tcc_probe_pid"] = tcc.probe_pid
    logger.info("tcc probe: %s (%s)", tcc.status, tcc.detail)

    top_cpu = format_top_cpu(table, TOP_PROCESS_COUNT)
    top_rss = format_top_rss(table, TOP_PROCESS_COUNT)
    for finding in (
        evaluate.evaluate_tcc(tcc),
        evaluate.evaluate_tccd_cpu(snapshot, state),
        evaluate.evaluate_memory(snapshot, state, top_rss),
        evaluate.evaluate_load(snapshot, state, top_cpu),
        evaluate.evaluate_disk(snapshot),
        evaluate.evaluate_landline(landline_health.recent_silent_kill_count(now), snapshot),
    ):
        if finding is not None:
            findings.append(finding)

    if table:
        findings.extend(handle_hung_jobs(table, launchctl_pids, jobs, state, now.timestamp()))

    browser = browser_hygiene.inspect_browser(table)
    logger.info("browser: running=%s tabs=%d renderers=%d hottest=%.0f%%",
                browser.server_running, browser.tab_count, len(browser.renderers), browser.hottest_renderer_cpu)
    stop_reason = evaluate.browser_needs_stop(browser, state)
    if stop_reason:
        outcome = browser_hygiene.stop_browser(DRY_RUN)
        stop_summary = "dry run, not stopped" if outcome is None else outcome_summary(outcome)
        logger.warning("browser stop (%s): %s", stop_reason, stop_summary)
        findings.append(evaluate.browser_finding(browser, stop_reason, stop_summary))
    orphans = browser_hygiene.orphaned_profile_processes(browser, table)
    if orphans:
        orphan_pids = [row.pid for row in orphans]
        killed, survivors = ([], []) if DRY_RUN else hung_jobs.terminate_pids(orphan_pids)
        logger.warning("orphaned browser processes %s: dry_run=%s killed=%s survivors=%s",
                       orphan_pids, DRY_RUN, killed, survivors)
        findings.append(evaluate.orphaned_browser_finding(len(orphans), killed, survivors, DRY_RUN))

    return findings


def handle_hung_jobs(table, launchctl_pids, jobs, state: dict, now_ts: float) -> List[Finding]:
    """Select, kill (skipping already-reported SIGKILL survivors), remember new survivors."""
    findings = []  # type: List[Finding]
    by_pid = index_by_pid(table)
    survivors = hung_jobs.prune_survivor_records(dict(state.get("survivors", {})), by_pid, now_ts)
    known_pids = [int(pid_text) for pid_text in survivors]

    candidates = hung_jobs.select_hung_jobs(table, launchctl_pids, jobs, os.getpid(), current_user())
    if candidates:
        for candidate in candidates:
            logger.warning("hung job: %s root pid %d age %ds pids %s",
                           candidate.name, candidate.root_pid, candidate.age_seconds, candidate.all_pids)
        report = hung_jobs.kill_job_trees(candidates, DRY_RUN, skip_pids=known_pids)
        logger.warning("kill report: dry_run=%s killed=%s survivors=%s known_survivors=%s",
                       report.dry_run, report.killed, report.survivors, report.known_survivors)
        for pid in report.survivors:
            if pid in by_pid:
                survivors[str(pid)] = hung_jobs.process_start_ts(by_pid[pid], now_ts)
        finding = evaluate.hung_jobs_finding(report)
        if finding is not None:
            findings.append(finding)

    state["survivors"] = survivors
    standing = evaluate.survivors_finding(known_pids)
    if standing is not None:
        findings.append(standing)
    return findings


def deliver(message: str) -> None:
    """Telegram first; on failure the message is already logged, try a bounded notification."""
    if DRY_RUN:
        logger.info("DRY_RUN: would send:\n%s", message)
        return
    outcome = alerting.send_telegram(message)
    if outcome.succeeded:
        logger.info("alert sent via Telegram (%s)", outcome_summary(outcome))
        return
    logger.error("Telegram send failed: %s %s", outcome_summary(outcome), outcome.stderr.strip()[-300:])
    notify = alerting.notify_macos(message)
    logger.info("macOS notification fallback: %s", outcome_summary(notify))


def run_once() -> int:
    """One full watchdog pass; returns the process exit code."""
    started = time.monotonic()
    now = dt.datetime.now()
    state = load_state(STATE_FILE)
    state["runs"] = int(state.get("runs", 0)) + 1
    logger.info("run %d start%s", state["runs"], " (DRY_RUN)" if DRY_RUN else "")
    if PRIVATE_CONFIG_ERROR:
        logger.warning("ignoring unusable per-install config: %s", PRIVATE_CONFIG_ERROR)

    findings = collect_findings(state, now)
    decision = decide_alerts(findings, state, time.time())
    for finding in findings:
        logger.warning("finding [%s] %s %s | %s", finding.check_id, finding.severity.value,
                       finding.title, " | ".join(finding.details))
    for finding in decision.suppressed:
        logger.info("suppressed by cooldown: %s", finding.check_id)
    for check_id in decision.recovered:
        logger.info("recovered: %s", check_id)

    if decision.to_send or decision.recovered:
        deliver(alerting.format_alert_message(decision.to_send, decision.recovered, now))

    if DRY_RUN:
        logger.info("DRY_RUN: state not persisted")
    else:
        save_state(STATE_FILE, state)
    logger.info("run done in %.1fs: %d finding(s), %d sent, %d suppressed, %d recovered",
                time.monotonic() - started, len(findings), len(decision.to_send),
                len(decision.suppressed), len(decision.recovered))
    return 0


def main() -> int:
    """CLI entry: logging, pause guard, one run, crash -> exit 1 with traceback logged."""
    setup_logging()
    if paused_until_today_or_later():
        logger.info("paused via %s; skipping run", PAUSE_FILE)
        return 0
    try:
        return run_once()
    except Exception:  # noqa: BLE001 - the watchdog must log its own crash, not die silently
        error_text = traceback.format_exc()
        logger.error("watchdog crashed:\n%s", error_text)
        if not DRY_RUN:
            try:
                outcome = alerting.send_telegram(alerting.format_crash_message(error_text, dt.datetime.now()))
                logger.info("crash alert: %s", outcome_summary(outcome))
            except Exception:  # noqa: BLE001 - best effort; the log already has the crash
                logger.error("crash alert failed:\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
