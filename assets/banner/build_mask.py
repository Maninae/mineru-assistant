"""Extract the alpha mask of the two marker strokes from the logo avatar.

    python3 build_mask.py  ->  mask.png (640x640, white = stroke)

The avatar has bright aurora haze behind the strokes, so a plain brightness threshold leaks. Two
guards keep the haze out: pixels are seeded from the stroke's pale core and grown only within a few
px of it, and growth is gated by a white top-hat (brightness above the local surroundings), which the
broad haze plateau fails and the narrow strokes pass. The two largest components are kept, pinholes
filled after the morphology, and the edge smoothed so it reads as a round marker.

- The glyph is one open spiral (left eye, violet diagonal, chin, right cheek, right ear, dip, left ear,
  left cheek) plus the separate right swoosh; the gap between the left-cheek cap and the diagonal is
  real and must stay open, which the top-hat gate guarantees (its brightness is haze-level).
"""
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "logo_source.jpg"
OUTPUT = HERE / "mask.png"

CORE_MAX_RGB = 0.86   # the pale centre of a stroke
CORE_MIN_RGB = 0.35   # low enough to accept the violet stretches, which have a dark blue channel
STROKE_MAX_RGB = 0.58 # how dim a pixel may be and still belong to the stroke's edge
GROW_PX = 8           # how far from the core the stroke may extend
TOPHAT_DISK_PX = 51   # local-background window for the top-hat; wider than any stroke
TOPHAT_MIN = 0.10     # haze plateaus score under this, stroke cores well above
HOLE_PX = 600         # holes smaller than this are pinholes; the face interior is far larger
EDGE_SIGMA = 2.2
DISK = np.array([[0, 1, 1, 1, 0], [1, 1, 1, 1, 1], [1, 1, 1, 1, 1], [1, 1, 1, 1, 1], [0, 1, 1, 1, 0]], bool)


def disk(diameter: int) -> np.ndarray:
    """Boolean disk footprint of the given diameter."""
    radius = diameter // 2
    yy, xx = np.mgrid[-radius:radius + 1, -radius:radius + 1]
    return (xx * xx + yy * yy) <= radius * radius


def build_mask() -> np.ndarray:
    """Boolean stroke mask at the avatar's resolution."""
    rgb = np.asarray(Image.open(SOURCE).convert("RGB")).astype(np.float32) / 255.0
    brightest, dimmest = rgb.max(axis=2), rgb.min(axis=2)
    tophat = brightest - ndimage.grey_opening(brightest, footprint=disk(TOPHAT_DISK_PX))
    core = (brightest > CORE_MAX_RGB) & (dimmest > CORE_MIN_RGB)
    grown = (brightest > STROKE_MAX_RGB) & (tophat > TOPHAT_MIN) & ndimage.binary_dilation(core, iterations=GROW_PX)
    labels, count = ndimage.label(grown)
    sizes = ndimage.sum(grown, labels, range(1, count + 1))
    keep = np.zeros_like(grown)
    for index in np.argsort(sizes)[::-1][:2]:
        keep |= labels == (index + 1)
    keep = ndimage.binary_closing(keep, structure=DISK)
    keep = ndimage.binary_opening(keep, structure=DISK)
    holes = ndimage.binary_fill_holes(keep) & ~keep
    hole_labels, hole_count = ndimage.label(holes)
    for index, size in enumerate(ndimage.sum(holes, hole_labels, range(1, hole_count + 1))):
        if size < HOLE_PX:
            keep |= hole_labels == (index + 1)
    return ndimage.gaussian_filter(keep.astype(np.float32), EDGE_SIGMA) > 0.5


def main() -> None:
    """Write the mask and print stroke-width statistics."""
    mask = build_mask()
    distance = ndimage.distance_transform_edt(mask)
    ridge = distance[(distance > 0) & (distance >= ndimage.maximum_filter(distance, size=5) - 0.01)]
    labels, count = ndimage.label(mask)
    holes = ndimage.binary_fill_holes(mask) & ~mask
    print(f"components {count}, enclosed hole px {int(holes.sum())}, stroke width along the ridge: "
          f"p5 {2 * np.percentile(ridge, 5):.0f}, median {2 * np.median(ridge):.0f}, p95 {2 * np.percentile(ridge, 95):.0f} px")
    Image.fromarray((mask * 255).astype(np.uint8)).save(OUTPUT)
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
