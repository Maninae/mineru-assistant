"""Reformat a Telegram-markdown brief into a clean iMessage-friendly copy.

iMessage renders no markdown (a literal `**bold**` shows the asterisks) and reads
best with emoji + whitespace carrying the hierarchy instead. This transform:

  - strips `**bold**` and `*italic*` markdown markers (iMessage shows them literally),
  - drops standalone `---` horizontal rules (they'd render as literal dashes),
  - swaps each bullet line's leading marker (`•` / `-` / `–`) for a TOPICAL emoji
    chosen from the bullet's lead phrase, falling back to a neutral emoji,
  - optionally inserts a blank line before each bullet for breathing room.

Three report types are wired to the iMessage arm, selected by name via
`format_for_imessage_by_name` (registry: IMESSAGE_RENDERERS):

  - `pet_summary` (a pet-health / litter-box report) and `household_finance`
    (a day-grouped transactions list) share the `format_for_imessage` engine and
    differ only in their per-bullet emoji map and whether bullets get breathing
    room (`space_bullets`). The pet report's few spread-out bullets read well with
    a blank line before each; the ~20-row transactions list stays compact. Both
    ADD a topical emoji to bullets that have none.
  - `monthly_finance` (a monthly financial checkup) uses a different engine,
    `format_monthly_financial`. Its bullets ALREADY lead with their own emoji and
    its sections use Markdown `#` headers, so it strips the headers, inserts `┄`
    section dividers (an "airy" look), and de-dashes bullets while KEEPING their
    existing emoji (adding one would double it up).

To wire a fourth report type, add a renderer to IMESSAGE_RENDERERS.

The Telegram copy is untouched — this only shapes the separate iMessage arm, so a
recipient without Telegram (e.g. a family member) gets a clean read while the
operator's Telegram delivery keeps its `•` bullets and rendered bold.
"""

import re
from typing import Dict, List, Optional

# --- Pet summary report (litter box): weight / usage / litter / fill / sensor / fault ---
# Keyword (looked for in the bullet's lead phrase, case-insensitive) -> emoji.
# Order matters: the first matching keyword wins, so put the more specific first.
PET_SUMMARY_BULLET_EMOJI: Dict[str, str] = {
    "weight": "⚖️",
    "usage": "🚽",
    "visit": "🚽",
    "uti": "🚽",
    "litter": "🧴",
    "fill": "⏳",
    "pace": "⏳",
    "sensor": "🔧",
    "stuck": "🔧",
    "gap": "⚠️",
    "fault": "⚠️",
    "error": "⚠️",
}
PET_SUMMARY_FALLBACK_EMOJI = "🐾"

# --- Household transactions report: one emoji per merchant / spending category ---
# First keyword found in the bullet's lead phrase wins, so specific merchants
# (named coffee chains) come before the generic category (dining). Anything with
# no match — an unknown vendor — falls back to a neutral money emoji.
TRANSACTIONS_BULLET_EMOJI: Dict[str, str] = {
    "starbucks": "☕",
    "peet": "☕",
    "blue bottle": "☕",
    "coffee": "☕",
    "café": "☕",
    "cafe": "☕",
    "costco": "🛒",
    "safeway": "🛒",
    "instacart": "🛒",
    "trader joe": "🛒",
    "whole foods": "🛒",
    "sprouts": "🛒",
    "grocer": "🛒",
    "groceries": "🛒",
    "chewy": "🐾",
    "petco": "🐾",
    "petsmart": "🐾",
    "vet": "🐾",
    "amazon": "📦",
    "target": "🎯",
    "sushi": "🍣",
    "pizza": "🍕",
    "bistro": "🍽️",
    "grill": "🍽️",
    "kitchen": "🍽️",
    "restaurant": "🍽️",
    "dining": "🍽️",
    "bevmo": "🍷",
    "total wine": "🍷",
    "wine": "🍷",
    "liquor": "🍷",
    "tesla": "🔌",
    "charging": "🔌",
    "shell": "⛽",
    "chevron": "⛽",
    "fuel": "⛽",
    "gas": "⛽",
    "fastrak": "🛣️",
    "toll": "🛣️",
    "paybyphone": "🅿️",
    "parking": "🅿️",
    "patreon": "🔁",
    "spotify": "🔁",
    "netflix": "🔁",
    "subscription": "🔁",
    "wealthfront": "💸",
    "advisory": "💸",
    "fees": "💸",
    "fee": "💸",
    "transfer": "🔀",
}
TRANSACTIONS_FALLBACK_EMOJI = "💵"

# The name->renderer registry (IMESSAGE_RENDERERS) lives at the bottom of this
# file, after the renderer functions it dispatches to are defined.

# A bullet line: optional leading whitespace, a bullet marker, a space, then text.
_BULLET_RE = re.compile(r"^(\s*)[•\-–]\s+(.*)$")
# A single-asterisk italic span (run AFTER `**` bold is stripped, so only true
# italic pairs remain). Used to de-markdown italic day headers like `*Thu Sep 3*`.
_ITALIC_RE = re.compile(r"\*([^*]+)\*")
# A standalone horizontal rule line (`---`, `----`, ...).
_HR_RE = re.compile(r"^\s*-{3,}\s*$")
# A Markdown ATX header line (`#`..`######` + text). iMessage renders the # literally.
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# The lead phrase is where the topic word lives ("Weight stable." -> "weight").
_LEAD_PHRASE_CHARS = 40
# Section divider inserted before each h2 header in the monthly "airy" treatment.
MONTHLY_SECTION_DIVIDER = "┄┄┄┄┄┄┄┄┄┄┄┄"


