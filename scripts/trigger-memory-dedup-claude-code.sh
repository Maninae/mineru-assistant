#!/bin/bash
# trigger-memory-dedup-claude-code.sh
# Runs the memory deduplication scan via Claude Code.
# Called by launchd on the 1st, 11th, and 21st at 10:30 PM.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

run_cc_job "memory-dedup" "claude-opus-5-5" "recurring/memory-dedup.md" \
  "briefs_dedup/dedup-*.md"
