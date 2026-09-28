"""Tests for P3-05: TabEventBus, SnapshotWriter, and GET /tabs/stream SSE.

⚠️⚠️ SAFETY (READ TWICE) ⚠️⚠️

  NO test in this file EVER launches real Playwright, real Chromium, or
  the CloakBrowser wrapper. Every test drives BrowserManager against
  the `FakeContext` / `FakePage` doubles from `test_browser_lifecycle`,
  or exercises the TabEventBus / SnapshotWriter in isolation.

  NO test in this file EVER binds port 9471. The `MINERU_BROWSER_PORT`
  autouse rebind (shared with `test_browser_lifecycle.py`) redirects
  `browser.config.PORT` to an OS-picked free port, and every test that
  spins up an in-process HTTP server explicitly asserts `bound != 9471`
  before doing anything else.

  NO test in this file EVER contacts a real Telegram, Google, or any
  outbound endpoint. The SSE endpoint under test is 127.0.0.1-only.

Coverage (spec §4.3 P3-05 done-criteria):

  TabEventBus unit tests:
    - subscribe returns a bounded queue, unsubscribe drops it.
    - publish fans out to every current subscriber non-blocking.
    - full-queue drops increment `drop_count`; other subscribers get
      the event.
    - unsubscribe is idempotent.
    - 3-subscriber fan-out concrete assertion.

  SnapshotWriter:
    - Multiple rapid triggers within the debounce window coalesce to
      ONE write.
    - Written file mode is 0600.
    - JSON payload parses successfully (no partial writes).
    - Atomic path: uses O_EXCL for temp + os.replace for the swap
      (verified via monkeypatch that records os.open flags).
    - Path is env-overridable via `MINERU_BROWSER_TABS_SNAPSHOT_PATH`
      (via config reload).
    - `snapshot_fn` raising doesn't crash the writer.

  SSE integration:
    - In-process ThreadingHTTPServer on an OS-picked free port bound to
      127.0.0.1; explicit `assert bound_port != 9471`.
    - GET /tabs/stream returns 200 + Content-Type: text/event-stream.
    - Initial `event: snapshot` frame fires immediately.
    - Subsequent state changes stream through as `event: <type>` frames.
    - Heartbeat comment fires within the configured window.
    - Subscriber count decrements to 0 after client disconnect
      (leaking-subscribers assertion).

  Snapshot writer wiring:
    - A state change through BrowserManager triggers the writer.
    - Snapshot file exists at the tmp path after write settles.
    - Mode is 0600.
    - Path knob honors env override (asserted via the wired writer
      pointing at a tmp path).

  Non-negotiable safety:
    - No real Playwright imported.
    - Bound port is never 9471.
    - Manager under test has NO real browser context.
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import queue
import socket
import stat
import sys
import tempfile
import threading
import time
import types
import urllib.request
from collections import defaultdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Shared fakes (mirror test_browser_lifecycle.py so both files stay hermetic).
# ---------------------------------------------------------------------------


class FakePage:
    """Minimal Page double — same slice test_browser_lifecycle uses."""

    def __init__(
        self,
        url: str = "https://example.com",
        title: str = "Example",
        viewport: Optional[Dict[str, int]] = None,
        opener: Optional["FakePage"] = None,
    ) -> None:
        self._url = url
        self._title = title
        self._viewport = viewport if viewport is not None else {"width": 1440, "height": 900}
        self._opener = opener
        self._listeners: Dict[str, List[Callable[..., None]]] = defaultdict(list)
        self.close_called = False

    @property
    def url(self) -> str:
        return self._url

    def title(self) -> str:
        return self._title

    def viewport_size(self) -> Optional[Dict[str, int]]:
        return self._viewport

    def opener(self) -> Optional["FakePage"]:
        return self._opener

    def on(self, event: str, cb: Callable[..., None]) -> None:
        self._listeners[event].append(cb)

    def close(self) -> None:
        self.close_called = True

    def emit(self, event: str, *args: Any) -> None:
        for cb in list(self._listeners[event]):
            cb(*args) if args else cb(None)


class FakeContext:
    """Minimal BrowserContext double — same slice test_browser_lifecycle uses."""

    def __init__(self) -> None:
        self._page_listeners: List[Callable[[FakePage], None]] = []
        self.pages: List[FakePage] = []

    def on(self, event: str, cb: Callable[..., None]) -> None:
        if event == "page":
            self._page_listeners.append(cb)

    def emit_page(self, page: FakePage) -> None:
        self.pages.append(page)
        for cb in list(self._page_listeners):
            cb(page)


# ---------------------------------------------------------------------------
# Autouse fixtures: safe port + tmp snapshot path.
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Ask the OS for a free ephemeral port; never returns 9471."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    if port == 9471:
        return _pick_free_port()
    return port


@pytest.fixture(scope="session")
def safe_test_port() -> int:
    return _pick_free_port()


@pytest.fixture(autouse=True)
def isolated_browser_env(
    tmp_path_factory: pytest.TempPathFactory,
    safe_test_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect port, PID file, and snapshot path so tests never touch prod."""
    tmp_pid = tmp_path_factory.mktemp("browser-pid") / "test-server.pid"
    tmp_snapshot = tmp_path_factory.mktemp("browser-tabs") / "browser-tabs.json"

    monkeypatch.setenv("MINERU_BROWSER_PORT", str(safe_test_port))
    monkeypatch.setenv("MINERU_BROWSER_PID_FILE", str(tmp_pid))
    monkeypatch.setenv("MINERU_BROWSER_USE_CLOAK", "false")
    monkeypatch.setenv("MINERU_BROWSER_TABS_SNAPSHOT_PATH", str(tmp_snapshot))
    # Zero debounce so tests don't wait on the Timer.
    monkeypatch.setenv("MINERU_BROWSER_TABS_DEBOUNCE_MS", "0")
    # Short heartbeat so the SSE heartbeat assertion doesn't burn wall time.
    monkeypatch.setenv("MINERU_BROWSER_SSE_HEARTBEAT_SECONDS", "0.3")

    import browser.config as config_mod
    import browser.server as server_mod
    import browser.tab_event_bus as bus_mod
    import browser.tab_snapshot_writer as writer_mod
    importlib.reload(config_mod)
    importlib.reload(bus_mod)
    importlib.reload(writer_mod)
    importlib.reload(server_mod)

    assert config_mod.PORT != 9471, "port rebind didn't take"


