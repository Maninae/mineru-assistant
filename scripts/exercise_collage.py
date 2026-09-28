#!/usr/bin/env python3
"""Compose a vertical exercise-collage PNG from per-exercise frame triples.

Layout, top-to-bottom, for each exercise:
    +----------------------------------------------------+
    |  BLACK BAR   "Group — Name (dose)"  (white text)   |
    +----------------+----------------+------------------+
    |   frame 01     |   frame 02     |   frame 03       |
    +----------------+----------------+------------------+

The black label bars double as the horizontal separators between exercises, so
there is no per-row border or padding to tune. Cell size is derived from the
target `collage_width` (default 1080px, sized for a phone) and the source frames'
aspect ratio — each cell is `collage_width / 3` wide and its height matches.

Public entry point:
    compose_collage(items, out_path, collage_width=1080, ...) -> Path

where `items` is a list of dicts: {"name": str, "dose": str, "frames": [Path, Path, Path]}
(optionally "group_label" — prepended to the header if present).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# --- Layout constants ---------------------------------------------------------

DEFAULT_COLLAGE_WIDTH = 1080         # matches Instagram / phone-portrait comfort
FRAMES_PER_ROW = 3                   # spec: 3 frames per exercise
LABEL_BAR_HEIGHT = 72                # px; comfortably readable at phone scale
LABEL_TEXT_PADDING_X = 20            # px inset for the text inside the bar
LABEL_FONT_SIZE = 30                 # px; fits ~50-char headers on a 1080-wide bar
BACKGROUND_COLOR = (0, 0, 0)         # matches the label bars — one visual system
LABEL_TEXT_COLOR = (255, 255, 255)   # white on black
JPEG_QUALITY = 92

# System-font search order; the first path that exists (and TrueType-loads) wins.
# Arial Bold is present on every stock macOS; DejaVuSans-Bold is the linux/matplotlib
# fallback. We degrade to Pillow's default bitmap font only as a last resort.
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


@dataclass(frozen=True)
class CollageItem:
    """Normalized per-exercise input to the collage renderer."""
    header: str                     # rendered into the black label bar
    frame_paths: tuple[Path, ...]   # exactly FRAMES_PER_ROW paths


def _load_font(size: int = LABEL_FONT_SIZE) -> ImageFont.ImageFont:
    """Load the first available TrueType font; fall back to Pillow's default."""
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    logger.warning("no TrueType font found; falling back to Pillow default bitmap font")
    return ImageFont.load_default()


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    """Return (width, height) of text as it will render, using textbbox."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _truncate_to_fit(text: str, max_width_px: int, draw: ImageDraw.ImageDraw, font) -> str:
    """If `text` doesn't fit in max_width_px, ellipsize it until it does."""
    width, _ = _measure_text(draw, text, font)
    if width <= max_width_px:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and _measure_text(draw, trimmed + ellipsis, font)[0] > max_width_px:
        trimmed = trimmed[:-1]
    return trimmed + ellipsis if trimmed else ellipsis


def _normalize_item(raw: dict) -> CollageItem:
    """Turn a caller-supplied dict into a CollageItem, validating shape."""
    frames = raw.get("frames") or []
    if len(frames) != FRAMES_PER_ROW:
        raise ValueError(
            f"exercise item needs exactly {FRAMES_PER_ROW} frames, got {len(frames)}: {raw.get('name')}"
        )
    frame_paths = tuple(Path(p) for p in frames)
    for fp in frame_paths:
        if not fp.exists():
            raise FileNotFoundError(f"frame missing: {fp} (for {raw.get('name')})")

    name = raw.get("name", "Unnamed")
    dose = raw.get("dose", "").strip()
    group_label = raw.get("group_label", "").strip()

    # Header shape: "Group Label — Name  (dose)"; drop empty pieces gracefully.
    parts = []
    if group_label:
        parts.append(f"{group_label} — {name}")
    else:
        parts.append(name)
    if dose:
        parts.append(f"({dose})")
    header = "  ".join(parts)

    return CollageItem(header=header, frame_paths=frame_paths)


def _resize_frame_to_cell(frame_path: Path, cell_width: int, cell_height: int) -> Image.Image:
    """Resize a source frame to (cell_width, cell_height), cover-fit + center-crop.

    Cover-fit keeps every cell fully painted (no letterboxing) and preserves the
    subject's proportions; center-cropping favors the middle of the frame, where
    the body usually is. If the source is already the right shape it's a straight resize.
    """
    with Image.open(frame_path) as im:
        rgb = im.convert("RGB")
    src_w, src_h = rgb.size
    if src_w <= 0 or src_h <= 0:
        raise ValueError(f"degenerate frame {frame_path} ({src_w}x{src_h})")
    src_ratio = src_w / src_h
    cell_ratio = cell_width / cell_height
    if abs(src_ratio - cell_ratio) < 1e-3:
        return rgb.resize((cell_width, cell_height), Image.LANCZOS)
    if src_ratio > cell_ratio:
        # Source is wider than the cell — crop the sides.
        new_h = src_h
        new_w = int(round(src_h * cell_ratio))
        left = (src_w - new_w) // 2
        cropped = rgb.crop((left, 0, left + new_w, new_h))
    else:
        # Source is taller than the cell — crop top/bottom equally.
        new_w = src_w
        new_h = int(round(src_w / cell_ratio))
        top = (src_h - new_h) // 2
        cropped = rgb.crop((0, top, new_w, top + new_h))
    return cropped.resize((cell_width, cell_height), Image.LANCZOS)


