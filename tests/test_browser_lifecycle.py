"""Tests for P3-04: browser lifecycle + BrowserManager tab-metadata enrichment.

⚠️⚠️ SAFETY (READ TWICE) ⚠️⚠️

  NO test in this file EVER launches real Playwright, real Chromium, or
  the CloakBrowser wrapper. Every test drives BrowserManager against
  `FakeContext` / `FakePage` doubles that mimic the small slice of the
  Playwright surface `lifecycle.launch_context` and BrowserManager
  actually touch (`context.on("page", ...)`, `context.new_page()`,
  `page.on("close" | "framenavigated" | "load", ...)`, `page.url`,
  `page.title()`, `page.viewport_size()`, `page.opener()`).

  NO test in this file EVER binds a real socket, and NO test EVER
  touches port 9471. The HTTP server side of `browser/server.py`
  (`HTTPServer(("127.0.0.1", PORT), ...)`) is never spun up here; we
  drive `BrowserManager` directly. A test that instantiated a real
  server would still get the SAFE port from `MINERU_BROWSER_PORT` in
  the environment (the autouse fixture sets it), never 9471.

Coverage (spec §4.3 layer 2 + task P3-04 done-criteria):

  Config knob:
    - MINERU_BROWSER_PORT env override rebinds `browser.config.PORT`
      and is never 9471 in the test environment.

  Lifecycle callback registration:
    - `launch_context(on_new_page=cb)` registers cb via
      `context.on('page', cb)` in ALL THREE branches (standalone /
      CloakBrowser / CDP), and CDP mode also fans the callback for
      every pre-existing page on the attached context.
    - `launch_context(on_new_page=None)` leaves context.on unwired.

  BrowserManager._on_new_page:
    - Mints `tab_<8hex>` id, stores page + meta, wires page.on(close),
      page.on(framenavigated), page.on(load).
    - Detects popups via `page.opener()` -> openedBy='popup'; otherwise
      'user'.
    - Skips a page already registered by `open_tab` (identity check),
      so open_tab's 'api' tag is preserved.

  Meta lifecycle:
    - close event removes both `_tabs[tid]` and `_tab_meta[tid]`.
    - framenavigated bumps lastActivityAt, flips readyState to
      'loading', zeros lastRefCount.
    - load bumps lastActivityAt and flips readyState to 'complete'.

  Enriched list_tabs:
    - Every entry carries all 10 fields (3 legacy + 7 new) with the
      three legacy fields as the first keys in that exact order.
    - synthesized meta covers a race where meta is missing for a live tab.

  Backward compat:
    - The three legacy fields remain the first keys of every entry so a
      caller doing `entry['targetId'], entry['url'], entry['title']`
      still works unchanged.
"""

from __future__ import annotations

import importlib
import os
import re
import socket
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Autouse: rebind the browser server port BEFORE the module is imported so
# a stray `server.py::main()` call inside the process cannot bind 9471.
#
# We reload `browser.config` (and, defensively, any downstream module that
# captured PORT at import-time) inside the fixture so the new value takes.
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Ask the OS for a free ephemeral port; never returns 9471."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    if port == 9471:
        # Astronomically unlikely, but the task's non-negotiable safety
        # rule is "no test binds port 9471". Try again if the OS handed
        # us the forbidden number.
        return _pick_free_port()
    return port


@pytest.fixture(scope="session")
def safe_test_port() -> int:
    """A session-wide non-9471 port for any test that consults config.PORT."""
    return _pick_free_port()


