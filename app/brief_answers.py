"""Answer notes on "answerable" briefs: line-1 status markers + one append write.

A brief in a feed flagged `answerable: true` (see config.parse_feed_entry) can
take answers from the reader. Its first line is its state, one of three
blockquote markers (the convention the curiosity-question job writes):

    > **⬜ UNANSWERED**                       open
    > **📝 ANSWERED · to file**               answered here, waiting for a job to file it
    > **✅ ANSWERED** · saved to <path>       filed

`append_answer_note` is the web app's ONLY write into the workspace (everything
else it writes lives under <state>/). Its scope is deliberately narrow:

- Append-only: the existing body is kept byte-for-byte except line 1, which
  flips UNANSWERED -> ANSWERED · to file. Nothing is ever deleted or rewritten.
- Refuses files whose line 1 is not a marker, and files already filed.
- Atomic (same-dir temp file + os.replace) and keeps the original file mode.
- Path safety (feed flag, allowlist, `.md` suffix) is the HTTP layer's job:
  see brief_answer_endpoint.py. This module trusts the path it is handed.

Python 3.9-compatible. Stdlib only.
"""

import os
import stat
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


MARKER_UNANSWERED = "> **⬜ UNANSWERED**"
MARKER_PENDING_FILING = "> **📝 ANSWERED · to file**"
# Filed files carry a suffix: `> **✅ ANSWERED** · saved to memory/x.md`.
MARKER_FILED_PREFIX = "> **✅ ANSWERED**"

STATUS_UNANSWERED = "unanswered"
STATUS_PENDING_FILING = "answered_pending_filing"
STATUS_FILED = "answered_filed"

ANSWER_TEXT_MAX_CHARS = 4000
# 4000 CJK/emoji chars JSON-escape to well past the global 4 KiB body cap.
MAX_ANSWER_BODY_BYTES = 16384

# Serializes read-modify-write so two concurrent saves can't drop a note.
APPEND_LOCK = threading.Lock()


class BriefNotAnswerableError(Exception):
    """Line 1 of the brief is not one of the answer-status markers."""


class BriefAlreadyFiledError(Exception):
    """The brief's answer was already filed into memory; no more notes."""


def classify_answer_status(markdown_text: str) -> Optional[str]:
    """Map line 1 of a brief to a STATUS_* constant, or None when it carries no marker."""
    first_line = markdown_text.split("\n", 1)[0].rstrip()
    if first_line == MARKER_UNANSWERED:
        return STATUS_UNANSWERED
    if first_line == MARKER_PENDING_FILING:
        return STATUS_PENDING_FILING
    if first_line.startswith(MARKER_FILED_PREFIX):
        return STATUS_FILED
    return None


def validate_answer_text(value: object) -> Optional[str]:
    """Return the cleaned answer text, or None when it is unusable.

    - Must be a str; CRLF / CR are normalized to LF, then outer whitespace stripped.
    - 1..ANSWER_TEXT_MAX_CHARS characters after stripping.
    - Rejects embedded NUL.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned or len(cleaned) > ANSWER_TEXT_MAX_CHARS:
        return None
    if "\x00" in cleaned:
        return None
    return cleaned


def format_answer_stamp(now: datetime) -> str:
    """`Nov 2, 2026, 10:45 AM PST` (same as strftime `%b %-d, %Y, %-I:%M %p %Z`).

    Built by hand because the `%-d` / `%-I` no-padding flags are a glibc/BSD
    extension, not portable strftime.
    """
    hour_12 = now.hour % 12 or 12
    stamp = f"{now:%b} {now.day}, {now:%Y}, {hour_12}:{now:%M} {now:%p}"
    zone_name = now.strftime("%Z")
    return f"{stamp} {zone_name}" if zone_name else stamp


def format_answer_note(answer_text: str, now: datetime) -> str:
    """The markdown block appended to the brief: rule, stamped heading, answer."""
    return f"\n\n---\n\n**💬 Answer · {format_answer_stamp(now)}**\n\n{answer_text}\n"


def append_answer_note(brief_path: Path, answer_text: str, now: datetime) -> str:
    """Append one answer note to `brief_path` and return the new status.

    Args:
        brief_path: an already-validated brief file (see module docstring).
        answer_text: output of validate_answer_text.
        now: timezone-aware local time for the stamp (`datetime.now().astimezone()`).
    Returns:
        STATUS_PENDING_FILING, always.
    Raises:
        BriefNotAnswerableError: line 1 is not a marker.
        BriefAlreadyFiledError: line 1 is the filed marker.

    - UNANSWERED flips to ANSWERED · to file; a pending file keeps its marker
      and simply gains another note.
    - The file's mode and mtime are preserved (the brief keeps its original date).
    - Trailing whitespace is trimmed so exactly one blank line precedes the
      `---` (which also keeps the rule from turning the last paragraph into a
      setext heading).
    """
    with APPEND_LOCK:
        try:
            original_text = brief_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as decode_error:
            # Never rewrite a file we cannot reproduce byte-for-byte.
            raise BriefNotAnswerableError(f"{brief_path}: not valid UTF-8") from decode_error
        status = classify_answer_status(original_text)
        if status is None:
            raise BriefNotAnswerableError(str(brief_path))
        if status == STATUS_FILED:
            raise BriefAlreadyFiledError(str(brief_path))

        updated_text = original_text
        if status == STATUS_UNANSWERED:
            remainder = original_text.split("\n", 1)[1] if "\n" in original_text else ""
            updated_text = MARKER_PENDING_FILING + "\n" + remainder
        updated_text = updated_text.rstrip() + format_answer_note(answer_text, now)

        original_stat = brief_path.stat()
        write_text_atomically_keeping_mode(brief_path, updated_text, stat.S_IMODE(original_stat.st_mode))
        # Keep the question's original mtime: feeds sort and the Today list key on it,
        # and an answer must not make an old question look like it just landed.
        os.utime(str(brief_path), ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    return STATUS_PENDING_FILING


def write_text_atomically_keeping_mode(target_path: Path, text: str, file_mode: int) -> None:
    """Write `text` to a same-dir temp file, chmod it to `file_mode`, os.replace over target."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target_path.parent),
        prefix="." + target_path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        try:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.chmod(tmp.name, file_mode)
        os.replace(tmp.name, str(target_path))
    except BaseException:
        # A failed write must not leave a dot-temp file in the brief dir.
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        raise