def pick_bullet_emoji(
    body: str, bullet_emoji: Dict[str, str], fallback: str
) -> str:
    """Choose a topical emoji for a bullet from its lead phrase, else the fallback."""
    lead = body[:_LEAD_PHRASE_CHARS].lower()
    for keyword, emoji in bullet_emoji.items():
        if keyword in lead:
            return emoji
    return fallback


def format_for_imessage(
    content: str,
    bullet_emoji: Optional[Dict[str, str]] = None,
    fallback: str = PET_SUMMARY_FALLBACK_EMOJI,
    space_bullets: bool = True,
) -> str:
    """Return an iMessage-friendly copy of a Telegram-markdown brief.

    Strips `**bold**` and `*italic*`, drops standalone `---` rules, and replaces
    each bullet marker with a topical emoji. Non-bullet lines (emoji vitals,
    headline, section headers, blanks) are otherwise preserved. Called with no
    `bullet_emoji`, it uses the pet-summary map + spacing (the original behavior), so
    existing callers are unaffected. Prefer `format_for_imessage_by_name`.

    Args:
        content: the Telegram-markdown brief text.
        bullet_emoji: keyword->emoji map for bullets (defaults to the pet-summary map).
        fallback: emoji for a bullet whose lead phrase matches no keyword.
        space_bullets: insert a blank line before each bullet (breathing room for
            a short list; leave False for a dense grouped list).
    """
    emoji_map = PET_SUMMARY_BULLET_EMOJI if bullet_emoji is None else bullet_emoji
    out = []
    for raw_line in content.split("\n"):
        line = raw_line.replace("**", "")            # strip bold
        line = _ITALIC_RE.sub(r"\1", line)           # strip single-* italic
        if _HR_RE.match(line):                        # drop horizontal rules
            out.append("")
            continue
        match = _BULLET_RE.match(line)
        if match:
            indent, body = match.group(1), match.group(2)
            emoji = pick_bullet_emoji(body, emoji_map, fallback)
            # Give each bullet breathing room in iMessage: a blank line before it.
            # Skip if the previous output line is already blank (avoid doubling).
            if space_bullets and out and out[-1].strip() != "":
                out.append("")
            out.append(f"{indent}{emoji} {body}")
        else:
            out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)            # collapse 3+ blank lines to 1
    return text.strip() + "\n"


def starts_with_emoji(text: str) -> bool:
    """True if the first visible character is an emoji/symbol.

    Used by the monthly renderer to tell a bullet that already carries its own
    leading emoji ("🔌 BofA…") from a plain one ("Income: $0"). Range-based (no
    external lib): covers arrows, misc-symbols/dingbats, the emoji planes, and
    the two double-! marks — enough for the financial report's vocabulary.
    """
    stripped = text.lstrip()
    if not stripped:
        return False
    o = ord(stripped[0])
    return (
        0x2190 <= o <= 0x21FF or 0x2300 <= o <= 0x23FF or 0x2460 <= o <= 0x24FF
        or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF or 0x1F000 <= o <= 0x1FAFF
        or o in (0x203C, 0x2049)
    )


def _render_monthly_bullet(indent: str, body: str) -> str:
    """A monthly-report bullet: keep the body's own leading emoji as the marker
    (adding one would double it up); a plain bullet gets a neutral •."""
    return f"{indent}{body}" if starts_with_emoji(body) else f"{indent}• {body}"


def format_monthly_financial(content: str) -> str:
    """iMessage copy of the monthly financial checkup — the "airy" treatment.

    The monthly report is a category/cashflow analysis (a dashboard, Markdown `#`
    section headers, and bullets that already lead with their own emoji), so it
    needs a different transform from the pet-summary/household-finance profiles:

    - strip `**bold**` / `*italic*` (iMessage shows the markers literally),
    - strip Markdown `#` headers and insert a `┄` divider before each h2 section
      (a scannable "airy" look),
    - drop source `---` rules (we supply our own dividers),
    - de-dash bullets while KEEPING each line's existing emoji.
    """
    out: List[str] = []
    for raw_line in content.split("\n"):
        line = raw_line.replace("**", "")
        line = _ITALIC_RE.sub(r"\1", line)
        if _HR_RE.match(line):
            continue  # drop source rules; the divider comes from the h2 handling
        header = _HEADER_RE.match(line)
        if header:
            level, title = len(header.group(1)), header.group(2)
            if level >= 2:
                out += ["", MONTHLY_SECTION_DIVIDER, title, ""]
            else:
                out += [title, ""]
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            out.append(_render_monthly_bullet(bullet.group(1), bullet.group(2)))
            continue
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse 3+ blank lines to one
    return text.strip() + "\n"


def _render_pet_summary(content: str) -> str:
    return format_for_imessage(content, PET_SUMMARY_BULLET_EMOJI, PET_SUMMARY_FALLBACK_EMOJI, space_bullets=True)


def _render_household_finance(content: str) -> str:
    return format_for_imessage(content, TRANSACTIONS_BULLET_EMOJI, TRANSACTIONS_FALLBACK_EMOJI, space_bullets=False)


# Registry: report-type name -> the renderer that shapes its iMessage copy.
IMESSAGE_RENDERERS = {
    "pet_summary": _render_pet_summary,
    "household_finance": _render_household_finance,
    "monthly_finance": format_monthly_financial,
}
DEFAULT_IMESSAGE_FORMAT = "pet_summary"


def format_for_imessage_by_name(
    content: str, format_name: str = DEFAULT_IMESSAGE_FORMAT
) -> str:
    """Format a brief using a named renderer from IMESSAGE_RENDERERS.

    An unknown name falls back to the default renderer rather than raising, since
    the iMessage arm is non-fatal and must never break the primary delivery.
    """
    renderer = IMESSAGE_RENDERERS.get(format_name) or IMESSAGE_RENDERERS[DEFAULT_IMESSAGE_FORMAT]
    return renderer(content)
