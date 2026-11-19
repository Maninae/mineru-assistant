"""Tests for the health watchdog's missed-run check: cadence math (job_schedule.py),
scope and signal rules (missed_runs.py), and verdicts (evaluate_missed_runs.py)."""

import datetime as dt
import os
import plistlib
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from health_watchdog import evaluate_missed_runs, missed_runs  # noqa: E402
from health_watchdog.alerting import format_alert_message, recovered_title  # noqa: E402
from health_watchdog.findings import Severity  # noqa: E402
from health_watchdog.job_schedule import expected_cadence_seconds  # noqa: E402
from health_watchdog.state import decide_alerts, default_state  # noqa: E402

HOUR = 3600
DAY = 24 * HOUR
PREFIX = "com.example"
NOW = dt.datetime(2026, 3, 11, 12, 0, 0)  # a Wednesday
NOW_TS = NOW.timestamp()


# --- cadence ----------------------------------------------------------------------

def test_start_interval_is_the_cadence():
    assert expected_cadence_seconds({"StartInterval": 300}) == 300


def test_daily_calendar_is_one_day():
    assert expected_cadence_seconds({"StartCalendarInterval": {"Hour": 7, "Minute": 0}}) == DAY


def test_weekday_set_uses_the_longest_gap_not_the_average():
    mon_wed_fri = [{"Weekday": day, "Hour": 11, "Minute": 0} for day in (1, 3, 5)]
    assert expected_cadence_seconds({"StartCalendarInterval": mon_wed_fri}) == 3 * DAY


def test_single_weekday_is_weekly_and_sunday_alias_matches():
    assert expected_cadence_seconds({"StartCalendarInterval": {"Weekday": 0, "Hour": 3, "Minute": 17}}) == 7 * DAY
    assert expected_cadence_seconds({"StartCalendarInterval": {"Weekday": 7, "Hour": 3, "Minute": 0}}) == 7 * DAY


def test_missing_minute_means_every_minute_of_that_hour():
    # Fires 03:00-03:59 daily: the gap is 23 h + 1 min.
    assert expected_cadence_seconds({"StartCalendarInterval": {"Hour": 3}}) == 23 * HOUR + 60


def test_day_of_month_is_monthly_and_keepalive_or_runatload_has_no_cadence():
    assert expected_cadence_seconds({"StartCalendarInterval": {"Day": 2, "Hour": 4}}) == 31 * DAY
    assert expected_cadence_seconds({"KeepAlive": True, "StartInterval": 60}) is None
    assert expected_cadence_seconds({"RunAtLoad": True}) is None


def test_allowed_silence_is_factor_or_minimum_grace_whichever_is_larger():
    assert missed_runs.allowed_silence_seconds(DAY) == int(1.5 * DAY)
    assert missed_runs.allowed_silence_seconds(300) == 300 + 4 * HOUR


# --- launchctl parsing --------------------------------------------------------------

def test_parse_loaded_labels_filters_by_prefix():
    text = "PID\tStatus\tLabel\n-\t0\tcom.example.morning\n123\t0\tcom.other.thing\n-\t0\tcom.examplex.no\n"
    assert missed_runs.parse_loaded_labels(text, [PREFIX, "com.other"]) == {"com.example.morning", "com.other.thing"}


# --- scope + signals ----------------------------------------------------------------

def write_plist(directory: Path, label: str, plist: dict, mtime: float) -> Path:
    path = directory / (label + ".plist")
    body = dict(plist)
    body["Label"] = label
    with open(path, "wb") as handle:
        plistlib.dump(body, handle)
    os.utime(path, (mtime, mtime))
    return path


def touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def build(tmp_path, loaded, registry=None, overrides=None):
    plists = missed_runs.load_plists([tmp_path / "agents"], [PREFIX])
    return missed_runs.build_job_activity(
        plists, set(loaded), registry or {}, overrides or {}, NOW,
        home=tmp_path / "home", logs_dir=tmp_path / "home" / "logs", paused_dir=tmp_path / "home" / "paused",
        prefixes=(PREFIX,), skip_labels=frozenset([PREFIX + ".health-watchdog"]),
    )


DAILY = {"StartCalendarInterval": {"Hour": 7, "Minute": 0}}
OLD = NOW_TS - 30 * DAY


