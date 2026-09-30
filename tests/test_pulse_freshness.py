#!/usr/bin/env python3
"""Tests for `app/pulse_freshness.py` — status classification + signal table.

The classifier turns `(cadence_seconds, last_output_ts)` into one of
`fresh / stale / failed / not-scheduled`. The multiplicative thresholds
(1.5x, 3.0x of cadence) matter because they scale correctly across daily,
weekly, and monthly jobs — the reason a Sunday-weekly job is still "fresh"
mid-week Wednesday of the following week.

We also assert the JOB_SIGNAL_TABLE has the right shape (every entry uses
one of the three allowed `kind`s), so a future edit that mistypes a kind
value fails loudly instead of returning None from every classify call.

Times are injected — no wall-clock dependence.

Run: python3 -m pytest tests/test_pulse_freshness.py -q
     python3 tests/test_pulse_freshness.py
"""

import json
import sys
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace, FrozenTime   # noqa: E402

import config   # noqa: E402
import launchd_jobs   # noqa: E402
import pulse_freshness   # noqa: E402
from launchd_jobs import SignalKind   # noqa: E402


DAY = 24 * 3600


def bundled_default_freshness_spec(label: str) -> dict:
    """Parsed freshness spec for `label` from the engine's bundled default registry.

    Reads `app/launchd-jobs.default.json` directly, so the result does not depend
    on whichever operator registry the module-level JOB_SIGNAL_TABLE loaded.
    """
    with open(launchd_jobs.BUNDLED_DEFAULT_JOBS_FILE, "r", encoding="utf-8") as registry_file:
        jobs = json.load(registry_file)["jobs"]
    for job in jobs:
        if config.LAUNCHD_LABEL_PREFIX + job["label_suffix"] == label:
            return launchd_jobs.parse_freshness(label, job["freshness"])
    raise KeyError(f"{label} not in the bundled default launchd job registry")


class ClassifyStatusBoundariesTest(unittest.TestCase):
    """Boundary conditions per cadence: fresh <= 1.5x, stale <= 3x, else failed."""

    def _at(self, now: float, cadence: int, last_activity: float) -> str:
        with FrozenTime(pulse_freshness, now):
            return pulse_freshness.classify_status(cadence, last_activity)

    def test_never_fired_is_failed(self):
        # last_activity=None is the "never ran" signal.
        self.assertEqual(pulse_freshness.classify_status(DAY, None), "failed")

    def test_no_cadence_is_not_scheduled(self):
        self.assertEqual(pulse_freshness.classify_status(None, 0.0), "not-scheduled")

    def test_daily_job_fresh_recent(self):
        # A daily brief written 4 hours ago is fresh (4h << 1.5 * 24h = 36h).
        now = 10_000.0
        self.assertEqual(self._at(now, DAY, now - 4 * 3600), "fresh")

    def test_daily_job_fresh_edge_at_36h(self):
        # exactly 1.5x cadence is still "fresh" per the <= boundary.
        now = 10_000.0
        self.assertEqual(self._at(now, DAY, now - int(1.5 * DAY)), "fresh")

    def test_daily_job_stale_between_1_5x_and_3x(self):
        now = 10_000.0
        # 2x cadence -> stale
        self.assertEqual(self._at(now, DAY, now - 2 * DAY), "stale")

    def test_daily_job_failed_past_3x(self):
        now = 10_000.0
        self.assertEqual(self._at(now, DAY, now - int(3.5 * DAY)), "failed")

    def test_weekly_job_fresh_through_wednesday_next_week(self):
        # Sunday-weekly cadence = 7d. A brief from 10 days ago is still
        # fresh (10 < 1.5 * 7 = 10.5).
        week = 7 * DAY
        now = 10_000.0
        self.assertEqual(self._at(now, week, now - 10 * DAY), "fresh")

    def test_weekly_job_stale_at_two_weeks_out(self):
        week = 7 * DAY
        now = 10_000.0
        self.assertEqual(self._at(now, week, now - 14 * DAY), "stale")

    def test_weekly_job_failed_past_three_weeks_out(self):
        week = 7 * DAY
        now = 10_000.0
        self.assertEqual(self._at(now, week, now - 25 * DAY), "failed")


class CadenceSecondsTest(unittest.TestCase):
    def test_start_interval(self):
        self.assertEqual(
            pulse_freshness.cadence_seconds({"StartInterval": 300}), 300,
        )

    def test_keep_alive_returns_none(self):
        self.assertIsNone(pulse_freshness.cadence_seconds({"KeepAlive": True}))

    def test_no_calendar_returns_none(self):
        self.assertIsNone(pulse_freshness.cadence_seconds({}))

    def test_monthly_day_of_month(self):
        # 31d cadence for monthly jobs.
        plist = {"StartCalendarInterval": {"Day": 2, "Hour": 4}}
        self.assertEqual(pulse_freshness.cadence_seconds(plist), 31 * DAY)

    def test_weekly_single_weekday(self):
        # `Weekday` set with one entry -> 7d cadence.
        plist = {"StartCalendarInterval": {"Weekday": 0, "Hour": 21}}
        self.assertEqual(pulse_freshness.cadence_seconds(plist), 7 * DAY)

    def test_weekly_three_weekdays(self):
        # Mon/Wed/Fri -> 7 // 3 = 2 days cadence between fires.
        plist = {"StartCalendarInterval": [
            {"Weekday": 1, "Hour": 23}, {"Weekday": 3, "Hour": 23},
            {"Weekday": 5, "Hour": 23},
        ]}
        self.assertEqual(pulse_freshness.cadence_seconds(plist), 2 * DAY)

    def test_daily_hour_only(self):
        # A single-hour daily = 24h cadence.
        plist = {"StartCalendarInterval": {"Hour": 7}}
        self.assertEqual(pulse_freshness.cadence_seconds(plist), 24 * 3600)


