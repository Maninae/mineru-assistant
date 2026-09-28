#!/usr/bin/env python3
"""One-shot export of Apple Notes 'Daily Journals' to plain text files.

Reads straight from the local Notes SQLite store instead of driving Notes.app
over AppleEvents. The AppleEvent/JXA path (the previous implementation) proved
unreliable on this Mac: as of ~Aug 2026 even a trivial query (`count of notes`)
hangs for the full ~2-minute AppleEvent timeout and returns error -1712, both in
launchd AND interactively, and a Notes.app relaunch does not clear it. The note
bodies live locally in the store's gzipped protobuf, so we decompress and pull
the `note_text` field directly — fast, deterministic, no Notes.app dependency.

Validated Sep 5 2026: DB extraction matched the prior AppleEvent exports exactly
for all 30 previously-exported journals (0 mismatches).

Usage: export-journals.py [limit]
  limit: number of most-recent entries to export (default: 30)
"""

import gzip
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

WORKSPACE = os.environ.get("MINERU_HOME", os.path.expanduser("~/.mineru"))
EXPORT_DIR = os.environ.get("MINERU_JOURNAL_EXPORTS_DIR") or os.path.join(WORKSPACE, "journal_exports")
NOTES_DB = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
)
JOURNAL_FOLDER = "Daily Journals"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 30

# Core Data timestamps count seconds from 2001-01-01 UTC; this offset converts to
# the Unix epoch (1970-01-01) so we can format an ISO date.
CORE_DATA_EPOCH_OFFSET_SECONDS = 978307200

# Apple Notes protobuf field numbers (stable, well-documented format): the note's
# plaintext lives at NoteStoreProto.document(2) -> Document.note(3) -> Note.note_text(2).
NOTESTORE_DOCUMENT_FIELD = 2
DOCUMENT_NOTE_FIELD = 3
NOTE_TEXT_FIELD = 2


def _read_varint(buf: bytes, i: int):
    """Read a protobuf base-128 varint at offset i; return (value, next_offset)."""
    shift = value = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, i
        shift += 7


def _iter_protobuf_fields(buf: bytes):
    """Yield (field_number, wire_type, value) for each top-level protobuf field.

    Length-delimited fields (wire type 2) yield their raw bytes; varints yield the
    int. Enough of the wire format to walk down to the note text; unknown/bad wire
    types stop iteration rather than raising (a malformed blob just yields nothing).
    """
    i, n = 0, len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        field_number, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, i = _read_varint(buf, i)
            yield field_number, wire_type, value
        elif wire_type == 2:
            length, i = _read_varint(buf, i)
            yield field_number, wire_type, buf[i:i + length]
            i += length
        elif wire_type == 5:
            yield field_number, wire_type, buf[i:i + 4]
            i += 4
        elif wire_type == 1:
            yield field_number, wire_type, buf[i:i + 8]
            i += 8
        else:
            return


def _get_length_delimited_field(buf: bytes, field_number: int):
    """Return the bytes of the first length-delimited field with this number, else None."""
    for number, wire_type, value in _iter_protobuf_fields(buf):
        if number == field_number and wire_type == 2:
            return value
    return None


def extract_note_text(note_store_blob: bytes):
    """Pull the plaintext note body out of a decompressed Notes protobuf blob.

    Walks NoteStoreProto -> Document -> Note -> note_text. Returns the note text
    (its first line is the note title, matching Notes.app's `plaintext`), or None
    if the expected structure isn't present.
    """
    document = _get_length_delimited_field(note_store_blob, NOTESTORE_DOCUMENT_FIELD)
    note = _get_length_delimited_field(document, DOCUMENT_NOTE_FIELD) if document else None
    text = _get_length_delimited_field(note, NOTE_TEXT_FIELD) if note else None
    return text.decode("utf-8", "replace") if text is not None else None


