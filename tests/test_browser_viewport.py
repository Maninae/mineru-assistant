#!/usr/bin/env python3
"""Tests for browser/server.py's viewport override plumbing.

Focus is resolve_viewport() — the one place three input shapes (explicit dict,
named device preset, flat width/height) collapse into a single {w, h} for
open_tab/screenshot. If this function is right, the HTTP handler is right too:
it's a pure passthrough. A live browser-launch test lives outside the unit
suite (documented in browser/CLAUDE.md), because it needs Playwright + a real
Chromium process — heavy for CI.

Run: python3 -m pytest tests/test_browser_viewport.py -v
"""

import sys
import unittest
from pathlib import Path

# The browser package expects its grandparent (repo root) on sys.path so
# `from browser.config import ...` resolves. Mirror what server.py itself does.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browser import server as browser_server  # noqa: E402


class ResolveViewportTest(unittest.TestCase):
    def test_none_when_nothing_supplied(self) -> None:
        self.assertIsNone(browser_server.resolve_viewport())

    def test_explicit_dict_wins(self) -> None:
        self.assertEqual(
            browser_server.resolve_viewport(viewport={"width": 320, "height": 640}),
            {"width": 320, "height": 640},
        )

    def test_device_preset_iphone(self) -> None:
        result = browser_server.resolve_viewport(device="iphone")
        self.assertEqual(result, browser_server.DEVICE_PRESETS["iphone"])

    def test_device_preset_case_insensitive(self) -> None:
        self.assertEqual(
            browser_server.resolve_viewport(device="IPhone"),
            browser_server.DEVICE_PRESETS["iphone"],
        )

    def test_device_preset_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            browser_server.resolve_viewport(device="not-a-phone")

    def test_flat_width_height(self) -> None:
        self.assertEqual(
            browser_server.resolve_viewport(width=390, height=844),
            {"width": 390, "height": 844},
        )

    def test_flat_width_without_height_raises(self) -> None:
        with self.assertRaises(ValueError):
            browser_server.resolve_viewport(width=390)

    def test_priority_dict_over_device_over_flat(self) -> None:
        # Explicit dict is highest priority, so device + flat are ignored.
        result = browser_server.resolve_viewport(
            viewport={"width": 100, "height": 200},
            device="iphone",
            width=999,
            height=999,
        )
        self.assertEqual(result, {"width": 100, "height": 200})

    def test_priority_device_over_flat(self) -> None:
        result = browser_server.resolve_viewport(
            device="mobile", width=999, height=999
        )
        self.assertEqual(result, browser_server.DEVICE_PRESETS["mobile"])

    def test_malformed_dict_raises(self) -> None:
        with self.assertRaises(ValueError):
            browser_server.resolve_viewport(viewport={"width": "abc", "height": 100})

    def test_zero_width_rejected(self) -> None:
        # L3 (audit): Playwright silently ignores a 0-dim viewport and
        # keeps the default. Reject at the boundary so a bad caller sees
        # a clear 400, not a confused "why is my shot 1440 wide?".
        with self.assertRaises(ValueError) as ctx:
            browser_server.resolve_viewport(viewport={"width": 0, "height": 800})
        self.assertIn("out of range", str(ctx.exception))

    def test_negative_dim_rejected(self) -> None:
        with self.assertRaises(ValueError):
            browser_server.resolve_viewport(viewport={"width": 800, "height": -1})

    def test_absurdly_large_dim_rejected(self) -> None:
        # A typo like `width=19200` (missed decimal) shouldn't allocate a
        # 19K x 800 canvas silently.
        big = browser_server.VIEWPORT_MAX_DIM + 1
        with self.assertRaises(ValueError):
            browser_server.resolve_viewport(viewport={"width": big, "height": 800})

    def test_zero_flat_width_rejected(self) -> None:
        # A `width=0 height=800` request looks legal to the "both given"
        # gate but is still a silent no-op in Playwright. Same rejection.
        # Note: `width=0` fails the truthiness check first, raising
        # "must be given together" — still an error (which is what we want).
        with self.assertRaises(ValueError):
            browser_server.resolve_viewport(width=0, height=800)

    def test_negative_flat_dims_rejected(self) -> None:
        # `-100, -100` are both truthy in Python, so they pass the "both
        # given" gate — but the range check must catch them.
        with self.assertRaises(ValueError) as ctx:
            browser_server.resolve_viewport(width=-100, height=-100)
        self.assertIn("out of range", str(ctx.exception))

    def test_boundary_min_dim_accepted(self) -> None:
        # 1x1 is legal (edge of the range). The bounds check accepts it.
        result = browser_server.resolve_viewport(viewport={"width": 1, "height": 1})
        self.assertEqual(result, {"width": 1, "height": 1})

    def test_boundary_max_dim_accepted(self) -> None:
        max_dim = browser_server.VIEWPORT_MAX_DIM
        result = browser_server.resolve_viewport(
            viewport={"width": max_dim, "height": max_dim}
        )
        self.assertEqual(result, {"width": max_dim, "height": max_dim})

    def test_all_presets_have_valid_shape(self) -> None:
        # Guard against a future preset getting keyed wrong. And now that
        # bounds exist, every preset must ALSO round-trip through
        # resolve_viewport without raising.
        for name, vp in browser_server.DEVICE_PRESETS.items():
            with self.subTest(preset=name):
                self.assertIn("width", vp)
                self.assertIn("height", vp)
                self.assertIsInstance(vp["width"], int)
                self.assertIsInstance(vp["height"], int)
                self.assertGreater(vp["width"], 0)
                self.assertGreater(vp["height"], 0)
                # Must not raise — preset dims must be in bounds.
                self.assertEqual(
                    browser_server.resolve_viewport(device=name), dict(vp)
                )


if __name__ == "__main__":
    unittest.main()
