"""Missed-run verdicts: ScheduledJobActivity rows in, one Finding per silent job out.

Python 3.9 compatible (see config.py). Pure: no commands, no file reads.

Each job gets its own check id (`missed_run:<label>`), so jobs cool down and recover
independently. Findings are CRITICAL because "recovered" lines are only sent for
CRITICAL checks, and a job firing again deserves that line; the 12 h cooldown
(`MISSED_RUN_COOLDOWN_SECONDS`) keeps a job that stays dead from paging every 30 min.
"""

from typing import List, Optional

from health_watchdog.browser_hygiene import format_age
from health_watchdog.config import MISSED_RUN_COOLDOWN_SECONDS
from health_watchdog.findings import Finding, Severity
from health_watchdog.missed_runs import ScheduledJobActivity

CHECK_MISSED_RUN_PREFIX = "missed_run:"


def missed_run_check_id(label: str) -> str:
    """Per-job check id used for cooldown and recovery bookkeeping."""
    return CHECK_MISSED_RUN_PREFIX + label


def evaluate_missed_run(job: ScheduledJobActivity, now_ts: float) -> Optional[Finding]:
    """CRITICAL when the newest sign of life is older than the job's allowed silence."""
    if job.last_activity_ts is not None and now_ts - job.last_activity_ts <= job.allowed_silence_seconds:
        return None
    if job.last_activity_ts is None:
        last_seen = "no sign of a run at all"
    else:
        last_seen = "last sign of a run %s ago (%s)" % (
            format_age(now_ts - job.last_activity_ts), job.last_activity_source)
    return Finding(
        check_id=missed_run_check_id(job.label),
        severity=Severity.CRITICAL,
        title="%s has not run on schedule" % job.display_name,
        details=[
            "%s: expected at least every %s, %s" % (job.label, format_age(job.cadence_seconds), last_seen),
            "Check: launchctl print gui/$(id -u)/%s (runs, last exit code)" % job.label,
            "Fix a stuck schedule: launchctl bootout then bootstrap the plist; a retired job should be booted out and its plist removed",
        ],
        cooldown_seconds=MISSED_RUN_COOLDOWN_SECONDS,
    )


def evaluate_missed_runs(jobs: List[ScheduledJobActivity], now_ts: float) -> List[Finding]:
    """One finding per job past its allowed silence."""
    findings = []  # type: List[Finding]
    for job in jobs:
        finding = evaluate_missed_run(job, now_ts)
        if finding is not None:
            findings.append(finding)
    return findings
