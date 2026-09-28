#!/usr/bin/env python3
"""Tests for library.humanize_dated_filename + the list_directory wiring.

The mobile Reports list historically read filenames verbatim
(`2026-03-06-quarterly-review`) and wrapped across three lines. Now the
backend parses that convention into a `display_name` ("Quarterly Review")
and a companion `date_label` ("Mar 6, 2026") so the row collapses to a title +
subtitle. Non-matching filenames keep the existing display_name behavior
(strip `.md`, otherwise unchanged) and omit `date_label` entirely.

Run: python3 -m pytest tests/test_library_dated_filenames.py -q
     python3 tests/test_library_dated_filenames.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace   # noqa: E402

import library   # noqa: E402


class HumanizeDatedFilenameTest(unittest.TestCase):
    def test_matches_yyyy_mm_dd_slug_convention(self):
        result = library.humanize_dated_filename("2026-03-06-quarterly-review.md")
        self.assertEqual(result, ("Quarterly Review", "Mar 6, 2026"))

    def test_single_digit_day_has_no_leading_zero(self):
        result = library.humanize_dated_filename("2026-01-01-a.md")
        self.assertEqual(result, ("A", "Jan 1, 2026"))

    def test_two_digit_day_stays_two_digit(self):
        result = library.humanize_dated_filename("2026-12-25-holiday-report.md")
        self.assertEqual(result, ("Holiday Report", "Dec 25, 2026"))

    def test_pdf_and_html_extensions_also_matched(self):
        pdf_result = library.humanize_dated_filename("2026-03-06-invoice.pdf")
        self.assertEqual(pdf_result, ("Invoice", "Mar 6, 2026"))
        # "tv" is in ACRONYM_SEGMENTS, so it uppercases whole.
        html_result = library.humanize_dated_filename("2026-06-11-cdrama-pinyin-tv-setup.html")
        self.assertEqual(html_result, ("Cdrama Pinyin TV Setup", "Jun 11, 2026"))

    def test_no_date_prefix_returns_none(self):
        # A perfectly normal report filename without the date convention.
        self.assertIsNone(library.humanize_dated_filename("papers.md"))
        self.assertIsNone(library.humanize_dated_filename("wdc.md"))
        self.assertIsNone(library.humanize_dated_filename("invoice.pdf"))

    def test_invalid_date_returns_none_even_when_regex_matches(self):
        # Feb 30 is not real; the datetime constructor rejects it, so the
        # humanizer must fall back to None rather than emitting a lie.
        self.assertIsNone(library.humanize_dated_filename("2026-02-30-foo.md"))
        self.assertIsNone(library.humanize_dated_filename("2026-13-01-foo.md"))

    def test_empty_slug_returns_none(self):
        # `2026-03-06-.md` has no slug words after cleanup — refuse to emit
        # an empty display_name; caller falls back to the raw basename.
        self.assertIsNone(library.humanize_dated_filename("2026-03-06-.md"))

    def test_extensionless_directory_shape_is_matched(self):
        # A dir-shaped name (no dot in the tail) uses the whole tail as slug.
        result = library.humanize_dated_filename("2026-03-06-quarterly-review")
        self.assertEqual(result, ("Quarterly Review", "Mar 6, 2026"))

    def test_extensionless_directory_single_word_slug(self):
        result = library.humanize_dated_filename("2026-08-05-scratch")
        self.assertEqual(result, ("Scratch", "Aug 5, 2026"))

    def test_dir_shape_with_invalid_date_returns_none(self):
        # Date invariants apply to dir-shaped inputs too.
        self.assertIsNone(library.humanize_dated_filename("2026-02-30-scratch"))

    def test_trailing_dot_returns_none(self):
        # A weird `scratch.` tail (empty extension) fails cleanly rather than
        # producing a stripped "Scratch" that hides the malformed shape.
        self.assertIsNone(library.humanize_dated_filename("2026-03-06-scratch."))


class HumanizeSlugWordAcronymTest(unittest.TestCase):
    """Acronym allowlist rendering (F4 from the design-pass review).

    `word.capitalize()` alone would emit `Ai` / `Cc` / `Tv` / `Api` for the
    common slug segments that read as acronyms — nasty on the mobile Reports
    list. `humanize_slug_word` uppercases known acronyms whole and lets every
    other segment fall through to `capitalize`.
    """

    def test_known_acronym_uppercases_whole(self):
        self.assertEqual(library.humanize_slug_word("ai"), "AI")
        self.assertEqual(library.humanize_slug_word("cc"), "CC")
        self.assertEqual(library.humanize_slug_word("api"), "API")
        self.assertEqual(library.humanize_slug_word("llm"), "LLM")
        self.assertEqual(library.humanize_slug_word("seo"), "SEO")

    def test_alphanumeric_acronyms_uppercase(self):
        self.assertEqual(library.humanize_slug_word("3d"), "3D")
        self.assertEqual(library.humanize_slug_word("v1"), "V1")
        self.assertEqual(library.humanize_slug_word("k12"), "K12")
        self.assertEqual(library.humanize_slug_word("1m"), "1M")

    def test_non_acronym_word_uses_capitalize(self):
        self.assertEqual(library.humanize_slug_word("quarterly"), "Quarterly")
        self.assertEqual(library.humanize_slug_word("review"), "Review")
        # Multi-syllable non-acronyms — first letter up, rest down.
        self.assertEqual(library.humanize_slug_word("PIPELINE"), "Pipeline")

    def test_membership_check_is_case_insensitive(self):
        # An input already in caps still resolves through the allowlist.
        self.assertEqual(library.humanize_slug_word("AI"), "AI")
        self.assertEqual(library.humanize_slug_word("Api"), "API")


class HumanizeDatedFilenameAcronymTest(unittest.TestCase):
    """Acronym rendering as it lands in the humanizer output pair."""

    def test_ai_cc_slug_renders_as_acronyms(self):
        result = library.humanize_dated_filename("2026-05-01-ai-cc-pipeline.md")
        self.assertEqual(result, ("AI CC Pipeline", "May 1, 2026"))

    def test_llm_api_migration(self):
        result = library.humanize_dated_filename("2026-08-24-llm-api-migration.md")
        self.assertEqual(result, ("LLM API Migration", "Aug 24, 2026"))

    def test_seo_sitemap(self):
        result = library.humanize_dated_filename("2026-07-14-seo-sitemap.md")
        self.assertEqual(result, ("SEO Sitemap", "Jul 14, 2026"))

    def test_3d_and_v1_slugs(self):
        result = library.humanize_dated_filename("2026-06-01-3d-renderer-v1.md")
        self.assertEqual(result, ("3D Renderer V1", "Jun 1, 2026"))

    def test_mixed_acronym_and_plain_words(self):
        # The generic-word branch must still fire for non-acronym segments.
        result = library.humanize_dated_filename("2026-04-12-pdf-invoice-export.md")
        self.assertEqual(result, ("PDF Invoice Export", "Apr 12, 2026"))

    def test_extensionless_directory_with_acronym(self):
        result = library.humanize_dated_filename("2026-07-25-mineru-cli-foundation")
        self.assertEqual(result, ("Mineru CLI Foundation", "Jul 25, 2026"))


class ListDirectoryDatedFilenameFieldsTest(unittest.TestCase):
    """`list_directory` merges the humanization into the JSON payload."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_dated_md_gains_display_name_and_date_label(self):
        self.ws.write_library_file("reports", "2026-03-06-quarterly-review.md", "# body")
        listing = library.list_directory("reports", "")
        by_name = {e["name"]: e for e in listing["entries"]}
        entry = by_name["2026-03-06-quarterly-review.md"]
        self.assertEqual(entry["display_name"], "Quarterly Review")
        self.assertEqual(entry["date_label"], "Mar 6, 2026")

    def test_non_dated_md_omits_date_label(self):
        self.ws.write_library_file("reports", "papers.md", "# body")
        listing = library.list_directory("reports", "")
        by_name = {e["name"]: e for e in listing["entries"]}
        entry = by_name["papers.md"]
        self.assertEqual(entry["display_name"], "papers")
        self.assertNotIn("date_label", entry)

    def test_dated_pdf_also_gets_both_fields(self):
        self.ws.write_library_file("reports", "2026-05-09-invoice.pdf", binary=b"%PDF-1.4\n")
        listing = library.list_directory("reports", "")
        by_name = {e["name"]: e for e in listing["entries"]}
        entry = by_name["2026-05-09-invoice.pdf"]
        self.assertEqual(entry["display_name"], "Invoice")
        self.assertEqual(entry["date_label"], "May 9, 2026")

    def test_extensionless_dated_directory_gets_both_fields(self):
        # Directories saved under the same `YYYY-MM-DD-slug` convention
        # (e.g. a report generator that drops assets alongside the doc)
        # humanize just like the sibling files, so the mobile Reports list
        # doesn't split its rendering rule down the file/dir boundary.
        (self.ws.root / "reports" / "2026-03-06-quarterly-review").mkdir(
            parents=True, exist_ok=True,
        )
        listing = library.list_directory("reports", "")
        by_name = {e["name"]: e for e in listing["entries"]}
        entry = by_name["2026-03-06-quarterly-review"]
        self.assertEqual(entry["kind"], "dir")
        self.assertEqual(entry["display_name"], "Quarterly Review")
        self.assertEqual(entry["date_label"], "Mar 6, 2026")

    def test_extensionless_dated_directory_short_slug(self):
        # Single-word dated directories humanize correctly too.
        (self.ws.root / "reports" / "2026-08-05-scratch").mkdir(parents=True, exist_ok=True)
        listing = library.list_directory("reports", "")
        by_name = {e["name"]: e for e in listing["entries"]}
        entry = by_name["2026-08-05-scratch"]
        self.assertEqual(entry["kind"], "dir")
        self.assertEqual(entry["display_name"], "Scratch")
        self.assertEqual(entry["date_label"], "Aug 5, 2026")

    def test_non_dated_directory_still_keeps_raw_name(self):
        # A directory that does NOT match the dated pattern must keep the
        # existing behavior (raw name, no date_label). Guards against
        # the humanizer over-reaching to plain folder names.
        (self.ws.root / "reports" / "images").mkdir(parents=True, exist_ok=True)
        listing = library.list_directory("reports", "")
        by_name = {e["name"]: e for e in listing["entries"]}
        entry = by_name["images"]
        self.assertEqual(entry["kind"], "dir")
        self.assertEqual(entry["display_name"], "images")
        self.assertNotIn("date_label", entry)


if __name__ == "__main__":
    unittest.main()
