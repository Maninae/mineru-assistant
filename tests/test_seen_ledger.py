#!/usr/bin/env python3
"""Tests for `app/seen_ledger.py` — the app's ONE writable surface.

Invariants guarded here:

- FIFO cap at `SEEN_LIST_MAX`: adding N+1 items evicts the oldest so the on-disk
  list never grows unbounded no matter how noisy the producer is.
- Idempotent dedup: marking the same brief seen twice is a no-op, not a growth.
- Bulk `mark_feed_items_seen`: one load + one atomic write, skips junk entries
  without raising.
- Atomic write: crash mid-write leaves the previous ledger intact; the on-disk
  file is valid JSON and the container carries `feeds` + `library` keys.
- chmod 600: the ledger is owner rw only, never world-readable.
- Type / shape rejects: non-string keys raise `ValueError` and don't corrupt disk.

Run: python3 -m pytest tests/test_seen_ledger.py -q
     python3 tests/test_seen_ledger.py
"""

import json
import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace   # noqa: E402

import seen_ledger   # noqa: E402


class SeenLedgerFIFOCapTest(unittest.TestCase):
    """Adding beyond SEEN_LIST_MAX evicts the oldest entries."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_cap_evicts_oldest_and_stays_at_max(self):
        # Add SEEN_LIST_MAX + 3 unique filenames one at a time.
        cap = seen_ledger.SEEN_LIST_MAX
        for i in range(cap + 3):
            seen_ledger.mark_feed_item_seen("alpha", f"brief-{i:06d}.md")
        ledger = seen_ledger.load_ledger()
        seen = ledger["feeds"]["alpha"]
        self.assertEqual(len(seen), cap,
                         "list must be exactly capped at SEEN_LIST_MAX")
        # The first 3 entries must have been evicted; the last 3 must be present.
        self.assertNotIn("brief-000000.md", seen)
        self.assertNotIn("brief-000001.md", seen)
        self.assertNotIn("brief-000002.md", seen)
        self.assertIn(f"brief-{cap + 2:06d}.md", seen)

    def test_bulk_mark_respects_cap(self):
        cap = seen_ledger.SEEN_LIST_MAX
        filenames = [f"brief-{i:06d}.md" for i in range(cap + 25)]
        marked = seen_ledger.mark_feed_items_seen("alpha", filenames)
        # marked counts every filename we tried, per the docstring.
        self.assertEqual(marked, len(filenames))
        seen = seen_ledger.load_ledger()["feeds"]["alpha"]
        self.assertEqual(len(seen), cap)


class SeenLedgerDedupTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_idempotent_mark_does_not_grow_list(self):
        for _ in range(5):
            seen_ledger.mark_feed_item_seen("alpha", "same-file.md")
        seen = seen_ledger.load_ledger()["feeds"]["alpha"]
        self.assertEqual(seen, ["same-file.md"],
                         "adding the same filename must not grow the list")

    def test_library_dedup_independent_of_feeds(self):
        seen_ledger.mark_feed_item_seen("alpha", "x.md")
        seen_ledger.mark_library_item_seen("reports", "x.md")
        ledger = seen_ledger.load_ledger()
        self.assertEqual(ledger["feeds"]["alpha"], ["x.md"])
        self.assertEqual(ledger["library"]["reports"], ["x.md"])


class SeenLedgerBulkTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_bulk_skips_junk_entries_but_counts_them(self):
        # Per the docstring: non-string / empty entries are skipped, not raised.
        marked = seen_ledger.mark_feed_items_seen(
            "alpha", ["real1.md", "", 42, None, "real2.md"]
        )
        seen = seen_ledger.load_ledger()["feeds"]["alpha"]
        # Only the two valid strings landed on disk.
        self.assertEqual(sorted(seen), ["real1.md", "real2.md"])
        # `marked` counts filenames actually applied (real1, real2) — junk skipped.
        self.assertEqual(marked, 2)

    def test_bulk_requires_string_feed_id(self):
        with self.assertRaises(ValueError):
            seen_ledger.mark_feed_items_seen("", ["x.md"])
        with self.assertRaises(ValueError):
            seen_ledger.mark_feed_items_seen(None, ["x.md"])


class SeenLedgerAtomicWriteAndModeTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_written_file_is_valid_json_with_expected_shape(self):
        seen_ledger.mark_feed_item_seen("alpha", "a.md")
        seen_ledger.mark_library_item_seen("reports", "r.md")
        with open(seen_ledger.SEEN_LEDGER_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("feeds", data)
        self.assertIn("library", data)
        self.assertEqual(data["feeds"]["alpha"], ["a.md"])
        self.assertEqual(data["library"]["reports"], ["r.md"])

    def test_file_permissions_are_0600(self):
        seen_ledger.mark_feed_item_seen("alpha", "a.md")
        st_mode = os.stat(seen_ledger.SEEN_LEDGER_PATH).st_mode
        perms = stat.S_IMODE(st_mode)
        self.assertEqual(perms, stat.S_IRUSR | stat.S_IWUSR,
                         f"expected 0600, got {oct(perms)}")

    def test_no_stray_tmp_files_left_behind(self):
        # Atomic writer uses a .tmp file + os.replace; nothing should linger.
        seen_ledger.mark_feed_item_seen("alpha", "a.md")
        seen_ledger.mark_feed_item_seen("alpha", "b.md")
        state_dir = seen_ledger.SEEN_LEDGER_PATH.parent
        leftover = list(state_dir.glob("seen.*.tmp"))
        self.assertEqual(leftover, [], f"stray tmp files: {leftover}")

    def test_corrupt_ledger_is_reset_not_raised(self):
        # A hand-corrupted ledger must not crash the loader; it should reset.
        seen_ledger.SEEN_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        seen_ledger.SEEN_LEDGER_PATH.write_text("not-json{{", encoding="utf-8")
        ledger = seen_ledger.load_ledger()
        self.assertEqual(ledger, {"feeds": {}, "library": {}})


class SeenLedgerTypeRejectTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_non_string_feed_id_or_filename_raises(self):
        for bad_feed_id, bad_filename in [(None, "a.md"), ("alpha", None),
                                          (42, "a.md"), ("alpha", 42), ("", "a.md"),
                                          ("alpha", "")]:
            with self.assertRaises(ValueError):
                seen_ledger.mark_feed_item_seen(bad_feed_id, bad_filename)

    def test_non_string_library_source_or_relpath_raises(self):
        for bad_source, bad_relpath in [(None, "x.md"), ("reports", None),
                                        (42, "x.md"), ("reports", 42),
                                        ("", "x.md"), ("reports", "")]:
            with self.assertRaises(ValueError):
                seen_ledger.mark_library_item_seen(bad_source, bad_relpath)

    def test_rejected_input_does_not_corrupt_ledger(self):
        seen_ledger.mark_feed_item_seen("alpha", "good.md")
        try:
            seen_ledger.mark_feed_item_seen(None, "bad.md")
        except ValueError:
            pass
        ledger = seen_ledger.load_ledger()
        self.assertEqual(ledger["feeds"]["alpha"], ["good.md"],
                         "a rejected write must not touch the ledger")


class AppendWithFifoCapDirectTest(unittest.TestCase):
    """Direct exercise of the helper — makes the cap semantics unambiguous."""

    def test_pure_function_evicts_oldest_only(self):
        cap = seen_ledger.SEEN_LIST_MAX
        lst = [f"x-{i}" for i in range(cap)]
        seen_ledger.append_with_fifo_cap(lst, "x-new")
        self.assertEqual(len(lst), cap)
        self.assertEqual(lst[-1], "x-new")
        self.assertNotIn("x-0", lst,
                         "oldest entry must be evicted when cap is exceeded")

    def test_pure_function_dedup_is_no_op(self):
        lst = ["a", "b", "c"]
        seen_ledger.append_with_fifo_cap(lst, "b")
        self.assertEqual(lst, ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
