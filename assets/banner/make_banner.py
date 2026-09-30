"""Render the README banner: the aurora fox logo filled with `mineru` command text, beside a zsh session.

    python3 assets/banner/make_banner.py   ->  assets/banner/banner.png  (2560x1440, 16:9)

The emblem is `emblem.png` from `text_fill.py` (mask from `build_mask.py`). Needs Playwright (with Chromium);
JetBrains Mono is fetched once into ~/.cache and embedded as data URIs.
Palette: dark slate ground, cool white type, aurora gradient (mint, cyan, violet) on the glyph.
"""
import base64
import html
import re
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BANNER_DIR = Path(__file__).resolve().parent
OUTPUT_PNG = BANNER_DIR / "banner.png"
FONT_CACHE_DIR = Path.home() / ".cache" / "banner-fonts"
FONT_WEIGHTS = (400, 600, 800)
EMBLEM_PNG = BANNER_DIR / "emblem.png"
EMBLEM_WIDTH = 660  # CSS px; the emblem is vertically centred at the page midline
EMBLEM_LEFT = 62

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
    emblem_uri = "data:image/png;base64," + base64.b64encode(EMBLEM_PNG.read_bytes()).decode()
    left, width = EMBLEM_LEFT, EMBLEM_WIDTH
    return f"""<!doctype html><html><head><style>{font_faces}
:root{{--bg:#1a183a;--ink:#e9ecf4;--dim:#8f94ad;--accent:#7fe9d8}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:1280px;height:720px;background:var(--bg);color:var(--ink);font-family:JBM,monospace;position:relative;overflow:hidden}}
.emblem{{position:absolute;top:50%;transform:translateY(-50%)}}
.side{{position:absolute;left:800px;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:34px}}
h1{{font-size:40px;font-weight:800;letter-spacing:-1px}}
.tagline{{font-size:17px;color:var(--dim);margin-top:10px;line-height:1.5}}
.terminal{{font-size:15px;line-height:1.85}}
.prompt{{color:var(--accent)}} .output{{color:var(--dim)}}
</style></head><body>
<img class="emblem" src="{emblem_uri}" style="left:{left}px;width:{width}px">
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
