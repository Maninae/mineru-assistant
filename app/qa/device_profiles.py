"""
Device profiles for the QA screenshot harness.

One place to add a new device: append a DeviceProfile to `PROFILES`. The
harness looks profiles up by `id`. Every field is CSS pixels / points
(Playwright's viewport is in CSS pixels, and iOS safe-area insets are
reported in points, which equal CSS pixels).

Source-of-truth for the iPhone 17 series numbers is Geoff Hackworth's
`useyourloaf.com/blog/iphone-17-screen-sizes/` (September 2025), which
publishes viewport + safe-area-insets in points for every model:
  iPhone 17 & 17 Pro : 402 x 874, DPR 3
    portrait  top 62, bottom 34, left 0, right 0
    landscape top 20, bottom 20, left 62, right 62
Dynamic-Island geometry (island pill 126 x 37 pt, ~11 pt from top; screen
corner radius ~55 pt; home indicator ~134 x 5 pt, ~8 pt from bottom) comes
from Apple's design resources for the iPhone 15/16/17 Pro island form
factor. The overlay uses these to draw the visible hardware chrome.

The `desktop-web` profile is a control — no insets, no overlay, so the same
harness verifies desktop layout doesn't regress.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


IPHONE_17_PRO_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)

DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class SafeAreaInsets:
    """CSS-pixel insets. Matches `Emulation.setSafeAreaInsetsOverride`'s shape."""
    top: int = 0
    left: int = 0
    right: int = 0
    bottom: int = 0

    def as_cdp_payload(self) -> Dict[str, Dict[str, int]]:
        return {"insets": {"top": self.top, "left": self.left, "right": self.right, "bottom": self.bottom}}


@dataclass(frozen=True)
class DynamicIsland:
    """iPhone 17 Pro island pill geometry, in CSS pixels within the viewport."""
    width: int
    height: int
    top: int  # distance from viewport top to the island's top edge


@dataclass(frozen=True)
class HomeIndicator:
    """Home indicator bar geometry, in CSS pixels within the viewport."""
    width: int
    height: int
    bottom: int  # distance from viewport bottom to the bar's bottom edge


@dataclass(frozen=True)
class DeviceProfile:
    """
    A concrete device + orientation to emulate.

    - `viewport` is Playwright's CSS-pixel viewport (width, height).
    - `insets` becomes the `Emulation.setSafeAreaInsetsOverride` CDP call.
    - `dynamic_island` / `home_indicator` / `corner_radius_px` drive the
      overlay layer. `None` = no hardware chrome (used by desktop-web).
    """
    id: str
    label: str
    viewport: Tuple[int, int]
    device_scale_factor: int
    is_mobile: bool
    user_agent: str
    insets: SafeAreaInsets = field(default_factory=SafeAreaInsets)
    dynamic_island: Optional[DynamicIsland] = None
    home_indicator: Optional[HomeIndicator] = None
    corner_radius_px: int = 0
    orientation: str = "portrait"  # "portrait" | "landscape"


# iPhone 17 Pro — 402 x 874, DPR 3, Dynamic Island (per Apple design specs).
# Portrait: island sits horizontally centered near the top; home bar centered
# at the bottom. Landscape: island moves to the top when the phone is rotated
# left (which is the default iOS landscape). Left/right insets shift into
# whichever side the island rides on; we model the "rotated-left" case here
# (the more common one), where the island is on the LEFT.

_ISLAND_17_PRO_PORTRAIT = DynamicIsland(width=126, height=37, top=11)
_HOME_BAR_17_PRO_PORTRAIT = HomeIndicator(width=134, height=5, bottom=8)

IPHONE_17_PRO_PORTRAIT = DeviceProfile(
    id="iphone-17-pro",
    label="iPhone 17 Pro (portrait)",
    viewport=(402, 874),
    device_scale_factor=3,
    is_mobile=True,
    user_agent=IPHONE_17_PRO_UA,
    insets=SafeAreaInsets(top=62, bottom=34, left=0, right=0),
    dynamic_island=_ISLAND_17_PRO_PORTRAIT,
    home_indicator=_HOME_BAR_17_PRO_PORTRAIT,
    corner_radius_px=55,
    orientation="portrait",
)

IPHONE_17_PRO_LANDSCAPE = DeviceProfile(
    id="iphone-17-pro-landscape",
    label="iPhone 17 Pro (landscape)",
    viewport=(874, 402),
    device_scale_factor=3,
    is_mobile=True,
    user_agent=IPHONE_17_PRO_UA,
    # Rotated-left orientation: island rides the LEFT edge, home bar on the
    # bottom (short axis). Values are from useyourloaf's iPhone 17 table.
    insets=SafeAreaInsets(top=20, bottom=20, left=62, right=62),
    # In landscape the island doesn't sit on a top edge inside the viewport
    # the same way; the overlay layer skips the island pill and just draws
    # the two side insets as a subtle darkened strip so the auditor can see
    # what iOS reserves. corner_radius still clips the four corners.
    dynamic_island=None,
    home_indicator=None,
    corner_radius_px=55,
    orientation="landscape",
)

DESKTOP_WEB = DeviceProfile(
    id="desktop-web",
    label="Desktop web (1440x900 @2x)",
    viewport=(1440, 900),
    device_scale_factor=2,
    is_mobile=False,
    user_agent=DESKTOP_CHROME_UA,
    insets=SafeAreaInsets(),
    dynamic_island=None,
    home_indicator=None,
    corner_radius_px=0,
    orientation="portrait",
)


PROFILES: Dict[str, DeviceProfile] = {
    p.id: p
    for p in (IPHONE_17_PRO_PORTRAIT, IPHONE_17_PRO_LANDSCAPE, DESKTOP_WEB)
}


def get_profile(profile_id: str) -> DeviceProfile:
    if profile_id not in PROFILES:
        raise KeyError(
            "unknown device profile %r; known: %s"
            % (profile_id, ", ".join(sorted(PROFILES)))
        )
    return PROFILES[profile_id]


def list_profile_ids() -> List[str]:
    return sorted(PROFILES)
