"""Extract the alpha mask of the two marker strokes from the logo avatar.

    python3 build_mask.py  ->  mask.png (640x640, white = stroke)

The avatar has bright aurora haze behind the strokes, so a plain brightness threshold leaks. The
strokes are told apart by their pale near-white core: pixels are seeded from that core and grown only
into bright pixels within a few px of it, then the two largest components (outline loop, swoosh) are
kept, pinholes filled, and the edge smoothed so it reads as a round marker.
"""
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "logo_source.jpg"
OUTPUT = HERE / "mask.png"

CORE_MAX_RGB = 0.86   # the pale centre of a stroke
CORE_MIN_RGB = 0.42
STROKE_MAX_RGB = 0.58 # how dim a pixel may be and still belong to the stroke's edge
GROW_PX = 8           # how far from the core the stroke may extend
HOLE_PX = 600         # holes smaller than this are pinholes; the face interior is far larger
EDGE_SIGMA = 1.6
DISK = np.array([[0, 1, 1, 1, 0], [1, 1, 1, 1, 1], [1, 1, 1, 1, 1], [1, 1, 1, 1, 1], [0, 1, 1, 1, 0]], bool)


def build_mask() -> np.ndarray:
    """Boolean stroke mask at the avatar's resolution."""
    rgb = np.asarray(Image.open(SOURCE).convert("RGB")).astype(np.float32) / 255.0
    brightest, dimmest = rgb.max(axis=2), rgb.min(axis=2)
    core = (brightest > CORE_MAX_RGB) & (dimmest > CORE_MIN_RGB)
    grown = (brightest > STROKE_MAX_RGB) & ndimage.binary_dilation(core, iterations=GROW_PX)
    labels, count = ndimage.label(grown)
    sizes = ndimage.sum(grown, labels, range(1, count + 1))
    keep = np.zeros_like(grown)
    for index in np.argsort(sizes)[::-1][:2]:
        keep |= labels == (index + 1)
    holes = ndimage.binary_fill_holes(keep) & ~keep
    hole_labels, hole_count = ndimage.label(holes)
    for index, size in enumerate(ndimage.sum(holes, hole_labels, range(1, hole_count + 1))):
        if size < HOLE_PX:
            keep |= hole_labels == (index + 1)
    keep = ndimage.binary_closing(keep, structure=DISK)
    keep = ndimage.binary_opening(keep, structure=DISK)
    return ndimage.gaussian_filter(keep.astype(np.float32), EDGE_SIGMA) > 0.5


def main() -> None:
    """Write the mask and print stroke-width statistics."""
    mask = build_mask()
    distance = ndimage.distance_transform_edt(mask)
    inside = distance[distance > 0]
    print(f"stroke width: median {2 * np.median(inside):.1f} px, max {2 * distance.max():.1f} px")
    Image.fromarray((mask * 255).astype(np.uint8)).save(OUTPUT)
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
