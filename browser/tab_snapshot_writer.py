"""Debounced-atomic on-disk snapshot writer for `list_tabs()` (P3-05).

Feeds the "cross-restart recovery" layer of spec §4.3: `mineru browser
tabs` falls back to `$MINERU_HOME/cache/browser-tabs.json` when the live
server is down, so a crashed-and-restarting server still exposes what
it was showing before the crash. The SnapshotWriter here is what keeps
that file up to date.

Design:
  - `trigger()` is called from every tab state change (opened / closed
    / navigated / focused). Multiple triggers within the debounce
    window (default 200 ms) coalesce into a SINGLE write of the FINAL
    state at write time. A hot burst of framenavigated events on N
    tabs yields one snapshot write, not N.
  - The write is atomic: write to a unique temp file with `O_EXCL`
    (so a UUID collision fails loud instead of corrupting an in-flight
    peer write), then `os.replace()` over the destination. Replace is
    atomic on the same filesystem per POSIX.
  - Both the temp and the final file live at mode `0600`. `os.open`
    with the mode arg + a defensive `os.chmod` after `replace` covers
    both the write-side and any umask surprises.
  - Threading: `_lock` guards the pending `Timer` handle so a
    concurrent trigger doesn't leak a timer. The write itself runs on
    the Timer thread, OFF the lock — so a slow disk can't block the
    caller of `trigger()`.

Fetches state on-demand at write time via the `snapshot_fn` callable —
the writer holds no tab state itself. This keeps the coupling one-way:
the writer knows how to atomically persist a list; it doesn't know
what a tab is.

Why capture state at write time (not trigger time)?

  If we captured at trigger time and buffered the payload, a burst of
  N triggers would compute the snapshot N times only to discard N-1.
  Since the point of debouncing is exactly to collapse the burst,
  computing once at the trailing edge is the correct semantics.

Python 3.9-compatible.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger("browser-server")


# 200 ms debounce window (spec §4.3): fast enough that a `/tabs` read
# from the recovery file sees near-live state; slow enough to coalesce
# a burst of state changes (multi-tab framenavigated wave) into one
# write. Configurable per-instance so tests can push it to 0 and skip
# the Timer scheduling entirely.
DEFAULT_DEBOUNCE_MS = 200

# The on-disk mode for both the temp file and the final destination.
# 0600 (owner read/write only) matches every other sensitive artifact
# in `$MINERU_HOME/cache/` (sent-image cache, cookie exports).
FILE_MODE = 0o600


class SnapshotWriter:
    """Debounced-atomic writer for the `/tabs` snapshot cache file.

    Not thread-safe against multiple concurrent `flush()` calls — the
    trigger() -> Timer path is serialized by `_lock`, and a manual
    `flush()` cancels the pending timer before writing, so a
    single-caller-per-writer usage is safe. If two threads call
    `flush()` at the same time, both may perform a write (each writes
    its own timestamp); no corruption results because each uses a
    unique temp file, but the debounce contract only holds against
    trigger()-driven writes.
    """

    def __init__(
        self,
        path: Path,
        snapshot_fn: Callable[[], List[Dict[str, Any]]],
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    ) -> None:
        self._path = Path(path)
        self._snapshot_fn = snapshot_fn
        # `max(..., 0)` guards a hostile / test config that passes a
        # negative value; treat it as "no debounce" not "arm a Timer
        # in the past."
        self._debounce_seconds = max(debounce_ms, 0) / 1000.0
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        # Observability: total triggers and total actual writes since
        # construction. `trigger_count > write_count` confirms the
        # debounce is coalescing; equality means no debounce hit or
        # every trigger drained on its own.
        self._trigger_count = 0
        self._write_count = 0
        # Set on every successful write to the caller-visible atomic
        # `os.replace` target — useful for tests that want to assert
        # "the writer ran at least once" without re-stating the path.
        self._last_generated_at: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger(self) -> None:
        """Schedule (or reset) the debounced write.

        Multiple rapid triggers within the debounce window coalesce to
        one write of the FINAL state (snapshot_fn is called once, at
        the trailing edge). If `debounce_ms == 0`, the write runs
        inline on the caller's thread so tests don't need to wait on
        a Timer.
        """
        with self._lock:
            self._trigger_count += 1
            if self._timer is not None:
                # Cancel the pending write; a fresh trigger extends the
                # window. Timer.cancel() is idempotent and safe to call
                # on an already-fired timer.
                self._timer.cancel()
                self._timer = None
            if self._debounce_seconds <= 0:
                write_inline = True
            else:
                self._timer = threading.Timer(self._debounce_seconds, self._write)
                # Daemon so a pending Timer doesn't block interpreter
                # shutdown; the write is best-effort recovery data, not
                # a durability contract.
                self._timer.daemon = True
                self._timer.start()
                write_inline = False
        if write_inline:
            # Off the lock: a slow disk cannot block another trigger()
            # (though callers should not spam trigger() in that mode).
            self._write()

    def flush(self) -> None:
        """Cancel any pending timer and write NOW (blocking, caller thread)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._write()

    def cancel(self) -> None:
        """Cancel any pending timer without writing.

        Used at server-shutdown time so we don't schedule a write into
        a Python that's mid-exit and would drop the log line for it.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def trigger_count(self) -> int:
        with self._lock:
            return self._trigger_count

    def write_count(self) -> int:
        with self._lock:
            return self._write_count

    def last_generated_at(self) -> Optional[str]:
        with self._lock:
            return self._last_generated_at

    # ------------------------------------------------------------------
    # Internal write path
    # ------------------------------------------------------------------

    def _write(self) -> None:
        """Compute the snapshot and atomically replace the destination.

        Runs on the Timer thread (or inline on the trigger thread when
        `debounce_ms == 0`). Never raises: every failure branch logs
        and returns so a broken snapshot cache can't take down the
        control plane. The atomic-rename story: write to a unique
        `<name>.tmp.<pid>.<uuid>` beside the destination, then
        `os.replace()`. A crash mid-write leaves the old destination
        untouched (which is exactly what recovery wants) and orphans
        the temp file (cleaned up on the next `trigger` cycle).
        """
        try:
            tabs = self._snapshot_fn()
        except Exception as exc:
            logger.warning("SnapshotWriter: snapshot_fn raised: %s", exc)
            return
        generated_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            payload = json.dumps(
                {"generatedAt": generated_at, "tabs": tabs},
                indent=2,
                ensure_ascii=False,
            )
        except (TypeError, ValueError) as exc:
            # Something in the tab dict is not JSON-serializable —
            # log and skip the write rather than crash. In practice
            # this only fires if a Path or datetime leaked into the
            # enriched-tab shape.
            logger.warning("SnapshotWriter: JSON encode failed: %s", exc)
            return

        parent = self._path.parent
        # Track whether we created the parent so we only tighten a directory
        # WE brought into existence — never chmod a pre-existing shared dir
        # (may belong to another tool / test harness).
        parent_pre_existed = parent.exists()
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("SnapshotWriter: parent mkdir failed: %s", exc)
            return
        if not parent_pre_existed:
            # Owner-only (0700) matches the 0600 file mode below and the
            # sensitive-artifact posture in `$MINERU_HOME/cache/`. A default
            # 0755 mkdir would leak the presence + names of snapshot files
            # to any user on the box. Best-effort: a chmod failure on our
            # own freshly-created dir is weird but not fatal.
            try:
                os.chmod(str(parent), 0o700)
            except Exception as exc:
                logger.warning(
                    "SnapshotWriter: chmod 0700 on new parent %s failed: %s",
                    parent,
                    exc,
                )

        # Unique temp path so two SnapshotWriters (or a retry after a
        # partial write) can never collide on the O_EXCL open. `PID` in
        # the name is defensive — the writer is single-process, but a
        # future multi-process test harness would appreciate it.
        temp_path = parent / f".{self._path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            fd = os.open(
                str(temp_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                FILE_MODE,
            )
            with os.fdopen(fd, "w") as f:
                f.write(payload)
        except FileExistsError:
            # UUID collision (astronomically unlikely) or a genuine
            # race with an identically-named temp. Skip; a later
            # trigger will retry with a fresh name.
            logger.warning(
                "SnapshotWriter: temp file %s already exists; skipping this write",
                temp_path,
            )
            return
        except Exception as exc:
            logger.warning("SnapshotWriter: temp write failed: %s", exc)
            # Best-effort cleanup so we don't leak partial temps.
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
            return

        try:
            os.replace(str(temp_path), str(self._path))
        except Exception as exc:
            logger.warning("SnapshotWriter: atomic rename failed: %s", exc)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
            return

        # os.replace preserves the SOURCE file's mode, which we opened
        # at FILE_MODE, so the destination is already 0600. `chmod`
        # defensively in case the source umask ever produces a
        # surprising result (belt-and-suspenders — never seen in prod
        # but the cost is one syscall).
        try:
            os.chmod(str(self._path), FILE_MODE)
        except Exception as exc:
            # A chmod failure on our own file after a successful write
            # is weird but not fatal; log and continue.
            logger.warning("SnapshotWriter: chmod after replace failed: %s", exc)

        with self._lock:
            self._write_count += 1
            self._last_generated_at = generated_at
