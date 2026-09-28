#!/usr/bin/env python3
"""Extract Claude Code session transcripts into memory/daily/ fragments.

Replaces the OpenClaw session-memory-daily hook. Designed to run as a
pre-step in the daily consolidation trigger, similar to journal export.

Scans CC session JSONL files modified since the last extraction,
extracts user/assistant messages, and writes fragments to memory/daily/.

Usage:
  python3 scripts/extract-cc-sessions.py [--since YYYY-MM-DD] [--dry-run]

Default --since: yesterday (PT).
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

WORKSPACE = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))).expanduser()
MEMORY_DAILY = WORKSPACE / "memory" / "daily"
# Claude Code names each project dir after the workspace's absolute path with
# every "/" and "." replaced by "-" (e.g. /Users/alice/.mineru ->
# -Users-alice--mineru). Derive it from MINERU_HOME so this tracks whatever
# workspace the CLI runs from rather than a hardcoded install path.
_MINERU_HOME = os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))
_CC_PROJECT_SLUG = _MINERU_HOME.replace("/", "-").replace(".", "-")
CC_PROJECT_DIRS = [
    Path.home() / ".claude" / "projects" / _CC_PROJECT_SLUG,
]
PT = ZoneInfo("America/Los_Angeles")


def parse_args():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    yesterday = (datetime.now(PT) - timedelta(days=1)).strftime("%Y-%m-%d")
    p.add_argument("--since", default=yesterday, help="Extract sessions modified on or after this date (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true", help="Print what would be written without writing")
    return p.parse_args()


def extract_messages(jsonl_path: Path) -> List[dict]:
    """Extract user/assistant text messages from a CC session JSONL."""
    messages = []
    with open(jsonl_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
            if entry_type not in ("user", "assistant"):
                continue

            msg = entry.get("message", {})
            content = msg.get("content", "")

            if isinstance(content, list):
                text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                text = "\n".join(t for t in text_parts if t)
            elif isinstance(content, str):
                text = content
            else:
                continue

            if not text or text.startswith("/"):
                continue

            # Skip tool-use-only assistant turns
            if entry_type == "assistant" and not text.strip():
                continue

            messages.append({
                "role": msg.get("role", entry_type),
                "text": text,
            })

    return messages


def session_date(jsonl_path: Path) -> str:
    """Get the date (PT) of a session from its modification time."""
    mtime = jsonl_path.stat().st_mtime
    dt = datetime.fromtimestamp(mtime, tz=PT)
    return dt.strftime("%Y-%m-%d")


def session_time(jsonl_path: Path) -> str:
    """Get the time (PT) of a session from its modification time."""
    mtime = jsonl_path.stat().st_mtime
    dt = datetime.fromtimestamp(mtime, tz=PT)
    return dt.strftime("%H-%M-%S")


def already_extracted(session_id: str) -> bool:
    """Check if this session was already extracted (by looking for its ID in existing fragments)."""
    for f in MEMORY_DAILY.glob("*.md"):
        try:
            head = f.read_text()[:500]
            if session_id in head:
                return True
        except Exception:
            continue
    return False


def main():
    args = parse_args()
    since_date = args.since

    MEMORY_DAILY.mkdir(parents=True, exist_ok=True)

    # Scan the workspace's CC project dir (slug derived from $MINERU_HOME)
    since_ts = datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=PT).timestamp()
    session_files: List[Path] = []
    for proj_dir in CC_PROJECT_DIRS:
        if proj_dir.exists():
            session_files.extend(proj_dir.glob("*.jsonl"))
    session_files.sort(key=lambda p: p.stat().st_mtime)

    if not session_files:
        print("No CC project dirs found", file=sys.stderr)
        sys.exit(1)

    candidates = [f for f in session_files if f.stat().st_mtime >= since_ts]
    print(f"Found {len(candidates)} session(s) modified since {since_date}")

    extracted = 0
    for sf in candidates:
        session_id = sf.stem
        date = session_date(sf)
        time = session_time(sf)

        if already_extracted(session_id):
            print(f"  Skip {session_id[:12]}... (already extracted)")
            continue

        messages = extract_messages(sf)
        if len(messages) < 3:
            print(f"  Skip {session_id[:12]}... ({len(messages)} messages, too short)")
            continue

        # Truncate to last 100 messages to keep fragments manageable
        messages = messages[-100:]

        fragment_name = f"{date}_{time}_cc.md"
        fragment_path = MEMORY_DAILY / fragment_name

        content_lines = [
            f"# Session: {date} (Claude Code)",
            "",
            f"- **Session ID**: {session_id}",
            f"- **Source**: claude-code",
            f"- **Messages**: {len(messages)}",
            "",
            "## Conversation",
            "",
        ]
        for m in messages:
            content_lines.append(f"{m['role']}: {m['text']}")
            content_lines.append("")

        content = "\n".join(content_lines)

        if args.dry_run:
            print(f"  Would write {fragment_name} ({len(content)} chars, {len(messages)} msgs)")
        else:
            fragment_path.write_text(content, encoding="utf-8")
            print(f"  Wrote {fragment_name} ({len(content)} chars, {len(messages)} msgs)")
            extracted += 1

    print(f"\nExtracted {extracted} session(s)")


if __name__ == "__main__":
    main()
