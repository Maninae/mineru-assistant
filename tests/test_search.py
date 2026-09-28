#!/usr/bin/env python3
"""Tests for `app/search.py` — literal-substring search over briefs + library.

The security contract is small but sharp: the query is treated as a **literal
substring** (Python's C `str.find` under the hood), never compiled as a regex,
so pathological patterns like `.*` or `(a+)+$` can neither match everything
nor blow up in catastrophic backtracking. Result ranking is title > tldr >
filename > body, newest-mtime tiebreak; dotfiles, symlinks, and files outside
the allowlist never appear; PDFs/HTML get their contents skipped (body-scan is
restricted to text-ish extensions).

Run: python3 -m pytest tests/test_search.py -q
     python3 tests/test_search.py
"""

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace   # noqa: E402

import search   # noqa: E402


class SearchLiteralSubstringTest(unittest.TestCase):
    """`q` must be a literal substring — never compiled as a regex."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()
        # Two briefs: one contains the literal ".*", the other doesn't.
        # If the code ever compiled q as a regex, `.*` would match everything.
        self.ws.write_brief("alpha", "with-literal-dot-star.md",
                            "# Regex literals\n\nBody has the literal .* pattern.\n",
                            mtime=100.0)
        self.ws.write_brief("alpha", "no-dot-star.md",
                            "# Plain text\n\nNothing regexy in here.\n",
                            mtime=200.0)

    def tearDown(self):
        self.ws.teardown()

    def test_dot_star_matches_only_literal_occurrences(self):
        payload = search.run_search(".*", "briefs", 40)
        result_files = [r["filename"] for r in payload["results"]]
        self.assertIn("with-literal-dot-star.md", result_files)
        self.assertNotIn("no-dot-star.md", result_files,
                         "'.*' must NOT be treated as a regex wildcard")

    def test_regex_meta_query_returns_no_match_when_not_literally_present(self):
        # Neither file literally contains `(?:foo)`, so the search must
        # return zero results even though the pattern is valid regex.
        payload = search.run_search("(?:foo)", "briefs", 40)
        self.assertEqual(payload["results"], [])

    def test_pathological_query_is_redos_safe(self):
        # Bury a mildly bad-for-regex query in a large body. Even without
        # a real ReDoS payload, if the code ever swaps to `re.search` on
        # user input a similar class of input will hang. This test proves
        # the substring path finishes fast on a wide input surface.
        big_body = "a" * 8000 + "\n"
        self.ws.write_brief("beta", "big.md", "# Big\n\n" + big_body, mtime=300.0)
        started = time.time()
        search.run_search("(a+)+$", "briefs", 40)
        elapsed = time.time() - started
        # 1s is a very generous ceiling; a real ReDoS regression takes seconds+.
        self.assertLess(elapsed, 1.0, "substring search must not hang on regex-shaped input")


class SearchRankingAndTierTest(unittest.TestCase):
    """Ranking: title > tldr > filename > body, newest mtime within a tier."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()
        # All four hits carry the token "needle" in a different field so
        # the sort is unambiguous. Mtimes are deliberately mixed so we can
        # also assert the newest-first-within-tier tiebreak.
        self.ws.write_brief("alpha", "body-hit.md",
                            "# Anodyne heading\n\nA line with needle somewhere in the body\n",
                            mtime=500.0)
        self.ws.write_brief("alpha", "filename-needle.md",
                            "# Anodyne heading\n\nsome anodyne body\n",
                            mtime=400.0)
        self.ws.write_brief("alpha", "tldr-hit.md",
                            "# Anodyne heading\n\nTLDR: needle in the tldr\n\nbody\n",
                            mtime=300.0)
        self.ws.write_brief("alpha", "title-hit.md",
                            "# Title with needle\n\nplain body\n",
                            mtime=200.0)

    def tearDown(self):
        self.ws.teardown()

    def test_tier_order_title_before_tldr_before_filename_before_body(self):
        payload = search.run_search("needle", "briefs", 40)
        matched_in = [r["matched_in"] for r in payload["results"]]
        # Every result should be one of these four, in this order.
        self.assertEqual(matched_in, ["title", "tldr", "filename", "body"])

    def test_within_tier_newest_mtime_wins(self):
        # Two title-hits with different mtimes: newest first.
        self.ws.write_brief("alpha", "title-hit-2.md",
                            "# Another needle title\n\nbody\n", mtime=250.0)
        self.ws.write_brief("alpha", "title-hit-3.md",
                            "# Yet another needle title\n\nbody\n", mtime=150.0)
        payload = search.run_search("needle", "briefs", 40)
        title_hits = [r for r in payload["results"] if r["matched_in"] == "title"]
        mtimes = [r["mtime"] for r in title_hits]
        self.assertEqual(mtimes, sorted(mtimes, reverse=True),
                         "within a tier, results must be newest-first")


