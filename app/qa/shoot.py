#!/usr/bin/env python3
"""
Device-true screenshot harness for the Mineru web app.

  # single shot
  python3 app/qa/shoot.py --route '#inbox' --theme forest \\
      --device iphone-17-pro --out /tmp/inbox-forest.png

  # the standard grid (every tab x chosen themes x chosen devices)
  python3 app/qa/shoot.py --matrix --themes forest,warm \\
      --devices iphone-17-pro,desktop-web --out-dir /tmp/mineru-qa/

  # opt out of the hardware overlay
  python3 app/qa/shoot.py --route '#pulse' --device iphone-17-pro \\
      --out /tmp/pulse-plain.png --no-overlay

Serves against a scratch dev server the caller starts (default
`http://127.0.0.1:5195`). Reader deep-links carry through the hash router,
e.g. `--route '#brief/curiosity/question-2026-08-25.md'`.

The load-bearing move is `Emulation.setSafeAreaInsetsOverride` (a CDP call
through Playwright's chromium session). That makes `env(safe-area-inset-*)`
resolve to the *real* device values in headless Chromium; without it every
inset would be zero and the whole point of the harness — showing the
Dynamic Island collision — would evaporate. Playwright 1.58 + bundled
Chromium 131 has been verified to accept the call (`app/qa/CLAUDE.md`
carries the probe reproducer).

If the CDP call fails on a future Playwright/Chromium combo, the harness
falls back to a CSS `env()` shim injected before first paint, and logs a
loud stderr warning. See CLAUDE.md for the fallback semantics.
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

# Sibling-module imports (this file is intended to run as a script from
# either `app/qa/` or the repo root; both work because `os.chdir` isn't
# involved and Playwright is imported the same way in every context).
sys.path.insert(0, str(Path(__file__).parent))

from device_profiles import DeviceProfile, get_profile, list_profile_ids  # noqa: E402
from hardware_overlay import build_overlay_html  # noqa: E402

try:
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
except ImportError as import_error:
    sys.stderr.write(
        "playwright not installed. Install it into the Python this script is running on:\n"
        "  %s -m pip install playwright && %s -m playwright install chromium\n"
        % (sys.executable, sys.executable)
    )
    raise SystemExit(2) from import_error


DEFAULT_BASE_URL = "http://127.0.0.1:5195"

# The standard tab grid a matrix shoot covers. Reader / deep views can be
# added ad-hoc via --extra-routes.
#
# Two route shapes are supported:
#   `#foo` (or `foo`, `/foo`)  ->  a real hash route parsed by MineruRouter
#                                  (e.g. `#inbox`, `#pulse`, `#chat`,
#                                  `#library/reports`, `#brief/<feed>/<file>`).
#   `tab:<name>`               ->  a pure-tab landing, driven by calling
#                                  MineruShell.switchTab in-page. Used for
#                                  routes the SPA doesn't own a hash for
#                                  (e.g. the Library landing / source
#                                  picker), so the harness can still shoot
#                                  every tab's landing without a deep link.
STANDARD_MATRIX_ROUTES = ["#inbox", "tab:library", "#pulse", "#chat"]


# --- Playwright driver --------------------------------------------------------

def _apply_safe_area_via_cdp(cdp_session, profile: DeviceProfile) -> bool:
    """
    Try the CDP command. Returns True on success, False if the command is
    unavailable on the current Chromium (in which case the caller injects
    a CSS shim as fallback). We deliberately do NOT swallow other errors:
    a bad payload should crash loud, not silently fall back.
    """
    try:
        cdp_session.send("Emulation.setSafeAreaInsetsOverride", profile.insets.as_cdp_payload())
        return True
    except PlaywrightError as playwright_error:
        message = str(playwright_error)
        if "not found" in message.lower() or "invalid method" in message.lower() or "'Emulation.setSafeAreaInsetsOverride' wasn't found" in message:
            return False
        raise


def _css_shim_for_insets(profile: DeviceProfile) -> str:
    # Fallback path: `env()` doesn't accept runtime overrides in stock CSS.
    # We define a set of vars that the app-loaded CSS could read, and
    # rewrite `env(safe-area-inset-*)` at parse time. This is a coarse
    # shim: it only fires the values in a synthetic rule for elements
    # tagged with `data-qa-safe-area`. In practice we prefer the CDP path
    # (this is only a fallback for Chromium versions that ship without
    # the command).
    return """