class SignalTableShapeTest(unittest.TestCase):
    """`JOB_SIGNAL_TABLE` covers exactly the three signal kinds."""

    ALLOWED_KINDS = {"newest_in_dir", "file_mtime", "neutral"}

    def test_every_entry_has_a_valid_kind(self):
        for label, spec in pulse_freshness.JOB_SIGNAL_TABLE.items():
            self.assertIn(spec.get("kind"), self.ALLOWED_KINDS,
                          f"job {label} has invalid kind: {spec.get('kind')!r}")

    def test_newest_in_dir_entries_have_dir(self):
        for label, spec in pulse_freshness.JOB_SIGNAL_TABLE.items():
            if spec["kind"] != "newest_in_dir":
                continue
            self.assertIn("dir", spec,
                          f"newest_in_dir job {label} missing `dir`")

    def test_file_mtime_entries_have_path(self):
        for label, spec in pulse_freshness.JOB_SIGNAL_TABLE.items():
            if spec["kind"] != "file_mtime":
                continue
            self.assertIn("path", spec,
                          f"file_mtime job {label} missing `path`")


class NeutralAndScheduledClassificationTest(unittest.TestCase):
    """Neutral jobs (no artifact) surface as 'scheduled' via pulse.build_job_row."""

    def test_daemon_watchdog_is_neutral(self):
        self.assertTrue(pulse_freshness.is_neutral("com.mineru.daemon-watchdog"))

    def test_cleanup_retention_is_neutral(self):
        self.assertTrue(pulse_freshness.is_neutral("com.mineru.cleanup-retention"))

    def test_brief_writer_is_not_neutral(self):
        self.assertFalse(pulse_freshness.is_neutral("com.mineru.morning-brief"))

    def test_unknown_label_is_not_neutral(self):
        self.assertFalse(pulse_freshness.is_neutral("com.some.unknown-job"))


class SignalTimestampTest(unittest.TestCase):
    """`signal_timestamp` picks up mtimes from the real artifact location."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_newest_in_dir_returns_max_mtime(self):
        # Write a couple of "briefs" into briefs_morning under the synthetic root.
        (self.ws.root / "briefs_morning").mkdir(parents=True, exist_ok=True)
        older = self.ws.root / "briefs_morning" / "older.md"
        newer = self.ws.root / "briefs_morning" / "newer.md"
        older.write_text("x")
        newer.write_text("x")
        import os
        os.utime(older, (1000.0, 1000.0))
        os.utime(newer, (2000.0, 2000.0))
        ts = pulse_freshness.signal_timestamp("com.mineru.morning-brief")
        self.assertEqual(ts, 2000.0)

    def test_neutral_returns_none(self):
        self.assertIsNone(pulse_freshness.signal_timestamp("com.mineru.daemon-watchdog"))

    def test_unmapped_label_returns_none(self):
        self.assertIsNone(pulse_freshness.signal_timestamp("com.some.unknown"))

    def test_file_mtime_returns_stat(self):
        # Pin house-scan's spec to the engine's bundled default registry: the
        # module-level table loads from the operator's registry when one exists,
        # and an operator may map house-scan to a different signal kind.
        label = "com.mineru.house-scan"
        bundled_spec = bundled_default_freshness_spec(label)
        self.assertEqual(bundled_spec["kind"], SignalKind.FILE_MTIME)
        target = self.ws.root / "logs" / "house-scan" / "house-scan.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("log line\n")
        import os
        os.utime(target, (12345.0, 12345.0))
        with mock.patch.dict(pulse_freshness.JOB_SIGNAL_TABLE, {label: bundled_spec}):
            ts = pulse_freshness.signal_timestamp(label)
        self.assertEqual(ts, 12345.0)


class PathStartsWithTest(unittest.TestCase):
    def test_component_wise_ancestor_matches(self):
        self.assertTrue(pulse_freshness.path_starts_with(
            Path("/tmp/memory/daily/x.md"), Path("/tmp/memory/daily")))

    def test_string_startswith_over_match_is_prevented(self):
        # The bug this method is here to prevent: "memory/daily" (string)
        # would swallow "memory/daily-tags". Component-wise must NOT.
        self.assertFalse(pulse_freshness.path_starts_with(
            Path("/tmp/memory/daily-tags/x.md"),
            Path("/tmp/memory/daily"),
        ))


if __name__ == "__main__":
    unittest.main()