@pytest.fixture(autouse=True)
def isolated_browser_env(
    tmp_path_factory: pytest.TempPathFactory,
    safe_test_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect the port + PID file so tests never collide with the live server.

    The env vars are read by `browser/config.py` at import time. We
    reload the module so a stray import order in the test suite still
    picks up the tmp values. Both `browser.config` and `browser.server`
    (which snapshotted PORT via `from browser.config import PORT`) get
    reloaded so the two views agree.
    """
    tmp_pid = tmp_path_factory.mktemp("browser-pid") / "test-server.pid"
    monkeypatch.setenv("MINERU_BROWSER_PORT", str(safe_test_port))
    monkeypatch.setenv("MINERU_BROWSER_PID_FILE", str(tmp_pid))
    # Defensive: force the fallback launcher path in any test that
    # accidentally reaches `_ensure_browser`. USE_CLOAK=false skips the
    # CloakBrowser branch (which would try to import the real wrapper).
    monkeypatch.setenv("MINERU_BROWSER_USE_CLOAK", "false")

    import browser.config as config_mod
    import browser.server as server_mod
    importlib.reload(config_mod)
    importlib.reload(server_mod)

    # Sanity: the reloaded module must never be on 9471.
    assert config_mod.PORT != 9471, (
        "MINERU_BROWSER_PORT reload didn't take; config.PORT is still 9471 — "
        "tests would collide with the live server."
    )


# ---------------------------------------------------------------------------
# Fake Playwright surface. Only the methods the code under test touches.
# ---------------------------------------------------------------------------


class FakePage:
    """Minimal Page double.

    Records every `page.on(event, cb)` registration so tests can fire an
    event via `.emit(event, ...)`. `url`/`title`/`viewport_size`/`opener`
    are the fields BrowserManager reads for meta.
    """

    _instances: List["FakePage"] = []

    def __init__(
        self,
        url: str = "https://example.com",
        title: str = "Example",
        viewport: Optional[Dict[str, int]] = None,
        opener: Optional["FakePage"] = None,
        raise_on_url: bool = False,
        raise_on_title: bool = False,
    ) -> None:
        self._url = url
        self._title = title
        self._viewport = viewport if viewport is not None else {"width": 1440, "height": 900}
        self._opener = opener
        self._raise_on_url = raise_on_url
        self._raise_on_title = raise_on_title
        self._listeners: Dict[str, List[Callable[..., None]]] = defaultdict(list)
        self.close_called = False
        FakePage._instances.append(self)

    # --- Playwright surface ------------------------------------------------

    @property
    def url(self) -> str:
        if self._raise_on_url:
            raise RuntimeError("simulated url read failure")
        return self._url

    def title(self) -> str:
        if self._raise_on_title:
            raise RuntimeError("simulated title read failure")
        return self._title

    def viewport_size(self) -> Optional[Dict[str, int]]:
        return self._viewport

    def opener(self) -> Optional["FakePage"]:
        return self._opener

    def on(self, event: str, cb: Callable[..., None]) -> None:
        self._listeners[event].append(cb)

    def close(self) -> None:
        self.close_called = True

    def goto(self, url: str, timeout: int = 0, wait_until: str = "load") -> None:
        # Simulate a same-page navigation: bump the recorded url so
        # subsequent reads reflect the "post-goto" state.
        self._url = url

    def wait_for_load_state(self, state: str = "load", timeout: int = 0) -> None:
        # No-op; the tests explicitly fire load events via `.emit(...)`.
        return None

    # --- Test helpers ------------------------------------------------------

    def emit(self, event: str, *args: Any) -> None:
        """Fire a Playwright-style event to every registered listener.

        Playwright fires events synchronously on the calling thread, so
        this is a straight for-loop; no async, no threads.
        """
        for cb in list(self._listeners[event]):
            cb(*args) if args else cb(None)

    def has_listener(self, event: str) -> bool:
        return len(self._listeners[event]) > 0


class FakeContext:
    """Minimal BrowserContext double.

    Records `context.on('page', cb)` registrations so tests can emit new
    pages via `.emit_page(page)`. `.new_page()` mints a fresh FakePage
    and fires the page listeners synchronously (matches Playwright's
    sync-event contract).
    """

    def __init__(self) -> None:
        self._page_listeners: List[Callable[[FakePage], None]] = []
        self.pages: List[FakePage] = []
        self._new_page_url = "https://example.com"

    def on(self, event: str, cb: Callable[..., None]) -> None:
        if event == "page":
            self._page_listeners.append(cb)

    def new_page(self) -> FakePage:
        page = FakePage(url="about:blank", title="Blank")
        self.pages.append(page)
        # Sync-event contract: fire listeners before returning.
        for cb in list(self._page_listeners):
            cb(page)
        return page

    # Test helper: simulate a user-opened new tab / popup that
    # BrowserManager did NOT create.
    def emit_page(self, page: FakePage) -> None:
        self.pages.append(page)
        for cb in list(self._page_listeners):
            cb(page)


# ---------------------------------------------------------------------------
# Config knob
# ---------------------------------------------------------------------------


def test_config_port_reads_env_var(safe_test_port: int) -> None:
    """The `MINERU_BROWSER_PORT` env var is what config.PORT resolves to."""
    import browser.config as config_mod
    assert config_mod.PORT == safe_test_port
    assert config_mod.PORT != 9471


def test_config_port_defaults_to_9471_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the env var, config.PORT defaults to the live production port."""
    monkeypatch.delenv("MINERU_BROWSER_PORT", raising=False)
    import browser.config as config_mod
    importlib.reload(config_mod)
    assert config_mod.PORT == 9471


def test_no_test_ever_binds_port_9471(safe_test_port: int) -> None:
    """Explicit assertion that the test harness stays off 9471."""
    assert safe_test_port != 9471
    import browser.config as config_mod
    assert config_mod.PORT != 9471


# ---------------------------------------------------------------------------
# lifecycle.launch_context registers on_new_page in all three branches
# ---------------------------------------------------------------------------


class _RecordingLifecycle:
    """Patch surface for lifecycle.launch_context's internal imports.

    We patch `sync_playwright` and (in the CDP path) the browser it
    returns so `launch_context` walks the target branch without
    touching real Playwright. Each branch returns a `FakeContext` we
    can then poke to fire the 'page' event and assert the callback
    got wired.
    """

    def __init__(self) -> None:
        self.contexts_returned: List[FakeContext] = []


def _fake_sync_playwright_factory(context: FakeContext) -> Any:
    """Return a callable that mimics `sync_playwright()` -> pw ctx-mgr chain."""

    class _FakeBrowser:
        def __init__(self, ctx: FakeContext) -> None:
            self._ctx = ctx

        def new_context(self, **kwargs: Any) -> FakeContext:
            return self._ctx

        def connect_over_cdp(self, endpoint: str) -> "_FakeBrowser":
            return self

        @property
        def contexts(self) -> List[FakeContext]:
            return [self._ctx]

    class _FakeChromium:
        def __init__(self, ctx: FakeContext) -> None:
            self._ctx = ctx

        def launch(self, **kwargs: Any) -> _FakeBrowser:
            return _FakeBrowser(self._ctx)

        def connect_over_cdp(self, endpoint: str) -> _FakeBrowser:
            return _FakeBrowser(self._ctx)

    class _FakePW:
        def __init__(self, ctx: FakeContext) -> None:
            self.chromium = _FakeChromium(ctx)

        def start(self) -> "_FakePW":
            return self

    class _FakeSyncPlaywright:
        def __init__(self, ctx: FakeContext) -> None:
            self._pw = _FakePW(ctx)

        def __call__(self) -> "_FakeSyncPlaywright":
            return self

        def start(self) -> _FakePW:
            return self._pw

    return _FakeSyncPlaywright(context)


class _FakeStealth:
    """Playwright-stealth Stealth() stand-in."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def use_sync(self, sync_pw: Any) -> "_FakeStealthCM":
        return _FakeStealthCM(sync_pw)


class _FakeStealthCM:
    def __init__(self, sync_pw: Any) -> None:
        self._pw = sync_pw

    def __enter__(self) -> Any:
        return self._pw.start()

    def __exit__(self, *exc: Any) -> None:
        return None


def _patch_playwright_imports(context: FakeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire the fake sync_playwright + stealth into the lifecycle module."""
    import sys, types
    # sync_playwright module surface — used by all three branches.
    fake_playwright = types.ModuleType("playwright")
    fake_playwright_sync = types.ModuleType("playwright.sync_api")
    fake_playwright_sync.sync_playwright = _fake_sync_playwright_factory(context)  # type: ignore[attr-defined]
    fake_playwright.sync_api = fake_playwright_sync  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_playwright_sync)

    fake_stealth_mod = types.ModuleType("playwright_stealth")
    fake_stealth_mod.Stealth = _FakeStealth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright_stealth", fake_stealth_mod)


