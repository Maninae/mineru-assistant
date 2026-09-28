#!/bin/bash
# trigger-prompts-alignment-claude-code.sh
# Runs the prompts & skills alignment check via Claude Code.
# Called by launchd Tue/Fri at 10:00 PM.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

run_cc_job "prompts-alignment" "claude-opus-5-5" "recurring/prompts-alignment.md" \
  "briefs_prompts_alignment/routine-*.md"
