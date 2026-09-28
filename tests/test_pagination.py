#!/usr/bin/env python3
"""Tests for `app/feeds.py` pagination + cursor round-trips.

The pagination invariant that mattered most in v1 review was the **tie bug**:
when two briefs share an mtime, a cursor-based "older than" comparison against
mtime alone silently DROPS one of them at the page boundary. The fix uses the
compound sort key `(-mtime, filename)`, encodes both fields into the opaque
cursor, and pages by strict-greater on that compound key. These tests wire
two same-mtime briefs and verify both survive across pages.

Also covers: empty feed, single item, exactly-`limit` items, has_more +
next_before correctness, dotfiles/symlinks excluded from listings and counts.

Run: python3 -m pytest tests/test_pagination.py -q
     python3 tests/test_pagination.py
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace   # noqa: E402

import feeds   # noqa: E402


def _all_filenames_across_pages(feed_id: str, limit: int) -> list:
    """Walk every page for `feed_id`, collect all items' display filenames."""
    all_names = []
    cursor = None
    for _guard in range(100):     # protects the test from an infinite pagination bug.
        page = feeds.list_feed_page(feed_id, seen_set=set(),
                                    before_cursor=cursor, limit=limit)
        all_names.extend(item["filename"] for item in page["items"])
        if not page["has_more"]:
            break
        cursor = feeds.decode_page_cursor(page["next_before"])
    return all_names


class CursorRoundTripTest(unittest.TestCase):
    def test_encode_then_decode_preserves_values(self):
        for mtime, name in [(1234567.89, "morning-2026-08-17.md"),
                            (0.0, "edge.md"),
                            (1e12, "briefs/nested/x.md")]:
            token = feeds.encode_page_cursor(mtime, name)
            got_mtime, got_name = feeds.decode_page_cursor(token)
            self.assertAlmostEqual(got_mtime, mtime, places=6)
            self.assertEqual(got_name, name)

    def test_decode_rejects_garbage(self):
        with self.assertRaises(ValueError):
            feeds.decode_page_cursor("not-base64-!!!!")

    def test_decode_rejects_non_object_payload(self):
        import base64
        token = base64.urlsafe_b64encode(b'[1,2]').decode("ascii")
        with self.assertRaises(ValueError):
            feeds.decode_page_cursor(token)

    def test_decode_rejects_wrong_field_types(self):
        import base64
        payload = json.dumps({"mtime": "not-a-number", "filename": "x"}).encode("utf-8")
        token = base64.urlsafe_b64encode(payload).decode("ascii")
        with self.assertRaises(ValueError):
            feeds.decode_page_cursor(token)