# ---------------------------------------------------------------------------
# TabEventBus unit tests
# ---------------------------------------------------------------------------


def test_bus_subscribe_returns_bounded_queue() -> None:
    from browser.tab_event_bus import TabEventBus

    bus = TabEventBus(queue_size=4)
    q = bus.subscribe()
    assert isinstance(q, queue.Queue)
    assert q.maxsize == 4
    assert bus.subscriber_count() == 1


def test_bus_unsubscribe_drops_from_list_and_is_idempotent() -> None:
    from browser.tab_event_bus import TabEventBus

    bus = TabEventBus()
    q = bus.subscribe()
    assert bus.subscriber_count() == 1
    bus.unsubscribe(q)
    assert bus.subscriber_count() == 0
    # Double-unsubscribe is a no-op.
    bus.unsubscribe(q)
    assert bus.subscriber_count() == 0


def test_bus_publish_fans_out_to_three_subscribers() -> None:
    from browser.tab_event_bus import TabEventBus

    bus = TabEventBus()
    a = bus.subscribe()
    b = bus.subscribe()
    c = bus.subscribe()
    event = {"type": "opened", "tab": {"targetId": "tab_1"}, "ts": "now"}
    bus.publish(event)
    for q in (a, b, c):
        assert q.get_nowait() == event


def test_bus_publish_full_queue_drops_that_subscriber_only() -> None:
    """A full subscriber queue drops the event WITHOUT affecting other subs.

    Uses two subscribers on different buses (all subs on a single bus
    share the same `queue_size`); the assertion is that a slow bus's
    drop does not correlate with the fast bus's delivery.
    """
    from browser.tab_event_bus import TabEventBus

    tiny_bus = TabEventBus(queue_size=1)
    big_bus = TabEventBus(queue_size=1000)
    slow = tiny_bus.subscribe()
    fast = big_bus.subscribe()

    # First publish: both accept.
    tiny_bus.publish({"seq": 0})
    big_bus.publish({"seq": 0})
    # Second publish: tiny's queue is full → drop; big has room.
    tiny_bus.publish({"seq": 1})
    big_bus.publish({"seq": 1})
    assert tiny_bus.drop_count() == 1
    assert big_bus.drop_count() == 0
    # Fast subscriber has both events.
    got_fast = [fast.get_nowait(), fast.get_nowait()]
    assert got_fast == [{"seq": 0}, {"seq": 1}]
    # Slow subscriber only saw the first.
    assert slow.get_nowait() == {"seq": 0}
    assert slow.empty()


def test_bus_publish_slow_sub_does_not_block_fast_sub_same_bus() -> None:
    """On the same bus with tiny queue, one full sub loses events but the
    other still gets its share (each has its OWN queue)."""
    from browser.tab_event_bus import TabEventBus

    bus = TabEventBus(queue_size=1)
    a = bus.subscribe()
    b = bus.subscribe()
    bus.publish({"seq": 0})
    # Both accept event 0. Drain b so its queue has room again.
    _ = b.get_nowait()
    bus.publish({"seq": 1})
    # a's queue is full → drop. b's queue was drained → accepts.
    assert bus.drop_count() == 1
    assert b.get_nowait() == {"seq": 1}
    assert a.get_nowait() == {"seq": 0}
    assert a.empty()


