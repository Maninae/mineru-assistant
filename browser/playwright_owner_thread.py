"""One long-lived thread that owns every Playwright sync-API object.

Playwright's sync API runs on a greenlet dispatcher bound to the thread
that called `sync_playwright().start()`. Every later call on a
Playwright/Browser/Context/Page object must come from that same thread,
or greenlet raises "cannot switch to a different thread". The HTTP
server is a `ThreadingHTTPServer` (one short-lived thread per request),
so without this module the browser gets launched on request #1's thread
and request #2 can no longer touch it.

`PlaywrightOwnerThread.call(fn, ...)` runs `fn` on the owner thread and
returns its result (or re-raises its exception) in the caller.

- Re-entrant: a call made FROM the owner thread (Playwright event
  callbacks, one manager method calling another) runs inline, so it can
  never deadlock waiting on itself.
- Calls are serialized in FIFO order; the owner thread never exits
  until `stop()`, so the objects it created stay usable for the process
  lifetime.
"""

import functools
import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable, Optional, Tuple

from browser.config import logger

OWNER_THREAD_NAME = "playwright-owner"

# Sentinel that tells the owner loop to exit.
STOP_SENTINEL = object()


class PlaywrightOwnerThread:
    """Dedicated worker thread that executes submitted callables in order."""

    def __init__(self, name=OWNER_THREAD_NAME):
        # type: (str) -> None
        self.name = name
        self.work_queue = queue.Queue()  # type: queue.Queue
        self.start_lock = threading.Lock()
        self.thread = None  # type: Optional[threading.Thread]

    def ensure_started(self):
        # type: () -> None
        """Start the owner thread on first use (idempotent, thread-safe)."""
        with self.start_lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self.run_loop, name=self.name, daemon=True)
            self.thread.start()

    def is_owner_thread(self):
        # type: () -> bool
        return self.thread is not None and threading.current_thread() is self.thread

    def call(self, fn, *args, **kwargs):
        # type: (Callable[..., Any], Any, Any) -> Any
        """Run `fn(*args, **kwargs)` on the owner thread and return its result."""
        if self.is_owner_thread():
            return fn(*args, **kwargs)
        self.ensure_started()
        result_future = Future()  # type: Future
        self.work_queue.put((result_future, fn, args, kwargs))
        return result_future.result()

    def run_loop(self):
        # type: () -> None
        while True:
            work_item = self.work_queue.get()
            if work_item is STOP_SENTINEL:
                return
            result_future, fn, args, kwargs = work_item  # type: Tuple[Future, Callable[..., Any], Any, Any]
            if not result_future.set_running_or_notify_cancel():
                continue
            try:
                result_future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # handed back to the caller, not swallowed
                result_future.set_exception(exc)

    def stop(self, timeout_s=5.0):
        # type: (float) -> None
        """Ask the owner thread to exit after draining queued work."""
        thread = self.thread
        if thread is None or not thread.is_alive():
            return
        self.work_queue.put(STOP_SENTINEL)
        if not self.is_owner_thread():
            thread.join(timeout_s)
            if thread.is_alive():
                logger.warning("playwright owner thread did not exit within %.1fs", timeout_s)


def runs_on_owner_thread(method):
    # type: (Callable[..., Any]) -> Callable[..., Any]
    """Decorator for BrowserManager methods: route the call to `self._owner_thread`."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        return self._owner_thread.call(method, self, *args, **kwargs)

    return wrapper
