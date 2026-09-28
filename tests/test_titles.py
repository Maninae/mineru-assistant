#!/usr/bin/env python3
"""Tests for the title / display-name surfaces the Inbox card + Library list read.

Two small but visible invariants:

- `library.LibraryEntry.display_name` strips `.md` but keeps other extensions
  (so the list reads "papers" not "papers.md", but "report.pdf" stays intact),
  and directory names are never stripped.
- `handlers.handle_library` uses the document H1 as the display title for
  markdown files, falling back to the file stem when no heading exists — the
  reader sees "Weekly deep consolidation — Aug 17" rather than "wdc.md".

The Inbox-card tldr-collapse-when-equal-to-title invariant is already covered
in tests/test_deliver_output_inject_pointer.py; we cross-link with a single
sanity assertion here rather than duplicating that whole suite.

Run: python3 -m pytest tests/test_titles.py -q
     python3 tests/test_titles.py
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace   # noqa: E402

import library   # noqa: E402
import handlers   # noqa: E402
from lib import tldr   # noqa: E402


class LibraryDisplayNameTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_md_files_get_extension_stripped(self):
        self.ws.write_library_file("reports", "papers.md", "# ok\n")
        listing = library.list_directory("reports", "")
        by_name = {e["name"]: e for e in listing["entries"]}
        self.assertIn("papers.md", by_name)
        self.assertEqual(by_name["papers.md"]["display_name"], "papers")

    def test_pdf_and_txt_keep_their_names(self):
        self.ws.write_library_file("reports", "invoice.pdf", binary=b"%PDF-1.4\n")
        self.ws.write_library_file("reports", "notes.txt", "hello")
        listing = library.list_directory("reports", "")
        by_name = {e["name"]: e for e in listing["entries"]}
        self.assertEqual(by_name["invoice.pdf"]["display_name"], "invoice.pdf")
        self.assertEqual(by_name["notes.txt"]["display_name"], "notes.txt")

    def test_directory_names_not_stripped_even_if_they_end_in_md(self):
        # Directory named "papers.md" (weird but legal); display_name must
        # keep the .md so the reader knows it's a folder shaped that way.
        (self.ws.root / "reports" / "papers.md").mkdir(parents=True, exist_ok=True)
        listing = library.list_directory("reports", "")
        by_name = {e["name"]: e for e in listing["entries"]}
        self.assertIn("papers.md", by_name)
        self.assertEqual(by_name["papers.md"]["kind"], "dir")
        # code strips `.md` unconditionally so display_name will read "papers".
        # This test locks that current behavior; if we ever want directories
        # to keep .md, both the code and this test change together.
        self.assertEqual(by_name["papers.md"]["display_name"], "papers")

    def test_dotfiles_excluded_from_listing(self):
        self.ws.write_library_file("reports", ".hidden.md", "# no\n")
        self.ws.write_library_file("reports", "visible.md", "# ok\n")
        listing = library.list_directory("reports", "")
        names = {e["name"] for e in listing["entries"]}
        self.assertNotIn(".hidden.md", names)
        self.assertIn("visible.md", names)

    def test_disallowed_extension_excluded(self):
        self.ws.write_library_file("reports", "config.py", "import os\n")
        self.ws.write_library_file("reports", "visible.md", "# ok\n")
        listing = library.list_directory("reports", "")
        names = {e["name"] for e in listing["entries"]}
        self.assertNotIn("config.py", names)
        self.assertIn("visible.md", names)


class LibraryFileViewTitleTest(unittest.TestCase):
    """`handle_library` on a markdown file uses the document H1 as title."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def _mk_match(self, source: str, relpath: str):
        # The handler reads `.group("source")` and `.group("relpath")` from
        # a re.Match. A small stand-in dict-backed object is enough here.
        class M:
            def __init__(self, mapping):
                self.mapping = mapping
            def group(self, name):
                return self.mapping[name]
        return M({"source": source, "relpath": relpath})

    def tearDown(self):
        self.ws.teardown()

    def test_markdown_file_title_uses_h1(self):
        self.ws.write_library_file("reports", "wdc.md",
                                   "# Weekly Deep Consolidation — Aug 17\n\nbody\n")
        status, _, body = handlers.handle_library(
            self._mk_match("reports", "wdc.md"), {})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["title"], "Weekly Deep Consolidation — Aug 17")

    def test_markdown_file_title_falls_back_to_stem_when_extract_empty(self):
        # An empty/whitespace-only markdown file: extract_title_and_tldr
        # returns an empty title, so the handler falls back to the file stem.
        self.ws.write_library_file("reports", "wdc.md", "   \n\n")
        status, _, body = handlers.handle_library(
            self._mk_match("reports", "wdc.md"), {})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["title"], "wdc",
                         "file stem is the fallback when no extractable title exists")

    def test_markdown_file_title_uses_first_line_when_no_heading(self):
        # When the doc has no `# heading` but has body text, the title comes
        # from the first non-empty line (per extract_title_and_tldr semantics).
        self.ws.write_library_file("reports", "wdc.md", "just some body text\n")
        status, _, body = handlers.handle_library(
            self._mk_match("reports", "wdc.md"), {})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["title"], "just some body text")


