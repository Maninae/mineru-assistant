#!/bin/bash
# trigger-news-brief-claude-code.sh
# Runs the daily local news brief via Claude Code.
# Called by launchd at 11:00 AM daily.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

run_cc_job "news-brief" "claude-sonnet-5-5" "recurring/news-brief.md" \
  "briefs_news/briefs/*.md"
