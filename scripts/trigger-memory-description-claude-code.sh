#!/bin/bash
# trigger-memory-description-claude-code.sh
# Runs the memory description maintenance via Claude Code.
# Called by launchd at 1:30 AM daily.
# Job touches memory frontmatter — no single brief file, so no expected-output check.
# A marker touched on success stands in for a brief: the DST fall-back night has two
# 1:30 AMs, and the guard turns the second launchd fire into a no-op.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

COMPLETION_MARKER="cache/job-markers/memory-description.done"
idempotent_guard "$COMPLETION_MARKER"

run_cc_job "memory-description" "claude-sonnet-5-5" "recurring/memory-description-maintenance.md"
mkdir -p "$WORKSPACE/cache/job-markers"
touch "$WORKSPACE/$COMPLETION_MARKER"
