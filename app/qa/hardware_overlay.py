"""
Hardware-chrome overlay for QA screenshots.

Renders a `pointer-events: none` layer on top of the page that draws the
iPhone 17 Pro's visible hardware chrome — Dynamic Island pill, screen-corner
masks, home-indicator bar, and a subtle status-bar mock (clock, signal,
Wi-Fi, battery, all in white per iOS `black-translucent`). This makes every
screenshot LOOK like the target phone, so collisions between our chrome and
the device chrome are immediately visible.

The overlay is drawn in a fixed-position `<div>` root at z-index 2**31-1 so
nothing in the app can paint over it. It never intercepts events.

Injection: call `build_overlay_html(profile)` and pass the returned string
to `page.evaluate("(html) => { ... }", html)` after all app rendering
settles. The overlay is a static snapshot — it does not react to scroll
or animation.
"""

from typing import Optional

from device_profiles import DeviceProfile


# White glyphs for the status bar mock. Enough to see whether the app
# chrome collides with them, without a full SF Pro re-implementation.
_STATUS_BAR_HTML_PORTRAIT = """
<div class="__mineru_qa_status_bar">
  <div class="__mineru_qa_status_time">9:41</div>
  <div class="__mineru_qa_status_right">
    <span class="__mineru_qa_signal" aria-hidden="true">•••••</span>
    <span class="__mineru_qa_wifi" aria-hidden="true">▲</span>
    <span class="__mineru_qa_battery" aria-hidden="true">▮▮▮</span>
  </div>
</div>
"""


def build_overlay_html(profile: DeviceProfile) -> str:
    """
    Return the overlay HTML fragment for `profile`. The fragment is
    self-contained (inline <style>) so it can be dropped into any page.

    For profiles with no dynamic_island / home_indicator / corner_radius,
    returns an empty string — desktop screenshots need no chrome.
    """
    if profile.corner_radius_px == 0 and profile.dynamic_island is None:
        return ""

    parts = []
    parts.append(_overlay_style(profile))
    parts.append('<div class="__mineru_qa_overlay_root" aria-hidden="true">')

    if profile.corner_radius_px:
        # Four corner masks: same color as an assumed matte-black bezel.
        # An inset-box-shadow trick would work but is anti-aliased fuzzy;
        # explicit corner masks are pixel-crisp at the exact radius.
        parts.append(_corner_masks_html())

    if profile.dynamic_island and profile.orientation == "portrait":
        island = profile.dynamic_island
        parts.append(_island_html(island.width, island.height, island.top))
        parts.append(_STATUS_BAR_HTML_PORTRAIT)

    if profile.home_indicator and profile.orientation == "portrait":
        bar = profile.home_indicator
        parts.append(_home_bar_html(bar.width, bar.height, bar.bottom))

    if profile.orientation == "landscape":
        # In landscape the island rides the left edge; draw a subtle darker
        # strip on both left and right insets so the auditor sees exactly
        # what iOS is reserving.
        parts.append(_landscape_side_strips_html(profile.insets.left, profile.insets.right))

    parts.append("</div>")
    return "".join(parts)


def _overlay_style(profile: DeviceProfile) -> str:
    # z-index at the max signed 32-bit value keeps the overlay above every
    # app z-index (highest live one is 30, the status-strip scrim).
    return """
<style>
.__mineru_qa_overlay_root, .__mineru_qa_overlay_root * {
  pointer-events: none;
  box-sizing: border-box;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
}
.__mineru_qa_overlay_root {
  position: fixed;
  inset: 0;
  z-index: 2147483647;
}
.__mineru_qa_island {
  position: fixed;
  background: #000;
  /* iOS Dynamic Island is a full-pill: border-radius equals height/2. */
  border-radius: 9999px;
}
.__mineru_qa_status_bar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: 62px;
  padding: 16px 32px 0;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  color: #fff;
  font-weight: 600;
  font-size: 17px;
  letter-spacing: 0.01em;
  /* Text is styled to mimic iOS SF Pro white glyphs. A soft shadow makes
     them stay visible even against a light app header (so a legibility
     bug is immediately obvious in the shot). */
  text-shadow: 0 0 2px rgba(0, 0, 0, 0.35);
}
.__mineru_qa_status_time { min-width: 60px; }
.__mineru_qa_status_right { display: flex; gap: 6px; align-items: center; font-size: 14px; }
.__mineru_qa_signal, .__mineru_qa_wifi, .__mineru_qa_battery { letter-spacing: 0; }
.__mineru_qa_home_bar {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  background: rgba(255, 255, 255, 0.85);
  border-radius: 9999px;
}
.__mineru_qa_corner {
  position: fixed;
  width: 60px;
  height: 60px;
  background: #000;
  /* radial-gradient technique: a solid circle at the inner corner cuts a
     quarter-round out of the outer square, leaving a corner-mask shape. */
}
.__mineru_qa_corner_tl {
  top: 0; left: 0;
  clip-path: path('M0,0 L60,0 A55,55 0 0 0 5,55 L0,60 Z');
}
.__mineru_qa_corner_tr {
  top: 0; right: 0;
  clip-path: path('M0,0 L60,0 L60,60 L55,55 A55,55 0 0 0 0,0 Z');
}
.__mineru_qa_corner_bl {
  bottom: 0; left: 0;
  clip-path: path('M0,60 L0,0 L5,5 A55,55 0 0 0 60,60 Z');
}
.__mineru_qa_corner_br {
  bottom: 0; right: 0;
  clip-path: path('M60,60 L0,60 A55,55 0 0 0 55,5 L60,0 Z');
}
.__mineru_qa_side_strip_left,
.__mineru_qa_side_strip_right {
  position: fixed;
  top: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.32);
  border-left: 1px solid rgba(255, 255, 255, 0.08);
  border-right: 1px solid rgba(255, 255, 255, 0.08);
}
.__mineru_qa_side_strip_left { left: 0; }
.__mineru_qa_side_strip_right { right: 0; }
</style>
""".strip()


def _corner_masks_html() -> str:
    return (
        '<div class="__mineru_qa_corner __mineru_qa_corner_tl"></div>'
        '<div class="__mineru_qa_corner __mineru_qa_corner_tr"></div>'
        '<div class="__mineru_qa_corner __mineru_qa_corner_bl"></div>'
        '<div class="__mineru_qa_corner __mineru_qa_corner_br"></div>'
    )


def _island_html(width: int, height: int, top: int) -> str:
    # `calc(50% - <half-width>)` centers a fixed-width pill; simpler than
    # translateX because the CSS already sits in a full-viewport root.
    half = width // 2
    return (
        '<div class="__mineru_qa_island" style="'
        'width:%dpx;height:%dpx;top:%dpx;left:calc(50%% - %dpx);"></div>'
    ) % (width, height, top, half)


def _home_bar_html(width: int, height: int, bottom: int) -> str:
    return (
        '<div class="__mineru_qa_home_bar" style="'
        'width:%dpx;height:%dpx;bottom:%dpx;"></div>'
    ) % (width, height, bottom)


def _landscape_side_strips_html(left_inset: int, right_inset: int) -> str:
    return (
        '<div class="__mineru_qa_side_strip_left" style="width:%dpx;"></div>'
        '<div class="__mineru_qa_side_strip_right" style="width:%dpx;"></div>'
    ) % (left_inset, right_inset)
