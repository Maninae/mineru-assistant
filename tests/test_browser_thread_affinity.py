"""Regression: Playwright objects must only be touched from the thread that created them.

The server is a ThreadingHTTPServer, so each HTTP request runs on its own
short-lived thread. Playwright's sync API is bound to the thread that
started it; before the owner-thread fix the browser launched on request
#1's thread, that thread exited, and every later `open` failed with
greenlet's "cannot switch to a different thread (which happens to have
exited)". These tests drive BrowserManager from several threads, one per
simulated request, the way the live server does.
"""

import importlib
import os
import socket
import threading
from typing import Any, Callable, Dict, List

import pytest


class WrongThreadError(RuntimeError):
    """Stands in for greenlet.error when a fake Playwright object is used off-thread."""


class ThreadBoundFake:
    """Base for fakes that remember their creating thread and reject any other."""

    def __init__(self) -> None:
        self.owner_thread = threading.current_thread()

    def assert_owner_thread(self) -> None:
        if threading.current_thread() is not self.owner_thread:
            raise WrongThreadError(
                "cannot switch to a different thread (created on %s, used on %s)"
                % (self.owner_thread.name, threading.current_thread().name)
            )


class ThreadBoundPage(ThreadBoundFake):
    def __init__(self) -> None:
        super().__init__()
        self.current_url = "about:blank"
        self.listeners: Dict[str, List[Callable[..., None]]] = {}

    @property
    def url(self) -> str:
        self.assert_owner_thread()
        return self.current_url

    def title(self) -> str:
        self.assert_owner_thread()
        return "Example"

    @property
    def viewport_size(self) -> Dict[str, int]:
        self.assert_owner_thread()
        return {"width": 1280, "height": 800}

    @property
    def opener(self) -> Any:
        return None

    def on(self, event: str, callback: Callable[..., None]) -> None:
        self.assert_owner_thread()
        self.listeners.setdefault(event, []).append(callback)

    def goto(self, url: str, timeout: int = 0, wait_until: str = "load") -> None:
        self.assert_owner_thread()
        self.current_url = url

    def wait_for_load_state(self, state: str = "load", timeout: int = 0) -> None:
        self.assert_owner_thread()


class ThreadBoundContext(ThreadBoundFake):
    def __init__(self) -> None:
        super().__init__()
        self.page_listeners: List[Callable[..., None]] = []

    @property
    def pages(self) -> List[Any]:
        # Real Playwright serves `pages` from a local cache with no thread switch,
        # which is why the liveness probe in _ensure_browser never caught the bug.
        return []

    def on(self, event: str, callback: Callable[..., None]) -> None:
        self.page_listeners.append(callback)

    def new_page(self) -> ThreadBoundPage:
        self.assert_owner_thread()
        page = ThreadBoundPage()
        for callback in self.page_listeners:
            callback(page)
        return page


@pytest.fixture
def server_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Reload browser.server against a throwaway port + PID file, never 9471."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    monkeypatch.setenv("MINERU_BROWSER_PORT", str(free_port))
    monkeypatch.setenv("MINERU_BROWSER_PID_FILE", str(tmp_path / "test.pid"))
    monkeypatch.setenv("MINERU_BROWSER_USE_CLOAK", "false")
    import browser.config as config_mod
    import browser.server as server_mod
    importlib.reload(config_mod)
    importlib.reload(server_mod)
    assert config_mod.PORT != 9471
    return server_mod


def stop_owner_thread(manager: Any) -> None:
    owner = getattr(manager, "_owner_thread", None)
    if owner is not None:
        owner.stop()


def run_on_fresh_thread(fn: Callable[[], Any]) -> Any:
    """Run fn on a new thread that exits afterwards, like one HTTP request."""
    outcome: Dict[str, Any] = {}

    def request_thread_body() -> None:
        try:
            outcome["result"] = fn()
        except BaseException as exc:
            outcome["error"] = exc

    request_thread = threading.Thread(target=request_thread_body)
    request_thread.start()
    request_thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def test_sequential_opens_on_different_request_threads_reuse_one_browser(
    server_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched_contexts: List[ThreadBoundContext] = []

    def fake_launch_context(**kwargs: Any) -> Any:
        context = ThreadBoundContext()
        context.on("page", kwargs["on_new_page"])
        launched_contexts.append(context)
        return object(), None, context, None

    monkeypatch.setattr(server_module.lifecycle, "launch_context", fake_launch_context)
    manager = server_module.BrowserManager()
    try:
        first = run_on_fresh_thread(lambda: manager.open_tab("https://example.com"))
        second = run_on_fresh_thread(lambda: manager.open_tab("https://example.org"))
        listed_tabs = manager.list_tabs()  # a third thread (pytest's main thread)
    finally:
        stop_owner_thread(manager)

    assert len(launched_contexts) == 1, "browser was relaunched instead of reused"
    assert first["url"] == "https://example.com"
    assert second["url"] == "https://example.org"
    assert {tab["targetId"] for tab in listed_tabs} == {first["targetId"], second["targetId"]}


def test_owner_thread_runs_nested_calls_inline_without_deadlock(server_module: Any) -> None:
    """A manager method invoked from the owner thread (event callback path) must not queue on itself."""
    from browser.playwright_owner_thread import PlaywrightOwnerThread

    owner = PlaywrightOwnerThread()
    try:
        observed = owner.call(lambda: owner.call(lambda: threading.current_thread().name))
    finally:
        owner.stop()
    assert observed == owner.name


def test_owner_thread_propagates_exceptions_to_caller(server_module: Any) -> None:
    from browser.playwright_owner_thread import PlaywrightOwnerThread

    def raise_value_error() -> None:
        raise ValueError("Unknown targetId: tab_x")

    owner = PlaywrightOwnerThread()
    try:
        with pytest.raises(ValueError, match="Unknown targetId"):
            owner.call(raise_value_error)
        assert owner.call(lambda: 42) == 42, "owner thread died after an exception"
    finally:
        owner.stop()


@pytest.mark.skipif(
    os.environ.get("MINERU_BROWSER_REAL_LAUNCH_TEST") != "1",
    reason="real headless Chromium launch; set MINERU_BROWSER_REAL_LAUNCH_TEST=1",
)
def test_real_headless_chromium_survives_requests_on_different_threads(server_module: Any) -> None:
    pytest.importorskip("playwright.sync_api")
    pytest.importorskip("playwright_stealth")
    manager = server_module.BrowserManager()
    try:
        first = run_on_fresh_thread(lambda: manager.open_tab("data:text/html,<title>one</title><button>Hi</button>"))
        second = run_on_fresh_thread(lambda: manager.open_tab("data:text/html,<title>two</title>"))
        snapshot = run_on_fresh_thread(lambda: manager.snapshot(first["targetId"]))
        assert first["title"] == "one"
        assert second["title"] == "two"
        assert "Hi" in str(snapshot)
    finally:
        manager.shutdown()
        stop_owner_thread(manager)
