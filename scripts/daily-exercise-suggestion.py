#!/usr/bin/env python3
"""Daily exercise suggestion — picks 3 movements across 3 different body groups and delivers them at noon.

Pure plumbing, no LLM ("code for plumbing"): the selection is a deterministic
date-seeded rotation, so day-to-day variety is guaranteed and reproducible, and it costs zero Max quota.

Rotation:
  - Body groups cycle via a consecutive window of 3 over `rotation_order` (a mixed upper/core/lower
    ordering), advancing one group per day. Every day therefore features 3 DISTINCT, balanced groups,
    and the window walks all 6 groups over 6 days.
  - Within each featured group, the specific exercise is chosen by the day index modulo the group size,
    so a group revisited a few days later surfaces a different movement.

Data source: $MINERU_BRIEFS_ROOT/briefs_exercise/exercises.json (private user data; shape in
engine/config/exercises.example.json). An optional top-level `library_note` string is printed as the
message footer (e.g. where the human-readable library lives).
Output: renders a collage PNG and sends it as a Telegram photo (caption = the exercise text) via
scripts/telegram_send_photo.py; falls back to plain text via scripts/deliver-output.py if the collage
path fails. Also archives briefs_exercise/exercise-YYYY-MM-DD.md (and .png).

Environment:
  MINERU_HOME          workspace root (default ~/.mineru)
  MINERU_BRIEFS_ROOT   parent of briefs_exercise/ (default $MINERU_HOME); `frames` paths resolve against it
  TZ                   the user's timezone decides "today" (launchd stamps it from the profile)

Usage:
  daily-exercise-suggestion.py                # today (PT): write + deliver (skips if already delivered)
  daily-exercise-suggestion.py --force        # re-deliver even if today's file exists
  daily-exercise-suggestion.py --dry-run      # print the message only; no file, no delivery
  daily-exercise-suggestion.py --date 2026-09-01 [--dry-run]   # a specific date (for testing rotation)
  daily-exercise-suggestion.py --collage-sample /path/out.png [--date YYYY-MM-DD]
                                              # compose the collage PNG only; DO NOT deliver anything

Collage feature (v1, LIVE):
  Every exercise in exercises.json carries a `frames` field with 3 cached JPEGs pulled from its
  source reel (see scripts/exercise_frame_select.py). At noon the job renders a stacked 3xN grid
  (one row per exercise: black label bar + 3 frames) and sends it as a Telegram photo. If the collage
  or the send fails (e.g. a newly-added exercise has no cached frames yet), it falls back to plain-text
  delivery so the notification never silently no-ops. `--collage-sample PATH` still renders the PNG
  only (no delivery) for review.
"""

import argparse
import datetime
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List

WORKSPACE = Path(os.environ.get("MINERU_HOME") or (Path.home() / ".mineru")).expanduser()
BRIEFS_ROOT = Path(os.environ.get("MINERU_BRIEFS_ROOT") or WORKSPACE).expanduser()
OUTPUT_DIR = BRIEFS_ROOT / "briefs_exercise"
DATASET_PATH = OUTPUT_DIR / "exercises.json"
SCRIPTS_DIR = Path(__file__).resolve().parent
DELIVER_SCRIPT = SCRIPTS_DIR / "deliver-output.py"

EXERCISES_PER_DAY = 3


def load_dataset() -> dict:
    """Load and lightly validate the exercise dataset."""
    with open(DATASET_PATH) as dataset_file:
        data = json.load(dataset_file)
    for required_key in ("groups", "rotation_order", "exercises"):
        if required_key not in data:
            raise ValueError(f"exercises.json missing required key: {required_key}")
    # Every group in the rotation must have at least one exercise, else the daily
    # pick would divide by zero when indexing into an empty group.
    groups_with_exercises = {exercise["group"] for exercise in data["exercises"]}
    empty_groups = [g for g in data["rotation_order"] if g not in groups_with_exercises]
    if empty_groups:
        raise ValueError(f"rotation_order references groups with no exercises: {empty_groups}")
    return data


def pick_exercises_for_day(data: dict, day_index: int) -> List[dict]:
    """Deterministically select one exercise from each of 3 rotating, distinct body groups.

    day_index is a monotonically increasing integer (a date's proleptic ordinal), so the same
    date always yields the same picks and consecutive dates rotate the featured groups by one.
    """
    rotation_order = data["rotation_order"]
    group_count = len(rotation_order)
    by_group = {group["key"]: [] for group in data["groups"]}
    for exercise in data["exercises"]:
        by_group[exercise["group"]].append(exercise)

    label_for = {group["key"]: group["label"] for group in data["groups"]}
    start = day_index % group_count
    picks = []
    # A library with fewer groups than EXERCISES_PER_DAY shows each group once, never a repeat.
    for offset in range(min(EXERCISES_PER_DAY, group_count)):
        group_key = rotation_order[(start + offset) % group_count]
        group_exercises = by_group[group_key]
        chosen = group_exercises[day_index % len(group_exercises)]
        picks.append({**chosen, "group_label": label_for[group_key]})
    return picks