def test_unloaded_keepalive_skipped_and_neutral_jobs_are_out_of_scope(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    for name in ("daily", "retired", "health-watchdog", "neutral"):
        write_plist(agents, PREFIX + "." + name, DAILY, OLD)
    write_plist(agents, PREFIX + ".service", {"KeepAlive": True, "RunAtLoad": True}, OLD)
    loaded = [PREFIX + "." + name for name in ("daily", "health-watchdog", "neutral", "service")]
    registry = {PREFIX + ".neutral": {"freshness": {"kind": "neutral"}}}
    rows = build(tmp_path, loaded, registry)
    assert [row.label for row in rows] == [PREFIX + ".daily"]


def test_newest_signal_wins_and_old_job_is_flagged(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    write_plist(agents, PREFIX + ".brief", dict(DAILY, StandardOutPath=str(tmp_path / "out.log")), OLD)
    touch(tmp_path / "out.log", NOW_TS - 5 * DAY)
    touch(tmp_path / "home" / "logs" / "brief" / "run.log", NOW_TS - 3 * DAY)
    registry = {PREFIX + ".brief": {"display_name": "Brief", "freshness": {"kind": "newest_in_dir", "dir": "briefs"}}}
    touch(tmp_path / "home" / "briefs" / "a.md", NOW_TS - 2 * DAY)
    [row] = build(tmp_path, [PREFIX + ".brief"], registry)
    assert row.last_activity_ts == NOW_TS - 2 * DAY and row.last_activity_source == "briefs"
    finding = evaluate_missed_runs.evaluate_missed_run(row, NOW_TS)
    assert finding.severity == Severity.CRITICAL
    assert finding.check_id == "missed_run:" + PREFIX + ".brief"
    assert "Brief has not run on schedule" == finding.title and "2d ago (briefs)" in finding.details[0]


def test_recent_log_means_no_finding(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    write_plist(agents, PREFIX + ".brief", DAILY, OLD)
    touch(tmp_path / "home" / "logs" / "brief" / "run.log", NOW_TS - 20 * HOUR)
    [row] = build(tmp_path, [PREFIX + ".brief"])
    assert evaluate_missed_runs.evaluate_missed_run(row, NOW_TS) is None


def test_freshly_installed_plist_is_not_judged_yet(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    write_plist(agents, PREFIX + ".new", {"StartCalendarInterval": {"Weekday": 0, "Hour": 3}}, NOW_TS - HOUR)
    [row] = build(tmp_path, [PREFIX + ".new"])
    assert row.last_activity_source == "plist installed"
    assert evaluate_missed_runs.evaluate_missed_run(row, NOW_TS) is None


def test_paused_job_is_skipped_and_an_expired_pause_restarts_the_clock(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    write_plist(agents, PREFIX + ".paused", DAILY, OLD)
    write_plist(agents, PREFIX + ".resumed", DAILY, OLD)
    touch(tmp_path / "home" / "paused" / "paused.until", OLD)
    (tmp_path / "home" / "paused" / "paused.until").write_text("2026-03-20\n")
    (tmp_path / "home" / "paused" / "resumed.until").write_text("2026-03-10\n")  # ended at midnight today
    [row] = build(tmp_path, [PREFIX + ".paused", PREFIX + ".resumed"])
    assert row.label == PREFIX + ".resumed" and row.last_activity_source == "pause ended"
    assert evaluate_missed_runs.evaluate_missed_run(row, NOW_TS) is None


def test_override_spec_wins_over_registry_and_supports_absolute_paths(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    write_plist(agents, PREFIX + ".ext", DAILY, OLD)
    touch(tmp_path / "elsewhere" / "gen.log", NOW_TS - HOUR)
    registry = {PREFIX + ".ext": {"freshness": {"kind": "neutral"}}}
    overrides = {PREFIX + ".ext": {"kind": "newest_in_dir", "dir": str(tmp_path / "elsewhere")}}
    [row] = build(tmp_path, [PREFIX + ".ext"], registry, overrides)
    assert row.last_activity_ts == NOW_TS - HOUR


def test_newest_in_dir_honours_excludes(tmp_path):
    touch(tmp_path / "memory" / "daily" / "today.md", NOW_TS)
    touch(tmp_path / "memory" / "people.md", NOW_TS - 9 * DAY)
    spec = {"kind": "newest_in_dir", "dir": "memory", "exclude": ["memory/daily"]}
    assert missed_runs.freshness_signal(spec, tmp_path) == NOW_TS - 9 * DAY


def test_first_directory_defining_a_label_wins(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    write_plist(first, PREFIX + ".job", DAILY, OLD)
    write_plist(second, PREFIX + ".job", {"StartInterval": 60}, OLD)
    plists = missed_runs.load_plists([first, second], [PREFIX])
    assert plists[PREFIX + ".job"][0].parent == first


def test_never_seen_job_is_flagged():
    job = missed_runs.ScheduledJobActivity(PREFIX + ".x", "X", DAY, int(1.5 * DAY), None, "no artifact found")
    assert "no sign of a run" in evaluate_missed_runs.evaluate_missed_run(job, NOW_TS).details[0]


# --- cooldown + recovery ------------------------------------------------------------

def test_missed_run_cools_for_twelve_hours_then_recovers_with_the_label():
    job = missed_runs.ScheduledJobActivity(PREFIX + ".x", "X", DAY, int(1.5 * DAY), NOW_TS - 3 * DAY, "logs/x/")
    state = default_state()
    first = decide_alerts(evaluate_missed_runs.evaluate_missed_runs([job], NOW_TS), state, NOW_TS)
    assert len(first.to_send) == 1
    later = decide_alerts(evaluate_missed_runs.evaluate_missed_runs([job], NOW_TS), state, NOW_TS + 6 * HOUR)
    assert later.to_send == [] and len(later.suppressed) == 1
    job.last_activity_ts = NOW_TS + 7 * HOUR
    cleared = decide_alerts(evaluate_missed_runs.evaluate_missed_runs([job], NOW_TS + 8 * HOUR), state, NOW_TS + 8 * HOUR)
    assert cleared.recovered == ["missed_run:" + PREFIX + ".x"]
    message = format_alert_message([], cleared.recovered, NOW)
    assert PREFIX + ".x ran again" in message
    assert recovered_title("disk") == "disk space"