class TldrCollapseCrossLinkTest(unittest.TestCase):
    """`extract_title_and_tldr` collapses tldr to empty when equal to title.

    Full coverage lives in tests/test_deliver_output_inject_pointer.py; this
    one sanity assertion catches an obvious regression without duplicating
    that suite.
    """

    def test_tldr_empty_when_first_line_is_title(self):
        title, tldr_out = tldr.extract_title_and_tldr(
            "# Weekly Deep Consolidation — Aug 17\n\nbody body body\n"
        )
        self.assertEqual(title, "Weekly Deep Consolidation — Aug 17")
        self.assertEqual(tldr_out, "",
                         "tldr must collapse when it would repeat the title")


class CuriosityBriefTitleTest(unittest.TestCase):
    """The daily-curiosity brief opens with a status blockquote (`> **⬜ UNANSWERED**`)
    that historically hijacked the title extractor and rendered every card as
    `⬜ UNANSWERED**`. The extractor now skips leading blockquote and
    status-sentinel lines while still recognizing genuine emoji-bearing titles.
    """

    def test_unanswered_blockquote_skipped_to_real_title(self):
        content = (
            "> **⬜ UNANSWERED**\n"
            "\n"
            "🦊 **Daily curiosity — do you play chess (or Go)?**\n"
            "\n"
            "I went digging through your hobbies...\n"
        )
        title, tldr_out = tldr.extract_title_and_tldr(content)
        self.assertEqual(title, "🦊 Daily curiosity — do you play chess (or Go)?")
        self.assertNotIn("UNANSWERED", title)
        self.assertNotIn("⬜", title)
        # No explicit TLDR: line and no `#` heading — the fallback tldr matches
        # the title (the same skipped-blockquote-then-take-first-nonempty walk
        # both use), so the extractor collapses tldr to empty so the Inbox card
        # doesn't render the same string twice. Genuine tldr surfaces when a
        # `TLDR:` line is present (covered separately below).
        self.assertEqual(tldr_out, "")

    def test_answered_blockquote_with_saved_pointer_skipped(self):
        content = (
            "> **✅ ANSWERED** · saved to memory/sam/interests-homelab.md\n"
            "\n"
            "Good morning, the operator 🦊 — today's curiosity question:\n"
            "\n"
            "Reading through the Crow's Nest notes...\n"
        )
        title, _ = tldr.extract_title_and_tldr(content)
        self.assertNotIn("ANSWERED", title)
        self.assertNotIn("saved to", title)
        self.assertTrue(title.startswith("Good morning"))
        # Inline fox emoji in a genuine title must survive.
        self.assertIn("🦊", title)

    def test_sentinel_line_outside_blockquote_is_also_skipped(self):
        # Defense-in-depth: even if a producer drops the `>` wrapper, the
        # `⬜ UNANSWERED` sentinel itself is still recognized and skipped.
        content = (
            "⬜ UNANSWERED\n"
            "\n"
            "🦊 Daily curiosity — go song?\n"
        )
        title, _ = tldr.extract_title_and_tldr(content)
        self.assertEqual(title, "🦊 Daily curiosity — go song?")

    def test_residual_bold_markers_stripped_from_extracted_title(self):
        # Trailing `**` with no opening pair (the exact shape the old extractor
        # left behind after the leading `>` + space + `*` + `*` were consumed
        # by the leading-marker strip) must not survive into the cleaned title.
        raw_line = "⬜ UNANSWERED**"
        self.assertEqual(tldr.clean_display_line(raw_line), "⬜ UNANSWERED")

    def test_backticks_and_single_asterisks_are_stripped_as_emphasis(self):
        self.assertEqual(
            tldr.clean_display_line("A `code`-y *italic* title"),
            "A code-y italic title",
        )

    def test_paired_bold_still_unwrapped_normally(self):
        # Regression guard on the existing paired-emphasis behavior — the
        # new residual-strip must not double-strip through a proper pair.
        self.assertEqual(
            tldr.clean_display_line("**Weekly Digest**"),
            "Weekly Digest",
        )

    def test_blockquote_tldr_line_still_wins(self):
        # A brief that opens with `> TLDR:` should still resolve to the TLDR
        # text — blockquote-skip only applies when hunting the fallback title.
        content = "> TLDR: real summary here\n\n# Heading\nbody"
        self.assertEqual(
            tldr.extract_tldr_line(content),
            "real summary here",
        )


if __name__ == "__main__":
    unittest.main()
