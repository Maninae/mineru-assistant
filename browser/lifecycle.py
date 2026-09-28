#!/usr/bin/env python3
"""
Mineru Browser Server — Browser Lifecycle

Pure functions for launching and tearing down the browser context. Three modes:
  1. CDP attach — connect to an existing Chrome via http://...:9222
  2. CloakBrowser — stealth-patched persistent context
  3. Standalone fallback — plain Playwright + playwright_stealth

Lazy imports (sync_playwright, Stealth, cloakbrowser) are intentional to avoid
import-time cost when the module loads.
"""

import os
from typing import Any, Callable, Optional, Tuple

from browser.config import (
    CDP_ENDPOINT,
    CLOAK_EXECUTABLE,
    HEADLESS,
    PERSISTENT_PROFILE,
    USE_CLOAK,
    logger,
)


def launch_context(
    headless,  # type: bool
    cdp_endpoint,  # type: str
    use_cloak,  # type: bool
    cloak_executable,  # type: str
    persistent_profile,  # type: Any
    on_new_page=None,  # type: Optional[Callable[[Any], None]]
):
    # type: (...) -> Tuple[Any, Any, Any, Any]
    """Launch browser, return (playwright, browser, context, stealth_cm) tuple.

    One of the three branches runs based on flags:
      1. CDP attach — playwright/browser set, stealth_cm None
      2. CloakBrowser — all None except context
      3. Standalone — all set

    on_new_page: optional callback invoked when the context reports a new
      page (via `context.on("page", ...)`). Fires for tabs opened by the
      user (window.open, target=_blank), popups, and also when we call
      `context.new_page()` from `open_tab`. The caller is responsible for
      distinguishing api-opened pages from user/popup ones. Registered on
      the context IN ALL THREE BRANCHES so the /tabs endpoint sees every
      tab regardless of launch mode.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    if cdp_endpoint:
        # CDP attach mode: connect to an existing Chrome instance.
        # No stealth needed — it's a real browser with a real profile.
        logger.info("Connecting to Chrome via CDP at %s", cdp_endpoint)
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(cdp_endpoint)
        # Use the browser's default context (the real Chrome profile)
        contexts = browser.contexts
        if contexts:
            context = contexts[0]
            logger.info("Attached to existing context (%d pages open)", len(context.pages))
        else:
            context = browser.new_context()
            logger.info("Created new context on attached browser")
        stealth_cm = None
    elif use_cloak and os.path.exists(cloak_executable):
        # CloakBrowser wrapper mode: handles stealth args, binary
        # management, locale/timezone via binary flags (not CDP
        # emulation), and Runtime.Enable suppression. The wrapper
        # manages the Playwright lifecycle internally.
        logger.info("Starting CloakBrowser via wrapper with persistent profile")
        persistent_profile.mkdir(parents=True, exist_ok=True)
        import cloakbrowser
        browser_context = cloakbrowser.launch_persistent_context(
            user_data_dir=str(persistent_profile),
            headless=headless,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone="America/Los_Angeles",
            color_scheme="light",
            stealth_args=True,
            backend="patchright",  # patches Runtime.Enable CDP leak
            humanize=False,  # we have our own Bézier + keystroke humanize
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        playwright = None  # wrapper manages lifecycle
        browser = None
        context = browser_context
        stealth_cm = None
    else:
        # Fallback: launch standard Chromium with playwright_stealth
        logger.info("Starting standard Chromium (CloakBrowser not available)")
        stealth = Stealth(navigator_platform_override="MacIntel")
        stealth_cm = stealth.use_sync(sync_playwright())
        pw = stealth_cm.__enter__()
        playwright = pw
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            # Keep this Chrome major version in sync with the CloakBrowser
            # Chromium binary (config.CLOAK_EXECUTABLE — currently 145). A UA
            # whose version doesn't match the actual engine is a bot tell.
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
        )

    mode = "cdp" if cdp_endpoint else ("cloak" if (use_cloak and os.path.exists(cloak_executable)) else "standalone")

    # Register the new-page listener AFTER the context is fully built but
    # BEFORE we return, so any subsequent `context.new_page()` / user-opened
    # tab / popup fires the callback. Same wiring for all three branches so
    # /tabs reports every tab regardless of launch mode (fixes the "user-
    # opened tabs are invisible" gap the audit flagged in spec §4.3).
    #
    # For CDP-attach mode, the pages the context ALREADY has are pre-existing
    # tabs from the attached Chrome; we invoke the callback for each so the
    # tab registry starts populated instead of only tracking tabs we open
    # after the attach.
    if on_new_page is not None:
        try:
            context.on("page", on_new_page)
        except Exception as exc:
            # A broken listener registration is not fatal — the server can
            # still drive tabs it opens itself; user-opened tabs just won't
            # show up. Log loudly so the operator can debug.
            logger.warning("Failed to register on_new_page listener: %s", exc)
        if cdp_endpoint:
            for existing_page in list(getattr(context, "pages", []) or []):
                try:
                    on_new_page(existing_page)
                except Exception as exc:
                    logger.warning("on_new_page failed for pre-existing tab: %s", exc)

    logger.info("Browser ready (mode=%s)", mode)

    return playwright, browser, context, stealth_cm


def cleanup(
    playwright,  # type: Any
    browser,  # type: Any
    context,  # type: Any
    stealth_cm,  # type: Any
    cdp_sessions,  # type: Any
    tabs,  # type: Any
    ref_registry,  # type: Any
):
    # type: (...) -> None
    """Tear down browser. Detach CDPs, close context/browser, exit stealth CM.

    Mutates the passed-in cdp_sessions/tabs dicts and ref_registry in place.
    No storage-state persistence in any mode:
      - CloakBrowser: persistent profile handles cookies automatically.
      - CDP: real Chrome profile handles persistence.
      - Standalone fallback: session cookies are ephemeral by design.
        Writing plaintext cookie state to disk is a security risk (C1).
    """
    for sid, cdp in list(cdp_sessions.items()):
        try:
            cdp.detach()
        except Exception:
            pass
    cdp_sessions.clear()
    tabs.clear()
    ref_registry.clear_all()

    # Close context first, then browser (if any). In CloakBrowser mode
    # browser is None — the persistent context owns the browser process,
    # so closing the context is sufficient.
    for obj in (context, browser):
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass

    # Exit stealth context manager (which wraps playwright) if used.
    if stealth_cm is not None:
        try:
            stealth_cm.__exit__(None, None, None)
        except Exception:
            pass