class PaginationTieMtimeSurvivesTest(unittest.TestCase):
    """The tie bug: two briefs sharing an mtime must both survive paging."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_two_same_mtime_briefs_both_returned_across_pages(self):
        # Three briefs total; two share the exact same mtime, one is older.
        # Page size 1 guarantees the cursor boundary lands INSIDE the tie group
        # — the exact spot the mtime-only cursor used to silently drop a file.
        shared_mtime = 1_000_000.0
        older_mtime = shared_mtime - 100.0
        self.ws.write_brief("alpha", "tie-a.md", "# Tie A\n\nbody\n", mtime=shared_mtime)
        self.ws.write_brief("alpha", "tie-b.md", "# Tie B\n\nbody\n", mtime=shared_mtime)
        self.ws.write_brief("alpha", "older.md", "# Older\n\nbody\n", mtime=older_mtime)

        collected = _all_filenames_across_pages("alpha", limit=1)
        self.assertEqual(
            sorted(collected),
            ["older.md", "tie-a.md", "tie-b.md"],
            "same-mtime siblings must both survive when paging by cursor",
        )
        # And no duplicates — a bad cursor could revisit a tie item.
        self.assertEqual(len(collected), len(set(collected)),
                         "no brief may appear on two pages")

    def test_within_tie_order_is_deterministic(self):
        shared_mtime = 42.0
        self.ws.write_brief("alpha", "b.md", "# B\n\nbody\n", mtime=shared_mtime)
        self.ws.write_brief("alpha", "a.md", "# A\n\nbody\n", mtime=shared_mtime)
        # Sort key is (-mtime, filename), so within tie the ALPHABETICALLY smaller
        # name comes first. Assert that so the cursor semantics have a floor to
        # stand on.
        page = feeds.list_feed_page("alpha", seen_set=set(), limit=10)
        names = [item["filename"] for item in page["items"]]
        self.assertEqual(names, ["a.md", "b.md"])


class PaginationEdgeCaseTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_empty_feed_returns_no_items_and_no_cursor(self):
        page = feeds.list_feed_page("alpha", seen_set=set(), limit=10)
        self.assertEqual(page["items"], [])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_before"])

    def test_single_item(self):
        self.ws.write_brief("alpha", "one.md", "# One\n\nbody\n", mtime=100.0)
        page = feeds.list_feed_page("alpha", seen_set=set(), limit=10)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["filename"], "one.md")
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_before"])

    def test_exactly_limit_items_no_more_flag(self):
        for i in range(5):
            self.ws.write_brief("alpha", f"item-{i}.md", f"# Item {i}\n", mtime=100.0 + i)
        page = feeds.list_feed_page("alpha", seen_set=set(), limit=5)
        self.assertEqual(len(page["items"]), 5)
        self.assertFalse(page["has_more"],
                         "has_more must be False when exactly `limit` items remain")
        self.assertIsNone(page["next_before"])

    def test_more_than_limit_has_next_cursor(self):
        for i in range(7):
            self.ws.write_brief("alpha", f"item-{i}.md", f"# Item {i}\n", mtime=100.0 + i)
        page = feeds.list_feed_page("alpha", seen_set=set(), limit=3)
        self.assertEqual(len(page["items"]), 3)
        self.assertTrue(page["has_more"])
        self.assertIsNotNone(page["next_before"])
        # The cursor must be a decodable opaque token.
        mtime, name = feeds.decode_page_cursor(page["next_before"])
        self.assertEqual(name, page["items"][-1]["filename"])

    def test_seen_flag_populated_from_seen_set(self):
        self.ws.write_brief("alpha", "read.md", "# R\n", mtime=100.0)
        self.ws.write_brief("alpha", "unread.md", "# U\n", mtime=101.0)
        page = feeds.list_feed_page("alpha", seen_set={"read.md"}, limit=10)
        by_name = {item["filename"]: item for item in page["items"]}
        self.assertTrue(by_name["read.md"]["seen"])
        self.assertFalse(by_name["unread.md"]["seen"])


class PaginationDotfileAndSymlinkExcludedTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_dotfile_briefs_excluded_from_listing(self):
        self.ws.write_brief("alpha", "real.md", "# R\n", mtime=100.0)
        self.ws.write_brief("alpha", ".hidden.md", "# H\n", mtime=101.0)
        page = feeds.list_feed_page("alpha", seen_set=set(), limit=10)
        names = [item["filename"] for item in page["items"]]
        self.assertIn("real.md", names)
        self.assertNotIn(".hidden.md", names)

    def test_dotdir_briefs_also_excluded(self):
        # Nested under a dot-directory: `.git/staged.md` must not surface.
        self.ws.write_brief("alpha", ".git/staged.md", "# S\n", mtime=100.0)
        self.ws.write_brief("alpha", "keep.md", "# K\n", mtime=101.0)
        page = feeds.list_feed_page("alpha", seen_set=set(), limit=10)
        names = [item["filename"] for item in page["items"]]
        self.assertEqual(names, ["keep.md"])

    def test_symlink_briefs_excluded_from_listing_and_bulk(self):
        real = self.ws.write_brief("alpha", "real.md", "# R\n", mtime=100.0)
        link = self.ws.root / "briefs_alpha" / "link-to-real.md"
        try:
            os.symlink(str(real), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported here")
        page = feeds.list_feed_page("alpha", seen_set=set(), limit=10)
        names = [item["filename"] for item in page["items"]]
        self.assertEqual(names, ["real.md"])
        # bulk mark-all-read enumerator must agree with the listing.
        bulk = feeds.all_display_filenames_for_feed("alpha")
        self.assertEqual(bulk, ["real.md"])


class SummarizeFeedCountsTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_summary_counts_only_visible_briefs(self):
        self.ws.write_brief("alpha", "a.md", "# A\n", mtime=100.0)
        self.ws.write_brief("alpha", "b.md", "# B\n", mtime=200.0)
        self.ws.write_brief("alpha", ".hidden.md", "# H\n", mtime=300.0)
        # cache is per-test cleared in setup, so no bleed.
        summary = feeds.summarize_feed("alpha", seen_set=set())
        self.assertEqual(summary["total_count"], 2)
        self.assertEqual(summary["unread_count"], 2)
        self.assertEqual(summary["latest_ts"], 200.0)


if __name__ == "__main__":
    unittest.main()
