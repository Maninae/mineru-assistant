"""Render the README demo: a sample morning brief as it arrives in Telegram.

    python3 assets/demo/make_demo.py  ->  assets/demo/morning-brief.png

The brief is a sample with invented people and errands, in the exact shape the morning-brief job
produces. Rendered as a Telegram-style dark chat bubble so a reader sees the product's face, not a
markdown quote. Needs Playwright with Chromium; the type is the system UI font on purpose, since
that is what Telegram uses.
"""
import html
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "morning-brief.png"

BRIEF_LINES = [
    "🌤️ <b>Tuesday, Oct 6</b>: light day, one errand.",
    "☀️ 64°F",
    "",
    "📅 9:30 AM: dentist (Market St, leave by 9:05)",
    "📅 7:00 PM: dinner with Sam, Thai place downtown",
    "📅 <b>Tomorrow:</b> car service drop-off, 8:00 AM",
    "",
    "💬 <b>Needs your attention:</b>",
    "• 🗓️ <b>Priya</b>: asked about the 18th or the 19th for brunch. Pick one so it sticks.",
    "• 📦 <b>Package</b>: the lamp arrived at the office, not home. Front desk has it.",
    "• 🧾 <b>Dentist</b>: new patient form still unsigned; it's in your inbox from Friday.",
    "",
    "🫧 <b>Skippable:</b> two newsletters, a shipping confirmation, a statement notice.",
]
REPLY = "what's on today?"
ANSWER = "Just the dentist at 9:30, then dinner with Sam at 7. I'll remind you to leave at 9:05."


def page_html() -> str:
    """A Telegram-like dark chat: the brief, your question, the answer."""
    brief = "<br>".join(line if line else "&nbsp;" for line in BRIEF_LINES)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#0e1621;font-family:-apple-system,"SF Pro Text","Helvetica Neue",Arial,sans-serif;color:#e9edf2}}
.chat{{width:720px;padding:28px 26px 26px;box-sizing:border-box}}
.head{{display:flex;align-items:center;gap:12px;margin-bottom:18px}}
.avatar{{width:38px;height:38px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#8cf7c8,#8f6bff 70%,#4b3d8f)}}
.name{{font-weight:600;font-size:15px}} .sub{{font-size:12px;color:#8b98a8}}
.msg{{max-width:560px;width:fit-content;padding:10px 14px 8px;border-radius:14px;font-size:14.5px;line-height:1.45;margin:8px 0;position:relative}}
.in{{background:#182533;border-bottom-left-radius:4px}}
.out{{background:#2b5278;margin-left:auto;border-bottom-right-radius:4px}}
.time{{display:block;text-align:right;font-size:11px;color:#8b98a8;margin-top:4px}}
.out .time{{color:#a9c4e0}}
</style></head><body><div class="chat">
<div class="head"><div class="avatar"></div><div><div class="name">Mineru</div><div class="sub">bot</div></div></div>
<div class="msg in">{brief}<span class="time">7:00 AM</span></div>
<div class="msg out">{html.escape(REPLY)}<span class="time">8:12 AM</span></div>
<div class="msg in">{html.escape(ANSWER)}<span class="time">8:12 AM</span></div>
</div></body></html>"""


def main() -> None:
    """Render the chat at 2x and crop to the chat column."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 720, "height": 600}, device_scale_factor=2)
        page.set_content(page_html())
        page.wait_for_timeout(300)
        page.locator(".chat").screenshot(path=str(OUTPUT))
        browser.close()
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
