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


ENGINE_DEFAULT_MARKERS = (
    "/bin/imsg-firewall", "/bin/imsg-named", "/bin/gog-firewall", "/scripts/boundary_validator.py",
    "/scripts/sweep_group_members.py", "/scripts/build_contact_map.py", "/scripts/extract-cc-sessions.py")


@pytest.fixture(autouse=True)
def engine_defaults(monkeypatch):
    """Pin the selector's config to engine defaults so a private config file cannot steer tests."""
    monkeypatch.setattr("health_watchdog.hung_jobs.JOB_COMMAND_MARKERS", ENGINE_DEFAULT_MARKERS)
    monkeypatch.setattr("health_watchdog.hung_jobs.JOB_MAX_AGE_OVERRIDES_SECONDS", {})
    monkeypatch.setattr("health_watchdog.hung_jobs.SERVICE_COMMAND_ALLOWLIST",
                        ("-m landline", "/app/server.py", "/browser/server.py", "Chromium", "cloakbrowser"))


def proc(pid, ppid, command, age=0, tty="??", user=USER, cpu=0.0):
    """Shorthand ProcessInfo factory."""
    return ProcessInfo(pid=pid, ppid=ppid, pgid=pid, tty=tty, user=user,
                       etime_seconds=age, cpu_percent=cpu, rss_kb=1000, command=command)


def job(suffix, keepalive=False, script=None):
    """Shorthand LaunchdJob factory; `script` is the plist's launched script path."""
    arguments = ["/bin/bash", script] if script else []
    return LaunchdJob(label="%s.%s" % (PREFIX, suffix), keepalive=keepalive, scheduled=not keepalive,
                      program_arguments=arguments)


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
    # 130 is a private helper no plist names: left alone unless the private config adds it.
    proc(130, 1, "/usr/bin/python3 %s/publish-anthology-pages.py" % SCRIPTS, age=20 * HOURS),
    proc(131, 1, "claude --permission-mode bypassPermissions --print "
                 "Read recurring/news-brief.md and execute the job.", age=11 * HOURS),
    proc(132, 1, "/usr/bin/python3 %s/boundary_validator.py --watch" % SCRIPTS, age=30 * HOURS),
    proc(133, 1, "/usr/bin/python3 %s/some-long-backfill.py" % SCRIPTS, age=5 * HOURS),  # nohup'd by hand
    proc(134, 1, "/usr/bin/python3 %s/export-journals.py 30" % SCRIPTS, age=8 * HOURS),  # plist script, orphaned
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
    job("morning-brief", script=SCRIPTS + "/trigger-morning-brief-claude-code.sh"),
    job("pre-export-journals", script=SCRIPTS + "/export-journals.py"),
    job("group-members-sweep", script=SCRIPTS + "/run-group-members-sweep.sh"),
    job("news-brief", script=SCRIPTS + "/trigger-news-brief-claude-code.sh"),
    job("daily-consolidation", script=SCRIPTS + "/trigger-daily-consolidation-claude-code.sh"),
    job("telegram-daemon", keepalive=True), job("webapp", keepalive=True),
    job("health-watchdog", script=SCRIPTS + "/health_watchdog/run-health-watchdog.sh"),
]


def select_incident():
    return select_hung_jobs(INCIDENT_TABLE, INCIDENT_LAUNCHCTL, INCIDENT_JOBS, SELF_PID, USER)


def test_selects_every_hung_incident_job_with_its_tree():
    roots = {candidate.root_pid: candidate for candidate in select_incident()}
    assert set(roots) == {100, 110, 120, 131, 132, 134}
    assert roots[100].descendant_pids == [101]
    assert set(roots[120].descendant_pids) == {121, 122}
    assert roots[100].label.endswith("morning-brief")
    assert roots[134].label is None


def test_private_marker_makes_an_indirect_helper_orphan_killable(monkeypatch):
    monkeypatch.setattr("health_watchdog.hung_jobs.JOB_COMMAND_MARKERS",
                        ENGINE_DEFAULT_MARKERS + ("/scripts/publish-anthology-pages.py",))
    assert 130 in {c.root_pid for c in select_incident()}


def test_selector_without_launchctl_still_finds_plist_scripts_and_engine_job_markers():
    # 120 runs the sweep's inner python directly (its bash wrapper is what the plist
    # names); the engine marker for sweep_group_members.py makes it selectable anyway,
    # with its imsg-firewall + imsg grandchildren. The nohup'd backfill (133) stays out.
    candidates = {c.root_pid: c for c in select_hung_jobs(INCIDENT_TABLE, {}, INCIDENT_JOBS, SELF_PID, USER)}
    assert {100, 110, 120, 131, 132, 134} <= set(candidates)
    assert set(candidates[120].descendant_pids) == {121, 122}
    assert 133 not in candidates and 130 not in candidates


