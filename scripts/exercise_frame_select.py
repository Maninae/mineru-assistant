#!/usr/bin/env python3
"""Pick 3 representative frames spanning an exercise's arc from its source reel.

The pipeline is a sharpness-gated, perceptual-hash-diverse three-pick — no ML model,
no OpenCV, no extra pip deps beyond Pillow + numpy + system ffmpeg:

  1. Densely extract candidate frames evenly across [start_s, end_s] (~3-4 fps, capped).
  2. Compute a focus score per frame (variance of a 3x3 Laplacian on the luma channel)
     and drop the blurriest quartile — those are motion-blur transitions, not poses.
  3. dHash each survivor (9x8 grayscale, adjacent-pixel comparisons → 64-bit fingerprint)
     so we can quantify silhouette difference with Hamming distance.
  4. Greedily pick 3 frames in reading order (one from each temporal third of the window)
     that maximize pairwise Hamming distance — this naturally captures start/mid/end of
     a movement. Static holds degrade gracefully to 3 near-identical evenly-spaced picks.
  5. Crop each pick's top `crop_top` fraction off (default 35%) to drop the reel's title
     banner / anatomy overlay, then save as sequential JPEGs.

Public entry point:
    select_frames(video_path, start_s, end_s, out_dir, crop_top=0.35) -> list[Path]
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# --- Tuning constants ---------------------------------------------------------

CANDIDATE_FPS = 4.0            # dense enough to see the movement arc, cheap to hash
MAX_CANDIDATES = 24            # cap so a long window doesn't balloon runtime
SHARPNESS_DROP_FRACTION = 0.25 # drop the blurriest quarter of candidates
DHASH_SIDE = 8                 # dHash produces (SIDE+1)xSIDE grayscale => SIDE*SIDE bits
DEFAULT_CROP_TOP_FRACTION = 0.35
JPEG_QUALITY = 88
FFMPEG_TIMEOUT_SECONDS = 30            # wall-clock so a wedged ffmpeg can't silently hang the noon job
BLANK_FRAME_VARIANCE_THRESHOLD = 1.0   # below this, a picked frame is treated as a blank/bad extract


# --- ffmpeg extraction --------------------------------------------------------

def extract_candidate_frames(
    video_path: Path,
    start_s: float,
    end_s: float,
    out_dir: Path,
    fps: float = CANDIDATE_FPS,
    max_frames: int = MAX_CANDIDATES,
) -> list[Path]:
    """Extract dense candidates over [start_s, end_s] via ffmpeg into out_dir.

    We shell out directly (not through scripts/extract-frames.sh) because that script
    samples the whole video, and here we want a per-window, higher-fps slice. Frame
    filenames are lexicographically ordered so the caller can recover time ordering.
    """
    if end_s <= start_s:
        raise ValueError(f"end_s ({end_s}) must be > start_s ({start_s})")
    duration = end_s - start_s
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("cand-*.jpg"):
        stale.unlink()

    pattern = str(out_dir / "cand-%04d.jpg")
    # `-ss` before `-i` for fast seek to start; `-t` bounds duration exactly.
    # `-vsync vfr` avoids duplicating frames when the source fps is low.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start_s:.3f}",
        "-i", str(video_path),
        "-t", f"{duration:.3f}",
        "-vf", f"fps={fps}",
        "-frames:v", str(max_frames),
        "-vsync", "vfr",
        pattern,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as timeout_error:
        raise RuntimeError(
            f"ffmpeg timed out (>{FFMPEG_TIMEOUT_SECONDS}s) extracting {video_path.name} [{start_s}..{end_s}]"
        ) from timeout_error
    frames = sorted(out_dir.glob("cand-*.jpg"))
    logger.info("extracted %d candidates from %s [%.2fs..%.2fs]", len(frames), video_path.name, start_s, end_s)
    return frames


# --- Focus / sharpness score --------------------------------------------------

# 3x3 Laplacian kernel used for the focus score. High-frequency energy proxy:
# sharp frames have a lot of edge content, so the variance of the response is high;
# motion-blurred frames wash out to near-uniform.
LAPLACIAN_KERNEL = np.array(
    [[0, -1, 0],
     [-1, 4, -1],
     [0, -1, 0]],
    dtype=np.float32,
)


def compute_focus_score(image_path: Path) -> float:
    """Variance-of-Laplacian focus score on the luma channel (higher = sharper)."""
    with Image.open(image_path) as im:
        # Downscale before convolution — the score is scale-invariant enough for
        # ranking, and the smaller pass is ~10x faster than doing it on full frames.
        gray = im.convert("L").resize((240, 240), Image.BILINEAR)
    arr = np.asarray(gray, dtype=np.float32)
    # Manual 2D convolution via 3x3 slicing — stdlib+numpy only, no scipy.
    center = arr[1:-1, 1:-1]
    up     = arr[0:-2, 1:-1]
    down   = arr[2:  , 1:-1]
    left   = arr[1:-1, 0:-2]
    right  = arr[1:-1, 2:  ]
    laplacian = 4.0 * center - up - down - left - right
    return float(laplacian.var())


# --- Perceptual hash (dHash) --------------------------------------------------

def compute_dhash(image_path: Path, side: int = DHASH_SIDE) -> int:
    """dHash: downscale to (side+1) x side grayscale, then compare adjacent pixels.

    Result is a `side*side`-bit integer. Two visually similar frames land at a small
    Hamming distance; frames with different silhouettes / limb positions land far apart.
    """
    with Image.open(image_path) as im:
        small = im.convert("L").resize((side + 1, side), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.int16)
    # Bit i is set iff pixel is brighter than the pixel to its right.
    diff = arr[:, 1:] > arr[:, :-1]
    bits = diff.flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two integer hashes."""
    return bin(a ^ b).count("1")


