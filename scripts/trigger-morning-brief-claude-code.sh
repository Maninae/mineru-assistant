#!/bin/bash
# trigger-morning-brief-claude-code.sh
# Runs the morning briefing via Claude Code (headless, bypassPermissions).
# Called by launchd at 7:00 AM daily.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

idempotent_guard "briefs_morning/morning-$(date '+%Y-%m-%d').md"

# Pre-warm the firewall's Ollama model in the background: after the quiet
# overnight window it's cold (a multi-GB model load), and the first Gmail screen
# of the day otherwise eats the load time inside the brief's critical path.
curl -sS -m 120 http://localhost:11434/api/chat \
  -d '{"model": "qwen3:8b", "messages": [{"role": "user", "content": "ok"}], "think": false, "keep_alive": "2h", "stream": false}' \
  >/dev/null 2>&1 &

run_cc_job "morning-brief" "claude-opus-5-5" "recurring/morning-brief.md" \
  "briefs_morning/morning-*.md"
