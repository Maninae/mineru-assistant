"""Render the README banner: an orange text-wireframe fox head beside a zsh session.

    python3 assets/banner/make_banner.py   ->  assets/banner/banner.png  (2560x1440, 16:9)

The fox itself lives in `fox_wireframe.py`. Needs Playwright (with Chromium);
JetBrains Mono is fetched once into ~/.cache and embedded as data URIs.
Palette matches the mineru.sh site's dark scheme (paper, ink, fox orange).
"""
import base64
import html
import re
import urllib.request
from pathlib import Path

from fox_wireframe import fox_svg
from playwright.sync_api import sync_playwright

BANNER_DIR = Path(__file__).resolve().parent
OUTPUT_PNG = BANNER_DIR / "banner.png"
FONT_CACHE_DIR = Path.home() / ".cache" / "banner-fonts"
FONT_WEIGHTS = (400, 600, 800)
FOX_CENTER = (452, 356)
FOX_SCALE = 0.92

TERMINAL_SESSION = [
    ("command", "mineru setup"),
    ("output", "profile hydrated into ~/.mineru"),
    ("command", "mineru memory warm-resume"),
    ("command", "mineru cron status"),
    ("cursor", ""),
]


def ensure_jetbrains_mono_fonts() -> dict:
    """Download the JetBrains Mono TTFs (OFL) from Google Fonts once, keyed by weight."""
    FONT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    font_paths = {}
    for weight in FONT_WEIGHTS:
        font_path = FONT_CACHE_DIR / f"JetBrainsMono-{weight}.ttf"
        if not font_path.exists():
            css = urllib.request.urlopen(f"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@{weight}").read().decode()
            ttf_url = re.search(r"https://[^)]+\.ttf", css).group(0)
            font_path.write_bytes(urllib.request.urlopen(ttf_url).read())
        font_paths[weight] = font_path
    return font_paths


def terminal_session_html() -> str:
    """The zsh session beside the fox."""
    lines = []
    for kind, text in TERMINAL_SESSION:
        if kind == "command":
            lines.append(f'<div><span class="prompt">❯</span> {html.escape(text)}</div>')
        elif kind == "output":
            lines.append(f'<div class="output">{html.escape(text)}</div>')
        else:
            lines.append('<div><span class="prompt">❯</span> █</div>')
    return "\n".join(lines)


def banner_page_html(font_paths: dict) -> str:
    """Full 1280x720 page; rendered at 2x."""
    font_faces = "".join(
        f"@font-face{{font-family:JBM;src:url(data:font/ttf;base64,{base64.b64encode(path.read_bytes()).decode()});font-weight:{weight}}}"
        for weight, path in font_paths.items()
    )
    center_x, center_y = FOX_CENTER
    return f"""<!doctype html><html><head><style>{font_faces}
:root{{--bg:#151311;--ink:#ece4d6;--dim:#a89f90;--fox:#e8842f;--fox-deep:#b8661f;--fur:#6b6358;--cream:#f3e6d2}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:1280px;height:720px;background:var(--bg);color:var(--ink);font-family:JBM,monospace;position:relative;overflow:hidden}}
svg{{position:absolute;left:0;top:0}}
svg text{{font-family:JBM,monospace;white-space:pre}}
.outline{{fill:var(--fox);font-size:12.5px;font-weight:600}}
.fur{{fill:var(--fox);font-size:10.5px;font-weight:600}}
.detail{{fill:var(--fox-deep);font-size:9.5px}}
.eye{{fill:var(--cream);font-size:12px;font-weight:800}}
.eye-lower{{fill:var(--fur);font-size:12px}}
.blaze{{fill:var(--fur);font-size:9px}}
.tail{{fill:var(--fox);font-size:11.5px;font-weight:600}}
.tail-inner{{fill:var(--fox-deep);font-size:11.5px;font-weight:600}}
.tail-core{{fill:var(--fur);font-size:9px}}
.tip-text{{fill:var(--cream);font-size:12px;font-weight:800}}
.nose{{fill:var(--cream)}}
.tip{{fill:var(--cream)}}
.pupil{{fill:var(--cream)}}
.side{{position:absolute;left:800px;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:34px}}
h1{{font-size:40px;font-weight:800;letter-spacing:-1px}}
.tagline{{font-size:17px;color:var(--dim);margin-top:10px;line-height:1.5}}
.terminal{{font-size:15px;line-height:1.85}}
.prompt{{color:var(--fox)}} .output{{color:var(--dim)}}
</style></head><body>
<svg width="1280" height="720" viewBox="0 0 1280 720">{fox_svg(center_x, center_y, FOX_SCALE)}</svg>
<div class="side">
  <div><h1>mineru</h1><div class="tagline">Clone one engine, hydrate<br>your own private AI assistant.</div></div>
  <div class="terminal">{terminal_session_html()}</div>
</div></body></html>"""


def render_banner() -> None:
    """Load the page in headless Chromium and screenshot it."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=2)
        page.set_content(banner_page_html(ensure_jetbrains_mono_fonts()))
        page.evaluate("Promise.all(['400','600','800'].map(w => document.fonts.load(w + ' 12px JBM')))")
        page.screenshot(path=str(OUTPUT_PNG))
        browser.close()
    print(f"wrote {OUTPUT_PNG}")


if __name__ == "__main__":
    render_banner()
