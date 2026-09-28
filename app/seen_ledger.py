"""App-private read/seen state for the web UI.

Persists to state/seen.json under the app dir. Two independent buckets:
  - feed items keyed by (feed_id, filename)
  - library items keyed by (source, relpath)

This is the ONLY writable surface exposed by the server. Writes go through
mark_feed_item_seen / mark_library_item_seen; both use temp-file + os.replace
so a killed process can't leave a half-written ledger, and both cap the
per-bucket list at SEEN_LIST_MAX with FIFO eviction so a malicious or noisy
producer can't grow the ledger unbounded.

The on-disk file is chmod 600: only the owner can read it. Layout is a plain
JSON dict so the file stays hand-editable in an emergency.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import json
import logging
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Dict, Iterable, Set

from config import SEEN_LEDGER_PATH, STATE_DIR


logger = logging.getLogger(__name__)


# One process-wide lock over both read and write. Ledger is small (KB range),
# so a coarse RLock keeps the code simple.
LEDGER_LOCK = threading.RLock()

# Per-feed / per-library-source cap on the seen list. FIFO evicted below.
# 5000 comfortably covers years of daily briefs across all feeds; anything
# past this is a signal that we're being fed junk, not that the user has read
# 5001 briefs.
SEEN_LIST_MAX = 5000

# chmod 600 — owner read/write only.
LEDGER_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


def ensure_state_dir() -> None:
    """Create the state/ dir if missing. Called from server startup."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_ledger() -> Dict:
    """Return the ledger dict from disk, or an empty one if it doesn't exist."""
    with LEDGER_LOCK:
        if not SEEN_LEDGER_PATH.exists():
            return {"feeds": {}, "library": {}}
        try:
            with open(SEEN_LEDGER_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as read_error:
            logger.warning("seen.json unreadable, resetting: %s", read_error)
            return {"feeds": {}, "library": {}}
        data.setdefault("feeds", {})
        data.setdefault("library", {})
        return data


def save_ledger(ledger: Dict) -> None:
    """Atomically write the ledger back to disk.

    Uses a same-dir NamedTemporaryFile + os.replace so a crash mid-write
    leaves the previous good ledger intact. chmods to 600 before rename.
    """
    with LEDGER_LOCK:
        ensure_state_dir()
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(STATE_DIR),
            prefix="seen.",
            suffix=".tmp",
            delete=False,
        )
        try:
            # Keep the on-disk JSON compact but still human-readable: a small
            # indent, no sort_keys (append-order is fine; the cap keeps it bounded).
            json.dump(ledger, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.chmod(tmp.name, LEDGER_FILE_MODE)
        os.replace(tmp.name, SEEN_LEDGER_PATH)


def get_seen_set_for_feed(ledger: Dict, feed_id: str) -> Set[str]:
    """Filenames the user has already opened in this feed."""
    return set(ledger.get("feeds", {}).get(feed_id, []))


def get_seen_set_for_library(ledger: Dict, source: str) -> Set[str]:
    """Relpaths the user has already opened in this library source."""
    return set(ledger.get("library", {}).get(source, []))


def append_with_fifo_cap(seen_list: list, entry: str) -> None:
    """Append entry to seen_list if not already there, evicting the oldest
    entries to keep the list under SEEN_LIST_MAX.

    Called under LEDGER_LOCK.
    """
    if entry in seen_list:
        return
    seen_list.append(entry)
    if len(seen_list) > SEEN_LIST_MAX:
        # FIFO: drop the oldest so the ledger stays bounded even if a
        # producer floods us with new filenames.
        del seen_list[: len(seen_list) - SEEN_LIST_MAX]


def mark_feed_item_seen(feed_id: str, filename: str) -> None:
    """Record that the user opened one brief. Idempotent.

    Caller is expected to have already validated that the brief resolves to
    a real file inside the allowlist (see handlers.handle_seen_post).
    """
    if not isinstance(feed_id, str) or not isinstance(filename, str):
        raise ValueError("feed_id and filename must be strings")
    if not feed_id or not filename:
        raise ValueError("feed_id and filename required")
    with LEDGER_LOCK:
        ledger = load_ledger()
        feeds = ledger.setdefault("feeds", {})
        seen_list = feeds.setdefault(feed_id, [])
        append_with_fifo_cap(seen_list, filename)
        save_ledger(ledger)


def mark_feed_items_seen(feed_id: str, filenames: Iterable[str]) -> int:
    """Bulk-mark many briefs seen under `feed_id`. Loads + saves the ledger
    once, unlike calling `mark_feed_item_seen` in a loop.

    Non-string / empty entries in `filenames` are skipped rather than raising,
    so a partial iteration from disk doesn't kill the whole call. Returns the
    number of filenames actually applied to the ledger (already-present items
    still count, matching the "N briefs marked" wording the frontend shows).
    The per-feed FIFO cap still applies, so this can't blow the ledger past
    SEEN_LIST_MAX no matter how large `filenames` is.
    """
    if not isinstance(feed_id, str) or not feed_id:
        raise ValueError("feed_id required")
    marked = 0
    with LEDGER_LOCK:
        ledger = load_ledger()
        feeds = ledger.setdefault("feeds", {})
        seen_list = feeds.setdefault(feed_id, [])
        for filename in filenames:
            if not isinstance(filename, str) or not filename:
                continue
            append_with_fifo_cap(seen_list, filename)
            marked += 1
        save_ledger(ledger)
    return marked


def mark_library_item_seen(source: str, relpath: str) -> None:
    """Record that the user opened one library file. Idempotent.

    Caller is expected to have already validated that the relpath resolves
    to a real file inside the source's root (see handlers.handle_seen_post).
    """
    if not isinstance(source, str) or not isinstance(relpath, str):
        raise ValueError("source and relpath must be strings")
    if not source or not relpath:
        raise ValueError("source and relpath required")
    with LEDGER_LOCK:
        ledger = load_ledger()
        library = ledger.setdefault("library", {})
        seen_list = library.setdefault(source, [])
        append_with_fifo_cap(seen_list, relpath)
        save_ledger(ledger)


def is_feed_item_seen(ledger: Dict, feed_id: str, filename: str) -> bool:
    """Quick membership check without reloading the ledger."""
    return filename in ledger.get("feeds", {}).get(feed_id, [])
