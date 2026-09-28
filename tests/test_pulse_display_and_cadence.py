#!/usr/bin/env python3
"""Tests for the three humanized fields Pulse now emits per job row.

Covers three surfaces:

- `pulse_launchd.display_name_for_label`: mapped labels render their curated
  human title; anything unmapped falls back to the stripped basename so a new
  com.mineru.* plist still reads as something.
- `pulse_launchd.cadence_bucket`: closed-set classification the frontend uses
  to group jobs under Daily / Weekly / Monthly / Always-on sections. Six
  possible values; each has a boundary test.
- `pulse.classify_heartbeat_age` + `pulse.build_heartbeat_signals`: dead is
  not stale. A signal past 30 days classifies as `off` so a six-month-dead
  producer stops reading as an active problem.

Run: python3 -m pytest tests/test_pulse_display_and_cadence.py -q
     python3 tests/test_pulse_display_and_cadence.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace   # noqa: E402

import pulse   # noqa: E402
import pulse_launchd   # noqa: E402


DAY = 24 * 3600


class DisplayNameForLabelTest(unittest.TestCase):
    def test_curated_label_maps_to_human_title(self):
        self.assertEqual(
            pulse_launchd.display_name_for_label("com.mineru.morning-brief"),
            "Morning Briefing",
        )
        self.assertEqual(
            pulse_launchd.display_name_for_label("com.mineru.weekly-deep-consolidation"),
            "Weekly Deep Consolidation",
        )
        self.assertEqual(
            pulse_launchd.display_name_for_label("com.mineru.pre-export-journals"),
            "Journal Pre-Export",
        )

    def test_unmapped_com_mineru_label_falls_back_to_basename(self):
        self.assertEqual(
            pulse_launchd.display_name_for_label("com.mineru.new-thing"),
            "new-thing",
        )

    def test_unrelated_label_is_returned_unchanged(self):
        self.assertEqual(
            pulse_launchd.display_name_for_label("com.other.thing"),
            "com.other.thing",
        )

    def test_registry_display_names_are_non_empty_strings(self):
        # Internal-consistency guard: every mapped label produces a non-empty
        # string title. Catches a registry file that shipped with an empty
        # `display_name` or a truncated JSON payload. Host-agnostic — does
        # not iterate ~/Library/LaunchAgents (which would fail on a machine
        # that has a plist installed but not yet registered locally).
        assert pulse_launchd.PULSE_JOB_DISPLAY_NAMES, "registry loaded empty"
        for label, display_name in pulse_launchd.PULSE_JOB_DISPLAY_NAMES.items():
            self.assertIsInstance(display_name, str, f"{label} value not a string")
            self.assertTrue(display_name.strip(), f"{label} maps to empty title")


class CadenceBucketTest(unittest.TestCase):
    def test_keep_alive_is_always_on(self):
        self.assertEqual(pulse_launchd.cadence_bucket({"KeepAlive": True}), "always_on")

    def test_start_interval_is_interval(self):
        self.assertEqual(
            pulse_launchd.cadence_bucket({"StartInterval": 300}),
            "interval",
        )

    def test_day_of_month_is_monthly(self):
        plist = {"StartCalendarInterval": {"Day": 2, "Hour": 4, "Minute": 7}}
        self.assertEqual(pulse_launchd.cadence_bucket(plist), "monthly")

    def test_single_weekday_is_weekly(self):
        plist = {"StartCalendarInterval": {"Weekday": 0, "Hour": 21}}
        self.assertEqual(pulse_launchd.cadence_bucket(plist), "weekly")

    def test_multi_weekday_is_still_weekly(self):
        # Mon/Wed/Fri list-shape is still a weekly cadence bucket.
        plist = {"StartCalendarInterval": [
            {"Weekday": 1, "Hour": 23},
            {"Weekday": 3, "Hour": 23},
            {"Weekday": 5, "Hour": 23},
        ]}
        self.assertEqual(pulse_launchd.cadence_bucket(plist), "weekly")

    def test_hour_only_is_daily(self):
        plist = {"StartCalendarInterval": {"Hour": 7, "Minute": 0}}
        self.assertEqual(pulse_launchd.cadence_bucket(plist), "daily")

    def test_empty_plist_is_unknown(self):
        self.assertEqual(pulse_launchd.cadence_bucket({}), "unknown")

    def test_calendar_without_recognized_keys_is_unknown(self):
        # A degenerate calendar entry with only Minute is not a schedule we
        # can bucket — return `unknown` rather than picking a wrong bucket.
        plist = {"StartCalendarInterval": {"Minute": 0}}
        self.assertEqual(pulse_launchd.cadence_bucket(plist), "unknown")


class HeartbeatAgeClassifierTest(unittest.TestCase):
    """Boundaries: <= 2d fresh, <= 30d stale, > 30d off."""

    def _at(self, now: float, epoch: float) -> str:
        return pulse.classify_heartbeat_age(epoch, now=now)

    def test_recent_is_fresh(self):
        now = 100_000.0
        self.assertEqual(self._at(now, now - 1 * 3600), "fresh")

    def test_edge_at_two_days_is_fresh(self):
        now = 100_000.0
        # <= 2d boundary is inclusive → fresh.
        self.assertEqual(self._at(now, now - 2 * DAY), "fresh")

    def test_just_past_two_days_is_stale(self):
        now = 100_000.0
        self.assertEqual(self._at(now, now - (2 * DAY + 1)), "stale")

    def test_edge_at_thirty_days_is_stale(self):
        now = 100_000.0
        # <= 30d boundary is inclusive → stale (not off).
        self.assertEqual(self._at(now, now - 30 * DAY), "stale")

    def test_just_past_thirty_days_is_off(self):
        now = 100_000.0
        self.assertEqual(self._at(now, now - (30 * DAY + 1)), "off")

    def test_ancient_signal_is_off(self):
        now = 100_000.0
        self.assertEqual(self._at(now, now - 200 * DAY), "off")


class BuildHeartbeatSignalsTest(unittest.TestCase):
    def test_flattens_nested_group_and_carries_status(self):
        # `lastChecks` is the shape currently on disk — nested one level deep.
        # Every epoch below is well past 30 days from any real "now", so all
        # three should classify as `off`.
        heartbeat = {"lastChecks": {"socialPulse": 1770160620, "email": 1770160620}}
        signals = pulse.build_heartbeat_signals(heartbeat)
        by_key = {row["key"]: row for row in signals}
        self.assertEqual(set(by_key), {"socialPulse", "email"})
        for row in signals:
            self.assertEqual(row["group"], "lastChecks")
            self.assertEqual(row["epoch"], 1770160620.0)
            self.assertEqual(row["status"], "off")

    def test_flat_top_level_scalar_is_captured_without_group(self):
        heartbeat = {"lastPing": 1770160620}
        signals = pulse.build_heartbeat_signals(heartbeat)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["key"], "lastPing")
        self.assertNotIn("group", signals[0])

    def test_non_numeric_leaves_are_skipped(self):
        # Only numeric epochs classify; strings/nulls carry no timestamp.
        heartbeat = {"lastChecks": {"note": "some string", "count": None}}
        self.assertEqual(pulse.build_heartbeat_signals(heartbeat), [])

    def test_millisecond_epoch_is_down_scaled(self):
        # A producer that writes ms-precision timestamps must still classify
        # (mirrors the frontend's existing coerceRow tolerance).
        heartbeat = {"lastPing": 1770160620000}
        signals = pulse.build_heartbeat_signals(heartbeat)
        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0]["epoch"], 1770160620.0)

    def test_missing_heartbeat_returns_empty_list(self):
        self.assertEqual(pulse.build_heartbeat_signals(None), [])
        self.assertEqual(pulse.build_heartbeat_signals({}), [])


class ComputeSnapshotIntegrationTest(unittest.TestCase):
    """The full snapshot carries the new fields end-to-end."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()
        # Point pulse.HEARTBEAT_PATH into the synthetic workspace so the
        # snapshot reads a controlled heartbeat instead of the operator's real file.
        self.original_heartbeat_path = pulse.HEARTBEAT_PATH
        pulse.HEARTBEAT_PATH = self.ws.root / "memory" / "heartbeat-state.json"
        pulse.HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        pulse.HEARTBEAT_PATH.write_text(
            '{"lastChecks": {"socialPulse": 1770160620}}',
            encoding="utf-8",
        )

    def tearDown(self):
        pulse.HEARTBEAT_PATH = self.original_heartbeat_path
        self.ws.teardown()

    def test_snapshot_includes_heartbeat_signals_and_jobs_shape(self):
        snapshot = pulse.compute_pulse_snapshot()
        # Backward-compat: raw heartbeat dict still present.
        self.assertIn("heartbeat", snapshot)
        self.assertEqual(snapshot["heartbeat"],
                         {"lastChecks": {"socialPulse": 1770160620}})
        # Additive: heartbeat_signals emits the enriched per-signal rows.
        self.assertIn("heartbeat_signals", snapshot)
        signals = snapshot["heartbeat_signals"]
        self.assertEqual(len(signals), 1)
        row = signals[0]
        self.assertEqual(row["key"], "socialPulse")
        self.assertEqual(row["group"], "lastChecks")
        self.assertEqual(row["status"], "off")


if __name__ == "__main__":
    unittest.main()
