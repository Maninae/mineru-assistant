#!/usr/bin/env python3
"""
Mineru Browser Automation Server

A persistent HTTP server (port 9471) that keeps a browser alive across calls,
providing ref-based accessibility tree snapshots and element interaction for
LLM agents. Includes stealth patches and humanized typing for anti-bot resilience.

Modes:
  Standalone  — Launches Playwright Chromium (default, stealth-patched)
  Headed      — MINERU_BROWSER_HEADLESS=false to show the window
  CDP attach  — MINERU_BROWSER_CDP=http://localhost:9222 to use real Chrome

Actions:
  open       — Open a new tab, navigate to URL
  snapshot   — Get accessibility tree with interactive element refs
  act        — click, type (with humanize), select, check, hover, scroll
  upload     — Upload files via file input or file chooser interception
  screenshot — Capture page screenshot (file or base64)
  navigate   — Navigate existing tab to new URL
  wait       — Wait for page to settle (networkidle/load/domcontentloaded)
  close      — Close a tab
  status     — Health check
  stop       — Shut down server

Python 3.9+ compatible (no union type syntax).
"""

import base64
import datetime
import json
import os
import queue
import signal
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Named device presets for the viewport param. Kept SMALL on purpose — the
# escape hatch is passing an explicit {"width":..., "height":...} viewport.
# Values are logical CSS pixels (no deviceScaleFactor), which matches what
# Playwright's viewport dict expects.
DEVICE_PRESETS: Dict[str, Dict[str, int]] = {
    "iphone": {"width": 393, "height": 852},        # iPhone 14 Pro
    "iphone-se": {"width": 375, "height": 667},
    "pixel": {"width": 412, "height": 915},
    "narrow": {"width": 375, "height": 812},
    "mobile": {"width": 390, "height": 844},        # generic modern phone
    "tablet": {"width": 820, "height": 1180},       # iPad Air
    "desktop": {"width": 1440, "height": 900},
}


# Viewport bounds enforced by resolve_viewport(). The floor is 1 (Playwright
# accepts any positive int, but 0 or negative silently falls back to the
# context default — the exact silent-no-op the audit flagged). The ceiling
# is 8192 to catch obvious typos (e.g. `width=19200` from a missed decimal)
# without blocking a legitimate hi-DPI test at 4K.
VIEWPORT_MIN_DIM = 1
VIEWPORT_MAX_DIM = 8192


def _validate_dims(width: int, height: int) -> Tuple[int, int]:
    """Reject non-positive or absurdly-large dims with a ValueError → 400.

    Playwright's page.set_viewport_size swallows a `{width: 0}` and keeps
    the default viewport; a caller then sees the response `viewport` reflect
    the default and can't tell their override was ignored. Fail loud instead.
    """
    if not (VIEWPORT_MIN_DIM <= width <= VIEWPORT_MAX_DIM):
        raise ValueError(
            "viewport width %s out of range [%d, %d]"
            % (width, VIEWPORT_MIN_DIM, VIEWPORT_MAX_DIM)
        )
    if not (VIEWPORT_MIN_DIM <= height <= VIEWPORT_MAX_DIM):
        raise ValueError(
            "viewport height %s out of range [%d, %d]"
            % (height, VIEWPORT_MIN_DIM, VIEWPORT_MAX_DIM)
        )
    return width, height


