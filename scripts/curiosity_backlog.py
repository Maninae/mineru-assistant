#!/usr/bin/env python3
"""Backlog gate for the daily curiosity question: decide, in plain code, whether to ask today.

    curiosity_backlog.py decide --workspace <MINERU_HOME>

Prints one line for the trigger script: `<action> <open> <pending> <threshold> <transition>`.

- Open questions are `briefs_curiosity/question-*.md` files whose first line carries the
  UNANSWERED marker; pending ones carry "ANSWERED · to file" (answered in the web app, not yet
  filed into memory). Only line 1 is read, because answer notes may contain these words.
- The limit comes from `config/curiosity.json` (`pause_at_open_backlog`); missing, 0, or
  negative means the gate is off and the job always runs. MINERU_CURIOSITY_PAUSE_AT overrides.
- At or above the limit the job asks nothing: `skip`, or `file-only` when answers are waiting.
- The state file doubles as the job's sign of life on paused days: the health watchdog and the
  web app judge this job by the newest file in `briefs_curiosity/`.

Python 3.9-compatible, stdlib only (launchd runs it with /usr/bin/python3).
"""
import argparse
import datetime
import json
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

BRIEFS_DIRNAME = "briefs_curiosity"
QUESTION_GLOB = "question-*.md"
STATE_FILENAME = "backlog-pause-state.json"
CONFIG_RELATIVE_PATH = Path("config") / "curiosity.json"
CONFIG_KEY = "pause_at_open_backlog"
THRESHOLD_ENV_VAR = "MINERU_CURIOSITY_PAUSE_AT"
OPEN_MARKER = "UNANSWERED"
PENDING_MARKER = "ANSWERED · to file"
GATE_OFF = 0


class GateAction(str, Enum):
    """What the trigger script should do today."""

    RUN = "run"
    SKIP = "skip"
    FILE_ONLY = "file-only"


class PauseTransition(str, Enum):
    """Whether today's decision starts or ends a pause (drives the one-line notices)."""

    NONE = "none"
    PAUSED = "paused"
    RESUMED = "resumed"


def read_first_line(question_path: Path) -> str:
    """First line of a question file, or "" when it cannot be read."""
    try:
        with question_path.open(encoding="utf-8", errors="replace") as question_file:
            return question_file.readline()
    except OSError:
        return ""


def count_open_and_pending(briefs_dir: Path) -> Tuple[int, int]:
    """(open, pending-filing) question counts from each file's status line."""
    open_count = 0
    pending_count = 0
    for question_path in sorted(briefs_dir.glob(QUESTION_GLOB)):
        status_line = read_first_line(question_path)
        if OPEN_MARKER in status_line:
            open_count += 1
        elif PENDING_MARKER in status_line:
            pending_count += 1
    return open_count, pending_count


def load_pause_threshold(workspace: Path, environ: Dict[str, str]) -> int:
    """Open-backlog limit: env override, else the config file, else off.

    A malformed value raises: a typo must not silently turn the gate off.
    """
    raw_override = environ.get(THRESHOLD_ENV_VAR, "").strip()
    if raw_override:
        return int(raw_override)
    config_path = workspace / CONFIG_RELATIVE_PATH
    if not config_path.exists():
        return GATE_OFF
    config = json.loads(config_path.read_text(encoding="utf-8"))
    threshold = config.get(CONFIG_KEY, GATE_OFF)
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise ValueError("%s: %s must be a whole number, got %r" % (config_path, CONFIG_KEY, threshold))
    return threshold


def choose_action(open_count: int, pending_count: int, threshold: int) -> GateAction:
    """Run below the limit; at or above it, file waiting answers or do nothing."""
    if threshold <= GATE_OFF or open_count < threshold:
        return GateAction.RUN
    return GateAction.FILE_ONLY if pending_count > 0 else GateAction.SKIP


def read_was_paused(state_path: Path) -> bool:
    """Whether the previous decision was a pause; a missing or unreadable state file is "no"."""
    try:
        return bool(json.loads(state_path.read_text(encoding="utf-8")).get("paused", False))
    except (OSError, ValueError, AttributeError):
        return False


def decide_and_record(workspace: Path, environ: Dict[str, str], now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Count the backlog, choose today's action, and record pause state.

    Returns:
        {"action", "open", "pending", "threshold", "transition"}.

    - The state file is written only while the gate is on, so an install without a limit
      gets no extra file.
    - Writing it on every gated run is what keeps the job looking alive on paused days.
    """
    briefs_dir = workspace / BRIEFS_DIRNAME
    threshold = load_pause_threshold(workspace, environ)
    open_count, pending_count = count_open_and_pending(briefs_dir)
    action = choose_action(open_count, pending_count, threshold)
    paused_now = action != GateAction.RUN
    transition = PauseTransition.NONE
    if threshold > GATE_OFF:
        state_path = briefs_dir / STATE_FILENAME
        was_paused = read_was_paused(state_path)
        if paused_now and not was_paused:
            transition = PauseTransition.PAUSED
        elif was_paused and not paused_now:
            transition = PauseTransition.RESUMED
        briefs_dir.mkdir(parents=True, exist_ok=True)
        checked_at = (now or datetime.datetime.now()).isoformat(timespec="seconds")
        state_path.write_text(json.dumps({
            "paused": paused_now,
            "open": open_count,
            "pending_filing": pending_count,
            "threshold": threshold,
            "checked_at": checked_at,
        }, indent=2) + "\n", encoding="utf-8")
    return {
        "action": action.value,
        "open": open_count,
        "pending": pending_count,
        "threshold": threshold,
        "transition": transition.value,
    }


def main() -> int:
    """CLI entry: print the one-line decision for the trigger script."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    decide_parser = subparsers.add_parser("decide", help="decide whether to ask today and record pause state")
    decide_parser.add_argument("--workspace", required=True, type=Path)
    arguments = parser.parse_args()
    decision = decide_and_record(arguments.workspace, dict(os.environ))
    print("%(action)s %(open)d %(pending)d %(threshold)d %(transition)s" % decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
