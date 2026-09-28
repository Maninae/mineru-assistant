#!/usr/bin/env python3
"""context-usage.py — Report token usage for the current Claude Code session.

Reads the session's JSONL conversation file and sums token counts from
assistant message usage metadata. Reports total input context, output tokens,
cache efficiency, and percentage of context window used.

Usage: python3 scripts/context-usage.py [session_id]
  If no session_id, uses the most recently modified .jsonl in the project dir.
"""

import json
import os
import sys
from pathlib import Path

# Claude Code names each project dir after the workspace's absolute path with
# every "/" and "." replaced by "-" (e.g. /Users/alice/.mineru ->
# -Users-alice--mineru). Derive it from MINERU_HOME rather than hardcoding.
_MINERU_HOME = os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))
_CC_PROJECT_SLUG = _MINERU_HOME.replace("/", "-").replace(".", "-")
PROJECT_DIR = Path.home() / ".claude" / "projects" / _CC_PROJECT_SLUG
CONTEXT_WINDOW = 1_000_000  # 1M-context models

def find_session(session_id=None):
    if session_id:
        p = PROJECT_DIR / f"{session_id}.jsonl"
        if p.exists():
            return p
        print(f"Session {session_id} not found", file=sys.stderr)
        sys.exit(1)
    # Most recent
    jsonls = sorted(PROJECT_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not jsonls:
        print("No session files found", file=sys.stderr)
        sys.exit(1)
    return jsonls[0]

def analyze(path):
    turns = 0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0
    last_input = 0
    model = "unknown"

    with open(path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            msg = entry.get("message", {})
            usage = msg.get("usage", {})
            if not usage:
                continue

            turns += 1
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_create = usage.get("cache_creation_input_tokens", 0)

            total_input += inp
            total_output += out
            total_cache_read += cache_read
            total_cache_create += cache_create
            last_input = inp + cache_read + cache_create
            model = msg.get("model", model)

    # The last turn's input context is the best measure of current context size
    # (it includes the full conversation history sent to the API)
    context_used = last_input
    pct = (context_used / CONTEXT_WINDOW) * 100 if CONTEXT_WINDOW else 0

    # Cache hit rate across all turns
    total_cache_ops = total_cache_read + total_cache_create
    cache_hit_rate = (total_cache_read / total_cache_ops * 100) if total_cache_ops else 0

    if pct < 30:
        status = "fresh"
        emoji = "🟢"
    elif pct < 60:
        status = "moderate"
        emoji = "🟡"
    elif pct < 85:
        status = "heavy"
        emoji = "🟠"
    else:
        status = "near limit"
        emoji = "🔴"

    print(f"Model: {model}")
    print(f"Turns: {turns}")
    print(f"Context: {context_used:,} / {CONTEXT_WINDOW:,} tokens ({pct:.1f}%) {emoji} {status}")
    print(f"Output (session total): {total_output:,} tokens")
    print(f"Cache hit rate: {cache_hit_rate:.0f}%")
    print(f"Session file: {path.name}")

if __name__ == "__main__":
    session_id = sys.argv[1] if len(sys.argv) > 1 else None
    path = find_session(session_id)
    analyze(path)