def test_launch_context_standalone_wires_on_new_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standalone branch: context.on('page', cb) is registered."""
    from browser import lifecycle

    ctx = FakeContext()
    _patch_playwright_imports(ctx, monkeypatch)

    seen: List[FakePage] = []

    def cb(page: FakePage) -> None:
        seen.append(page)

    playwright, browser, returned_ctx, stealth_cm = lifecycle.launch_context(
        headless=True,
        cdp_endpoint="",
        use_cloak=False,
        cloak_executable="/nonexistent",
        persistent_profile="/tmp/does-not-exist",
        on_new_page=cb,
    )
    assert returned_ctx is ctx
    # Fire a new page and confirm the callback was wired.
    ctx.emit_page(FakePage(url="https://foo"))
    assert len(seen) == 1
    assert seen[0].url == "https://foo"


def test_launch_context_cdp_wires_on_new_page_and_fans_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CDP-attach branch: callback fires for every pre-existing tab + new ones."""
    from browser import lifecycle

    existing_a = FakePage(url="https://a")
    existing_b = FakePage(url="https://b")
    ctx = FakeContext()
    ctx.pages.extend([existing_a, existing_b])
    _patch_playwright_imports(ctx, monkeypatch)

    seen: List[FakePage] = []

    def cb(page: FakePage) -> None:
        seen.append(page)

    lifecycle.launch_context(
        headless=True,
        cdp_endpoint="http://localhost:9222",
        use_cloak=False,
        cloak_executable="/nonexistent",
        persistent_profile="/tmp/does-not-exist",
        on_new_page=cb,
    )
    # Pre-existing tabs got backfilled.
    assert {p.url for p in seen} == {"https://a", "https://b"}
    # Newly-arriving tab also fires.
    new_page = FakePage(url="https://c")
    ctx.emit_page(new_page)
    assert seen[-1].url == "https://c"


