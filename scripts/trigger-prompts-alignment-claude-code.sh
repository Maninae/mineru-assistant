#!/bin/bash
# trigger-prompts-alignment-claude-code.sh
# Runs the prompts & skills alignment check via Claude Code.
# Called by launchd Tue/Fri at 10:00 PM.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

# The longest-running job (median ~10 min, observed max 22 min): 90 min instead
# of the lib's 60 so a slow alignment pass is never killed as a hang.
CC_JOB_TIMEOUT_SECONDS=5400

run_cc_job "prompts-alignment" "claude-opus-5-5" "recurring/prompts-alignment.md" \
  "briefs_prompts_alignment/routine-*.md"
