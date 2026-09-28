#!/usr/bin/env python3
"""Tests for the iMessage reformatting arm (lib/imessage_format.py) and the
--imessage-format selector in scripts/deliver-output.py.

The iMessage arm reshapes a Telegram-markdown brief for a recipient without
Telegram (e.g. a family member). Profiles pinned here: `pet_summary` (few
spread-out topical bullets, blank line before each), `household_finance` (dense day-grouped
purchase list, compact, per-merchant emoji) and `monthly_finance`. These tests pin the profiles and
the flag plumbing so a future edit can't silently regress one report's copy.

Run: python3 tests/test_imessage_format.py
"""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.imessage_format import (
    DEFAULT_IMESSAGE_FORMAT,
    MONTHLY_SECTION_DIVIDER,
    format_for_imessage,
    format_for_imessage_by_name,
    format_monthly_financial,
)

# deliver-output.py has a hyphen so importlib is the only way to load it here.
MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "deliver-output.py"
spec = importlib.util.spec_from_file_location("deliver_output", MODULE_PATH)
deliver_output = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deliver_output)


PET_SUMMARY_BRIEF = """\
🐱 **Pet summary** · Wed, Sep 2

🗑️ **Waste drawer:** 34%

**Worth noting**
• **Weight stable.** Median ~13.8 lbs, no shift.
• **Usage normal.** ~2–3 visits/day.
"""

TRANSACTIONS_BRIEF = """\
📊 **Daily Transactions — Aug 30–Sep 3**

**20 spending transactions, 5 days: ~$1,020**

*Wed Sep 2*
• Chewy: $86.58 (pet supplies)
• Starbucks: $15.15
• Amazon: $14.79 (pending)

*Sat Aug 30*
• Corner Coffee: $35.99

---

⚠️ **2 items to eyeball:**

1. **Acme Holdings LLC: $149** — unfamiliar vendor. Expected?
"""


class SharedMarkdownStripTest(unittest.TestCase):
    """Both profiles must de-markdown: no literal `**`, `*italic*`, or `---`."""

    def test_no_bold_markers_survive(self):
        for fmt in ("pet_summary", "household_finance"):
            brief = PET_SUMMARY_BRIEF if fmt == "pet_summary" else TRANSACTIONS_BRIEF
            out = format_for_imessage_by_name(brief, fmt)
            self.assertNotIn("**", out, f"{fmt}: bold marker leaked")

    def test_italic_day_headers_destarred(self):
        out = format_for_imessage_by_name(TRANSACTIONS_BRIEF, "household_finance")
        self.assertIn("Wed Sep 2", out)
        self.assertNotIn("*Wed Sep 2*", out)
        self.assertNotIn("*Sat Aug 30*", out)

    def test_horizontal_rule_dropped(self):
        out = format_for_imessage_by_name(TRANSACTIONS_BRIEF, "household_finance")
        for line in out.split("\n"):
            self.assertNotEqual(line.strip(), "---", "raw --- rule survived")


class PetSummaryProfileTest(unittest.TestCase):
    """Pet summary: topical emoji bullets + a blank line before each bullet."""

    def test_topical_emoji_from_lead_phrase(self):
        out = format_for_imessage_by_name(PET_SUMMARY_BRIEF, "pet_summary")
        self.assertIn("⚖️ Weight stable.", out)
        self.assertIn("🚽 Usage normal.", out)

    def test_blank_line_before_each_bullet(self):
        out = format_for_imessage_by_name(PET_SUMMARY_BRIEF, "pet_summary")
        lines = out.split("\n")
        for idx, line in enumerate(lines):
            if line.startswith(("⚖️", "🚽")):
                self.assertEqual(
                    lines[idx - 1].strip(), "",
                    "Pet-summary bullets should have a blank line before them",
                )

    def test_default_profile_is_pet_summary(self):
        # No profile name == the pet-summary profile == the bare-function default.
        self.assertEqual(DEFAULT_IMESSAGE_FORMAT, "pet_summary")
        self.assertEqual(
            format_for_imessage_by_name(PET_SUMMARY_BRIEF),
            format_for_imessage(PET_SUMMARY_BRIEF),
        )