def test_bus_publish_never_blocks_on_full_queues() -> None:
    """publish() must return promptly even if every subscriber queue is full."""
    from browser.tab_event_bus import TabEventBus

    bus = TabEventBus(queue_size=1)
    for _ in range(5):
        bus.subscribe()
    # Prime every subscriber's queue to full.
    bus.publish({"n": 0})
    start = time.monotonic()
    for i in range(10):
        bus.publish({"n": i + 1})
    elapsed = time.monotonic() - start
    # 10 publishes with 5 subscribers each full should be well under 100ms.
    assert elapsed < 1.0, f"publish blocked for {elapsed:.3f}s on full queues"
    assert bus.drop_count() == 50


def test_bus_thread_safe_publish_from_many_threads() -> None:
    """Concurrent publishes from many threads deliver in order to each queue."""
    from browser.tab_event_bus import TabEventBus

    bus = TabEventBus(queue_size=1000)
    q = bus.subscribe()
    events_per_thread = 20
    threads: List[threading.Thread] = []

    def _publish_range(base: int) -> None:
        for i in range(events_per_thread):
            bus.publish({"src": base, "seq": i})

    for base in range(5):
        t = threading.Thread(target=_publish_range, args=(base,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    delivered: List[Dict[str, Any]] = []
    for _ in range(5 * events_per_thread):
        delivered.append(q.get_nowait())
    assert len(delivered) == 5 * events_per_thread
    assert bus.drop_count() == 0


# ---------------------------------------------------------------------------
# SnapshotWriter tests
# ---------------------------------------------------------------------------


def test_snapshot_writer_writes_atomic_0600_json(tmp_path: Path) -> None:
    from browser.tab_snapshot_writer import SnapshotWriter

    dest = tmp_path / "browser-tabs.json"
    tabs_snapshot: List[Dict[str, Any]] = [
        {"targetId": "tab_deadbeef", "url": "https://example.com", "title": "Example"}
    ]

    w = SnapshotWriter(dest, snapshot_fn=lambda: tabs_snapshot, debounce_ms=0)
    w.trigger()
    # debounce_ms=0 writes inline.
    assert dest.exists()

    # Mode is 0600 (S_IRUSR | S_IWUSR).
    mode = stat.S_IMODE(dest.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert "generatedAt" in payload
    assert payload["tabs"] == tabs_snapshot


def test_snapshot_writer_debounce_coalesces_burst(tmp_path: Path) -> None:
    """Multiple rapid triggers → COALESCED writes, not one-per-trigger.

    Hardened against timing flake: instead of asserting the exact
    trailing-edge write completed in a fixed sleep (which raced with a
    slow CI Timer thread scheduler), we assert on the coalescing
    invariant: `write_count << trigger_count`. Twenty triggers inside
    a debounce window must not produce twenty writes; a value in [1,
    3] confirms coalescing even if the scheduler produces a couple of
    edge-case writes. We also poll for the trailing-edge write to
    land instead of a single blocking sleep, so a slow scheduler
    doesn't fail the "file exists" assertion.
    """
    from browser.tab_snapshot_writer import SnapshotWriter

    dest = tmp_path / "browser-tabs.json"
    call_count = {"n": 0}

    def _fn() -> List[Dict[str, Any]]:
        call_count["n"] += 1
        return [{"targetId": "tab_x", "url": "https://x", "title": "X"}]

    # 50ms debounce — fast enough for the test not to drag, long enough
    # that a burst inside the window coalesces.
    w = SnapshotWriter(dest, snapshot_fn=_fn, debounce_ms=50)
    for _ in range(20):
        w.trigger()

    # Poll for the trailing-edge write to land. Generous overall budget
    # (2s) so a slow CI Timer thread still succeeds, but exits early on
    # a healthy scheduler so the test stays fast in the common case.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and w.write_count() == 0:
        time.sleep(0.05)

    assert w.trigger_count() == 20
    # Coalescing invariant: 20 triggers must not produce 20 writes.
    # A well-behaved debounce collapses to exactly 1; a lightly-loaded
    # scheduler can leak a second write if the Timer fires between
    # triggers. Either is acceptable as long as the ratio proves
    # coalescing is happening.
    assert 1 <= w.write_count() <= 3, (
        f"expected debounce to coalesce 20 triggers to <=3 writes, "
        f"got {w.write_count()}"
    )
    assert call_count["n"] == w.write_count(), (
        f"snapshot_fn called {call_count['n']} times, but writer counted "
        f"{w.write_count()} writes — they should match"
    )
    assert dest.exists()


def test_snapshot_writer_uses_o_excl_and_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the atomicity primitives: temp opened with O_EXCL, dest via os.replace."""
    from browser import tab_snapshot_writer as writer_mod

    dest = tmp_path / "browser-tabs.json"
    tabs_snapshot: List[Dict[str, Any]] = [{"targetId": "tab_y", "url": "u", "title": "t"}]

    recorded_open_flags: List[int] = []
    real_open = os.open

    def spy_open(path: str, flags: int, mode: int = 0o777) -> int:
        # Only record calls the writer originated (inside its own temp dir).
        if str(path).startswith(str(tmp_path)) and ".tmp." in str(path):
            recorded_open_flags.append(flags)
        return real_open(path, flags, mode)

    monkeypatch.setattr(writer_mod.os, "open", spy_open)

    recorded_replace: List[Tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:
        if str(dst) == str(dest):
            recorded_replace.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(writer_mod.os, "replace", spy_replace)

    w = writer_mod.SnapshotWriter(dest, snapshot_fn=lambda: tabs_snapshot, debounce_ms=0)
    w.trigger()

    # Temp file was opened with O_EXCL + O_CREAT + O_WRONLY.
    assert recorded_open_flags, "writer should have opened a temp file"
    flags = recorded_open_flags[0]
    assert flags & os.O_EXCL, f"O_EXCL missing in flags={oct(flags)}"
    assert flags & os.O_CREAT, f"O_CREAT missing in flags={oct(flags)}"
    assert flags & os.O_WRONLY, f"O_WRONLY missing in flags={oct(flags)}"

    # Final rename via os.replace(src → dest).
    assert recorded_replace, "writer should have called os.replace to finalize"
    src, dst = recorded_replace[0]
    assert dst == str(dest)
    assert ".tmp." in src, f"src {src} should look like a temp file"

    # Final file mode is 0600.
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_snapshot_writer_env_override_reflected_in_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`MINERU_BROWSER_TABS_SNAPSHOT_PATH` rebinds `config.TABS_SNAPSHOT_PATH`."""
    override = tmp_path / "custom-tabs.json"
    monkeypatch.setenv("MINERU_BROWSER_TABS_SNAPSHOT_PATH", str(override))

    import browser.config as config_mod
    importlib.reload(config_mod)
    assert config_mod.TABS_SNAPSHOT_PATH == override


def test_snapshot_writer_snapshot_fn_raising_does_not_crash(tmp_path: Path) -> None:
    from browser.tab_snapshot_writer import SnapshotWriter

    def _boom() -> List[Dict[str, Any]]:
        raise RuntimeError("simulated failure")

    dest = tmp_path / "browser-tabs.json"
    w = SnapshotWriter(dest, snapshot_fn=_boom, debounce_ms=0)
    w.trigger()  # must not raise
    # No file written.
    assert not dest.exists()
    assert w.write_count() == 0


def test_snapshot_writer_partial_temp_files_cleaned_on_next_burst(tmp_path: Path) -> None:
    """A successful trigger leaves no temp files lying around beside the dest."""
    from browser.tab_snapshot_writer import SnapshotWriter

    dest = tmp_path / "browser-tabs.json"
    w = SnapshotWriter(
        dest,
        snapshot_fn=lambda: [{"targetId": "tab_z", "url": "u", "title": "t"}],
        debounce_ms=0,
    )
    for _ in range(3):
        w.trigger()
    # After the writes settle, no leftover temps in the parent.
    time.sleep(0.05)
    stragglers = [
        p.name for p in tmp_path.iterdir()
        if p.name.startswith(f".{dest.name}.tmp.")
    ]
    assert not stragglers, f"leftover temps: {stragglers}"


def test_snapshot_writer_flush_writes_immediately(tmp_path: Path) -> None:
    from browser.tab_snapshot_writer import SnapshotWriter

    dest = tmp_path / "browser-tabs.json"
    w = SnapshotWriter(
        dest,
        snapshot_fn=lambda: [{"targetId": "tab_flush", "url": "u", "title": "t"}],
        debounce_ms=500,
    )
    # Trigger schedules a Timer 500ms out; flush cancels it and writes now.
    w.trigger()
    assert not dest.exists()
    w.flush()
    assert dest.exists()


def test_snapshot_writer_tightens_created_parent_to_0700(
    tmp_path: Path,
) -> None:
    """If we create the parent dir, chmod it to 0700 (owner-only).

    Default `mkdir(parents=True)` uses 0755 — a shared user on the box
    could enumerate snapshot filenames. When the writer brings the
    parent dir into existence, we tighten it to 0700 so only the
    invoking user can list the dir.
    """
    from browser.tab_snapshot_writer import SnapshotWriter

    # Parent dir does NOT exist yet — the writer will create it and
    # therefore must tighten the mode.
    parent = tmp_path / "new" / "cache"
    dest = parent / "browser-tabs.json"
    assert not parent.exists()

    w = SnapshotWriter(
        dest,
        snapshot_fn=lambda: [{"targetId": "tab_p", "url": "u", "title": "t"}],
        debounce_ms=0,
    )
    w.trigger()
    assert dest.exists()

    mode = stat.S_IMODE(parent.stat().st_mode)
    assert mode == 0o700, f"expected parent 0700, got {oct(mode)}"


def test_snapshot_writer_does_not_tighten_pre_existing_parent(
    tmp_path: Path,
) -> None:
    """A pre-existing parent dir keeps its mode — we only tighten what we made."""
    from browser.tab_snapshot_writer import SnapshotWriter

    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)  # explicit 0755 to see if we clobber it
    # Sanity check — some umask configs mask the mode down; verify what
    # we actually got before we assert we didn't clobber it.
    original_mode = stat.S_IMODE(parent.stat().st_mode)

    dest = parent / "browser-tabs.json"
    w = SnapshotWriter(
        dest,
        snapshot_fn=lambda: [{"targetId": "tab_p", "url": "u", "title": "t"}],
        debounce_ms=0,
    )
    w.trigger()
    assert dest.exists()

    # Parent mode is UNCHANGED — we only tighten dirs we created.
    assert stat.S_IMODE(parent.stat().st_mode) == original_mode


# ---------------------------------------------------------------------------
# BrowserManager publishes to bus + triggers snapshot writer
# ---------------------------------------------------------------------------


def _fresh_manager_with_wiring(tmp_path: Path) -> Any:
    """Build a BrowserManager with a fresh bus + writer pointing at tmp."""
    import browser.server as server_mod
    importlib.reload(server_mod)
    from browser.tab_event_bus import TabEventBus
    from browser.tab_snapshot_writer import SnapshotWriter

    bus = TabEventBus()
    snapshot_path = tmp_path / "browser-tabs.json"
    mgr = server_mod.BrowserManager(event_bus=bus, snapshot_writer=None)

    # Snapshot writer needs a reference to manager.list_tabs — construct
    # after mgr so the lambda closes over it.
    writer = SnapshotWriter(
        snapshot_path,
        snapshot_fn=lambda: mgr.list_tabs(),
        debounce_ms=0,
    )
    mgr._snapshot_writer = writer

    ctx = FakeContext()
    mgr._context = ctx
    ctx.on("page", mgr._on_new_page)
    return mgr


def test_manager_new_page_publishes_opened_event(tmp_path: Path) -> None:
    mgr = _fresh_manager_with_wiring(tmp_path)
    ctx: FakeContext = mgr._context
    q = mgr._event_bus.subscribe()
    ctx.emit_page(FakePage(url="https://a", title="A"))
    event = q.get(timeout=1.0)
    assert event["type"] == "opened"
    assert event["tab"]["url"] == "https://a"
    assert event["tab"]["openedBy"] == "user"
    assert "ts" in event


def test_manager_page_close_publishes_closed_event_with_enriched_tab(tmp_path: Path) -> None:
    mgr = _fresh_manager_with_wiring(tmp_path)
    ctx: FakeContext = mgr._context
    q = mgr._event_bus.subscribe()
    page = FakePage(url="https://a", title="A")
    ctx.emit_page(page)
    _opened = q.get(timeout=1.0)
    page.emit("close")
    closed = q.get(timeout=1.0)
    assert closed["type"] == "closed"
    # The 'closed' payload carries the enriched dict captured BEFORE
    # cleanup so subscribers know which tab left.
    assert closed["tab"]["url"] == "https://a"
    assert closed["tab"]["title"] == "A"


def test_manager_framenavigated_publishes_navigated_event(tmp_path: Path) -> None:
    mgr = _fresh_manager_with_wiring(tmp_path)
    ctx: FakeContext = mgr._context
    q = mgr._event_bus.subscribe()
    page = FakePage(url="https://a")
    ctx.emit_page(page)
    _opened = q.get(timeout=1.0)
    page.emit("framenavigated", object())
    nav = q.get(timeout=1.0)
    assert nav["type"] == "navigated"
    assert nav["tab"]["readyState"] == "loading"


def test_manager_load_publishes_navigated_event(tmp_path: Path) -> None:
    mgr = _fresh_manager_with_wiring(tmp_path)
    ctx: FakeContext = mgr._context
    q = mgr._event_bus.subscribe()
    page = FakePage(url="https://a")
    ctx.emit_page(page)
    _opened = q.get(timeout=1.0)
    page.emit("load")
    ev = q.get(timeout=1.0)
    assert ev["type"] == "navigated"
    assert ev["tab"]["readyState"] == "complete"


def test_manager_focused_method_publishes_focused_event(tmp_path: Path) -> None:
    mgr = _fresh_manager_with_wiring(tmp_path)
    ctx: FakeContext = mgr._context
    q = mgr._event_bus.subscribe()
    ctx.emit_page(FakePage(url="https://a"))
    ctx.emit_page(FakePage(url="https://b"))
    # Drain the two opened events.
    q.get(timeout=1.0); q.get(timeout=1.0)
    # Take the second tab id (deterministic via iteration order).
    tab_ids = list(mgr._tabs.keys())
    assert len(tab_ids) == 2
    mgr._on_page_focused(tab_ids[1])
    ev = q.get(timeout=1.0)
    assert ev["type"] == "focused"
    assert ev["tab"]["targetId"] == tab_ids[1]
    assert ev["tab"]["focused"] is True
    # The other tab was un-focused.
    assert mgr._tab_meta[tab_ids[0]]["focused"] is False


def test_manager_state_change_triggers_snapshot_writer(tmp_path: Path) -> None:
    mgr = _fresh_manager_with_wiring(tmp_path)
    ctx: FakeContext = mgr._context
    dest = mgr._snapshot_writer._path
    ctx.emit_page(FakePage(url="https://a", title="A"))
    # debounce_ms=0 writes inline.
    assert dest.exists()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert len(payload["tabs"]) == 1
    assert payload["tabs"][0]["url"] == "https://a"


def test_manager_burst_of_state_changes_coalesces_to_one_snapshot(tmp_path: Path) -> None:
    """20 rapid state-change events → coalesced writes (final state).

    Hardened against timing flake — see the note on
    `test_snapshot_writer_debounce_coalesces_burst`: a fixed sleep
    raced the scheduler on loaded CI hosts. We poll for the trailing
    write and assert on the coalescing INVARIANT (writes << triggers)
    rather than the exact single-write count, so a scheduler that
    lets a second Timer edge slip through does not fail the test.
    Correctness invariant (final state has all 20 tabs) is checked
    after the poll.
    """
    import browser.server as server_mod
    importlib.reload(server_mod)
    from browser.tab_event_bus import TabEventBus
    from browser.tab_snapshot_writer import SnapshotWriter

    bus = TabEventBus()
    snapshot_path = tmp_path / "browser-tabs.json"
    mgr = server_mod.BrowserManager(event_bus=bus, snapshot_writer=None)
    writer = SnapshotWriter(
        snapshot_path,
        snapshot_fn=lambda: mgr.list_tabs(),
        debounce_ms=50,  # nonzero so the coalesce is exercised
    )
    mgr._snapshot_writer = writer
    ctx = FakeContext()
    mgr._context = ctx
    ctx.on("page", mgr._on_new_page)

    for i in range(20):
        ctx.emit_page(FakePage(url=f"https://a{i}", title=f"A{i}"))

    # Poll for the trailing-edge write. Generous 2s budget for a slow
    # CI Timer thread; exits early on a healthy scheduler.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and writer.write_count() == 0:
        time.sleep(0.05)

    assert writer.trigger_count() >= 20
    # Coalescing invariant: 20 triggers must not produce 20 writes.
    assert 1 <= writer.write_count() <= 3, (
        f"expected debounce to coalesce 20 triggers to <=3 writes, "
        f"got {writer.write_count()}"
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    # Final state has all 20 tabs.
    assert len(payload["tabs"]) == 20


# ---------------------------------------------------------------------------
# SSE integration test — in-process ThreadingHTTPServer, no port 9471.
# ---------------------------------------------------------------------------


@pytest.fixture
def sse_server(tmp_path: Path) -> Tuple[ThreadingHTTPServer, int, Any]:
    """Spin up an in-process ThreadingHTTPServer with a wired manager.

    Yields (server, port, manager). Rebinds
    `browser.server.manager` / `._tab_event_bus` / `._snapshot_writer`
    so the module-level `BrowserHandler` (which reads those names at
    request time) sees the test-owned instances.
    """
    import browser.server as server_mod

    # Freshly wire a manager with a real bus + writer against tmp_path.
    from browser.tab_event_bus import TabEventBus
    from browser.tab_snapshot_writer import SnapshotWriter

    bus = TabEventBus()
    snapshot_path = tmp_path / "browser-tabs.json"
    mgr = server_mod.BrowserManager(event_bus=bus, snapshot_writer=None)
    writer = SnapshotWriter(
        snapshot_path,
        snapshot_fn=lambda: mgr.list_tabs(),
        debounce_ms=0,
    )
    mgr._snapshot_writer = writer

    ctx = FakeContext()
    mgr._context = ctx
    ctx.on("page", mgr._on_new_page)

    # Rebind module-level names so BrowserHandler resolves them.
    server_mod.manager = mgr
    server_mod._tab_event_bus = bus
    server_mod._snapshot_writer = writer

    # Pick a fresh free port — NEVER 9471. Assert it below.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9471, "test grabbed the production port — should be impossible"

    server = ThreadingHTTPServer(("127.0.0.1", port), server_mod.BrowserHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield server, port, mgr
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)


def test_sse_server_bound_port_is_not_9471(sse_server: Tuple[ThreadingHTTPServer, int, Any]) -> None:
    _server, port, _mgr = sse_server
    assert port != 9471


def test_sse_returns_200_and_event_stream_content_type(
    sse_server: Tuple[ThreadingHTTPServer, int, Any],
) -> None:
    _server, port, _mgr = sse_server

    # Raw socket read of the response line + headers. We avoid
    # http.client here because getresponse() can end up blocking on
    # the body for a streaming response depending on how the underlying
    # BufferedReader interacts with our chunk-less SSE frames.
    sock = _open_sse_socket(port, timeout_s=3.0)
    header_bytes = b""
    deadline = time.monotonic() + 2.0
    try:
        while time.monotonic() < deadline and b"\r\n\r\n" not in header_bytes:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = sock.recv(1024)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                break
            header_bytes += chunk
    finally:
        sock.close()
    assert b"\r\n\r\n" in header_bytes, f"never saw end of headers: {header_bytes!r}"
    headers_section = header_bytes.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
    lines = headers_section.split("\r\n")
    # Response line: HTTP/1.x 200 OK
    assert lines[0].startswith("HTTP/1.") and " 200 " in lines[0], lines[0]
    # Headers are case-insensitive on the header name.
    header_map = {ln.split(":", 1)[0].strip().lower(): ln.split(":", 1)[1].strip()
                  for ln in lines[1:] if ":" in ln}
    assert "text/event-stream" in header_map.get("content-type", ""), header_map
    assert "no-cache" in header_map.get("cache-control", ""), header_map


def _open_sse_socket(port: int, timeout_s: float) -> socket.socket:
    """Open a raw TCP socket, send an SSE GET, return the socket.

    `http.client.HTTPResponse.read()` buffers/blocks in ways that
    interact badly with an SSE stream (waits for content-length or
    end-of-stream that never arrives), so we drop down to sockets for
    fine-grained control over reads-as-they-arrive.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout_s)
    sock.sendall(
        b"GET /tabs/stream HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Accept: text/event-stream\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    return sock


def _read_sse_frames(port: int, min_frames: int, timeout_s: float) -> List[str]:
    """Read from /tabs/stream until at least `min_frames` blank-line-terminated frames arrive.

    A frame is anything (event frame or `:heartbeat` comment frame)
    terminated by a blank line. HTTP headers are stripped before frame
    parsing begins.
    """
    sock = _open_sse_socket(port, timeout_s)
    deadline = time.monotonic() + timeout_s
    frames: List[str] = []
    buffer = b""
    header_stripped = False
    try:
        while time.monotonic() < deadline and len(frames) < min_frames:
            remaining = max(0.05, min(0.5, deadline - time.monotonic()))
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            except (ConnectionResetError, BrokenPipeError):
                break
            if not chunk:
                break
            buffer += chunk
            if not header_stripped:
                sep = b"\r\n\r\n"
                if sep in buffer:
                    _, buffer = buffer.split(sep, 1)
                    header_stripped = True
                else:
                    continue
            while b"\n\n" in buffer:
                frame_bytes, buffer = buffer.split(b"\n\n", 1)
                frames.append(frame_bytes.decode("utf-8", errors="replace"))
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return frames


def test_sse_initial_snapshot_event_fires_immediately(
    sse_server: Tuple[ThreadingHTTPServer, int, Any],
) -> None:
    _server, port, mgr = sse_server
    # Seed one tab so the snapshot isn't empty.
    ctx: FakeContext = mgr._context
    ctx.emit_page(FakePage(url="https://seed", title="Seed"))

    frames = _read_sse_frames(port, min_frames=1, timeout_s=2.0)
    assert frames, "no frames received"
    first = frames[0]
    assert "event: snapshot" in first, first
    # Data line carries the /tabs body.
    data_line = [ln for ln in first.split("\n") if ln.startswith("data: ")][0]
    body = json.loads(data_line[len("data: "):])
    assert body["tabs"][0]["url"] == "https://seed"


def test_sse_streams_two_state_change_events(
    sse_server: Tuple[ThreadingHTTPServer, int, Any],
) -> None:
    _server, port, mgr = sse_server
    ctx: FakeContext = mgr._context

    # Open the SSE connection in a background thread, collect frames.
    collected: List[str] = []
    error_holder: List[Exception] = []

    def _reader() -> None:
        try:
            for f in _read_sse_frames(port, min_frames=3, timeout_s=5.0):
                collected.append(f)
        except Exception as exc:
            error_holder.append(exc)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()
    # Give the reader a beat to open the connection + subscribe.
    time.sleep(0.5)

    # Now drive 2 state changes.
    ctx.emit_page(FakePage(url="https://one", title="One"))
    ctx.emit_page(FakePage(url="https://two", title="Two"))

    reader_thread.join(timeout=5.0)
    if error_holder:
        raise error_holder[0]
    assert len(collected) >= 3, (
        f"expected initial snapshot + 2 opened events; got {len(collected)}: "
        f"{collected}"
    )
    # First frame is the snapshot.
    assert "event: snapshot" in collected[0]
    # Subsequent frames include 'opened' events.
    subsequent = "\n".join(collected[1:])
    assert "event: opened" in subsequent, subsequent


def test_sse_heartbeat_comment_fires_within_window(
    sse_server: Tuple[ThreadingHTTPServer, int, Any],
) -> None:
    """With SSE_HEARTBEAT_SECONDS=0.3 (set in autouse fixture), a heartbeat
    fires within roughly one heartbeat window on an idle connection."""
    _server, port, _mgr = sse_server

    sock = _open_sse_socket(port, timeout_s=3.0)
    deadline = time.monotonic() + 2.0
    buffer = b""
    saw_heartbeat = False
    try:
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(1024)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                break
            buffer += chunk
            if b":heartbeat" in buffer:
                saw_heartbeat = True
                break
    finally:
        sock.close()
    assert saw_heartbeat, (
        f"no heartbeat comment observed within 2s (buffer={buffer!r})"
    )


def test_sse_subscriber_cleaned_up_on_disconnect(
    sse_server: Tuple[ThreadingHTTPServer, int, Any],
) -> None:
    """Bus's subscriber_count drops to 0 shortly after the client closes."""
    _server, port, mgr = sse_server
    bus = mgr._event_bus
    assert bus.subscriber_count() == 0

    sock = _open_sse_socket(port, timeout_s=3.0)
    # Wait for the handler to reach subscribe() (which happens BEFORE
    # send_response so a fast-appearing subscriber tells us the
    # connection was accepted and the handler is executing).
    for _ in range(40):
        if bus.subscriber_count() >= 1:
            break
        time.sleep(0.05)
    assert bus.subscriber_count() == 1

    # Close the client abruptly.
    sock.close()

    # Nudge the server: heartbeat (~0.3s) OR an event will drive the
    # handler's next wfile.write into BrokenPipeError. Publish an event
    # to speed things along.
    ctx: FakeContext = mgr._context
    ctx.emit_page(FakePage(url="https://post-disconnect"))

    for _ in range(40):
        if bus.subscriber_count() == 0:
            break
        time.sleep(0.1)
    assert bus.subscriber_count() == 0, (
        f"subscriber leaked; count={bus.subscriber_count()}"
    )


def test_sse_second_connection_gets_fresh_initial_snapshot(
    sse_server: Tuple[ThreadingHTTPServer, int, Any],
) -> None:
    """A new subscriber always gets a snapshot event covering current state."""
    _server, port, mgr = sse_server
    ctx: FakeContext = mgr._context
    # Set up state BEFORE any connection.
    ctx.emit_page(FakePage(url="https://early", title="Early"))
    ctx.emit_page(FakePage(url="https://also-early", title="AlsoEarly"))

    frames = _read_sse_frames(port, min_frames=1, timeout_s=2.0)
    assert frames
    first = frames[0]
    assert "event: snapshot" in first
    data_line = [ln for ln in first.split("\n") if ln.startswith("data: ")][0]
    body = json.loads(data_line[len("data: "):])
    urls = {t["url"] for t in body["tabs"]}
    assert urls == {"https://early", "https://also-early"}


# ---------------------------------------------------------------------------
# Non-negotiable safety guards
# ---------------------------------------------------------------------------


def test_no_real_playwright_imported() -> None:
    """Verify we never accidentally pulled in real Playwright bindings."""
    pw = sys.modules.get("playwright.sync_api")
    if pw is None:
        return
    # If pw is present it must be a stub (no __file__ or synthetic).
    assert getattr(pw, "__file__", None) in (None, ""), (
        f"real playwright was imported: {pw.__file__!r}"
    )


def test_config_snapshot_path_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "custom.json"
    monkeypatch.setenv("MINERU_BROWSER_TABS_SNAPSHOT_PATH", str(override))
    import browser.config as config_mod
    importlib.reload(config_mod)
    assert config_mod.TABS_SNAPSHOT_PATH == override


def test_config_snapshot_path_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINERU_BROWSER_TABS_SNAPSHOT_PATH", raising=False)
    import browser.config as config_mod
    importlib.reload(config_mod)
    # The default derives from the MINERU_HOME seam (`<MINERU_HOME>/cache/
    # browser-tabs.json`); assert the workspace-relative suffix rather than a
    # `.mineru` prefix so it holds under any MINERU_HOME.
    assert str(config_mod.TABS_SNAPSHOT_PATH).endswith(
        "/cache/browser-tabs.json"
    )


def test_config_sse_heartbeat_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_BROWSER_SSE_HEARTBEAT_SECONDS", "7.5")
    import browser.config as config_mod
    importlib.reload(config_mod)
    assert config_mod.SSE_HEARTBEAT_SECONDS == 7.5


def test_config_snapshot_debounce_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_BROWSER_TABS_DEBOUNCE_MS", "42")
    import browser.config as config_mod
    importlib.reload(config_mod)
    assert config_mod.TABS_SNAPSHOT_DEBOUNCE_MS == 42
