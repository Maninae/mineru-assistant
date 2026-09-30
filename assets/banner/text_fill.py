"""Fill the logo's stroke mask with `mineru` command text in the logo's own colors.

    python3 text_fill.py  ->  emblem.png (RGBA, cropped to the strokes plus glow margin)

Inputs, both derived from the Telegram avatar `logo_source.jpg`:
- `mask.png`: the alpha of the two marker strokes (white = stroke), built by `build_mask.py`.
- the avatar itself, sampled per glyph so every character carries the exact color the stroke
  has at that spot (the mint-to-violet gradient comes from the picture, not from a palette).

Method: rows of monospace command text are rasterised across the emblem canvas, each glyph tinted by
the avatar color under it; the text coverage times the mask is the alpha. Two blurred copies of the
result sit underneath as the neon glow, matching the soft halo in the source.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FONT_PATH = Path.home() / ".cache" / "banner-fonts" / "JetBrainsMono-800.ttf"
SOURCE = HERE / "logo_source.jpg"
MASK = HERE / "mask.png"
OUTPUT = HERE / "emblem.png"

CANVAS = 1280  # device pixels; the avatar is 640, everything is drawn at 2x
FONT_PX = 17   # glyph size at 2x, so ~8.5 CSS px: two rows fit inside a marker stroke
ROW_PITCH = 18
ROW_STAGGER_CHARS = 5
GLOW_LAYERS = [(5, 0.32), (18, 0.26)]  # (blur radius, opacity), tight glow then wide halo
CROP_MARGIN = 70  # device px kept around the strokes so the halo is not clipped
COLOR_LIFT = 1.08  # the source is JPEG-soft; a small lift keeps the text as bright as the marker

COMMANDS = ["mineru setup", "mineru profile install --apply", "mineru memory warm-resume",
            "mineru secrets set", "mineru cron status", "mineru memory consolidate",
            "mineru profile export", "mineru memory backup", "mineru calendar list",
            "mineru gmail search", "mineru telegram send", "mineru memory tree",
            "mineru cron install --dry-run", "mineru profile validate", "mineru secrets list"]


def text_rows(row_count: int, chars_per_row: int) -> list:
    """Rows of command text, each row starting further into the cycle so words never align vertically."""
    stream = "  ·  ".join(COMMANDS) + "  ·  "
    stream = stream * (chars_per_row * row_count // len(stream) + 2)
    rows = []
    for row in range(row_count):
        offset = (row * (chars_per_row + ROW_STAGGER_CHARS)) % len(stream)
        rows.append((stream + stream)[offset: offset + chars_per_row])
    return rows


def render_text_coverage() -> np.ndarray:
    """White text on black across the canvas, as a float coverage map in [0, 1]."""
    font = ImageFont.truetype(str(FONT_PATH), FONT_PX)
    advance = font.getlength("m")
    chars_per_row = int(CANVAS / advance) + 2
    row_count = CANVAS // ROW_PITCH + 2
    sheet = Image.new("L", (CANVAS, CANVAS), 0)
    draw = ImageDraw.Draw(sheet)
    for row, text in enumerate(text_rows(row_count, chars_per_row)):
        draw.text((0, row * ROW_PITCH - 2), text, font=font, fill=255)
    return np.asarray(sheet).astype(np.float32) / 255.0


def source_colors() -> np.ndarray:
    """The avatar upscaled to the canvas and softened, so each glyph samples a smooth stroke color."""
    image = Image.open(SOURCE).convert("RGB").resize((CANVAS, CANVAS), Image.LANCZOS).filter(ImageFilter.GaussianBlur(2.5))
    return np.clip(np.asarray(image).astype(np.float32) / 255.0 * COLOR_LIFT, 0, 1)


def build_emblem() -> Image.Image:
    """The masked, colored text with its glow, as an RGBA image on a transparent ground."""
    coverage = render_text_coverage()
    mask = np.asarray(Image.open(MASK).convert("L").resize((CANVAS, CANVAS), Image.LANCZOS)).astype(np.float32) / 255.0
    alpha = coverage * mask
    rgb = source_colors()
    crisp = np.dstack([rgb, alpha[..., None]])
    crisp_image = Image.fromarray((crisp * 255).astype(np.uint8), "RGBA")
    # Glow: the solid stroke (mask, not the text) in the stroke's colors, blurred, under the text.
    solid = Image.fromarray((np.dstack([rgb, mask[..., None]]) * 255).astype(np.uint8), "RGBA")
    emblem = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    for radius, opacity in GLOW_LAYERS[::-1]:
        layer = solid.filter(ImageFilter.GaussianBlur(radius))
        layer_alpha = np.asarray(layer)[..., 3].astype(np.float32) * opacity
        layer.putalpha(Image.fromarray(layer_alpha.astype(np.uint8)))
        emblem = Image.alpha_composite(emblem, layer)
    return Image.alpha_composite(emblem, crisp_image)


def crop_to_strokes(emblem: Image.Image) -> Image.Image:
    """Trim the canvas to the strokes plus the glow margin, so the page can place the glyph itself."""
    mask = Image.open(MASK).convert("L").resize((CANVAS, CANVAS), Image.LANCZOS)
    left, top, right, bottom = mask.getbbox()
    box = (max(left - CROP_MARGIN, 0), max(top - CROP_MARGIN, 0), min(right + CROP_MARGIN, CANVAS), min(bottom + CROP_MARGIN, CANVAS))
    return emblem.crop(box)


def main() -> None:
    """Write the emblem."""
    crop_to_strokes(build_emblem()).save(OUTPUT)
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
