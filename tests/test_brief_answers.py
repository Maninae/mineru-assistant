#!/usr/bin/env python3
"""Unit tests for app/brief_answers.py: status markers + the answer-note append.

Run: /usr/bin/python3 -m pytest tests/test_brief_answers.py -q
"""

import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _app_test_setup  # noqa: E402,F401  (puts app/ on sys.path)

import brief_answers  # noqa: E402
from brief_answers import (  # noqa: E402
    MARKER_FILED_PREFIX,
    MARKER_PENDING_FILING,
    MARKER_UNANSWERED,
    STATUS_FILED,
    STATUS_PENDING_FILING,
    STATUS_UNANSWERED,
)


FIXED_NOW = datetime(2026, 11, 2, 10, 45, tzinfo=timezone(timedelta(hours=-8), "PST"))
QUESTION_BODY = "🦊 **Curiosity question**\n\nWhat did the quiz make you?\n"


class ClassifyAnswerStatusTest(unittest.TestCase):
    def test_unanswered(self):
        self.assertEqual(brief_answers.classify_answer_status(MARKER_UNANSWERED + "\n\nQ"), STATUS_UNANSWERED)

    def test_pending_with_trailing_whitespace(self):
        self.assertEqual(brief_answers.classify_answer_status(MARKER_PENDING_FILING + "  \r\nQ"),
                         STATUS_PENDING_FILING)

    def test_filed_with_suffix(self):
        text = MARKER_FILED_PREFIX + " · saved to memory/people/x.md\n\nQ"
        self.assertEqual(brief_answers.classify_answer_status(text), STATUS_FILED)

    def test_no_marker(self):
        self.assertIsNone(brief_answers.classify_answer_status("# Morning\n\n" + MARKER_UNANSWERED))
        self.assertIsNone(brief_answers.classify_answer_status(""))


class ValidateAnswerTextTest(unittest.TestCase):
    def test_rejects_non_string(self):
        for bad in (None, 5, ["a"], {"t": "a"}):
            self.assertIsNone(brief_answers.validate_answer_text(bad))

    def test_rejects_empty_and_whitespace(self):
        self.assertIsNone(brief_answers.validate_answer_text(""))
        self.assertIsNone(brief_answers.validate_answer_text(" \n\t "))

    def test_rejects_too_long(self):
        self.assertIsNone(brief_answers.validate_answer_text("x" * 4001))
        self.assertEqual(len(brief_answers.validate_answer_text("x" * 4000)), 4000)

    def test_rejects_nul(self):
        self.assertIsNone(brief_answers.validate_answer_text("a\x00b"))

    def test_strips_and_normalizes_newlines(self):
        self.assertEqual(brief_answers.validate_answer_text("  hi\r\nthere \n"), "hi\nthere")


class FormatAnswerNoteTest(unittest.TestCase):
    def test_shape(self):
        note = brief_answers.format_answer_note("Charmander.", FIXED_NOW)
        self.assertEqual(note, "\n\n---\n\n**💬 Answer · Nov 2, 2026, 10:45 AM PST**\n\nCharmander.\n")

    def test_single_digit_hour_and_pm(self):
        later = FIXED_NOW.replace(hour=15, minute=5)
        self.assertIn("Nov 2, 2026, 3:05 PM PST", brief_answers.format_answer_note("x", later))


class AppendAnswerNoteTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="mineru-answers-")
        self.brief_path = Path(self.tmpdir.name) / "question-2026-11-01.md"

    def tearDown(self):
        self.tmpdir.cleanup()

    def write(self, text: str) -> None:
        self.brief_path.write_text(text, encoding="utf-8")

    def read(self) -> str:
        return self.brief_path.read_text(encoding="utf-8")

    def test_unanswered_flips_marker_and_appends_note(self):
        self.write(MARKER_UNANSWERED + "\n\n" + QUESTION_BODY + "\n\n")
        status = brief_answers.append_answer_note(self.brief_path, "Charmander.", FIXED_NOW)
        self.assertEqual(status, STATUS_PENDING_FILING)
        result = self.read()
        self.assertTrue(result.startswith(MARKER_PENDING_FILING + "\n\n🦊"))
        self.assertNotIn("UNANSWERED", result)
        # Exactly one blank line between the old last line and the rule.
        self.assertIn("What did the quiz make you?\n\n---\n\n**💬 Answer · ", result)
        self.assertTrue(result.endswith("\n\nCharmander.\n"))

    def test_second_note_on_pending_keeps_marker(self):
        self.write(MARKER_UNANSWERED + "\n\n" + QUESTION_BODY)
        brief_answers.append_answer_note(self.brief_path, "First.", FIXED_NOW)
        status = brief_answers.append_answer_note(self.brief_path, "Second.", FIXED_NOW)
        self.assertEqual(status, STATUS_PENDING_FILING)
        result = self.read()
        self.assertEqual(result.count(MARKER_PENDING_FILING), 1)
        self.assertEqual(result.count("**💬 Answer · "), 2)
        self.assertLess(result.index("First."), result.index("Second."))
        self.assertIn("First.\n\n---\n\n", result)

    def test_filed_reopens_to_pending_and_appends_answer(self):
        self.write(MARKER_FILED_PREFIX + " · saved to memory/x.md\n\n" + QUESTION_BODY)
        status = brief_answers.append_answer_note(self.brief_path, "Correction.", FIXED_NOW)
        self.assertEqual(status, STATUS_PENDING_FILING)
        result = self.read()
        self.assertTrue(result.startswith(MARKER_PENDING_FILING + "\n\n🦊"))
        self.assertNotIn("saved to memory/x.md", result)
        self.assertIn("**💬 Answer · ", result)
        self.assertTrue(result.endswith("\n\nCorrection.\n"))

    def test_no_marker_appends_plain_note_and_keeps_line_one(self):
        original = "# Morning brief\n\nRain until noon.\n"
        self.write(original)
        status = brief_answers.append_answer_note(self.brief_path, "Bring a coat.", FIXED_NOW)
        self.assertIsNone(status)
        result = self.read()
        self.assertTrue(result.startswith(original.rstrip() + "\n\n---\n\n**💬 Note · "))
        self.assertNotIn("**💬 Answer", result)
        self.assertTrue(result.endswith("\n\nBring a coat.\n"))

    def test_file_mode_preserved(self):
        self.write(MARKER_UNANSWERED + "\n\n" + QUESTION_BODY)
        os.chmod(self.brief_path, 0o644)
        brief_answers.append_answer_note(self.brief_path, "x", FIXED_NOW)
        self.assertEqual(stat.S_IMODE(self.brief_path.stat().st_mode), 0o644)

    def test_mtime_preserved_so_the_question_keeps_its_date(self):
        self.write(MARKER_UNANSWERED + "\n\n" + QUESTION_BODY)
        original_mtime_ns = 1_700_000_000_000_000_000
        os.utime(self.brief_path, ns=(original_mtime_ns, original_mtime_ns))
        brief_answers.append_answer_note(self.brief_path, "x", FIXED_NOW)
        self.assertEqual(self.brief_path.stat().st_mtime_ns, original_mtime_ns)

    def test_non_utf8_file_raises_and_is_left_untouched(self):
        raw = (MARKER_UNANSWERED + "\n\ncaf").encode("utf-8") + b"\xe9\n"
        self.brief_path.write_bytes(raw)
        with self.assertRaises(brief_answers.BriefNotAnswerableError):
            brief_answers.append_answer_note(self.brief_path, "x", FIXED_NOW)
        self.assertEqual(self.brief_path.read_bytes(), raw)

    def test_content_without_trailing_newline(self):
        self.write(MARKER_UNANSWERED + "\n\nNo newline at end")
        brief_answers.append_answer_note(self.brief_path, "x", FIXED_NOW)
        self.assertIn("No newline at end\n\n---\n\n", self.read())

    def test_marker_only_file(self):
        self.write(MARKER_UNANSWERED)
        brief_answers.append_answer_note(self.brief_path, "x", FIXED_NOW)
        self.assertTrue(self.read().startswith(MARKER_PENDING_FILING + "\n\n---\n\n"))

    def test_no_temp_files_left_behind(self):
        self.write(MARKER_UNANSWERED + "\n\n" + QUESTION_BODY)
        brief_answers.append_answer_note(self.brief_path, "x", FIXED_NOW)
        self.assertEqual(sorted(p.name for p in self.brief_path.parent.iterdir()), [self.brief_path.name])


if __name__ == "__main__":
    unittest.main()
