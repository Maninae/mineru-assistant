"""Guards the daily exercise rotation in scripts/daily-exercise-suggestion.py.

Invariants: picks are deterministic per date, each day's picks come from
distinct groups, the shipped example library validates, and --dry-run
reads the library from MINERU_BRIEFS_ROOT and writes nothing.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "daily-exercise-suggestion.py"
EXAMPLE_LIBRARY_PATH = REPO_ROOT / "engine" / "config" / "exercises.example.json"

spec = importlib.util.spec_from_file_location("daily_exercise_suggestion", SCRIPT_PATH)
daily_exercise_suggestion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily_exercise_suggestion)


def six_group_library() -> dict:
    keys = ["a", "b", "c", "d", "e", "f"]
    return {
        "groups": [{"key": k, "label": k.upper()} for k in keys],
        "rotation_order": keys,
        "exercises": [
            {"name": f"{k}{n}", "group": k, "dose": "1 min", "how": "move"}
            for k in keys for n in range(2)
        ],
    }


def test_picks_are_deterministic_and_from_distinct_groups():
    library = six_group_library()
    for day in range(738000, 738012):
        first = daily_exercise_suggestion.pick_exercises_for_day(library, day)
        again = daily_exercise_suggestion.pick_exercises_for_day(library, day)
        assert first == again
        assert len({pick["group"] for pick in first}) == 3


def test_consecutive_days_rotate_groups():
    library = six_group_library()
    today = [p["group"] for p in daily_exercise_suggestion.pick_exercises_for_day(library, 738000)]
    tomorrow = [p["group"] for p in daily_exercise_suggestion.pick_exercises_for_day(library, 738001)]
    assert today[1:] == tomorrow[:2]


def test_example_library_has_no_repeat_with_two_groups():
    library = json.loads(EXAMPLE_LIBRARY_PATH.read_text())
    picks = daily_exercise_suggestion.pick_exercises_for_day(library, 738000)
    assert sorted(p["group"] for p in picks) == sorted(library["rotation_order"])


def test_dry_run_reads_briefs_root_and_writes_nothing(tmp_path):
    exercise_dir = tmp_path / "briefs" / "briefs_exercise"
    exercise_dir.mkdir(parents=True)
    (exercise_dir / "exercises.json").write_text(EXAMPLE_LIBRARY_PATH.read_text())
    env = dict(os.environ, MINERU_HOME=str(tmp_path), MINERU_BRIEFS_ROOT=str(tmp_path / "briefs"))
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dry-run", "--date", "2026-01-05"],
        capture_output=True, text=True, env=env, check=True,
    )
    assert "Wall Angels" in result.stdout and "Dead Bug" in result.stdout
    assert "From your exercise library." in result.stdout
    assert sorted(p.name for p in exercise_dir.iterdir()) == ["exercises.json"]