def test_service_scripts_stay_protected_when_launchctl_is_empty():
    # daemon-watchdog is an allowlisted service label; with launchctl unavailable its
    # script must still be recognised by path and never selected, even when old.
    jobs = INCIDENT_JOBS + [job("daemon-watchdog", script="/some/deploy/watchdog.sh")]
    table = INCIDENT_TABLE + [proc(950, 1, "/bin/bash /some/deploy/watchdog.sh", age=5 * HOURS)]
    roots = {c.root_pid for c in select_hung_jobs(table, {}, jobs, SELF_PID, USER)}
    assert 950 not in roots and 100 in roots


def test_handle_hung_jobs_survivor_cadence_one_critical_then_cooldown(monkeypatch):
    """Unkillable orphan CC job across 8 runs: one CRITICAL at the kill, silence until the
    30 min survivor cooldown expires, one recovery when it finally disappears."""
    from health_watchdog import __main__ as watchdog_main
    from health_watchdog.state import decide_alerts, default_state

    monkeypatch.setattr("health_watchdog.hung_jobs.terminate_pids", lambda pids: ([], list(pids)))
    monkeypatch.setattr("health_watchdog.__main__.DRY_RUN", False)
    monkeypatch.setattr("health_watchdog.__main__.current_user", lambda: USER)
    monkeypatch.setattr("os.getpid", lambda: SELF_PID)
    state = default_state()
    sent = []
    base = 1_000_000.0
    for run in range(8):
        now_ts = base + run * 300
        table = [
            proc(600, 1, "claude-fda --permission-mode bypassPermissions --print "
                         "Read recurring/news-brief.md and execute the job.", age=4 * HOURS + run * 300),
            proc(SELF_PID, 1, WATCHDOG_CMD, age=5),
        ]
        findings = watchdog_main.handle_hung_jobs(table, {}, INCIDENT_JOBS, state, now_ts)
        decision = decide_alerts(findings, state, now_ts)
        sent.append(["%s:%s" % (f.check_id, f.severity.value) for f in decision.to_send])
    assert sent == [["hung_jobs:CRITICAL"], [], [], [], [], [], ["hung_jobs_survivor:CRITICAL"], []]
    assert set(state["survivors"]) == {"600"}
    findings = watchdog_main.handle_hung_jobs([proc(SELF_PID, 1, WATCHDOG_CMD, age=5)], {}, INCIDENT_JOBS,
                                              state, base + 8 * 300)
    decision = decide_alerts(findings, state, base + 8 * 300)
    assert decision.to_send == [] and decision.recovered == ["hung_jobs_survivor"]
    assert state["survivors"] == {}


def test_oldest_candidate_comes_first():
    ages = [candidate.age_seconds for candidate in select_incident()]
    assert ages == sorted(ages, reverse=True)


@pytest.mark.parametrize("pid", [130, 133, 200, 201, 210, 220, 221, 222, 300, 301, 310, 400, 500, SELF_PID])
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


def test_known_survivors_are_skipped_not_resignalled(monkeypatch):
    sent = []
    monkeypatch.setattr("health_watchdog.hung_jobs.send_signal", lambda pids, sig: sent.append(list(pids)))
    monkeypatch.setattr("health_watchdog.hung_jobs.wait_for_exit", lambda pids, grace: [])
    candidates = [c for c in select_incident() if c.root_pid == 100]
    report = kill_job_trees(candidates, dry_run=False, skip_pids=[101])
    assert sent == [[100]] and report.killed == [100] and report.known_survivors == [101]
    # Every pid already known: nothing is signalled and the report says so.
    quiet = kill_job_trees(candidates, dry_run=False, skip_pids=[100, 101])
    assert sent == [[100]] and not quiet.signalled_anything


def test_survivor_records_prune_dead_and_reused_pids():
    from health_watchdog.hung_jobs import prune_survivor_records
    from health_watchdog.process_table import index_by_pid

    now = 1_000_000.0
    table = [proc(101, 1, "claude --print", age=9 * HOURS), proc(555, 1, "bash new", age=60)]
    records = {"101": now - 9 * HOURS + 30, "555": now - 5 * HOURS, "999": now - HOURS, "junk": 1.0}
    kept = prune_survivor_records(records, index_by_pid(table), now)
    assert set(kept) == {"101"}  # 555 was reused by a young process, 999 is gone


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