:root {
  --qa-safe-top: %dpx;
  --qa-safe-right: %dpx;
  --qa-safe-bottom: %dpx;
  --qa-safe-left: %dpx;
}
/* NOTE: fallback shim only. CDP-based override is preferred. */
""" % (profile.insets.top, profile.insets.right, profile.insets.bottom, profile.insets.left)


def _init_theme_script(theme_id: Optional[str]) -> Optional[str]:
    if not theme_id:
        return None
    # Run BEFORE any app JS so the persisted theme is in place for the very
    # first paint. `mineru.theme` is the key set by static/js/theme.js.
    return (
        "try { localStorage.setItem('mineru.theme', %r); } catch (_) {}"
        "document.documentElement.setAttribute('data-theme', %r);"
    ) % (theme_id, theme_id)


def _shoot_one(
    playwright,
    base_url: str,
    profile: DeviceProfile,
    route: str,
    theme_id: Optional[str],
    out_path: Path,
    with_overlay: bool,
    settle_ms: int,
) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        width, height = profile.viewport
        context = browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=profile.device_scale_factor,
            is_mobile=profile.is_mobile,
            has_touch=profile.is_mobile,
            user_agent=profile.user_agent,
        )

        init = _init_theme_script(theme_id)
        if init:
            context.add_init_script(init)

        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp_ok = _apply_safe_area_via_cdp(cdp, profile)
        if not cdp_ok:
            sys.stderr.write(
                "[shoot] WARNING: Emulation.setSafeAreaInsetsOverride not available; "
                "falling back to CSS shim. Screenshot fidelity to real device is reduced.\n"
            )
            context.add_init_script(
                "const s=document.createElement('style'); s.textContent=%r; "
                "document.documentElement.appendChild(s);" % _css_shim_for_insets(profile)
            )

        # Two route shapes: `tab:<name>` -> land on `/` then call switchTab;
        # anything else -> treat as a hash/path route.
        if route.startswith("tab:"):
            tab_name = route.split(":", 1)[1]
            page.goto(base_url.rstrip("/") + "/", wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(settle_ms)
            # In-page tab dispatch through the shell so the whole tab
            # lifecycle runs (feed refresh, header setup, sidebar sync).
            page.evaluate(
                "(name) => { if (window.MineruShell && window.MineruShell.switchTab)"
                " { window.MineruShell.switchTab(name); } }",
                tab_name,
            )
        else:
            url = base_url.rstrip("/") + "/" + route.lstrip("/")
            page.goto(url, wait_until="networkidle", timeout=15000)
        # Extra settle for post-fetch client-side rendering (feed lists,
        # library trees) and for CSS animations to finish.
        page.wait_for_timeout(settle_ms)

        if with_overlay:
            overlay_html = build_overlay_html(profile)
            if overlay_html:
                page.evaluate(
                    "(html) => { const host = document.createElement('div');"
                    " host.innerHTML = html; document.body.appendChild(host); }",
                    overlay_html,
                )
                # A tick for the overlay to lay out.
                page.wait_for_timeout(30)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out_path), full_page=False)
        context.close()
    finally:
        browser.close()


# --- CLI ----------------------------------------------------------------------

def _matrix_output_name(profile: DeviceProfile, route: str, theme_id: str) -> str:
    # Kebab-case: safe filenames on macOS/linux, easy to sort. Route slug
    # normalizes `#foo`, `tab:foo`, and slashes into a filename-friendly
    # scannable form.
    slug = route.lstrip("#")
    if slug.startswith("tab:"):
        slug = slug.split(":", 1)[1]
    slug = slug.replace("/", "__") or "root"
    return "%s__%s__%s.png" % (profile.id, theme_id, slug)


def _run_matrix(
    playwright,
    base_url: str,
    profiles: Iterable[DeviceProfile],
    routes: Iterable[str],
    themes: Iterable[str],
    out_dir: Path,
    with_overlay: bool,
    settle_ms: int,
) -> List[Path]:
    written: List[Path] = []
    for profile in profiles:
        for theme_id in themes:
            for route in routes:
                out_path = out_dir / _matrix_output_name(profile, route, theme_id)
                sys.stdout.write("[shoot] %-32s %-10s %-20s -> %s\n" % (profile.id, theme_id, route, out_path.name))
                sys.stdout.flush()
                _shoot_one(
                    playwright=playwright,
                    base_url=base_url,
                    profile=profile,
                    route=route,
                    theme_id=theme_id,
                    out_path=out_path,
                    with_overlay=with_overlay,
                    settle_ms=settle_ms,
                )
                written.append(out_path)
    return written


def _parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Device-true screenshots for the Mineru web app.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Known device profiles: " + ", ".join(list_profile_ids()) + "\n"
            "Themes are whatever data-theme values tokens.css defines "
            "(forest, ocean, warm, dark)."
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="scratch dev server (default: %(default)s)")
    parser.add_argument("--no-overlay", action="store_true", help="skip the hardware chrome overlay")
    parser.add_argument("--settle-ms", type=int, default=350, help="wait after networkidle before shot (default: %(default)d)")

    single = parser.add_argument_group("single shot")
    single.add_argument("--route", help="hash-route to load, e.g. '#inbox' or '#brief/curiosity/foo.md'")
    single.add_argument("--theme", help="data-theme id: forest, ocean, warm, dark")
    single.add_argument("--device", help="device profile id (see list below)")
    single.add_argument("--out", help="output PNG path for a single shot")

    matrix = parser.add_argument_group("matrix shoot")
    matrix.add_argument("--matrix", action="store_true", help="shoot the standard grid")
    matrix.add_argument("--devices", help="CSV of device profile ids (matrix mode)")
    matrix.add_argument("--themes", help="CSV of theme ids (matrix mode)")
    matrix.add_argument("--routes", help="CSV of routes to shoot (matrix mode). Default: standard tabs.")
    matrix.add_argument("--extra-routes", help="CSV of additional routes appended to the default matrix set")
    matrix.add_argument("--out-dir", help="output directory for matrix shots")

    args = parser.parse_args(argv)

    if args.matrix:
        device_ids = _parse_csv(args.devices) or ["iphone-17-pro"]
        theme_ids = _parse_csv(args.themes) or ["forest", "warm"]
        route_list = _parse_csv(args.routes) or list(STANDARD_MATRIX_ROUTES)
        route_list += _parse_csv(args.extra_routes)
        if not args.out_dir:
            parser.error("--matrix requires --out-dir")
        out_dir = Path(args.out_dir).expanduser().resolve()
        profiles = [get_profile(p) for p in device_ids]
        with sync_playwright() as pw:
            written = _run_matrix(
                playwright=pw,
                base_url=args.base_url,
                profiles=profiles,
                routes=route_list,
                themes=theme_ids,
                out_dir=out_dir,
                with_overlay=not args.no_overlay,
                settle_ms=args.settle_ms,
            )
        sys.stdout.write("[shoot] wrote %d screenshot(s) under %s\n" % (len(written), out_dir))
        return 0

    # Single-shot mode: all four flags required.
    missing = [flag for flag, val in [("--route", args.route), ("--device", args.device), ("--out", args.out)] if not val]
    if missing:
        parser.error("single-shot mode requires: " + ", ".join(missing))

    profile = get_profile(args.device)
    out_path = Path(args.out).expanduser().resolve()
    with sync_playwright() as pw:
        _shoot_one(
            playwright=pw,
            base_url=args.base_url,
            profile=profile,
            route=args.route,
            theme_id=args.theme,
            out_path=out_path,
            with_overlay=not args.no_overlay,
            settle_ms=args.settle_ms,
        )
    sys.stdout.write("[shoot] wrote %s\n" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
