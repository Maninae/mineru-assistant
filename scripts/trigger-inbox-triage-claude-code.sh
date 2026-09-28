#!/bin/bash
# trigger-inbox-triage-claude-code.sh
# Runs inbox triage via Claude Code.
# Called by launchd Sundays at 2:00 PM.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

idempotent_guard "briefs_inbox/triage-$(date '+%Y-%m-%d').md"
run_cc_job "inbox-triage" "claude-sonnet-5" "recurring/inbox-triage.md" \
  "briefs_inbox/triage-*.md"
