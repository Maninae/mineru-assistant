#!/usr/bin/env python3
"""Tests for the shared TLDR extraction + the inject-queue pointer payload.

Two surfaces, one implementation:

- Delivery-side (scripts/deliver-output.py + build_inject_pointer): the pointer
  that lands in the daemon's inject queue. Full body must never leak; the TLDR
  and the resolved brief path must both be present.
- Web-side (lib.tldr.extract_title_and_tldr): the (title, tldr) tuple the Inbox
  card consumes. Repeated titles (tldr == title) must collapse to an empty
  tldr so the UI can hide the duplicate row.

Both sides go through `lib/tldr.py`, so the same regex fixes the delivery
false-positive AND the web card. Run: python3 tests/test_deliver_output_inject_pointer.py
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

# lib/ is one level above this tests/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import tldr

# deliver-output.py has a hyphen so importlib is the only way to load it here.
MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "deliver-output.py"
spec = importlib.util.spec_from_file_location("deliver_output", MODULE_PATH)
deliver_output = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deliver_output)


class ExtractTldrLineTest(unittest.TestCase):
    """Covers the delivery-side path. Every case runs against BOTH the
    re-exported `deliver_output.extract_tldr_line` and the underlying
    `lib.tldr.extract_tldr_line`, so a drift between the two would fail loudly.
    """

    def _both(self, content: str) -> str:
        via_delivery = deliver_output.extract_tldr_line(content)
        via_lib = tldr.extract_tldr_line(content)
        self.assertEqual(via_delivery, via_lib, "delivery and lib disagree")
        return via_lib

    def test_explicit_tldr_line_wins_over_heading(self):
        content = "# 🌅 Morning Brief\n\nTLDR: Rain until noon, River's checkup at 3pm.\n\nLong body..."
        self.assertEqual(self._both(content), "Rain until noon, River's checkup at 3pm.")

    def test_bold_tldr_variant(self):
        content = "# Header\n\n**TLDR:** Budget via Example Vendor is the winner.\n"
        self.assertEqual(self._both(content), "Budget via Example Vendor is the winner.")

    def test_falls_back_to_first_heading(self):
        content = "\n## 💰 Weekly Financial Summary — Aug 17\n\nSpending was up..."
        self.assertEqual(self._both(content), "💰 Weekly Financial Summary — Aug 17")

    def test_falls_back_to_first_nonempty_line(self):
        content = "\n\nJuno went for a walk 9 times since Friday.\nMore detail..."
        self.assertEqual(self._both(content), "Juno went for a walk 9 times since Friday.")

    def test_skips_yaml_frontmatter(self):
        content = "---\ndescription: x\ntags: [a]\n---\n# Real Heading\nbody"
        self.assertEqual(self._both(content), "Real Heading")

    def test_truncates_long_lines(self):
        content = "TLDR: " + "word " * 100
        tldr_out = self._both(content)
        self.assertLessEqual(len(tldr_out), tldr.TLDR_MAX_CHARS)
        self.assertTrue(tldr_out.endswith("…"))

    def test_empty_content_returns_empty(self):
        self.assertEqual(self._both(""), "")

    def test_tldr_prefix_words_do_not_hijack(self):
        # "TLDRs"/"TLDR-worthy" prose must NOT match; falls back to the heading.
        content = "TLDRs are great and I love them\n# Actual Heading\nbody"
        self.assertEqual(self._both(content), "Actual Heading")
        content = "# Actual Heading\nTLDR-worthy summary of the day\nbody"
        self.assertEqual(self._both(content), "Actual Heading")

    def test_bare_tldr_with_no_text_falls_back(self):
        content = "TLDR:\n# Fallback Heading\nbody"
        self.assertEqual(self._both(content), "Fallback Heading")

    def test_canonical_tl_semicolon_dr_spelling(self):
        content = "# Header\nTL;DR: The canonical spelling works too.\n"
        self.assertEqual(self._both(content), "The canonical spelling works too.")

    def test_crlf_line_endings(self):
        content = "# Header\r\nTLDR: Windows line endings handled.\r\nbody\r\n"
        self.assertEqual(self._both(content), "Windows line endings handled.")

    def test_real_brief_produces_sane_tldr(self):
        # Sanity against a real workspace brief when available (skipped elsewhere).
        briefs = sorted(Path.home().glob(".mineru/briefs_morning/*.md"))
        if not briefs:
            self.skipTest("no real morning briefs on this machine")
        result = self._both(briefs[-1].read_text(encoding="utf-8"))
        self.assertTrue(result)
        self.assertLessEqual(len(result), tldr.TLDR_MAX_CHARS)
        self.assertNotIn("\n", result)


class ExtractTitleAndTldrWebTest(unittest.TestCase):
    """Covers the web/UI wrapper. Every card is (title, tldr); a matching
    tldr collapses to empty so the Inbox card doesn't show the title twice.
    """

    def test_title_from_first_heading(self):
        title, tldr_out = tldr.extract_title_and_tldr(
            "# Morning Brief\n\nTLDR: Rain, then sun.\n"
        )
        self.assertEqual(title, "Morning Brief")
        self.assertEqual(tldr_out, "Rain, then sun.")

    def test_title_from_first_line_when_no_heading(self):
        title, tldr_out = tldr.extract_title_and_tldr(
            "Juno went to the park 9 times.\nMore body..."
        )
        self.assertEqual(title, "Juno went to the park 9 times.")
        # tldr falls back to the same first line -> empty tldr, no dup row.
        self.assertEqual(tldr_out, "")

    def test_tldr_empty_when_equal_to_title(self):
        # No explicit TLDR: line, single-heading brief -> tldr collapses.
        title, tldr_out = tldr.extract_title_and_tldr(
            "# Weekly Deep Consolidation — Aug 17\n\nbody body body\n"
        )
        self.assertEqual(title, "Weekly Deep Consolidation — Aug 17")
        self.assertEqual(tldr_out, "")

    def test_tldrs_prose_does_not_hijack_title(self):
        # The old regex extracted "'s are great" out of "TLDRs are great" and
        # served that as the tldr; the fixed regex demands the ':' separator.
        content = "TLDRs are great and I love them\n# Actual Heading\nbody"
        title, tldr_out = tldr.extract_title_and_tldr(content)
        self.assertEqual(title, "Actual Heading")
        # tldr fallback lands on the first non-empty line ("TLDRs are great...")
        # which differs from title, so it does show up here.
        self.assertNotIn("'s are great", tldr_out)

    def test_tl_semicolon_dr_web_spelling(self):
        title, tldr_out = tldr.extract_title_and_tldr(
            "# Header\nTL;DR: Canonical spelling wins.\n"
        )
        self.assertEqual(title, "Header")
        self.assertEqual(tldr_out, "Canonical spelling wins.")

    def test_frontmatter_skipped_web(self):
        title, tldr_out = tldr.extract_title_and_tldr(
            "---\ndescription: x\n---\n# Real Heading\nTLDR: Skip the frontmatter.\n"
        )
        self.assertEqual(title, "Real Heading")
        self.assertEqual(tldr_out, "Skip the frontmatter.")


class BuildInjectPointerTest(unittest.TestCase):
    def test_pointer_contains_label_path_tldr_but_not_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "morning-2026-08-17.md"
            body_marker = "seventeen paragraphs of detailed brief body"
            brief.write_text(f"# Morning Brief\n\nTLDR: All quiet today.\n\n{body_marker}")
            payload = deliver_output.build_inject_pointer(
                "morning-2026-08-17", brief, brief.read_text()
            )
        self.assertIn("morning-2026-08-17", payload)
        self.assertIn(str(brief), payload)
        self.assertIn("TLDR: All quiet today.", payload)
        self.assertNotIn(body_marker, payload)
        self.assertIn("delivered to the operator via Telegram", payload)

    def test_pointer_omits_companion_line_when_none(self):
        # A lone brief (no sidecar json) must not grow a "Structured data" line.
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "morning-2026-08-17.md"
            brief.write_text("# Morning Brief\n\nTLDR: All quiet.\n")
            payload = deliver_output.build_inject_pointer(
                "morning-2026-08-17", brief, brief.read_text()
            )
        self.assertNotIn("Structured data", payload)

    def test_pointer_surfaces_companion_ids_manifest(self):
        # A triage brief that dropped a `<stem>-ids.json` manifest must have it
        # named in the pointer so a later "clear" reply acts on real data.
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "triage-2026-09-13.md"
            brief.write_text("📬 Inbox Triage\n\nTLDR: 21 emails flagged safe across 5 clumps.\n")
            manifest = Path(tmp) / "triage-2026-09-13-ids.json"
            manifest.write_text('{"date": "2026-09-13", "clumps": {}}')
            payload = deliver_output.build_inject_pointer(
                "triage-2026-09-13", brief, brief.read_text()
            )
        self.assertIn("Structured data", payload)
        self.assertIn(str(manifest.resolve()), payload)
        self.assertIn("TLDR: 21 emails flagged safe across 5 clumps.", payload)
        # The .md brief itself is never mistaken for a companion data file.
        self.assertNotIn(f"{brief.resolve()} —", payload)


if __name__ == "__main__":
    unittest.main()
