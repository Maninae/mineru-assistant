#!/bin/bash
# trigger-daily-transactions-claude-code.sh
# Runs the WEEKLY transactions recap via Claude Code (scans the prior complete
# Mon-Sun week; also runs the weekly credential auth-watch). Name kept as
# "daily-transactions" for plist/dir stability. Called by launchd Thursdays 10 PM.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

run_cc_job "daily-transactions" "claude-sonnet-5-5" "recurring/daily-transactions.md" \
  "briefs_financial/daily-*.md"