def _derive_cell_dimensions(sample_frame_path: Path, collage_width: int) -> tuple[int, int]:
    """Derive per-cell (width, height) from the target collage width + a sample frame."""
    cell_width = collage_width // FRAMES_PER_ROW
    with Image.open(sample_frame_path) as im:
        src_w, src_h = im.size
    if src_w <= 0 or src_h <= 0:
        raise ValueError(f"degenerate sample frame {sample_frame_path} ({src_w}x{src_h})")
    # Preserve the source frame's aspect ratio so a portrait reel stays portrait.
    cell_height = int(round(cell_width * (src_h / src_w)))
    return cell_width, cell_height


def compose_collage(
    items: Iterable[dict],
    out_path: Path,
    collage_width: int = DEFAULT_COLLAGE_WIDTH,
    label_bar_height: int = LABEL_BAR_HEIGHT,
    label_font_size: int = LABEL_FONT_SIZE,
) -> Path:
    """Render a stacked exercise collage to `out_path` and return it.

    Args:
        items: iterable of dicts, one per exercise. Each dict needs:
            - "name" (str), "dose" (str)
            - optional "group_label" (str) — prepended in the header
            - "frames": list of exactly FRAMES_PER_ROW image paths, in reading order
        out_path: where to write the PNG.
        collage_width: total width in pixels (default 1080, matches phone view).
        label_bar_height / label_font_size: black-bar tuning knobs.
    """
    items = [_normalize_item(raw) for raw in items]
    if not items:
        raise ValueError("compose_collage: items is empty")

    # All rows share cell dimensions derived from the FIRST exercise's first frame
    # (in practice every reel is portrait 9:16, so this is uniform). Frames from a
    # differently-shaped source are cover-fit into the same cell, keeping the grid.
    cell_width, cell_height = _derive_cell_dimensions(items[0].frame_paths[0], collage_width)
    row_height = label_bar_height + cell_height
    canvas_height = row_height * len(items)
    # Round the canvas width to FRAMES_PER_ROW * cell_width so cells align exactly
    # (integer division above can drop 1-2px, and a stray sliver looks unintentional).
    canvas_width = cell_width * FRAMES_PER_ROW

    canvas = Image.new("RGB", (canvas_width, canvas_height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(canvas)
    font = _load_font(label_font_size)

    for row_index, item in enumerate(items):
        row_top = row_index * row_height

        # 1. Label bar (black by default via canvas background; draw the header text).
        max_text_width = canvas_width - 2 * LABEL_TEXT_PADDING_X
        header = _truncate_to_fit(item.header, max_text_width, draw, font)
        _, text_h = _measure_text(draw, header, font)
        text_y = row_top + (label_bar_height - text_h) // 2
        draw.text((LABEL_TEXT_PADDING_X, text_y), header, fill=LABEL_TEXT_COLOR, font=font)

        # 2. Frame row directly below the bar.
        for col, frame_path in enumerate(item.frame_paths):
            cell = _resize_frame_to_cell(frame_path, cell_width, cell_height)
            canvas.paste(cell, (col * cell_width, row_top + label_bar_height))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # PNG for the artifact itself (crisp text bars); the Telegram sender will
    # decide whether to reencode or upload as-is.
    canvas.save(out_path, "PNG")
    logger.info(
        "wrote collage %s (%dx%d, %d rows, cell=%dx%d)",
        out_path, canvas_width, canvas_height, len(items), cell_width, cell_height,
    )
    return out_path


def _cli() -> None:
    """Debug entry: build a collage from a JSON spec file.

    Spec format:
        [
          {"name": "...", "dose": "...", "group_label": "...", "frames": ["/a.jpg", "/b.jpg", "/c.jpg"]},
          ...
        ]
    """
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Render an exercise collage PNG from a JSON spec.")
    parser.add_argument("spec_json", type=Path, help="path to JSON spec (see module docstring)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--width", type=int, default=DEFAULT_COLLAGE_WIDTH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with open(args.spec_json) as f:
        items = json.load(f)
    compose_collage(items, args.out, collage_width=args.width)


if __name__ == "__main__":
    _cli()
