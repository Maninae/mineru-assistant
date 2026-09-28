#!/bin/bash
# trigger-model-watch-claude-code.sh
# Weekly audit of recurring-job model assignments against Anthropic's current
# Claude lineup. Called by launchd Saturday 9:00 AM. The job always writes a
# brief (a one-liner when nothing changed, a full report otherwise); the
# instruction file decides whether to deliver it to Telegram.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

run_cc_job "model-watch" "claude-sonnet-5" "recurring/model-watch.md" \
  "briefs_model_watch/model-watch-*.md"
