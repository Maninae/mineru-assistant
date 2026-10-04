#!/bin/bash
# trigger-memory-description-claude-code.sh
# Runs the memory description maintenance via Claude Code.
# Called by launchd at 1:30 AM daily.
# Job touches memory frontmatter — no single brief file, so no expected-output check.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

run_cc_job "memory-description" "claude-sonnet-5-5" "recurring/memory-description-maintenance.md"
