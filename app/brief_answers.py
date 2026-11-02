"""Reader notes on any markdown file the app shows: line-1 markers + one append write.

Every `.md` the app serves (feed briefs and library `.md` files) can take a
note from the reader. A file whose line 1 is one of three blockquote markers
(the convention the curiosity-question job writes) is a question with a state:

    > **⬜ UNANSWERED**                       open
    > **📝 ANSWERED · to file**               answered here, waiting for a job to file it
    > **✅ ANSWERED** · saved to <path>       filed

`append_answer_note` is the web app's ONLY write into the workspace (everything
else it writes lives under <state>/). Its scope is deliberately narrow:

- Append-only: the existing body is kept byte-for-byte except line 1.
- Marker file (any of the three): line 1 becomes `📝 ANSWERED · to file` and the
  note is labelled `Answer`. A note on a filed question re-opens it, which is
  how corrections reach memory on the next filing run.
- No marker: the note (labelled `Note`) is appended and nothing else changes.
- Refuses only files that are not valid UTF-8 (cannot be reproduced byte-for-byte).
- Atomic (same-dir temp file + os.replace), keeps the original mode and mtime.
- Path safety (allowlist, `.md` suffix, dot-files) is the HTTP layer's job:
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


NOTE_LABEL_ANSWER = "Answer"
NOTE_LABEL_PLAIN = "Note"


class BriefNotAnswerableError(Exception):
    """The file is not valid UTF-8, so it cannot be rewritten safely."""


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


def format_answer_note(answer_text: str, now: datetime, label: str = NOTE_LABEL_ANSWER) -> str:
    """The markdown block appended to the file: rule, stamped `Answer`/`Note` heading, text."""
    return f"\n\n---\n\n**💬 {label} · {format_answer_stamp(now)}**\n\n{answer_text}\n"


def append_answer_note(brief_path: Path, answer_text: str, now: datetime) -> Optional[str]:
    """Append one note to `brief_path` and return the resulting answer status.

    Args:
        brief_path: an already-validated `.md` file (see module docstring).
        answer_text: output of validate_answer_text.
        now: timezone-aware local time for the stamp (`datetime.now().astimezone()`).
    Returns:
        STATUS_PENDING_FILING when line 1 was a marker, None when it was not.
    Raises:
        BriefNotAnswerableError: the file is not valid UTF-8 (left untouched).

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
        previous_status = classify_answer_status(original_text)

        if previous_status is None:
            new_status = None
            updated_text = original_text.rstrip() + format_answer_note(answer_text, now, NOTE_LABEL_PLAIN)
        else:
            # Any marker (even filed) becomes pending: the new answer still needs filing.
            new_status = STATUS_PENDING_FILING
            remainder = original_text.split("\n", 1)[1] if "\n" in original_text else ""
            updated_text = (MARKER_PENDING_FILING + "\n" + remainder).rstrip()
            updated_text += format_answer_note(answer_text, now, NOTE_LABEL_ANSWER)

        original_stat = brief_path.stat()
        write_text_atomically_keeping_mode(brief_path, updated_text, stat.S_IMODE(original_stat.st_mode))
        # Keep the file's original mtime: feeds sort and the Today list key on it,
        # and a note must not make an old brief look like it just landed.
        os.utime(str(brief_path), ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    return new_status


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