def render_message(picks: List[dict], target_date: datetime.date, library_note: str = "") -> str:
    """Build the Telegram-friendly markdown message (bold group + move header, dose, one-line how)."""
    pretty_date = target_date.strftime("%a, %b %-d")
    lines = [
        f"# 🏋️ Daily Movement — {pretty_date}",
        "",
        "3 for today, each from a different area. Easy pace, a few minutes each.",
        "",
    ]
    for pick in picks:
        lines.append(f"**{pick['group_label']} — {pick['name']}** ({pick['dose']})")
        lines.append(pick["how"])
        lines.append("")
    lines.append(f"_{library_note}_" if library_note else "_From your exercise library._")
    return "\n".join(lines)


def build_caption(picks: List[dict]) -> str:
    """Minimal collage caption: just the one-sentence 'how' cue per exercise, in collage order.

    The collage image already carries each move's group, name, and dose in its label bar, so the
    caption doesn't repeat them — it adds only the descriptive cue the image can't show.
    """
    return "\n\n".join(pick["how"] for pick in picks)


def deliver(markdown_path: Path) -> None:
    """Hand the written brief to the standard delivery helper (Telegram + web-app feed)."""
    subprocess.run(["python3", str(DELIVER_SCRIPT), str(markdown_path)], check=True)


def render_collage_for_picks(picks: List[dict], out_path: Path) -> Path:
    """Compose the day's exercise collage PNG at `out_path` and return it.

    The exercise_collage module is loaded lazily so the text-only default path
    doesn't pay the Pillow import cost. Each pick must already have a `frames`
    field (populated by the one-shot backfill in scripts/exercise_frame_select).
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    exercise_collage = importlib.import_module("exercise_collage")

    missing = [p["name"] for p in picks if not p.get("frames")]
    if missing:
        raise ValueError(f"exercises missing `frames` field (run the backfill): {missing}")

    items = []
    for pick in picks:
        items.append({
            "name": pick["name"],
            "dose": pick["dose"],
            "group_label": pick.get("group_label", ""),
            # `frames` is stored as "briefs_exercise/frames/..."; resolve against the briefs root.
            "frames": [str(BRIEFS_ROOT / rel) for rel in pick["frames"]],
        })
    return exercise_collage.compose_collage(items, out_path)


def deliver_collage(picks: List[dict], target_date: datetime.date) -> bool:
    """Render the day's collage and send it as a Telegram photo with a minimal cue caption.

    Returns True if the photo was delivered, False on any failure so the caller can fall back to
    plain-text delivery. The collage png is archived next to the markdown (exercise-YYYY-MM-DD.png).
    The sibling send helper is imported lazily to keep the module's import surface light for the
    non-delivery code paths. Caption is plain text (no parse_mode) — see build_caption.
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    telegram_send_photo = importlib.import_module("telegram_send_photo")

    png_path = OUTPUT_DIR / f"exercise-{target_date.isoformat()}.png"
    render_collage_for_picks(picks, png_path)
    caption = build_caption(picks)
    return telegram_send_photo.send_photo(png_path, caption, parse_mode=None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily exercise suggestion (noon).")
    parser.add_argument("--date", help="YYYY-MM-DD override (default: today in the local timezone, TZ).")
    parser.add_argument("--dry-run", action="store_true", help="Print the message only; no file, no delivery.")
    parser.add_argument("--force", action="store_true", help="Re-deliver even if today's file already exists.")
    parser.add_argument(
        "--collage-sample",
        type=Path,
        metavar="PATH",
        help=(
            "Compose the day's exercise collage PNG and write it to PATH. "
            "Does NOT deliver anything; used to review the collage before it's wired live."
        ),
    )
    args = parser.parse_args()

    target_date = (
        datetime.date.fromisoformat(args.date)
        if args.date
        else datetime.date.today()
    )

    data = load_dataset()
    picks = pick_exercises_for_day(data, target_date.toordinal())
    message = render_message(picks, target_date, str(data.get("library_note") or ""))

    # Collage-sample mode: write the PNG for review, nothing else. Bypasses both
    # the dry-run print and the delivery pipeline so a scripted sample run has
    # zero side effects beyond the PNG file.
    if args.collage_sample:
        out_path = render_collage_for_picks(picks, args.collage_sample)
        print(f"Wrote collage {out_path} for {target_date.isoformat()}")
        return

    if args.dry_run:
        print(message)
        return

    output_path = OUTPUT_DIR / f"exercise-{target_date.isoformat()}.md"
    if output_path.exists() and not args.force:
        print(f"Already delivered for {target_date.isoformat()} ({output_path}); use --force to resend.", file=sys.stderr)
        return

    output_path.write_text(message)

    # Primary delivery: the visual collage as a Telegram photo, exercise text as the caption.
    # Any failure in the collage/photo path (missing frames for a newly-added exercise, a render
    # error, a Telegram error) falls back to plain-text delivery so noon never silently no-ops.
    try:
        if deliver_collage(picks, target_date):
            print(f"Delivered collage photo for {target_date.isoformat()}")
            return
        print("collage photo send failed; falling back to text delivery.", file=sys.stderr)
    except Exception as collage_error:
        print(f"collage delivery errored ({collage_error}); falling back to text delivery.", file=sys.stderr)

    deliver(output_path)
    print(f"Delivered {output_path} (text fallback)")


if __name__ == "__main__":
    main()