def resolve_viewport(
    viewport: Optional[Dict[str, Any]] = None,
    device: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> Optional[Dict[str, int]]:
    """Fold the three ways to ask for a viewport into a single {w, h} dict.

    Priority order (first non-empty wins):
      1. Explicit `viewport={"width":..., "height":...}` dict.
      2. Named `device` string against DEVICE_PRESETS.
      3. Flat `width` / `height` ints (both required for a valid override).

    Returns None when no valid override was supplied, so callers can keep
    Playwright's default viewport from the launched context. Raises ValueError
    on a garbage input (unknown device, width without height, dim <=0 or >
    VIEWPORT_MAX_DIM) so a typo lands as a 400 instead of a silent no-op —
    Playwright itself swallows a bad size and keeps the default, which is
    the exact silent-fallback the audit flagged.
    """
    if viewport:
        try:
            w = int(viewport["width"])
            h = int(viewport["height"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError("viewport dict must have int width and height") from e
        w, h = _validate_dims(w, h)
        return {"width": w, "height": h}
    if device:
        preset = DEVICE_PRESETS.get(str(device).lower())
        if not preset:
            raise ValueError(
                "unknown device '%s' (known: %s)"
                % (device, ", ".join(sorted(DEVICE_PRESETS)))
            )
        return dict(preset)
    if width or height:
        if not (width and height):
            raise ValueError("width and height must be given together")
        w, h = _validate_dims(int(width), int(height))
        return {"width": w, "height": h}
    return None

# When run as a script (python3 browser/server.py), the script's *parent*
# directory (browser/) is on sys.path, not its grandparent. Add the
# grandparent so `from browser.config import ...` works in script mode too.
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from browser import actions, auth_token, lifecycle
from browser.accessibility import RefRegistry, build_snapshot
from browser.config import (  # noqa: F401  (re-exported for compatibility)
    CDP_ENDPOINT,
    CLOAK_EXECUTABLE,
    HEADLESS,
    LOG_DIR,
    PERSISTENT_PROFILE,
    PID_FILE,
    PORT,
    SSE_HEARTBEAT_SECONDS,
    TABS_SNAPSHOT_DEBOUNCE_MS,
    TABS_SNAPSHOT_PATH,
    USE_CLOAK,
    logger,
)
from browser.tab_event_bus import TabEventBus
from browser.tab_snapshot_writer import SnapshotWriter

# ---------------------------------------------------------------------------
# Browser Manager (thread-safe)
# ---------------------------------------------------------------------------


class BrowserManager:
    """Manages a single Chromium browser instance and multiple named tabs.

    Threading: `_lock` is a re-entrant RLock. Playwright fires page/
    context events synchronously from within the thread that called the
    triggering method (e.g. `context.new_page()` synchronously invokes
    every `context.on("page", …)` listener). Since almost every public
    method holds `_lock`, an event callback that also needs the lock
    would deadlock a plain `threading.Lock`. RLock lets the same thread
    re-enter without releasing, which matches Playwright's sync-event
    model. Cross-thread lock semantics are unchanged.

    Tab metadata: `_tab_meta` maps `targetId` -> the enrichment dict that
    `list_tabs()` fuses on top of the legacy (targetId, url, title) view
    per spec §4.3 layer 2 (openedAt, lastActivityAt, openedBy, viewport,
    readyState, focused, lastRefCount). Both maps are keyed by the same
    `targetId`; a missing meta entry for a live tab is a bug — we log
    and fall back to a synthesized default so /tabs never blanks out.
    """

    def __init__(
        self,
        event_bus: Optional[TabEventBus] = None,
        snapshot_writer: Optional[SnapshotWriter] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._playwright = None  # type: Any
        self._browser = None  # type: Any
        self._context = None  # type: Any
        self._stealth_cm = None  # type: Any
        self._tabs = {}  # type: Dict[str, Any]  # targetId -> page
        self._tab_meta = {}  # type: Dict[str, Dict[str, Any]]  # targetId -> meta
        self._cdp_sessions = {}  # type: Dict[str, Any]  # targetId -> cdp session
        self._refs = RefRegistry()
        # P3-05 wiring — both are optional so a bare BrowserManager()
        # from an existing test still works without an event bus /
        # snapshot writer. Production callsite wires the module-level
        # `_tab_event_bus` + `_snapshot_writer` in.
        self._event_bus = event_bus
        self._snapshot_writer = snapshot_writer
        # `open_tab` sets this to True around the `context.new_page()`
        # call so the sync-fired `_on_new_page` skips publishing the
        # (soon-to-be-re-tagged) 'user' event; `open_tab` publishes its
        # own 'opened' event after the 'api' re-tag settles.
        self._suppress_open_publish = False

    def _ensure_browser(self) -> None:
        """Start browser if not running. Must hold self._lock."""
        if self._context is not None:
            try:
                # Check if context is still alive
                self._context.pages
                return
            except Exception:
                logger.warning("Browser crashed, restarting...")
                self._cleanup_browser()

        self._playwright, self._browser, self._context, self._stealth_cm = lifecycle.launch_context(
            headless=HEADLESS,
            cdp_endpoint=CDP_ENDPOINT,
            use_cloak=USE_CLOAK,
            cloak_executable=CLOAK_EXECUTABLE,
            persistent_profile=PERSISTENT_PROFILE,
            on_new_page=self._on_new_page,
        )

    def _cleanup_browser(self) -> None:
        """Tear down browser. Must hold self._lock."""
        lifecycle.cleanup(
            playwright=self._playwright,
            browser=self._browser,
            context=self._context,
            stealth_cm=self._stealth_cm,
            cdp_sessions=self._cdp_sessions,
            tabs=self._tabs,
            ref_registry=self._refs,
        )
        # cleanup() detached CDPs and cleared tabs/refs/cdp_sessions in place;
        # null out our own handles too and drop tab metadata so a subsequent
        # /tabs read on a torn-down manager returns [] instead of stale rows.
        self._tab_meta.clear()
        self._context = None
        self._browser = None
        self._stealth_cm = None
        self._playwright = None

    # ------------------------------------------------------------------
    # Tab-metadata helpers (spec §4.3 layer 2)
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        """UTC-offset local ISO 8601, second precision (matches spec §4.3 example)."""
        return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _detect_opener_kind(page: Any) -> str:
        """Return 'popup' if the page has a Playwright opener, else 'user'.

        `page.opener()` returns the parent page for popup windows opened via
        window.open / target=_blank targeting a new window, or None for a
        plain user-opened tab / a page we opened ourselves. Any exception
        (older Playwright, CDP-attach quirks) is swallowed and defaults to
        'user' — misclassifying a popup as 'user' is much less bad than
        crashing the event callback.
        """
        try:
            opener = page.opener()
        except Exception:
            return "user"
        return "popup" if opener is not None else "user"

    @staticmethod
    def _safe_viewport(page: Any) -> Dict[str, int]:
        """Return the page's viewport size or the CloakBrowser default.

        Falls back to {1440, 900} — the CloakBrowser default in
        lifecycle.py — so a page that hasn't finalised its viewport yet
        (fresh popup, mid-load) still reports something sensible.
        """
        try:
            vp = page.viewport_size()
        except Exception:
            vp = None
        if not vp:
            return {"width": 1440, "height": 900}
        return {"width": int(vp.get("width", 1440)), "height": int(vp.get("height", 900))}

    def _new_tab_meta(self, page: Any, opened_by: str) -> Dict[str, Any]:
        """Build the initial meta dict for a freshly-registered tab.

        `readyState` starts at 'loading' — we can't safely call into the
        page from the event callback (a popup may still be initializing),
        and the load/framenavigated listeners will bump it as soon as the
        page settles. `lastRefCount` starts at 0 and is updated by
        `snapshot()` after a successful CDP AX-tree fetch.
        """
        now = self._now_iso()
        return {
            "openedAt": now,
            "lastActivityAt": now,
            "openedBy": opened_by,
            "viewport": self._safe_viewport(page),
            "readyState": "loading",
            "focused": False,
            "lastRefCount": 0,
        }

    def _wire_page_listeners(self, target_id: str, page: Any) -> None:
        """Attach close / framenavigated / load listeners on a page.

        Each listener defers to a named `_on_page_*` method on the
        manager. Playwright fires these events synchronously on the
        calling thread (which may already hold `_lock` — hence the
        RLock in `__init__`). Publishing to the TabEventBus / firing
        the SnapshotWriter is folded into each of those methods so a
        subscriber (SSE, CLI --watch) sees state changes without
        polling.
        """
        page.on("close", lambda _arg=None: self._on_page_close(target_id))
        page.on("framenavigated", lambda _arg=None: self._on_page_navigated(target_id))
        page.on("load", lambda _arg=None: self._on_page_load(target_id))

    # ------------------------------------------------------------------
    # Named page-event handlers (P3-05). Split out of `_wire_page_listeners`
    # so they can be unit-tested directly and can publish + trigger the
    # snapshot writer AFTER the internal state mutation settles.
    # ------------------------------------------------------------------

    def _snapshot_tab(self, target_id: str) -> Dict[str, Any]:
        """Build the enriched dict for a single tab.

        Called by `_publish_and_trigger` to seed the SSE event payload.
        Returns a minimal `{"targetId": …, "gone": True}` stub for a
        tab that's already been removed (the 'closed' path captures
        the enriched dict BEFORE cleanup, so this branch mostly guards
        stale replays).
        """
        page = self._tabs.get(target_id)
        meta = self._tab_meta.get(target_id)
        if page is None or meta is None:
            return {"targetId": target_id, "gone": True}
        try:
            url = page.url
            title = page.title()
        except Exception:
            url = "error"
            title = "error"
        return {
            "targetId": target_id,
            "url": url,
            "title": title,
            "openedAt": meta["openedAt"],
            "lastActivityAt": meta["lastActivityAt"],
            "openedBy": meta["openedBy"],
            "viewport": dict(meta["viewport"]),
            "readyState": meta["readyState"],
            "focused": meta["focused"],
            "lastRefCount": meta["lastRefCount"],
        }

    def _publish_and_trigger(
        self,
        event_type: str,
        target_id: str,
        tab_snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Fan out an SSE event and prime the on-disk snapshot writer.

        `tab_snapshot` is expected to be pre-captured INSIDE the manager
        RLock by every event-emitting callsite (opened / closed /
        navigated / focused). That keeps the fan-out itself unlocked
        (so a slow bus consumer can't wedge BrowserManager) without
        having `_snapshot_tab` re-read `self._tabs` / `self._tab_meta`
        (and re-issue CDP round-trips via `page.url` + `page.title()`)
        while another thread mutates them. When the caller cannot
        pre-capture (e.g. a bare-manager test path where the state
        cannot race), the fallback re-derives from live state — a
        deliberately narrow degradation. Both branches are no-ops when
        the manager was constructed without a bus / writer.
        """
        if tab_snapshot is None:
            tab_snapshot = self._snapshot_tab(target_id)
        event = {
            "type": event_type,
            "tab": tab_snapshot,
            "ts": self._now_iso(),
        }
        if self._event_bus is not None:
            try:
                self._event_bus.publish(event)
            except Exception as exc:  # defensive; publish() is non-blocking
                logger.warning("BrowserManager: event publish failed: %s", exc)
        if self._snapshot_writer is not None:
            try:
                self._snapshot_writer.trigger()
            except Exception as exc:
                logger.warning("BrowserManager: snapshot trigger failed: %s", exc)

    def _on_page_close(self, target_id: str) -> None:
        """Handle Playwright's `page.on('close', …)`.

        Captures the enriched view BEFORE removing the tab from
        `_tab_meta` so the 'closed' event carries the same shape a
        listener saw for the 'opened' event; then tears down state.
        """
        with self._lock:
            snapshot = self._snapshot_tab(target_id)
            self._tabs.pop(target_id, None)
            self._tab_meta.pop(target_id, None)
            cdp = self._cdp_sessions.pop(target_id, None)
            if cdp is not None:
                try:
                    cdp.detach()
                except Exception:
                    pass
            self._refs.pop(target_id)
        # Publish OUTSIDE the lock so a slow bus consumer can't wedge
        # BrowserManager. The bus is non-blocking today (put_nowait);
        # keeping this outside the lock is defensive against a future
        # publisher that isn't.
        self._publish_and_trigger("closed", target_id, tab_snapshot=snapshot)

    def _on_page_navigated(self, target_id: str) -> None:
        """Handle Playwright's `page.on('framenavigated', …)`."""
        with self._lock:
            meta = self._tab_meta.get(target_id)
            if meta is None:
                return
            meta["lastActivityAt"] = self._now_iso()
            # framenavigated means the page just started a nav; browser
            # considers it 'loading' from this moment until the 'load'
            # event fires.
            meta["readyState"] = "loading"
            # Refs are invalidated on nav (same rule the navigate()
            # path enforces) — bump lastRefCount back to 0 so the /tabs
            # view doesn't advertise a stale count.
            meta["lastRefCount"] = 0
            # Pre-capture the enriched snapshot INSIDE the lock so the
            # subsequent unlocked _publish_and_trigger doesn't re-derive
            # it from live state that another thread may be mutating.
            snapshot = self._snapshot_tab(target_id)
        self._publish_and_trigger("navigated", target_id, tab_snapshot=snapshot)

    def _on_page_load(self, target_id: str) -> None:
        """Handle Playwright's `page.on('load', …)`.

        The 'load' signal is folded into the 'navigated' SSE event
        type per spec §4.3's four-verb enum {opened, closed,
        navigated, focused}. Subscribers distinguish start-of-nav
        from end-of-nav by reading `tab.readyState` on the payload.
        """
        with self._lock:
            meta = self._tab_meta.get(target_id)
            if meta is None:
                return
            meta["lastActivityAt"] = self._now_iso()
            meta["readyState"] = "complete"
            # Pre-capture INSIDE the lock — see _on_page_navigated.
            snapshot = self._snapshot_tab(target_id)
        self._publish_and_trigger("navigated", target_id, tab_snapshot=snapshot)

    def _on_page_focused(self, target_id: str, focused: bool = True) -> None:
        """Mark a tab focused/unfocused and emit a 'focused' event.

        Playwright's Page object does not surface a native "this tab
        is now the active tab" event; this method is invoked either
        from a future CDP `Target.targetInfoChanged` hook, from a
        higher-level UI, or from tests. When `focused` is True the
        manager un-focuses every other tracked tab so the flag stays
        single-valued across the context.
        """
        with self._lock:
            meta = self._tab_meta.get(target_id)
            if meta is None:
                return
            meta["focused"] = bool(focused)
            meta["lastActivityAt"] = self._now_iso()
            if focused:
                for other_id, other_meta in self._tab_meta.items():
                    if other_id != target_id:
                        other_meta["focused"] = False
            # Pre-capture INSIDE the lock — see _on_page_navigated.
            snapshot = self._snapshot_tab(target_id)
        self._publish_and_trigger("focused", target_id, tab_snapshot=snapshot)

    def _on_new_page(self, page: Any) -> None:
        """Callback for `context.on('page', ...)`.

        Fires for every new page the context sees — including pages our
        own `open_tab` created via `context.new_page()`. `open_tab` pre-
        registers its page BEFORE calling `goto`, so we detect the
        already-tracked object here and skip re-registering it (keeping
        its 'api' tag intact).

        For genuinely new pages (user opened via window.open / target=
        _blank, or a popup) we mint a `tab_<8hex>` id, tag `openedBy`
        based on `page.opener()`, and wire the same close/nav/load
        listeners `open_tab` wires so /tabs stays live.

        Publishes an 'opened' event UNLESS `_suppress_open_publish` is
        set (which `open_tab` toggles around its `new_page()` call so
        the api-re-tagged event fires instead of the raw 'user' one).
        """
        should_publish: bool
        minted_id: Optional[str] = None
        snapshot: Optional[Dict[str, Any]] = None
        with self._lock:
            # Skip if `open_tab` already registered this page — identity
            # comparison, not equality, so a re-navigated page keeps its
            # original tag.
            for existing_page in self._tabs.values():
                if existing_page is page:
                    return

            target_id = "tab_%s" % uuid.uuid4().hex[:8]
            opened_by = self._detect_opener_kind(page)
            self._tabs[target_id] = page
            self._tab_meta[target_id] = self._new_tab_meta(page, opened_by)
            self._wire_page_listeners(target_id, page)
            logger.info(
                "Registered %s new tab %s (url=%s)", opened_by, target_id, self._safe_page_url(page)
            )
            should_publish = not self._suppress_open_publish
            if should_publish:
                minted_id = target_id
                # Pre-capture the enriched snapshot INSIDE the lock so
                # the subsequent unlocked _publish_and_trigger doesn't
                # re-derive it from live state another thread may be
                # mutating.
                snapshot = self._snapshot_tab(target_id)
        if should_publish and minted_id is not None:
            # Publish outside the lock — see `_on_page_close` for the
            # same rationale — but with a pre-captured snapshot so the
            # unlocked path never has to re-read shared tab state.
            self._publish_and_trigger("opened", minted_id, tab_snapshot=snapshot)

    @staticmethod
    def _safe_page_url(page: Any) -> str:
        try:
            return page.url
        except Exception:
            return "error"

    def _get_cdp(self, target_id: str) -> Any:
        """Get or create CDP session for a tab. Must hold self._lock."""
        page = self._tabs.get(target_id)
        if page is None:
            raise ValueError("Unknown targetId: %s" % target_id)

        cdp = self._cdp_sessions.get(target_id)
        if cdp is None:
            cdp = page.context.new_cdp_session(page)
            self._cdp_sessions[target_id] = cdp
        return cdp

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------

    def open_tab(
        self,
        url: str,
        timeout_ms: int = 30000,
        viewport: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Open a new tab.

        `viewport={"width":..., "height":...}` (optional) resizes this tab's
        viewport BEFORE navigation, so the first render sees the target
        dimensions. Omit to inherit the launched context's default viewport
        (unchanged from prior behavior — existing callers are not affected).
        Use this + screenshot with `fullPage=true` to produce a mobile-width
        full-page screenshot in a single open/screenshot pair.
        """
        with self._lock:
            self._ensure_browser()

            # Mint the id FIRST and register the page BEFORE calling
            # context.new_page() to be robust against Playwright's
            # synchronous 'page' event: `context.new_page()` fires our
            # `_on_new_page` callback before returning; the callback
            # skips pages already known to `_tabs`, keeping the 'api'
            # tag we set here from being overwritten as 'user'/'popup'.
            #
            # We have to insert the page reference under the id BEFORE
            # the `new_page()` call... except we don't have the page
            # object yet. So we register it in a "guard" set instead:
            # we call new_page(), then IMMEDIATELY under the same lock
            # check whether _on_new_page already claimed the page
            # (it will have, because sync events fire before new_page()
            # returns). Whichever id it minted, we take ownership of
            # that entry (re-tag 'api', ensure listeners wired) and
            # return.
            #
            # P3-05: suppress the callback's 'opened' publish so the
            # single 'opened' event a subscriber sees carries the final
            # 'api' tag, not the transient 'user'/'popup' one the
            # callback would have inferred.
            self._suppress_open_publish = True
            try:
                page = self._context.new_page()
            finally:
                self._suppress_open_publish = False

            # Look up the id the callback minted for this page (if any).
            target_id = None
            for tid, tracked in self._tabs.items():
                if tracked is page:
                    target_id = tid
                    break

            if target_id is None:
                # No callback registered (very old Playwright, or
                # on_new_page wiring failed silently). Fall back to
                # minting + registering ourselves.
                target_id = "tab_%s" % uuid.uuid4().hex[:8]
                self._tabs[target_id] = page
                self._tab_meta[target_id] = self._new_tab_meta(page, "api")
                self._wire_page_listeners(target_id, page)
            else:
                # The callback already minted an id. Re-tag it as 'api'
                # (the callback couldn't know) and leave listeners in
                # place — the callback wired them.
                meta = self._tab_meta.get(target_id)
                if meta is None:
                    self._tab_meta[target_id] = self._new_tab_meta(page, "api")
                else:
                    meta["openedBy"] = "api"

            if viewport:
                # set_viewport_size updates both the JS-reported inner size
                # AND the browser's rendering viewport, so screenshots come
                # back at the requested width. Only errors on invalid dims.
                try:
                    page.set_viewport_size(viewport)
                except Exception as viewport_error:
                    logger.warning(
                        "viewport override failed (falling back to context default): %s",
                        viewport_error,
                    )

            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                # Extra wait for JS rendering
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
            except Exception as e:
                # Page may have loaded partially — that's OK
                logger.warning("Navigation warning for %s: %s", url, e)

            # Refresh meta post-nav so /tabs sees the final viewport +
            # readyState (the load listener may not have fired if the
            # page timed out; the wait_for_load_state above still gives
            # us a settled DOM).
            meta = self._tab_meta.get(target_id)
            if meta is not None:
                meta["lastActivityAt"] = self._now_iso()
                if meta["readyState"] == "loading":
                    meta["readyState"] = "complete"
                meta["viewport"] = self._safe_viewport(page)

            result = {
                "targetId": target_id,
                "url": page.url,
                "title": page.title(),
                "viewport": page.viewport_size,
            }
        # Publish 'opened' with the final 'api' tag AFTER the lock
        # releases so a slow subscriber can't wedge open_tab.
        self._publish_and_trigger("opened", target_id)
        return result

    def snapshot(self, target_id: str) -> Dict[str, Any]:
        with self._lock:
            page = self._tabs.get(target_id)
            if page is None:
                raise ValueError("Unknown targetId: %s" % target_id)

            cdp = self._get_cdp(target_id)
            result = build_snapshot(page, cdp, self._refs, target_id)
            # Refresh meta from this snapshot so /tabs advertises the
            # ref count a caller would see if they invoked act() next.
            meta = self._tab_meta.get(target_id)
            if meta is not None:
                meta["lastRefCount"] = len(self._refs.get_map(target_id))
                meta["lastActivityAt"] = self._now_iso()
            return result

    def act(self, target_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            page = self._tabs.get(target_id)
            if page is None:
                raise ValueError("Unknown targetId: %s" % target_id)

            def get_cdp() -> Any:
                return self._get_cdp(target_id)

            return actions.dispatch(page, get_cdp, self._refs, target_id, request)

    def evaluate(self, target_id: str, expression: str, arg: Any = None) -> Dict[str, Any]:
        """Execute JavaScript in the page context and return the result.

        Audit trail (Sep 4 2026 security audit fix): every `evaluate`
        call is logged before dispatch with a length-capped expression
        summary. The full expression can be arbitrarily large (a page
        function body); truncating at 200 chars gives the operator
        enough signal to spot a `document.cookie` / `localStorage`
        exfil attempt without spamming the log.
        """
        summary = expression if len(expression) <= 200 else expression[:200] + "…"
        logger.info(
            "browser audit: evaluate targetId=%s expr=%r arg_present=%s",
            target_id, summary, arg is not None,
        )
        with self._lock:
            page = self._tabs.get(target_id)
            if page is None:
                raise ValueError("Unknown targetId: %s" % target_id)
            if arg is not None:
                result = page.evaluate(expression, arg)
            else:
                result = page.evaluate(expression)
            return {"action": "evaluate", "result": result}

    def responsebody(self, target_id: str, url_pattern: str, timeout_ms: int = 10000) -> Dict[str, Any]:
        """Navigate or wait, then capture the response body matching a URL pattern."""
        with self._lock:
            page = self._tabs.get(target_id)
            if page is None:
                raise ValueError("Unknown targetId: %s" % target_id)
            import fnmatch
            captured = {}  # type: Dict[str, Any]

            def on_response(response: Any) -> None:
                if fnmatch.fnmatch(response.url, url_pattern):
                    try:
                        captured["url"] = response.url
                        captured["status"] = response.status
                        captured["body"] = response.text()
                    except Exception:
                        captured["body"] = None

            page.on("response", on_response)
            try:
                page.wait_for_timeout(timeout_ms)
            except Exception:
                pass
            page.remove_listener("response", on_response)

            if not captured:
                return {"action": "responsebody", "matched": False, "pattern": url_pattern}
            return {
                "action": "responsebody",
                "matched": True,
                "url": captured.get("url"),
                "status": captured.get("status"),
                "body": captured.get("body"),
            }

    def navigate(self, target_id: str, url: str, timeout_ms: int = 30000) -> Dict[str, Any]:
        """Navigate an existing tab to a new URL."""
        with self._lock:
            page = self._tabs.get(target_id)
            if page is None:
                raise ValueError("Unknown targetId: %s" % target_id)
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
            except Exception as e:
                logger.warning("Navigate warning for %s: %s", url, e)
            # Invalidate refs since DOM changed
            self._refs.set_map(target_id, {})
            meta = self._tab_meta.get(target_id)
            if meta is not None:
                meta["lastActivityAt"] = self._now_iso()
                meta["readyState"] = "complete"
                meta["lastRefCount"] = 0
            return {
                "targetId": target_id,
                "url": page.url,
                "title": page.title(),
            }

    def wait(self, target_id: str, timeout_ms: int = 2000, until: str = "networkidle") -> Dict[str, Any]:
        """Wait for page activity to settle. until: 'networkidle' | 'domcontentloaded' | 'load'."""
        with self._lock:
            page = self._tabs.get(target_id)
            if page is None:
                raise ValueError("Unknown targetId: %s" % target_id)
            valid = {"networkidle", "domcontentloaded", "load"}
            state = until if until in valid else "networkidle"
            try:
                page.wait_for_load_state(state, timeout=timeout_ms)
            except Exception:
                pass
            return {"targetId": target_id, "url": page.url, "until": state}

    def upload(self, target_id: str, ref: str, paths: List[str], via_click: bool = False) -> Dict[str, Any]:
        """Upload files.

        via_click=False (default): ref must be an <input type="file"> element.
          Uses set_input_files() — no OS dialog, no popup.

        via_click=True: ref is any element whose click triggers a file-chooser
          dialog (e.g. a styled button). Playwright intercepts the dialog before
          it appears and injects the files directly.
        """
        with self._lock:
            page = self._tabs.get(target_id)
            if page is None:
                raise ValueError("Unknown targetId: %s" % target_id)

            ref_info = self._refs.get_map(target_id).get(ref)
            if ref_info is None:
                raise ValueError(
                    "Unknown ref '%s'. Take a new snapshot to get current refs." % ref
                )

            backend_id = ref_info["backendDOMNodeId"]
            cdp = self._get_cdp(target_id)

            if via_click:
                # Intercept the file chooser that clicking the element would open
                el = actions.resolve_element(page, cdp, backend_id)
                if el is None:
                    raise ValueError("Cannot resolve trigger element for ref '%s'" % ref)
                with page.expect_file_chooser(timeout=5000) as fc_info:
                    el.click(timeout=5000)
                fc_info.value.set_files(paths)
            else:
                el = actions.resolve_element(page, cdp, backend_id)
                if el is None:
                    raise ValueError("Cannot resolve file input element for ref '%s'" % ref)
                el.set_input_files(paths, timeout=10000)

            return {
                "action": "upload",
                "ref": ref,
                "files": paths,
                "via_click": via_click,
            }

    def screenshot(
        self,
        target_id: str,
        path: Optional[str] = None,
        full_page: bool = False,
        viewport: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Capture a screenshot of the tab.

        `viewport={"width":..., "height":...}` (optional) resizes the tab
        BEFORE the shot. Useful for a one-call "mobile-width full-page
        screenshot" — pass the mobile viewport plus fullPage=true and the
        server does the resize+shot in a single lock-held critical section.
        Omitted, the tab keeps whatever viewport it already has (backward-
        compatible default).

        The viewport override PERSISTS on the tab after the shot — we do not
        restore the previous size. This mirrors what a caller usually wants
        (a phone-view tab stays a phone-view tab). If a caller needs desktop
        back, take another screenshot with the desktop viewport, or reopen.
        """
        with self._lock:
            page = self._tabs.get(target_id)
            if page is None:
                raise ValueError("Unknown targetId: %s" % target_id)

            if viewport:
                try:
                    page.set_viewport_size(viewport)
                except Exception as viewport_error:
                    logger.warning(
                        "screenshot viewport override failed: %s", viewport_error
                    )

            screenshot_bytes = page.screenshot(full_page=full_page)

            result = {
                "targetId": target_id,
                "url": page.url,
                "title": page.title(),
                "size": len(screenshot_bytes),
                "viewport": page.viewport_size,
                "fullPage": full_page,
            }  # type: Dict[str, Any]

            if path:
                out_path = Path(path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(screenshot_bytes)
                result["path"] = str(out_path)
            else:
                result["base64"] = base64.b64encode(screenshot_bytes).decode("ascii")

            return result

    def close_tab(self, target_id: str) -> Dict[str, Any]:
        with self._lock:
            page = self._tabs.get(target_id)
            if page is None:
                return {"targetId": target_id, "status": "already_closed"}

            # Capture BEFORE cleanup so the SSE 'closed' event carries
            # the enriched dict.
            snapshot = self._snapshot_tab(target_id)

            self._tabs.pop(target_id, None)
            self._tab_meta.pop(target_id, None)
            cdp = self._cdp_sessions.pop(target_id, None)
            if cdp is not None:
                try:
                    cdp.detach()
                except Exception:
                    pass

            self._refs.pop(target_id)

            try:
                page.close()
            except Exception:
                pass

        # Publish outside the lock. Note: `page.close()` will ALSO
        # trigger `_on_page_close` via the page listener, which would
        # publish a second 'closed'. To avoid the duplicate, we call
        # page.close() with the listener still wired — but the second
        # call is a no-op because `_tabs` no longer has target_id.
        # `_snapshot_tab` returns `{gone: True}` for the second event;
        # subscribers can dedupe on target_id + type if needed, but
        # neither path corrupts state.
        self._publish_and_trigger("closed", target_id, tab_snapshot=snapshot)
        return {"targetId": target_id, "status": "closed"}

    def list_tabs(self) -> List[Dict[str, Any]]:
        """Return the enriched per-tab view (spec §4.3 layer 2).

        Legacy consumers see `targetId`, `url`, `title` as the first
        three keys of every entry, in that exact order. New consumers
        additionally get `openedAt`, `lastActivityAt`, `openedBy`,
        `viewport`, `readyState`, `focused`, `lastRefCount` (from the
        meta side-map).

        If meta is somehow missing for a tracked tab (bug, race across a
        crash + reboot), we synthesize a defaults dict so /tabs never
        blanks a live tab — that would be a worse UX than a stale row.
        """
        with self._lock:
            result = []
            for tid, page in list(self._tabs.items()):
                try:
                    url = page.url
                    title = page.title()
                except Exception:
                    url = "error"
                    title = "error"
                # Legacy fields FIRST so dict-ordered consumers (JSON
                # serializers, human readers scanning the columns) see
                # the historical shape at the top of each entry.
                entry = {
                    "targetId": tid,
                    "url": url,
                    "title": title,
                }
                meta = self._tab_meta.get(tid)
                if meta is None:
                    # Live tab with no meta: synthesize a default row
                    # instead of dropping the tab. Log once per event.
                    logger.warning(
                        "list_tabs: no meta for tracked tab %s; synthesizing default meta", tid
                    )
                    meta = self._new_tab_meta(page, "unknown")
                    self._tab_meta[tid] = meta
                # Insert every enrichment field, in the spec's example order.
                entry["openedAt"] = meta["openedAt"]
                entry["lastActivityAt"] = meta["lastActivityAt"]
                entry["openedBy"] = meta["openedBy"]
                entry["viewport"] = dict(meta["viewport"])
                entry["readyState"] = meta["readyState"]
                entry["focused"] = meta["focused"]
                entry["lastRefCount"] = meta["lastRefCount"]
                result.append(entry)
            return result

    def _validate_cookie_path(self, path: str) -> str:
        """Validate a cookie export/import path.

        The Sep 4 2026 security audit tightened this from the historical
        "$MINERU_HOME or /tmp" allowlist down to `$MINERU_HOME` only:
        `/tmp` is world-writable and a shared surface between local
        users on the same box, so a cookie file sitting in `/tmp/` is
        one `ls -la /tmp/` away from another account harvesting it.
        Also: even inside the allowed root, the target must NOT be a
        symlink (checked on the raw path, before `.resolve()`), and
        every intermediate directory component must be resolved to
        something inside `$MINERU_HOME` — a symlink deeper in the path
        can otherwise redirect the final write into `/etc/…` etc.

        `.is_relative_to()` handles the "inside" check without the
        pre-3.9 `parents` walk; the earlier form silently accepted a
        path that resolved TO $MINERU_HOME (i.e., the directory itself)
        as a valid file target, which would have crashed the writer.
        """
        home = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))).resolve()
        raw = Path(path)
        if raw.is_symlink():
            raise ValueError("Cookie path must not be a symlink")
        resolved = raw.resolve()
        # `is_relative_to` returns True iff `resolved` is a strict
        # descendant of `home` (or equal); we exclude equal-to-home
        # because you cannot write a file AT a directory path.
        try:
            resolved.relative_to(home)
        except ValueError:
            raise ValueError(
                "Cookie path must resolve under %s (got %s -> %s)" % (home, path, resolved)
            )
        if resolved == home:
            raise ValueError("Cookie path cannot be $MINERU_HOME itself (%s)" % home)
        return str(resolved)

    def cookies(self, operation: str, path: Optional[str] = None,
                domain: Optional[str] = None) -> Dict[str, Any]:
        """Cookie management. Export requires explicit path (no default dump).

        Audit trail (Sep 4 2026 security audit fix): every export /
        import call is logged with the operation, target path, and
        domain filter BEFORE any browser work happens, so an operator
        reading `logs/browser-server/server.log` can see when a
        sensitive-cookie op fired even if the browser side later
        raised. The `list` operation is deliberately logged too (it
        does not return values, but it names domains, which is signal).
        """
        # Audit log FIRST — even a rejected/validation-failed call
        # should show up in the log so the operator can see attempts.
        logger.info(
            "browser audit: cookies op=%r path=%r domain=%r",
            operation, path, domain,
        )
        with self._lock:
            self._ensure_browser()
            if operation == "export":
                if not path:
                    raise ValueError("export requires a path (no default — avoid leaving session tokens on disk)")
                safe_path = self._validate_cookie_path(path)
                all_cookies = self._context.cookies()
                if domain:
                    all_cookies = [c for c in all_cookies if domain in c.get("domain", "")]
                Path(safe_path).parent.mkdir(parents=True, exist_ok=True)
                # O_NOFOLLOW blocks a race where a hostile process swaps
                # the final path component for a symlink between our
                # validate step and the open() call — with O_NOFOLLOW the
                # open() raises ELOOP instead of following into (e.g.)
                # `/etc/…`. O_EXCL is not desirable here because a re-
                # export should be able to overwrite an old dump.
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(safe_path, flags, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(all_cookies, f, indent=2)
                logger.info("Cookie export: %d cookies to %s", len(all_cookies), safe_path)
                return {"action": "cookies", "operation": "export",
                        "count": len(all_cookies), "path": safe_path}
            elif operation == "import":
                if not path:
                    raise ValueError("import requires a path")
                safe_path = self._validate_cookie_path(path)
                with open(safe_path) as f:
                    cookie_list = json.load(f)
                self._context.add_cookies(cookie_list)
                return {"action": "cookies", "operation": "import",
                        "count": len(cookie_list), "path": safe_path}
            elif operation == "list":
                all_cookies = self._context.cookies()
                if domain:
                    all_cookies = [c for c in all_cookies if domain in c.get("domain", "")]
                return {"action": "cookies", "operation": "list",
                        "count": len(all_cookies),
                        "cookies": [{"name": c["name"], "domain": c["domain"]}
                                    for c in all_cookies]}
            else:
                raise ValueError("Unknown cookie operation: %s (use export/import/list)" % operation)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            browser_ok = False
            try:
                if self._context is not None:
                    # Check context liveness (works for both plain Browser
                    # contexts and persistent contexts where _browser is None).
                    self._context.pages
                    browser_ok = True
            except Exception:
                pass

            return {
                "status": "ok" if browser_ok else "no_browser",
                "tabs": len(self._tabs),
                "pid": os.getpid(),
                "port": PORT,
            }

    def shutdown(self) -> None:
        with self._lock:
            self._cleanup_browser()


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

# P3-05: process-wide event bus + on-disk snapshot writer.
#
# The bus is stateless-ish (holds subscriber list + drop counter); the
# writer holds a debounce Timer handle. Both are wired into the
# `manager` at construction time so every state-change method on
# BrowserManager can publish + trigger without knowing where the wiring
# lives.
#
# `snapshot_fn` closes over the module-level `manager` — that circular
# reference is OK because Python resolves it lazily at call time.
_tab_event_bus = TabEventBus()
_snapshot_writer = SnapshotWriter(
    TABS_SNAPSHOT_PATH,
    snapshot_fn=lambda: manager.list_tabs(),
    debounce_ms=TABS_SNAPSHOT_DEBOUNCE_MS,
)
manager = BrowserManager(event_bus=_tab_event_bus, snapshot_writer=_snapshot_writer)


class BrowserHandler(BaseHTTPRequestHandler):
    """Handle browser automation requests."""

    def log_message(self, format: str, *args: Any) -> None:
        logger.info(format, *args)

    def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message: str, status: int = 400) -> None:
        self._send_json({"error": message}, status=status)

    def _send_response_no_body(self, status: int) -> None:
        """Emit a bare status line + Content-Length: 0 (no body, generic message).

        Used for the 401 rejection path: a rejected caller must not
        learn anything about the server's shape — no error message, no
        JSON hint, just the status code. `WWW-Authenticate: Bearer` is
        the RFC 7235 required challenge so a well-behaved client knows
        which auth scheme to try (this is a hint to the operator, not
        a discovery hole — the token file location is well-known).
        """
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()

    def _authenticated(self) -> bool:
        """Return True iff the request carries a matching bearer token.

        Reads the current on-disk token on every call so a token
        rotation (rare — only on server restart today, but a future
        rotation feature would slot in here) takes effect without a
        handler restart. `verify_bearer` is constant-time.
        """
        expected = auth_token.read_token_or_none()
        header_value = self.headers.get("Authorization")
        return auth_token.verify_bearer(header_value, expected)

    # ------------------------------------------------------------------
    # P3-05 SSE stream: GET /tabs/stream
    # ------------------------------------------------------------------

    def _sse_frame(self, event_type: str, data: str) -> bytes:
        """Format an SSE frame with an event: line + data: line.

        SSE requires each frame end with a blank line (two \\n). Data
        MUST NOT contain a newline — JSON's compact-ish dump satisfies
        this because we don't ask for indent.
        """
        # Guard: if the JSON payload happens to contain a raw newline
        # (unusual — json.dumps escapes them), split across `data:`
        # lines per SSE spec.
        if "\n" in data:
            data_lines = "\n".join("data: " + line for line in data.split("\n"))
        else:
            data_lines = "data: " + data
        return ("event: " + event_type + "\n" + data_lines + "\n\n").encode("utf-8")

    def _sse_heartbeat_frame(self) -> bytes:
        """SSE comment frame — parseable-but-ignored by any conformant client."""
        return b":heartbeat\n\n"

    def _stream_tabs(self) -> None:
        """Serve GET /tabs/stream as an SSE stream.

        Contract:
          1. Send 200 + Content-Type: text/event-stream headers.
          2. Emit an initial `event: snapshot` frame with the full
             `/tabs` body (same shape `GET /tabs` returns), so a
             fresh subscriber has state without a separate poll.
          3. Loop: block on the subscriber queue with a
             `SSE_HEARTBEAT_SECONDS` timeout; write each event as
             `event: <type>\\ndata: <json>\\n\\n`. On timeout, write
             a `:heartbeat\\n\\n` comment so intermediaries + clients
             know the socket is still alive.
          4. On disconnect (BrokenPipeError, ConnectionResetError,
             OSError from the fd), unsubscribe from the bus in the
             finally block so subscriber lists don't leak.

        Thread-safety: this runs on a `ThreadingHTTPServer` worker
        thread; each SSE connection holds its own worker thread for
        the lifetime of the connection. That's fine for the expected
        small subscriber count (spec §4.3 is one CLI --watch at a
        time), and lets the same server serve unrelated `POST /action`
        calls concurrently.
        """
        subscription: "queue.Queue[Dict[str, Any]]" = _tab_event_bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            # X-Accel-Buffering hint for reverse proxies (nginx). We're
            # localhost-only today but the cost is one header line.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # Initial snapshot event — same shape `GET /tabs` returns.
            # If list_tabs raises, publish an empty snapshot so the
            # client isn't left hanging on the first byte.
            try:
                initial_body = json.dumps(
                    {"tabs": manager.list_tabs()}, ensure_ascii=False
                )
            except Exception as exc:
                logger.warning("SSE: initial list_tabs failed: %s", exc)
                initial_body = json.dumps({"tabs": [], "error": str(exc)})
            self.wfile.write(self._sse_frame("snapshot", initial_body))
            self.wfile.flush()

            # Stream loop.
            while True:
                try:
                    event = subscription.get(timeout=SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    # Heartbeat comment. A broken pipe here surfaces
                    # as BrokenPipeError and we exit the loop cleanly.
                    self.wfile.write(self._sse_heartbeat_frame())
                    self.wfile.flush()
                    continue
                event_type = event.get("type", "message")
                try:
                    data = json.dumps(event, ensure_ascii=False)
                except (TypeError, ValueError) as exc:
                    logger.warning("SSE: event JSON encode failed: %s", exc)
                    continue
                self.wfile.write(self._sse_frame(event_type, data))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected. Normal termination — no traceback.
            pass
        except OSError as exc:
            # Errno EPIPE (32) / ECONNRESET (54) may surface as OSError
            # depending on platform / Python version. Treat as clean
            # disconnect; anything else logs.
            if exc.errno in (32, 54, 104):  # EPIPE, ECONNRESET, ECONNRESET-linux
                pass
            else:
                logger.warning("SSE: unexpected OSError: %s", exc)
        except Exception as exc:  # noqa: BLE001 — SSE must never crash the server
            logger.error("SSE: stream failed: %s\n%s", exc, traceback.format_exc())
        finally:
            _tab_event_bus.unsubscribe(subscription)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(manager.status())
        elif self.path == "/tabs":
            self._send_json({"tabs": manager.list_tabs()})
        elif self.path == "/tabs/stream":
            self._stream_tabs()
        else:
            self._send_error("Unknown endpoint: %s" % self.path, 404)

    def do_POST(self) -> None:
        # Auth gate — FIRST, before we read a single byte of the body.
        # do_POST is the only mutating verb the server exposes (the GET
        # surface — /health, /tabs, /tabs/stream — is intentionally
        # read-only). Every unauth'd POST is rejected with 401 and an
        # empty body so a curious local caller learns nothing about
        # whether the server exists / what its shape looks like.
        # `read_token_or_none` on the server side and the
        # `Authorization: Bearer <tok>` header on the client side keep
        # the accept/reject decision constant-time (hmac.compare_digest).
        if not self._authenticated():
            self._send_response_no_body(401)
            return

        if self.path != "/action":
            self._send_error("POST to /action only", 404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_error("Empty request body")
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError as e:
            self._send_error("Invalid JSON: %s" % e)
            return

        action = body.get("action", "")

        try:
            if action == "open":
                url = body.get("targetUrl") or body.get("url", "")
                if not url:
                    self._send_error("Missing targetUrl")
                    return
                timeout = body.get("timeout", 30000)
                # Optional viewport override — accepts either a dict, a named
                # device preset, or flat width/height. See resolve_viewport().
                viewport = resolve_viewport(
                    viewport=body.get("viewport"),
                    device=body.get("device"),
                    width=body.get("width"),
                    height=body.get("height"),
                )
                result = manager.open_tab(url, timeout_ms=timeout, viewport=viewport)
                self._send_json(result)

            elif action == "snapshot":
                target_id = body.get("targetId", "")
                if not target_id:
                    self._send_error("Missing targetId")
                    return
                result = manager.snapshot(target_id)
                self._send_json(result)

            elif action == "act":
                target_id = body.get("targetId", "")
                if not target_id:
                    self._send_error("Missing targetId")
                    return

                request = {}  # type: Dict[str, Any]
                # Support both nested request object and flat args
                if "request" in body:
                    request = body["request"]
                else:
                    request = {
                        "kind": body.get("kind", ""),
                        "ref": body.get("ref", ""),
                        "text": body.get("text", ""),
                        "value": body.get("value", ""),
                        "checked": body.get("checked", True),
                        "clear": body.get("clear", True),
                        "humanize": body.get("humanize", False),
                        "x": body.get("x", 0),
                        "y": body.get("y", 500),
                        "redact": body.get("redact", False),
                    }
                if not request.get("ref"):
                    request["ref"] = body.get("ref", "")

                result = manager.act(target_id, request)
                self._send_json(result)

            elif action == "navigate":
                target_id = body.get("targetId", "")
                url = body.get("url", "") or body.get("targetUrl", "")
                if not target_id or not url:
                    self._send_error("Missing targetId or url")
                    return
                timeout = body.get("timeout", 30000)
                result = manager.navigate(target_id, url, timeout_ms=timeout)
                self._send_json(result)

            elif action == "wait":
                target_id = body.get("targetId", "")
                if not target_id:
                    self._send_error("Missing targetId")
                    return
                timeout = body.get("timeout", 2000)
                until = body.get("until", "networkidle")
                result = manager.wait(target_id, timeout_ms=timeout, until=until)
                self._send_json(result)

            elif action == "upload":
                target_id = body.get("targetId", "")
                ref = body.get("ref", "")
                paths = body.get("paths", [])
                if not target_id or not ref or not paths:
                    self._send_error("Missing targetId, ref, or paths")
                    return
                via_click = body.get("via_click", False)
                result = manager.upload(target_id, ref, paths, via_click=via_click)
                self._send_json(result)

            elif action == "evaluate":
                target_id = body.get("targetId", "")
                expression = body.get("expression", "")
                if not target_id or not expression:
                    self._send_error("Missing targetId or expression")
                    return
                arg = body.get("arg")
                result = manager.evaluate(target_id, expression, arg=arg)
                self._send_json(result)

            elif action == "responsebody":
                target_id = body.get("targetId", "")
                url_pattern = body.get("urlPattern", body.get("url_pattern", ""))
                if not target_id or not url_pattern:
                    self._send_error("Missing targetId or urlPattern")
                    return
                timeout = body.get("timeout", 10000)
                result = manager.responsebody(target_id, url_pattern, timeout_ms=timeout)
                self._send_json(result)

            elif action == "screenshot":
                target_id = body.get("targetId", "")
                if not target_id:
                    self._send_error("Missing targetId")
                    return
                path = body.get("path")
                full_page = body.get("fullPage", False)
                viewport = resolve_viewport(
                    viewport=body.get("viewport"),
                    device=body.get("device"),
                    width=body.get("width"),
                    height=body.get("height"),
                )
                result = manager.screenshot(
                    target_id, path=path, full_page=full_page, viewport=viewport
                )
                self._send_json(result)

            elif action == "close":
                target_id = body.get("targetId", "")
                if not target_id:
                    self._send_error("Missing targetId")
                    return
                result = manager.close_tab(target_id)
                self._send_json(result)

            elif action == "cookies":
                operation = body.get("operation", "")
                if not operation:
                    self._send_error("Missing operation (export/import/list)")
                    return
                path = body.get("path")
                domain = body.get("domain")
                result = manager.cookies(operation, path=path, domain=domain)
                self._send_json(result)

            elif action == "status":
                self._send_json(manager.status())

            elif action == "stop":
                self._send_json({"status": "shutting_down"})
                threading.Thread(target=_shutdown_server, daemon=True).start()

            else:
                self._send_error("Unknown action: %s" % action)

        except ValueError as e:
            self._send_error(str(e), 400)
        except Exception as e:
            message = str(e)
            tb = traceback.format_exc()
            # Belt-and-suspenders for redacted (credential) fills: even if some
            # deeper frame raised with the secret embedded, it must never land in
            # server.log / stderr.log or the HTTP response body. dispatch() already
            # scrubs at its raise site; this catches anything that slips past.
            nested = body.get("request") if isinstance(body.get("request"), dict) else {}
            if body.get("redact") or nested.get("redact"):
                secret = body.get("text") or nested.get("text") or ""
                message = actions.scrub_secret_from_text(message, secret)
                tb = actions.scrub_secret_from_text(tb, secret)
            logger.error("Action '%s' failed: %s\n%s", action, message, tb)
            self._send_error("Internal error: %s" % message, 500)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

_server = None  # type: Optional[HTTPServer]


def _shutdown_server() -> None:
    global _server
    time.sleep(0.3)
    if _server:
        _server.shutdown()


def _write_pid() -> None:
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid() -> None:
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def _signal_handler(signum: int, frame: Any) -> None:
    logger.info("Received signal %d, shutting down...", signum)
    manager.shutdown()
    _remove_pid()
    sys.exit(0)


def main() -> None:
    global _server

    # Check if already running
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # Check if process exists
            logger.error("Server already running (PID %d). Use 'browser stop' first.", old_pid)
            sys.exit(1)
        except (OSError, ValueError):
            # Process not running, stale PID file
            _remove_pid()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    _write_pid()
    # Mint a fresh per-boot bearer token BEFORE binding the listen
    # socket. Any local client that races the server startup will
    # either see "connection refused" (pre-bind) or find the token
    # file already on disk (post-bind), never a window where the
    # server accepts POSTs with no token check in place.
    auth_token.generate_and_write_token()
    logger.info("Starting browser server on port %d (PID %d)", PORT, os.getpid())

    try:
        # P3-05: ThreadingHTTPServer so a long-lived SSE connection on
        # /tabs/stream doesn't block unrelated POST /action requests.
        # BrowserManager still serializes browser-touching methods
        # via its own RLock; the server-side threading is orthogonal.
        _server = ThreadingHTTPServer(("127.0.0.1", PORT), BrowserHandler)
        _server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, shutting down...")
    finally:
        # Cancel any pending debounced snapshot write so we don't leak
        # a Timer thread past interpreter shutdown.
        try:
            _snapshot_writer.cancel()
        except Exception:
            pass
        manager.shutdown()
        _remove_pid()
        logger.info("Server stopped")


if __name__ == "__main__":
    main()
