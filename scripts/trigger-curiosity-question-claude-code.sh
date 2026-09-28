#!/bin/bash
# trigger-curiosity-question-claude-code.sh
# Runs the daily curiosity question via Claude Code.
# Called by launchd at 11:30 AM daily.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

# Runs at the global xhigh effort level (no per-job max-thinking override).
# Opus 5.5 — must reason hard + verify a gap is real before asking.
idempotent_guard "briefs_curiosity/question-$(date '+%Y-%m-%d').md"
run_cc_job "curiosity-question" "claude-opus-5-5" "recurring/daily-curiosity-question.md" \
  "briefs_curiosity/question-*.md"