class TransactionsProfileTest(unittest.TestCase):
    """Transactions: per-merchant emoji, compact (no per-bullet blank lines)."""

    def test_merchant_emoji_mapping(self):
        out = format_for_imessage_by_name(TRANSACTIONS_BRIEF, "household_finance")
        self.assertIn("🐾 Chewy:", out)          # pet
        self.assertIn("☕ Starbucks:", out)       # coffee
        self.assertIn("📦 Amazon:", out)          # shipping
        self.assertIn("☕ Corner Coffee:", out)   # coffee (contains "coffee")

    def test_unknown_vendor_falls_back_to_money(self):
        # Acme Holdings LLC matches no keyword -> the 💵 fallback. It appears in a
        # numbered flag line, not a bullet, so it should NOT be emoji-prefixed;
        # the fallback only applies to true bullet lines. Assert the bullet path.
        out = format_for_imessage_by_name("• Acme Holdings LLC: $149.00\n", "household_finance")
        self.assertIn("💵 Acme Holdings LLC: $149.00", out)

    def test_same_day_bullets_stay_compact(self):
        # Chewy / Starbucks / Amazon are consecutive same-day rows: no blank line
        # should be injected between them (that pet-summary behavior would break the
        # day grouping on a 20-row list).
        out = format_for_imessage_by_name(TRANSACTIONS_BRIEF, "household_finance")
        lines = out.split("\n")
        chewy_idx = next(i for i, l in enumerate(lines) if l.startswith("🐾 Chewy"))
        self.assertTrue(lines[chewy_idx + 1].startswith("☕ Starbucks"))
        self.assertTrue(lines[chewy_idx + 2].startswith("📦 Amazon"))


MONTHLY_BRIEF = """\
# Monthly Financial Checkup — September 6, 2026

💰 **Net Worth: $1,400K** (+$5K MoM)
💳 **Credit Cards: $15K** outstanding

---

## ⚠️ Attention Required

- 🔌 **BofA accounts stale** — haven't synced in 2 months.
- 🚗 **DMV renewal $382** due soon.

---

## 💵 The Numbers

- Income: $50,265
- Savings rate: ~93%
"""


class MonthlyProfileTest(unittest.TestCase):
    """Monthly financial checkup: strip # headers, insert ┄ dividers, keep each
    bullet's own leading emoji (the 'airy' treatment)."""

    def setUp(self):
        self.out = format_for_imessage_by_name(MONTHLY_BRIEF, "monthly_finance")

    def test_no_markdown_survives(self):
        self.assertNotIn("**", self.out)
        for line in self.out.split("\n"):
            self.assertFalse(line.startswith("#"), f"markdown header leaked: {line!r}")
            self.assertNotEqual(line.strip(), "---", "raw --- rule survived")

    def test_divider_before_each_section(self):
        self.assertIn(MONTHLY_SECTION_DIVIDER, self.out)
        lines = self.out.split("\n")
        attn = next(i for i, l in enumerate(lines) if l == "⚠️ Attention Required")
        # the divider sits two lines above the section title (divider, title, blank)
        self.assertEqual(lines[attn - 1], MONTHLY_SECTION_DIVIDER)

    def test_leading_emoji_bullet_kept_not_doubled(self):
        # "- 🔌 **BofA accounts stale**" -> "🔌 BofA accounts stale" (dash dropped,
        # emoji kept as the marker, no second emoji prepended, bold gone).
        self.assertIn("🔌 BofA accounts stale — haven't synced in 2 months.", self.out)
        self.assertNotIn("• 🔌", self.out)

    def test_plain_bullet_gets_neutral_dot(self):
        self.assertIn("• Income: $50,265", self.out)
        self.assertIn("• Savings rate: ~93%", self.out)

    def test_by_name_routes_to_monthly_renderer(self):
        self.assertEqual(self.out, format_monthly_financial(MONTHLY_BRIEF))


class FormatSelectorTest(unittest.TestCase):
    """The --imessage-format flag plumbing in deliver-output.py."""

    def test_flag_parsed_and_removed(self):
        argv, fmt = deliver_output.extract_imessage_format(
            ["brief.md", "--imessage-format", "household_finance"]
        )
        self.assertEqual(fmt, "household_finance")
        self.assertEqual(argv, ["brief.md"])

    def test_default_when_absent(self):
        argv, fmt = deliver_output.extract_imessage_format(["brief.md"])
        self.assertEqual(fmt, DEFAULT_IMESSAGE_FORMAT)
        self.assertEqual(argv, ["brief.md"])

    def test_unknown_name_does_not_crash(self):
        # A typo'd profile must degrade to the default, never raise (the iMessage
        # arm is non-fatal and must not break the primary Telegram delivery).
        out = format_for_imessage_by_name(PET_SUMMARY_BRIEF, "bogus-profile")
        self.assertEqual(out, format_for_imessage_by_name(PET_SUMMARY_BRIEF, "pet_summary"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
