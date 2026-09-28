"""Shared title + TLDR extraction for Mineru markdown briefs.

Single source of truth for the two callers that pull a display summary out of
a brief:

- `extract_tldr_line`: the delivery/CLI path (scripts/deliver-output.py). Returns
  the first heading (or the first non-empty line) as a fallback so the inject-queue
  pointer and Telegram card always have SOMETHING to show.
- `extract_title_and_tldr`: the web/UI path (app/feeds.py). Returns `(title, tldr)`
  where `tldr` is intentionally empty when it would repeat the title, so the Inbox
  card can hide the redundant row.

Both skip a leading YAML frontmatter block, strip markdown decoration, collapse
whitespace, and truncate to `TLDR_MAX_CHARS`. When hunting for the fallback
title (first heading / first non-empty line), both ALSO skip leading blockquote
lines (`> ...`) and status-sentinel lines (`⬜ UNANSWERED` / `✅ ANSWERED`
markers) — the daily-curiosity brief carries its answer-status as a top-of-file
`> **⬜ UNANSWERED**` blockquote, and without that skip the extractor picks up
"⬜ UNANSWERED**" as the title instead of the real question one blank line
below. Explicit `TLDR:` lines are NOT skipped even when blockquoted (`> TLDR:`
is a common producer shape).

The `TL;?DR` regex demands an explicit `:` / `-` separator so prose like
`TLDRs are great` or `TLDR-worthy` cannot hijack the extraction; both `TLDR:`
and the canonical `TL;DR:` spelling match, with optional bold decoration and
quote/list markers.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import re
from typing import List, Tuple


TLDR_MAX_CHARS = 200

# Tolerates `> **TLDR:**`, `- TLDR:`, `TLDR - `, `TL;DR:`, `**TLDR**: ` etc.
# The `[:\-]\**\s+` tail is load-bearing: it forces a real separator between
# the TLDR marker and the summary text, so words starting with "TLDR" cannot
# swallow the extraction.
TLDR_LINE_RE = re.compile(
    r"^[>\s*-]*\s*TL;?DR\s*\**\s*[:\-]\**\s+(.+)$",
    re.IGNORECASE,
)

# The curiosity brief's `> **⬜ UNANSWERED**` / `> **✅ ANSWERED** · saved to ...`
# status-line pattern. Matches the box glyph + optional bold decoration + the
# ANSWERED/UNANSWERED word anywhere in the line, so the extractor can skip it
# even when authors drop the surrounding blockquote wrapper.
STATUS_SENTINEL_RE = re.compile(r"[⬜✅]\s*\**\s*(UN)?ANSWERED", re.IGNORECASE)


def strip_yaml_frontmatter(lines: List[str]) -> List[str]:
    """Return `lines` with a leading `--- ... ---` block dropped, if present."""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[i + 1:]
    return lines


def is_title_skip_line(stripped_line: str) -> bool:
    """True when a leading line must NOT become the fallback title/first-line.

    Two kinds of lines are skipped:
      - Blockquote lines (`> ...`): usually meta / status / attribution, not
        the brief's real headline. Callers that want the TLDR out of a
        blockquoted `> TLDR:` still match earlier via TLDR_LINE_RE.
      - Status sentinels (`⬜ UNANSWERED` / `✅ ANSWERED` and their bolded
        variants), whether or not they sit inside a blockquote.
    """
    if stripped_line.startswith(">"):
        return True
    if STATUS_SENTINEL_RE.search(stripped_line):
        return True
    return False


def clean_display_line(line: str) -> str:
    """Strip markdown decoration, collapse whitespace, truncate.

    Runs the paired-emphasis replacements first (`**bold**` → `bold`,
    `__bold__` → `bold`) so the readable text survives, then wipes any
    residual emphasis markers left over — an unpaired trailing `**` (e.g.
    `⬜ UNANSWERED**` after the leading-strip ate the opening pair), a bare
    single `*` italic marker, and inline backticks. Inline emoji are preserved.
    """
    line = re.sub(r"^[#>\s*-]+", "", line)          # heading/quote/list markers
    line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)    # bold pairs
    line = re.sub(r"__(.+?)__", r"\1", line)        # bold-underscore pairs
    line = re.sub(r"\*+", "", line)                 # residual asterisks (unpaired **, single *)
    line = re.sub(r"__+", "", line)                 # residual double-underscore runs
    line = re.sub(r"`+", "", line)                  # inline-code backticks
    line = " ".join(line.split())
    if len(line) > TLDR_MAX_CHARS:
        line = line[:TLDR_MAX_CHARS - 1].rstrip() + "…"
    return line


def extract_tldr_line(content: str) -> str:
    """Pull a one-line TLDR from brief markdown.

    Preference order:
      1. An explicit `TLDR:` line (case-insensitive, tolerates `**TLDR:**` /
         list markers / the `TL;DR` spelling). Blockquoted `> TLDR:` matches too.
      2. The first markdown heading (skipping blockquote / status-sentinel lines).
      3. The first non-empty non-skip line.
    """
    lines = strip_yaml_frontmatter(content.splitlines())
    first_heading = ""
    first_nonempty = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        tldr_match = TLDR_LINE_RE.match(stripped)
        if tldr_match:
            return clean_display_line(tldr_match.group(1))
        if is_title_skip_line(stripped):
            continue
        if not first_heading and stripped.startswith("#"):
            first_heading = stripped
        if not first_nonempty:
            first_nonempty = stripped
    return clean_display_line(first_heading or first_nonempty)


def extract_title_and_tldr(content: str) -> Tuple[str, str]:
    """Web-facing wrapper: `(title, tldr)` for an Inbox card.

    - title: first `# heading`, else first non-empty non-skip line
      (blockquote / status-sentinel lines are ignored — see
      `is_title_skip_line`).
    - tldr:  same as `extract_tldr_line`, BUT returned as an empty string when
      it would equal the title. The web UI hides the tldr row in that case, so
      Inbox cards no longer show the same string twice.
    """
    lines = strip_yaml_frontmatter(content.splitlines())
    first_heading = ""
    first_nonempty = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if is_title_skip_line(stripped):
            continue
        if not first_heading and stripped.startswith("#"):
            first_heading = stripped
        if not first_nonempty:
            first_nonempty = stripped
        if first_heading and first_nonempty:
            break
    title = clean_display_line(first_heading or first_nonempty)
    tldr = extract_tldr_line(content)
    if not tldr or tldr == title:
        return title, ""
    return title, tldr
