#!/bin/bash
# trigger-curiosity-question-claude-code.sh
# Runs the daily curiosity question via Claude Code.
# Called by launchd at 11:30 AM daily.
#
# Backlog gate (scripts/curiosity_backlog.py): while the open-question backlog is at or
# above the operator's limit (config/curiosity.json), no question is asked. Answers typed
# into the web app are still filed by a short filing-only run. One Telegram line marks the
# day a pause starts and the day it ends.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

JOB_NAME="curiosity-question"
# Opus 5.5 at the global xhigh effort level: it must verify a gap is real before asking.
JOB_MODEL="claude-opus-5-5"

idempotent_guard "briefs_curiosity/question-$(date '+%Y-%m-%d').md"

if ! gate_decision=$(/usr/bin/python3 "$(dirname "$0")/curiosity_backlog.py" decide --workspace "$WORKSPACE"); then
  cc_alert "$JOB_NAME" 1 "backlog gate failed"
  exit 1
fi
read -r gate_action open_count pending_count pause_threshold pause_transition <<GATE
$gate_decision
GATE
echo "[backlog gate] action=$gate_action open=$open_count pending_filing=$pending_count limit=$pause_threshold transition=$pause_transition"

# DELIVER_BIN may be a multi-word command.
# shellcheck disable=SC2086
case "$pause_transition" in
  paused)
    $DELIVER_BIN --raw "⏸️ Curiosity questions paused: $open_count open (limit $pause_threshold). Answers you add still get filed. Questions resume once fewer than $pause_threshold are open." 2>/dev/null || true ;;
  resumed)
    $DELIVER_BIN --raw "▶️ Curiosity questions resume: the backlog is down to $open_count open (limit $pause_threshold)." 2>/dev/null || true ;;
esac

case "$gate_action" in
  skip)
    echo "[backlog gate] paused and nothing to file; no run today."
    exit 0 ;;
  file-only)
    run_cc_job "$JOB_NAME" "$JOB_MODEL" "recurring/curiosity-file-pending-answers.md" \
      "briefs_curiosity/question-*.md" ;;
  run)
    run_cc_job "$JOB_NAME" "$JOB_MODEL" "recurring/daily-curiosity-question.md" \
      "briefs_curiosity/question-*.md" ;;
  *)
    cc_alert "$JOB_NAME" 1 "backlog gate returned unknown action '$gate_action'"
    exit 1 ;;
esac