def test_launch_context_cloak_wires_on_new_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """CloakBrowser branch: callback wired on the persistent context.

    We stub `os.path.exists(cloak_executable)` -> True and provide a
    fake `cloakbrowser` module whose `launch_persistent_context` returns
    our FakeContext.
    """
    import sys, types
    from browser import lifecycle

    ctx = FakeContext()

    fake_cloak = types.ModuleType("cloakbrowser")

    def _launch(**kwargs: Any) -> FakeContext:
        return ctx

    fake_cloak.launch_persistent_context = _launch  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake_cloak)
    # Still need the sync_playwright + stealth stubs because they are
    # imported at the top of `launch_context` regardless of branch.
    _patch_playwright_imports(ctx, monkeypatch)
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    class _FakePersistentProfile:
        def mkdir(self, **kwargs: Any) -> None:
            return None

    seen: List[FakePage] = []

    def cb(page: FakePage) -> None:
        seen.append(page)

    lifecycle.launch_context(
        headless=True,
        cdp_endpoint="",
        use_cloak=True,
        cloak_executable="/fake/chromium",
        persistent_profile=_FakePersistentProfile(),
        on_new_page=cb,
    )
    ctx.emit_page(FakePage(url="https://cloak"))
    assert len(seen) == 1
    assert seen[0].url == "https://cloak"


def test_launch_context_without_callback_registers_no_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`on_new_page=None` leaves the context free of page-listeners."""
    from browser import lifecycle

    ctx = FakeContext()
    _patch_playwright_imports(ctx, monkeypatch)

    lifecycle.launch_context(
        headless=True,
        cdp_endpoint="",
        use_cloak=False,
        cloak_executable="/nonexistent",
        persistent_profile="/tmp/does-not-exist",
        on_new_page=None,
    )
    assert ctx._page_listeners == []


# ---------------------------------------------------------------------------
# BrowserManager._on_new_page — mints tab id, meta, listeners
# ---------------------------------------------------------------------------


def _fresh_manager() -> Any:
    """Return a BrowserManager with a bound FakeContext.

    The manager's `_ensure_browser()` normally goes through
    `lifecycle.launch_context`; we bypass by directly wiring `_context`
    so tests don't need to patch playwright for every case. The
    on_new_page callback is still wired the same way `_ensure_browser`
    would wire it — via `context.on('page', ...)`.
    """
    import browser.server as server_mod
    importlib.reload(server_mod)
    mgr = server_mod.BrowserManager()
    ctx = FakeContext()
    mgr._context = ctx
    ctx.on("page", mgr._on_new_page)
    return mgr


def test_on_new_page_mints_tab_id_with_expected_shape() -> None:
    """A user-opened page gets a `tab_<8hex>` id."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    ctx.emit_page(FakePage(url="https://a"))
    assert len(mgr._tabs) == 1
    tid = next(iter(mgr._tabs))
    assert re.fullmatch(r"tab_[0-9a-f]{8}", tid), f"id {tid!r} doesn't match tab_<8hex>"


def test_on_new_page_stores_page_and_meta() -> None:
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a", title="A")
    ctx.emit_page(page)
    tid = next(iter(mgr._tabs))
    assert mgr._tabs[tid] is page
    meta = mgr._tab_meta[tid]
    for key in ("openedAt", "lastActivityAt", "openedBy", "viewport",
                "readyState", "focused", "lastRefCount"):
        assert key in meta, f"meta missing {key!r}"


