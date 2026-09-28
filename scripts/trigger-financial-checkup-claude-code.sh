#!/bin/bash
# trigger-financial-checkup-claude-code.sh
# Runs the MONTHLY financial checkup via Claude Code.
# launchd fires this every Sunday at 9:00 PM (Weekday=0); the guard below
# no-ops on all but the FIRST Sunday of the month, giving an effective monthly
# cadence. (launchd's StartCalendarInterval can't express "first Sunday", and
# Day + Weekday in one entry OR together rather than AND, so the guard is the
# clean way to pin "first Sunday of the month".)

set -euo pipefail

# First-Sunday-of-month guard: proceed only when day-of-month is 1-7. The 10#
# prefix forces base-10 so a zero-padded day like "08" is not parsed as octal.
DOM=$((10#$(date +%d)))
if [ "$DOM" -gt 7 ]; then
  echo "Not the first Sunday of the month (day-of-month $DOM) — skipping monthly financial checkup."
  exit 0
fi

. "$(dirname "$0")/cc-job-lib.sh"

run_cc_job "financial-checkup" "claude-opus-5-5" "recurring/financial-checkup.md" \
  "briefs_financial/monthly-*.md"