class SearchScopeTest(unittest.TestCase):
    """scope=briefs walks only briefs_*; scope=all also walks library sources."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()
        self.ws.write_brief("alpha", "brief-hit.md",
                            "# Brief\n\nneedle in the brief\n", mtime=100.0)
        self.ws.write_library_file("reports", "report-hit.md",
                                   "# Report\n\nneedle in a report\n", mtime=200.0)
        self.ws.write_library_file("creations", "creation-hit.md",
                                   "# Creation\n\nneedle in a creation\n", mtime=300.0)

    def tearDown(self):
        self.ws.teardown()

    def test_scope_briefs_excludes_library(self):
        payload = search.run_search("needle", "briefs", 40)
        kinds = {r["kind"] for r in payload["results"]}
        self.assertEqual(kinds, {"brief"})

    def test_scope_all_includes_library(self):
        payload = search.run_search("needle", "all", 40)
        kinds = {r["kind"] for r in payload["results"]}
        self.assertEqual(kinds, {"brief", "library"})


class SearchSnippetTest(unittest.TestCase):
    """Snippet contains the match and is bounded to ~SEARCH_SNIPPET_CHARS."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_body_snippet_contains_match_and_is_bounded(self):
        # Bury `needle` deep in a big body so the snippet has to window around it.
        prefix = "some prose " * 200
        suffix = "more prose " * 200
        body = prefix + " here comes the needle in the body " + suffix
        self.ws.write_brief("alpha", "big-body.md", "# Boring\n\n" + body, mtime=100.0)
        payload = search.run_search("needle", "briefs", 40)
        results = [r for r in payload["results"] if r["filename"] == "big-body.md"]
        self.assertEqual(len(results), 1)
        snippet = results[0]["snippet"]
        self.assertIn("needle", snippet.lower())
        # SEARCH_SNIPPET_CHARS is 160; allow a small margin for the "…" edges.
        self.assertLessEqual(len(snippet), search.SEARCH_SNIPPET_CHARS + 16)