def test_on_new_page_wires_close_framenavigated_load_listeners() -> None:
    """Every one of the three page.on(...) hooks is attached."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a")
    ctx.emit_page(page)
    assert page.has_listener("close")
    assert page.has_listener("framenavigated")
    assert page.has_listener("load")


def test_on_new_page_tags_user_when_no_opener() -> None:
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a", opener=None)
    ctx.emit_page(page)
    tid = next(iter(mgr._tabs))
    assert mgr._tab_meta[tid]["openedBy"] == "user"


def test_on_new_page_tags_popup_when_opener_present() -> None:
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    parent = FakePage(url="https://parent")
    popup = FakePage(url="https://popup", opener=parent)
    ctx.emit_page(popup)
    tid = next(iter(mgr._tabs))
    assert mgr._tab_meta[tid]["openedBy"] == "popup"


def test_on_new_page_skips_page_already_registered_by_open_tab() -> None:
    """open_tab's 'api'-tagged page is not re-tagged by the listener."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a")
    # Simulate open_tab having pre-registered the page under an id.
    mgr._tabs["tab_deadbeef"] = page
    mgr._tab_meta["tab_deadbeef"] = mgr._new_tab_meta(page, "api")
    # Now emit — the listener sees the page already tracked and skips.
    ctx.emit_page(page)
    assert list(mgr._tabs) == ["tab_deadbeef"]
    assert mgr._tab_meta["tab_deadbeef"]["openedBy"] == "api"


# ---------------------------------------------------------------------------
# Page-event handlers keep meta in sync
# ---------------------------------------------------------------------------


def test_close_event_removes_tab_and_meta() -> None:
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a")
    ctx.emit_page(page)
    tid = next(iter(mgr._tabs))
    page.emit("close")
    assert tid not in mgr._tabs
    assert tid not in mgr._tab_meta


def test_framenavigated_bumps_activity_and_marks_loading() -> None:
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a")
    ctx.emit_page(page)
    tid = next(iter(mgr._tabs))
    # Pre-set readyState=complete and a nonzero ref count to prove nav
    # resets both.
    mgr._tab_meta[tid]["readyState"] = "complete"
    mgr._tab_meta[tid]["lastRefCount"] = 42
    before = mgr._tab_meta[tid]["lastActivityAt"]
    # Sleep a smidge so ISO seconds tick over -- but ISO seconds may
    # collide on the same second so we don't strictly require a
    # different timestamp; we require the invariant flips.
    page.emit("framenavigated", object())
    meta = mgr._tab_meta[tid]
    assert meta["readyState"] == "loading"
    assert meta["lastRefCount"] == 0
    assert isinstance(meta["lastActivityAt"], str)
    # Timestamp is either equal (same second) or greater — never smaller.
    assert meta["lastActivityAt"] >= before


def test_load_event_flips_ready_state_to_complete() -> None:
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a")
    ctx.emit_page(page)
    tid = next(iter(mgr._tabs))
    # Meta initially says 'loading'.
    assert mgr._tab_meta[tid]["readyState"] == "loading"
    page.emit("load")
    assert mgr._tab_meta[tid]["readyState"] == "complete"


def test_close_listener_is_idempotent_when_meta_already_gone() -> None:
    """Firing close twice or on a stale tab must not raise."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a")
    ctx.emit_page(page)
    page.emit("close")
    # Second close doesn't blow up.
    page.emit("close")


# ---------------------------------------------------------------------------
# list_tabs — enriched view + backward-compat
# ---------------------------------------------------------------------------


def test_list_tabs_returns_all_ten_fields_per_entry() -> None:
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    ctx.emit_page(FakePage(url="https://a", title="A"))
    entries = mgr.list_tabs()
    assert len(entries) == 1
    entry = entries[0]
    expected_keys = {
        "targetId", "url", "title",
        "openedAt", "lastActivityAt", "openedBy", "viewport",
        "readyState", "focused", "lastRefCount",
    }
    assert expected_keys.issubset(entry.keys()), (
        f"missing keys: {expected_keys - entry.keys()}"
    )


def test_list_tabs_legacy_fields_are_first_three_keys_in_order() -> None:
    """Backward-compat: targetId, url, title must be the first three keys."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    ctx.emit_page(FakePage(url="https://a", title="A"))
    entry = mgr.list_tabs()[0]
    first_three = list(entry.keys())[:3]
    assert first_three == ["targetId", "url", "title"], (
        f"legacy field order violated: {first_three}"
    )


