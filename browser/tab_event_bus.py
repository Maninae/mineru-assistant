"""Thread-safe fan-out bus for browser-tab state-change events (P3-05).

Publishes events to zero or more in-process subscribers so the SSE
handler for `GET /tabs/stream` can push tab state changes to LLM
clients / CLI watchers without polling. Sits between BrowserManager
(the publisher, one process-wide instance) and BrowserHandler (the
subscriber, one instance per live SSE connection).

Design:
  - Each subscriber owns a bounded `queue.Queue`. `subscribe()` mints
    the queue and returns it; `unsubscribe(q)` drops it. The bus keeps
    a private list of live queues.
  - `publish(event)` fans out non-blocking via `put_nowait`. A full
    queue means the subscriber isn't draining fast enough — we drop
    that event for that subscriber and increment a debug-visible drop
    counter, rather than block the publisher (which shares a thread
    with `BrowserManager._lock`). Backpressure never propagates
    upstream into the browser control plane.
  - All state mutations are guarded by a plain `threading.Lock`. The
    lock is NEVER held across the subscriber-facing `put_nowait` call:
    the publisher copies the subscribers list under the lock, then
    fans out unlocked, so a stuck subscriber can never wedge publish().

Not owned by this module:
  - Event shape. The publisher constructs the dict (`{type, tab, ts}`
    per spec §4.3) before calling `publish()`; the bus is agnostic to
    payload.
  - Serialization. The SSE handler encodes the dict as JSON at write
    time; the bus stores the raw dict.
  - Delivery guarantees. `publish` is best-effort per subscriber. A
    fresh subscriber sees ONLY events that arrive after `subscribe()`
    returned — the SSE handler sends its initial snapshot separately
    to close the ordering gap for new listeners.

Python 3.9-compatible (comment-style annotations sit alongside real
annotations here since this module is the newest one and doesn't need
to match `actions.py`'s comment-only style).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Dict, List


logger = logging.getLogger("browser-server")


# Bounded per-subscriber queue size. Chosen large enough that a burst
# of tab opens/closes doesn't drop under normal browsing (opening 100
# tabs in a session is uncommon), small enough that a truly stuck
# subscriber doesn't bloat memory. Configurable per-instance for tests
# that want to exercise the drop path with a tiny queue.
DEFAULT_QUEUE_SIZE = 256


class TabEventBus:
    """Fan-out publisher for tab state-change events.

    One instance per browser server process. Multiple subscribers are
    supported (one per live SSE connection); each gets its own bounded
    queue and drains independently.
    """

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        # Guards `_subscribers` mutations AND `_drop_count`. Never held
        # across a subscriber's `put_nowait` — the publisher copies the
        # list under the lock, then fans out unlocked.
        self._lock = threading.Lock()
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self._queue_size = queue_size
        # Observability: total `put_nowait` drops across all subscribers
        # since bus creation. Exposed via `drop_count()` for tests + a
        # future `/health`-style diagnostic; not resettable at runtime.
        self._drop_count = 0

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        """Register a new subscriber; return its bounded queue.

        The caller drains the queue on its own cadence (typically the
        SSE handler's `queue.get(timeout=...)` loop). The queue's
        `maxsize` matches the bus's configured `queue_size`.
        """
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        """Drop `q` from the subscriber list; no-op if already absent.

        Idempotent so an SSE handler's `finally: bus.unsubscribe(q)`
        pattern is safe against a double-close or a subscribe that
        never happened (defensive branch).
        """
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                # Already unsubscribed / never subscribed — that's fine.
                pass

    def publish(self, event: Dict[str, Any]) -> None:
        """Non-blocking fan-out to every current subscriber.

        A full subscriber queue increments `_drop_count` and drops the
        event FOR THAT SUBSCRIBER ONLY — other subscribers still get
        their delivery. The publisher never blocks and never raises,
        so a state-change callback in BrowserManager can call this
        under `_lock` without risking a deadlock or a lost tab event.
        """
        # Snapshot the subscribers list under the lock, then fan out
        # unlocked. A subscriber whose queue is full costs one lock
        # re-acquire (for the drop-count bump), but never blocks the
        # publisher's caller.
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                with self._lock:
                    self._drop_count += 1
                logger.debug(
                    "TabEventBus: dropped event for a full subscriber queue "
                    "(total drops=%d)", self._drop_count
                )

    def subscriber_count(self) -> int:
        """Return how many subscribers are currently registered."""
        with self._lock:
            return len(self._subscribers)

    def drop_count(self) -> int:
        """Return the running count of full-queue drops."""
        with self._lock:
            return self._drop_count
