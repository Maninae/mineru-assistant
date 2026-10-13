"""Tests for scripts/health_watchdog/hung_jobs.py: the selector must pick the
incident's hung jobs and must never pick services or interactive sessions."""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from health_watchdog import config  # noqa: E402
from health_watchdog.hung_jobs import (  # noqa: E402
    is_cc_job_command,
    kill_job_trees,
    protected_pids,
    select_hung_jobs,
)
from health_watchdog.plist_inventory import LaunchdJob  # noqa: E402
from health_watchdog.process_table import ProcessInfo  # noqa: E402

HOME = str(config.MINERU_HOME)
SCRIPTS = HOME + "/scripts"
PREFIX = config.LAUNCHD_PREFIX
USER = "operator"
SELF_PID = 99999
HOURS = 3600
WATCHDOG_CMD = "/usr/bin/python3 -m health_watchdog"


def proc(pid, ppid, command, age=0, tty="??", user=USER, cpu=0.0):
    """Shorthand ProcessInfo factory."""
    return ProcessInfo(pid=pid, ppid=ppid, pgid=pid, tty=tty, user=user,
                       etime_seconds=age, cpu_percent=cpu, rss_kb=1000, command=command)


def job(suffix, keepalive=False):
    """Shorthand LaunchdJob factory."""
    return LaunchdJob(label="%s.%s" % (PREFIX, suffix), keepalive=keepalive, scheduled=not keepalive)


# The 2026-10-13 incident, reconstructed: hung jobs 9-37 h old next to services
# and interactive sessions that must survive.
INCIDENT_TABLE = [
    # Hung scheduled jobs (launchd still tracks the first three).
    proc(100, 1, "/bin/bash %s/trigger-morning-brief-claude-code.sh" % SCRIPTS, age=9 * HOURS),
    proc(101, 100, "claude-fda --permission-mode bypassPermissions --model m --verbose --print "
                   "Read recurring/morning-brief.md and execute the job.", age=9 * HOURS),
    proc(110, 1, "/usr/bin/python3 %s/export-journals.py" % SCRIPTS, age=37 * HOURS),
    proc(120, 1, "/usr/bin/python3 %s/sweep_group_members.py" % SCRIPTS, age=12 * HOURS),
    proc(121, 120, "/usr/bin/python3 %s/bin/imsg-firewall chats --json" % HOME, age=12 * HOURS),
    proc(122, 121, "/opt/homebrew/bin/imsg chats --json", age=12 * HOURS),
    # Orphaned job pieces re-parented to launchd (their trigger script died).
    proc(130, 1, "/usr/bin/python3 %s/publish-anthology-pages.py" % SCRIPTS, age=20 * HOURS),
    proc(131, 1, "claude --permission-mode bypassPermissions --print "
                 "Read recurring/news-brief.md and execute the job.", age=11 * HOURS),
    proc(132, 1, "/usr/bin/python3 %s/boundary_validator.py --watch" % SCRIPTS, age=30 * HOURS),
    # KeepAlive services and their trees.
    proc(200, 1, "/usr/bin/python3 -m landline", age=16 * 24 * HOURS),
    proc(201, 200, "claude --permission-mode bypassPermissions --print --output-format stream-json",
         age=30 * HOURS),
    proc(210, 1, "/usr/bin/python3 %s/app/server.py" % HOME, age=16 * 24 * HOURS),
    proc(220, 1, "/opt/homebrew/bin/python3 %s/browser/server.py" % HOME, age=5 * 24 * HOURS),
    proc(221, 220, "/opt/cloakbrowser/chromium/Chromium.app/Contents/MacOS/Chromium "
                   "--user-data-dir=%s/cache/cloakbrowser-profile" % HOME, age=5 * 24 * HOURS),
    proc(222, 221, "/opt/cloakbrowser/chromium/Chromium.app/Contents/MacOS/Chromium Helper "
                   "--type=renderer --user-data-dir=%s/cache/cloakbrowser-profile" % HOME,
         age=5 * 24 * HOURS, cpu=98.0),
    # Interactive sessions: TTY-attached, or a headless resume without the job marker.
    proc(300, 299, "claude --dangerously-skip-permissions", age=20 * 24 * HOURS, tty="ttys000"),
    proc(301, 300, "/bin/zsh -c %s/trigger-morning-brief-claude-code.sh" % SCRIPTS, age=5 * HOURS,
         tty="ttys000"),
    proc(310, 1, "claude --dangerously-skip-permissions --resume --print", age=20 * HOURS),
    # Young job: must wait until it is actually old.
    proc(400, 1, "/bin/bash %s/trigger-news-brief-claude-code.sh" % SCRIPTS, age=10 * 60),
    # Another user's old job-looking process.
    proc(500, 1, "/usr/bin/python3 %s/export-journals.py" % SCRIPTS, age=40 * HOURS, user="someone"),
    # The watchdog itself and its launchd-tracked parent.
    proc(SELF_PID, 1, WATCHDOG_CMD, age=5),
]