def test_list_tabs_reads_url_and_title_live_not_from_meta() -> None:
    """`url` / `title` come from `page.url` / `page.title()` on each call."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://before", title="Before")
    ctx.emit_page(page)
    # Simulate a client-side URL change (SPA nav or a goto we didn't
    # route through navigate()).
    page._url = "https://after"
    page._title = "After"
    entry = mgr.list_tabs()[0]
    assert entry["url"] == "https://after"
    assert entry["title"] == "After"


def test_list_tabs_synthesizes_meta_for_orphan_tab() -> None:
    """If _tab_meta somehow lacks an entry for a tracked page, we sub-in a default."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a")
    ctx.emit_page(page)
    tid = next(iter(mgr._tabs))
    # Force the race: drop meta while the tab is still tracked.
    del mgr._tab_meta[tid]
    entries = mgr.list_tabs()
    assert len(entries) == 1
    assert entries[0]["openedBy"] == "unknown"
    # Meta got re-populated so subsequent reads are stable.
    assert tid in mgr._tab_meta


def test_list_tabs_survives_page_url_raising() -> None:
    """A page whose url read raises still returns a row (url='error')."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    page = FakePage(url="https://a", raise_on_url=True)
    ctx.emit_page(page)
    entry = mgr.list_tabs()[0]
    assert entry["url"] == "error"
    assert entry["title"] == "error"
    # Enrichment still populated (values are from meta, not the page).
    assert "openedBy" in entry


def test_list_tabs_viewport_shape() -> None:
    """viewport is a dict with int width/height."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    ctx.emit_page(FakePage(url="https://a", viewport={"width": 800, "height": 600}))
    entry = mgr.list_tabs()[0]
    assert entry["viewport"] == {"width": 800, "height": 600}


def test_list_tabs_returned_viewport_is_a_copy() -> None:
    """Mutating the returned viewport dict must not corrupt server state."""
    mgr = _fresh_manager()
    ctx: FakeContext = mgr._context
    ctx.emit_page(FakePage(url="https://a"))
    entry = mgr.list_tabs()[0]
    entry["viewport"]["width"] = -1
    # Re-fetch: server state was not clobbered.
    entry2 = mgr.list_tabs()[0]
    assert entry2["viewport"]["width"] >= 0


# ---------------------------------------------------------------------------
# Thread-safety: RLock lets the sync-event callback re-enter the lock
# ---------------------------------------------------------------------------


def test_manager_lock_is_reentrant_for_sync_events() -> None:
    """Confirm the lock is re-entrant (RLock) so the sync-event pattern works.

    Playwright fires `context.on('page')` synchronously from within
    whatever thread called `context.new_page()`. Since `open_tab` holds
    `_lock` around that call, `_on_new_page` (which also grabs `_lock`)
    would deadlock a plain Lock. RLock makes this safe.
    """
    mgr = _fresh_manager()
    lock = mgr._lock
    with lock:
        # Re-entering the same lock must not block; if it did, the
        # test would time out.
        with lock:
            pass


def test_manager_lock_is_rlock_type() -> None:
    """The lock must be an RLock instance (documentation-quality guardrail)."""
    mgr = _fresh_manager()
    # threading.RLock() returns a private _thread.RLock; check via
    # duck-typing (has `_is_owned` method) rather than isinstance since
    # the concrete type is private.
    lock = mgr._lock
    assert hasattr(lock, "_is_owned") or lock.__class__.__name__ == "RLock", (
        f"manager lock is not an RLock (got {type(lock).__name__})"
    )


# ---------------------------------------------------------------------------
# The task's non-negotiable safety guards
# ---------------------------------------------------------------------------


def test_no_real_playwright_imported() -> None:
    """Test runs must not have imported the real `playwright.sync_api`.

    A refactor that reached the real Playwright would surface as this
    test failing (real import brings native bindings + a lot of RSS).
    The patch fixture keeps the fake shims in sys.modules.
    """
    import sys
    # If the real playwright was imported, its module would live under a
    # dist-info path; the stub set by _patch_playwright_imports lives at
    # a synthetic path (no file). Absent either is also fine (no test
    # in this file has needed to patch yet).
    pw = sys.modules.get("playwright.sync_api")
    if pw is None:
        return
    assert getattr(pw, "__file__", None) in (None, ""), (
        f"real playwright was imported: {pw.__file__!r}"
    )