def core_data_timestamp_to_iso(seconds_since_2001: float) -> str:
    """Convert a Core Data timestamp to an ISO-8601 UTC string with milliseconds."""
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=seconds_since_2001 + CORE_DATA_EPOCH_OFFSET_SECONDS
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def fetch_journals_from_db(limit: int):
    """Read the most-recent journals from the local Notes SQLite store.

    Returns a list of {name, date, body} dicts, newest first. Opens the live DB
    read-only (mode=ro) so we never lock Notes. A note whose body can't be
    decompressed/parsed (e.g. a password-locked note) is skipped with a warning
    rather than sinking the whole run.
    """
    if not os.path.exists(NOTES_DB):
        raise FileNotFoundError(f"Notes store not found: {NOTES_DB}")

    connection = sqlite3.connect(f"file:{NOTES_DB}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT note.ZTITLE1, note.ZMODIFICATIONDATE1, data.ZDATA
            FROM ZICCLOUDSYNCINGOBJECT note
            JOIN ZICCLOUDSYNCINGOBJECT folder ON note.ZFOLDER = folder.Z_PK
            JOIN ZICNOTEDATA data ON note.ZNOTEDATA = data.Z_PK
            WHERE folder.ZTITLE2 = ? AND data.ZDATA IS NOT NULL
            ORDER BY note.ZMODIFICATIONDATE1 DESC
            LIMIT ?
            """,
            (JOURNAL_FOLDER, limit),
        ).fetchall()
    finally:
        connection.close()

    entries = []
    for title, modified, blob in rows:
        if title == "README":
            continue
        try:
            body = extract_note_text(gzip.decompress(blob))
        except (OSError, IndexError) as parse_error:
            print(f"  Skipping {title!r}: could not decode body ({type(parse_error).__name__})")
            continue
        if body is None:
            print(f"  Skipping {title!r}: no note text found in blob")
            continue
        entries.append({
            "name": title,
            "date": core_data_timestamp_to_iso(modified),
            "body": body,
        })
    return entries


def sanitize_filename(name):
    """Turn a note title into a safe filename."""
    # Replace path-unsafe chars
    safe = re.sub(r'[/:\\]', '-', name)
    # Normalize curly quotes to straight
    safe = safe.replace('‘', "'").replace('’', "'")
    safe = safe.replace('“', '"').replace('”', '"')
    # Collapse whitespace
    safe = safe.strip()
    return safe


def write_exports(entries):
    """Write entries to EXPORT_DIR atomically via a temp directory."""
    tmp_dir = tempfile.mkdtemp(dir=WORKSPACE, prefix=".journal-export-")

    try:
        # Preserve README.md if it exists
        readme_src = os.path.join(EXPORT_DIR, "README.md")
        if os.path.exists(readme_src):
            with open(readme_src, "r", encoding="utf-8") as f:
                readme_content = f.read()
            with open(os.path.join(tmp_dir, "README.md"), "w", encoding="utf-8") as f:
                f.write(readme_content)

        # Write each entry
        exported = 0
        for entry in entries:
            fname = sanitize_filename(entry["name"]) + ".txt"
            path = os.path.join(tmp_dir, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write(entry.get("body", ""))
            exported += 1

        # Write index manifest (names + dates only, no bodies)
        index = [{"name": e["name"], "date": e["date"]} for e in entries]
        with open(os.path.join(tmp_dir, "_index.json"), "w") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)

        # Atomic swap: remove old dir, rename tmp to target
        if os.path.exists(EXPORT_DIR):
            backup = EXPORT_DIR + ".bak"
            if os.path.exists(backup):
                shutil.rmtree(backup)
            os.rename(EXPORT_DIR, backup)

        os.rename(tmp_dir, EXPORT_DIR)

        # Clean up backup
        backup = EXPORT_DIR + ".bak"
        if os.path.exists(backup):
            shutil.rmtree(backup)

        return exported

    except Exception:
        # On any failure, clean up temp dir but don't touch the original
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting journal export (limit={LIMIT})")

    print(f"  Reading journals from Notes store: {NOTES_DB}")
    entries = fetch_journals_from_db(LIMIT)

    if len(entries) == 0:
        print("  WARNING: No journal entries found.")
        sys.exit(0)

    print(f"  Got {len(entries)} entries. Writing to {EXPORT_DIR}/...")
    exported = write_exports(entries)

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Done. Exported {exported} entries.")


if __name__ == "__main__":
    main()