# --- Diverse triple selection -------------------------------------------------

def pick_three_diverse(
    candidate_indices: list[int],
    hashes: dict[int, int],
    total_candidates: int,
) -> list[int]:
    """Pick 3 time-ordered candidates that maximize pairwise Hamming distance.

    Reading order is enforced by drawing pick 1 from the first temporal third of the
    candidate list, pick 2 from the middle third, and pick 3 from the last third.
    Within those three bins we choose the triple with the maximum summed pairwise
    Hamming distance — for a moving exercise this picks start / mid / end poses; for
    a static hold every distance is small and it degrades to 3 evenly-spaced frames.
    """
    if len(candidate_indices) < 3:
        # Not enough surviving candidates — return whatever we have, padded by repeating
        # the endpoints so the caller always gets 3 slots.
        picks = list(candidate_indices)
        while len(picks) < 3:
            picks.append(picks[-1])
        return picks[:3]

    # Bin candidates by their position in the ORIGINAL (pre-drop) time axis so a
    # blurry first-third doesn't push pick 1 into the middle.
    third = total_candidates / 3.0
    bin_first, bin_mid, bin_last = [], [], []
    for idx in candidate_indices:
        if idx < third:
            bin_first.append(idx)
        elif idx < 2 * third:
            bin_mid.append(idx)
        else:
            bin_last.append(idx)

    # Fallback: if a bin ended up empty (e.g. all sharp frames clustered in the middle),
    # borrow from the nearest non-empty bin so we still get 3 time-ordered picks.
    def borrow_if_empty(bin_, donors):
        if bin_:
            return bin_
        for donor in donors:
            if donor:
                return [donor[len(donor) // 2]]
        return []

    bin_first = borrow_if_empty(bin_first, [bin_mid, bin_last])
    bin_last  = borrow_if_empty(bin_last,  [bin_mid, bin_first])
    bin_mid   = borrow_if_empty(bin_mid,   [bin_first, bin_last])

    best_triple = None
    best_score = -1
    for a in bin_first:
        for b in bin_mid:
            for c in bin_last:
                score = (
                    hamming_distance(hashes[a], hashes[b])
                    + hamming_distance(hashes[b], hashes[c])
                    + hamming_distance(hashes[a], hashes[c])
                )
                if score > best_score:
                    best_score = score
                    best_triple = (a, b, c)
    return list(best_triple) if best_triple else candidate_indices[:3]


# --- Cropping + writing -------------------------------------------------------

def crop_and_save(source_path: Path, dest_path: Path, crop_top_fraction: float) -> None:
    """Crop the top `crop_top_fraction` off the image and write JPEG to dest_path."""
    with Image.open(source_path) as im:
        rgb = im.convert("RGB")
        width, height = rgb.size
        top = int(height * crop_top_fraction) if 0.0 < crop_top_fraction < 1.0 else 0
        cropped = rgb.crop((0, top, width, height))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(dest_path, "JPEG", quality=JPEG_QUALITY)


# --- Public entry point -------------------------------------------------------

def select_frames(
    video_path: Path,
    start_s: float,
    end_s: float,
    out_dir: Path,
    crop_top: float = DEFAULT_CROP_TOP_FRACTION,
    fps: float = CANDIDATE_FPS,
    max_frames: int = MAX_CANDIDATES,
) -> list[Path]:
    """Select 3 sharp, diverse, time-ordered frames from the given window.

    Args:
        video_path: source reel on disk.
        start_s, end_s: seconds bounding the exercise's segment in the reel.
        out_dir: destination directory; will contain 01.jpg, 02.jpg, 03.jpg.
        crop_top: fraction of the top of each frame to trim (default 0.35, i.e. keep
            the bottom 65%). Set to 0.0 to keep the whole frame.
        fps: candidate-extraction rate (default 4 fps).
        max_frames: hard cap on candidates extracted (default 24).

    Returns the 3 written paths in reading order.
    """
    video_path = Path(video_path)
    out_dir = Path(out_dir)
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")

    # Extract into a scratch dir so we can clean up candidates and only leave the 3 picks.
    with tempfile.TemporaryDirectory(prefix="mineru-frames-") as tmp_str:
        tmp_dir = Path(tmp_str)
        candidates = extract_candidate_frames(video_path, start_s, end_s, tmp_dir, fps=fps, max_frames=max_frames)
        if not candidates:
            raise RuntimeError(f"ffmpeg produced no candidates for {video_path.name} [{start_s}..{end_s}]")

        # Sharpness-gate: drop the blurriest quartile.
        focus_scores = [compute_focus_score(p) for p in candidates]
        n = len(candidates)
        drop_count = max(0, min(n - 3, int(round(n * SHARPNESS_DROP_FRACTION))))
        ranked = sorted(range(n), key=lambda i: focus_scores[i])
        dropped = set(ranked[:drop_count])
        survivor_indices = sorted(i for i in range(n) if i not in dropped)
        logger.info("sharpness gate kept %d/%d frames (dropped %d)", len(survivor_indices), n, drop_count)

        # Perceptual hashes for the survivors only.
        hashes = {i: compute_dhash(candidates[i]) for i in survivor_indices}

        # Time-ordered, diversity-maximizing triple.
        picks = pick_three_diverse(survivor_indices, hashes, total_candidates=n)

        # Wipe any prior 01/02/03.jpg in out_dir so a re-run doesn't leave a mixed set.
        out_dir.mkdir(parents=True, exist_ok=True)
        for stale in out_dir.glob("[0-9][0-9].jpg"):
            stale.unlink()

        written = []
        for slot, cand_index in enumerate(picks, start=1):
            dest = out_dir / f"{slot:02d}.jpg"
            crop_and_save(candidates[cand_index], dest, crop_top_fraction=crop_top)
            written.append(dest)

    # Sanity: a near-blank pick (e.g. ffmpeg emitted a black keyframe) must fail LOUD,
    # not ship a blank collage tile. Raising lets the noon job's try/except fall back to
    # text delivery so the user still gets the notification.
    for p in written:
        with Image.open(p) as im:
            arr = np.asarray(im.convert("L"), dtype=np.float32)
        if arr.var() < BLANK_FRAME_VARIANCE_THRESHOLD:
            raise RuntimeError(
                f"selected frame {p} is nearly blank (variance={arr.var():.2f}); "
                "refusing to ship a blank collage tile"
            )

    logger.info("wrote 3 picks to %s", out_dir)
    return written


# --- CLI (for debugging one exercise at a time) -------------------------------

def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Select 3 representative frames from a video window.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--crop-top", type=float, default=DEFAULT_CROP_TOP_FRACTION)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    paths = select_frames(args.video, args.start, args.end, args.out_dir, crop_top=args.crop_top)
    for p in paths:
        print(p)


if __name__ == "__main__":
    _cli()