class SearchDotfileAndSymlinkTest(unittest.TestCase):
    """Dotfiles are skipped from library walks; symlinks are skipped everywhere."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_library_dotfile_skipped(self):
        self.ws.write_library_file("reports", ".hidden-hit.md",
                                   "# Hidden\n\nneedle stays hidden\n", mtime=100.0)
        self.ws.write_library_file("reports", "visible.md",
                                   "# Visible\n\nneedle is here too\n", mtime=200.0)
        payload = search.run_search("needle", "all", 40)
        relpaths = {r.get("relpath") for r in payload["results"] if r.get("kind") == "library"}
        self.assertIn("visible.md", relpaths)
        self.assertNotIn(".hidden-hit.md", relpaths,
                         "dotfiles must not surface in library search")

    def test_library_symlink_skipped(self):
        real = self.ws.write_library_file("reports", "real.md",
                                          "# Real\n\nneedle here\n", mtime=100.0)
        link = self.ws.root / "reports" / "link-to-real.md"
        try:
            os.symlink(str(real), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported on this filesystem")
        payload = search.run_search("needle", "all", 40)
        relpaths = [r.get("relpath") for r in payload["results"] if r.get("kind") == "library"]
        self.assertIn("real.md", relpaths)
        self.assertNotIn("link-to-real.md", relpaths,
                         "symlinks must never surface — they can escape the allowlist")


class SearchBodyExtensionAllowlistTest(unittest.TestCase):
    """PDF/HTML in the library can match on filename/title but never on body."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_pdf_body_not_scanned_but_filename_matches(self):
        # A PDF-looking file whose "body" contains the needle. Since PDFs are
        # binary + not in LIBRARY_BODY_EXTENSIONS, body-scan must be skipped.
        # The needle DOES appear in the filename, so we should still get a
        # `filename` match — proving the file was visited and correctly filtered.
        pdf_bytes = b"%PDF-1.4\nHere is a needle sitting inside the raw bytes\n"
        self.ws.write_library_file("reports", "invoice-needle.pdf",
                                   binary=pdf_bytes, mtime=100.0)
        payload = search.run_search("needle", "all", 40)
        pdf_hits = [r for r in payload["results"]
                    if r.get("kind") == "library" and r.get("relpath") == "invoice-needle.pdf"]
        self.assertEqual(len(pdf_hits), 1)
        self.assertIn(pdf_hits[0]["matched_in"], ("filename", "title"),
                      "PDF body must never be scanned; must match on filename/title only")

    def test_pdf_without_needle_in_name_not_returned(self):
        # This PDF has the needle only in its bytes, not in name or "title".
        # Since the body of a PDF is never scanned, it must NOT match.
        pdf_bytes = b"%PDF-1.4\n" + b"needle " * 100 + b"\n"
        self.ws.write_library_file("reports", "quarterly.pdf",
                                   binary=pdf_bytes, mtime=100.0)
        payload = search.run_search("needle", "all", 40)
        self.assertEqual(
            [r for r in payload["results"] if r.get("relpath") == "quarterly.pdf"],
            [],
            "PDF body bytes must be invisible to search",
        )


class SearchAllowlistBoundaryTest(unittest.TestCase):
    """Search only ever walks configured feed dirs and library sources."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_files_outside_allowlisted_roots_never_returned(self):
        # A file just above the library-source root: search must never see it.
        outside = self.ws.root / "outside-secret.md"
        outside.write_text("# Secret\n\nneedle sitting outside every allowed root\n",
                           encoding="utf-8")
        payload = search.run_search("needle", "all", 40)
        # Every returned path must live under briefs_alpha/, briefs_beta/,
        # reports/, or creations/ — never outside the allowlist.
        for row in payload["results"]:
            if row["kind"] == "brief":
                self.assertIn(row["filename"], {})  # no briefs written; empty set is fine
            elif row["kind"] == "library":
                self.assertIn(row["source"], {"reports", "creations"})


class SearchHandlerRejectsBadQueryTest(unittest.TestCase):
    """The handler enforces the empty / oversized-q rejection before search runs."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_empty_query_rejected_400(self):
        import handlers
        status, _, body = handlers.handle_search(None, {"q": [""]})
        self.assertEqual(status, 400)
        self.assertIn(b"empty query", body)

    def test_missing_query_rejected_400(self):
        import handlers
        status, _, body = handlers.handle_search(None, {})
        self.assertEqual(status, 400)

    def test_oversized_query_rejected_400(self):
        import handlers
        big_q = "x" * (search.SEARCH_QUERY_MAX_LEN + 1)
        status, _, body = handlers.handle_search(None, {"q": [big_q]})
        self.assertEqual(status, 400)
        self.assertIn(b"query too long", body)


if __name__ == "__main__":
    unittest.main()