INCIDENT_LAUNCHCTL = {
    "%s.morning-brief" % PREFIX: 100,
    "%s.pre-export-journals" % PREFIX: 110,
    "%s.group-members-sweep" % PREFIX: 120,
    "%s.news-brief" % PREFIX: 400,
    "%s.telegram-daemon" % PREFIX: 200,
    "%s.webapp" % PREFIX: 210,
    "%s.health-watchdog" % PREFIX: SELF_PID,
    "%s.daily-consolidation" % PREFIX: None,
}

INCIDENT_JOBS = [
    job("morning-brief"), job("pre-export-journals"), job("group-members-sweep"), job("news-brief"),
    job("daily-consolidation"), job("telegram-daemon", keepalive=True), job("webapp", keepalive=True),
    job("health-watchdog"),
]


def select_incident():
    return select_hung_jobs(INCIDENT_TABLE, INCIDENT_LAUNCHCTL, INCIDENT_JOBS, SELF_PID, USER)


def test_selects_every_hung_incident_job_with_its_tree():
    roots = {candidate.root_pid: candidate for candidate in select_incident()}
    assert set(roots) == {100, 110, 120, 130, 131, 132}
    assert roots[100].descendant_pids == [101]
    assert set(roots[120].descendant_pids) == {121, 122}
    assert roots[100].label.endswith("morning-brief")
    assert roots[130].label is None


def test_oldest_candidate_comes_first():
    ages = [candidate.age_seconds for candidate in select_incident()]
    assert ages == sorted(ages, reverse=True)


@pytest.mark.parametrize("pid", [200, 201, 210, 220, 221, 222, 300, 301, 310, 400, 500, SELF_PID])
def test_services_sessions_and_young_jobs_are_never_selected(pid):
    selected = {p for candidate in select_incident() for p in candidate.all_pids}
    assert pid not in selected


def test_protected_set_covers_service_trees_and_tty_trees():
    protected = protected_pids(INCIDENT_TABLE, INCIDENT_LAUNCHCTL, INCIDENT_JOBS, SELF_PID)
    assert {200, 201, 210, 220, 221, 222, 300, 301, SELF_PID} <= protected
    assert 100 not in protected and 130 not in protected


def test_launchd_tracked_job_whose_tree_touches_a_protected_pid_is_skipped():
    # A job that somehow parents a TTY process must not be killed as a tree.
    table = INCIDENT_TABLE + [proc(102, 101, "/bin/zsh", age=HOURS, tty="ttys009")]
    roots = {c.root_pid for c in select_hung_jobs(table, INCIDENT_LAUNCHCTL, INCIDENT_JOBS, SELF_PID, USER)}
    assert 100 not in roots


def test_per_job_age_override_applies_to_label(monkeypatch):
    monkeypatch.setitem(config.JOB_MAX_AGE_OVERRIDES_SECONDS, "morning-brief", 10 * HOURS)
    monkeypatch.setattr("health_watchdog.hung_jobs.JOB_MAX_AGE_OVERRIDES_SECONDS",
                        config.JOB_MAX_AGE_OVERRIDES_SECONDS)
    roots = {c.root_pid for c in select_incident()}
    assert 100 not in roots and 110 in roots


@pytest.mark.parametrize("command, expected", [
    ("claude --print Read recurring/x.md and execute the job.", True),
    ("/path/claude-fda --model m --print Read recurring/x.md and execute the job.", True),
    ("claude --print --output-format stream-json", False),
    ("claude --dangerously-skip-permissions", False),
    ("claude --resume --print Read recurring/x.md", False),
    ("python3 recurring/x.md --print", False),
])
def test_cc_job_command_detection(command, expected):
    assert is_cc_job_command(command) is expected


def test_dry_run_kill_touches_nothing():
    candidates = select_incident()
    report = kill_job_trees(candidates, dry_run=True)
    assert report.dry_run and report.killed == [] and report.survivors == []
    assert report.candidates == candidates


def test_real_kill_reports_survivors_and_killed(monkeypatch):
    sent = []
    monkeypatch.setattr("health_watchdog.hung_jobs.send_signal", lambda pids, sig: sent.append((list(pids), sig)))
    # First wait: 101 refuses TERM; second wait: 101 also survives KILL.
    waits = iter([[101], [101]])
    monkeypatch.setattr("health_watchdog.hung_jobs.wait_for_exit", lambda pids, grace: next(waits))
    candidates = [c for c in select_incident() if c.root_pid == 100]
    report = kill_job_trees(candidates, dry_run=False)
    assert sent[0][0] == [100, 101] and sent[1][0] == [101]
    assert report.killed == [100] and report.survivors == [101]
