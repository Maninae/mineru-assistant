# Mineru web app — QA harness

Device-true screenshots for the Mineru web app. This is what closes the loop
between what an agent sees during a design pass (headless Chromium at
"whatever the default viewport is") and what the user sees on an iPhone 17 Pro
(402 x 874 CSS pixels, DPR 3, Dynamic Island reserving the top 62 px). Before
this harness, safe-area collisions were invisible until someone opened the PWA
and screenshotted; now the harness reproduces them locally.

## Files

| File | Responsibility |
|---|---|
| `device_profiles.py` | `DeviceProfile` dataclass + concrete profiles (`iphone-17-pro`, `iphone-17-pro-landscape`, `desktop-web`). One place to add a new device. |
| `hardware_overlay.py` | Renders the iPhone hardware chrome (Dynamic Island pill, screen-corner masks, home-indicator bar, white status-bar mock) as a `pointer-events: none` overlay so shots LOOK like the phone. |
| `shoot.py` | The CLI. Handles Playwright driver, CDP safe-area override, matrix runs. |

## How the safe-area emulation actually works

`env(safe-area-inset-*)` is normally zero in headless Chromium because
there's no hardware chrome to work around. Two ways to lie to the browser:

1. **CDP `Emulation.setSafeAreaInsetsOverride`** (primary). Playwright
   exposes CDP via `context.new_cdp_session(page)`. The command takes
   `{"insets": {"top": N, "left": N, "right": N, "bottom": N}}`, and after
   that call `env(safe-area-inset-top)` resolves to `N` in every stylesheet.
   Verified on Playwright 1.58 + bundled Chromium 131.

2. **CSS shim** (fallback). If the CDP method is unavailable on a future
   Chromium, the harness prints a loud stderr warning and defines
   `--qa-safe-*` custom properties. Coarse — the app CSS uses `env()`
   directly and would need to be rewritten to consume the vars, so the
   fallback is a signal that a version bump is coming, not a permanent
   substitute.

**Reproducer** (paste into any Playwright-enabled Python to re-verify a
future Chromium hasn't dropped the command):

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 402, "height": 874}, device_scale_factor=3, is_mobile=True)
    page = ctx.new_page()
    cdp = ctx.new_cdp_session(page)
    cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": {"top": 62, "left": 0, "right": 0, "bottom": 34}})
    page.set_content('<div style="padding-top:env(safe-area-inset-top,0);background:#f0c;">x</div>')
    print(page.evaluate("() => getComputedStyle(document.querySelector('div')).paddingTop"))
    # expected: "62px"
```

## Where the device numbers come from

`useyourloaf.com/blog/iphone-17-screen-sizes/` (September 2025) is the
authoritative source: iPhone 17 & 17 Pro viewport is 402 x 874 pt, DPR 3,
portrait insets `{top:62, bottom:34, left:0, right:0}`, landscape insets
`{top:20, bottom:20, left:62, right:62}`. Dynamic Island geometry (126 x 37
pt pill, ~11 pt from top) and corner radius (~55 pt) come from Apple's own
design resources for the 15/16/17 Pro island form factor.

Adding a new device: append a `DeviceProfile` to `PROFILES` in
`device_profiles.py`. That's it — the CLI and matrix both pick it up from
the registry.

## Usage

Single shot:

```
/opt/homebrew/bin/python3 app/qa/shoot.py \
    --route '#inbox' --theme forest --device iphone-17-pro \
    --out /tmp/inbox-forest.png
```

Matrix (every tab x chosen themes x chosen devices):

```
/opt/homebrew/bin/python3 app/qa/shoot.py --matrix \
    --devices iphone-17-pro,desktop-web \
    --themes forest,warm \
    --out-dir /tmp/mineru-qa/
```

- `--base-url` defaults to `http://127.0.0.1:5195` (the launchd webapp). For
  a dev pass, START YOUR OWN server on `--port 5199` and pass
  `--base-url http://127.0.0.1:5199` — the launchd server on 5195 is the
  live tailnet UI, don't POST to it and don't restart it.
- `--no-overlay` disables the hardware chrome overlay (useful when you want
  a plain screenshot to hand off without the pill/corners drawn on).
- `--extra-routes` appends to the standard matrix set — good for including
  a specific brief reader deep-link.

## Python

The harness runs on `/opt/homebrew/bin/python3` (the Homebrew Python where
Playwright is installed). It is not tied to the webapp's Python 3.9 target
(the webapp uses `/usr/bin/python3`). Written in a 3.9-compatible style
(`typing.List/Optional/Dict`, no PEP 604 unions) as a courtesy, but the
Playwright dependency pins it to whatever Python has Playwright installed.

## What to shoot when you edit chrome-adjacent CSS

The invariants the harness is here to protect:

1. On `iphone-17-pro` portrait, every fixed/sticky element clears the
   Dynamic Island (island bottom sits at ~48 px; safe-area-inset-top is 62
   px). Header content, back button, fox mark, search overlay — none of it
   should overlap the pill.
2. On `iphone-17-pro-landscape`, the left/right insets (62 px each) push
   content away from the island's shadow. The tabbar tiles stay reachable.
3. On the light themes (warm, and light-OS default), the status-bar strip
   keeps enough contrast that the always-white iOS glyphs remain legible.
   The overlay draws the glyphs in white; if you can't read the mock
   "9:41" over the header, iOS wouldn't be readable either.
4. On `desktop-web`, none of the above rules apply and nothing should
   regress at 1440 x 900 (sidebar layout).

Convention: for a chrome change, shoot at least `iphone-17-pro` and
`desktop-web`, in `forest` and `warm`, and READ every PNG with the
multimodal Read tool before claiming the change works. Log the shots under
`~/.claude/history/<session>/iphone-screens/` for the review trail.
