#!/bin/bash
# trigger-morning-brief-claude-code.sh
# Runs the morning briefing via Claude Code (headless, bypassPermissions).
# Called by launchd at 7:00 AM daily.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

idempotent_guard "briefs_morning/morning-$(date '+%Y-%m-%d').md"

run_cc_job "morning-brief" "claude-opus-5-5" "recurring/morning-brief.md" \
  "briefs_morning/morning-*.md"
