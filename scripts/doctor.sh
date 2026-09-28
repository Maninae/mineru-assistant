#!/bin/bash
# doctor.sh — /doctor handler: isolated diagnostic CC session, report to Telegram.
#
# Invoked DETACHED by the Landline daemon (doctor_script in landline.json).
#   $1 (optional): the operator's issue text from `/doctor <text>`.
#
# Isolation: the CC session runs from "$MINERU_HOME-doctor", a bare directory
# OUTSIDE the workspace, so it inherits no Mineru persona/memory CLAUDE.md
# (Claude Code loads every ancestor CLAUDE.md, which is why a subdir of
# $MINERU_HOME would not work). Its instructions live in prompts/DOCTOR.md.
set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

ISSUE="${1:-}"
setup_logging "doctor"

DOCTOR_HOME="${MINERU_HOME:-$HOME/.mineru}-doctor"
mkdir -p "$DOCTOR_HOME"

REPORT="$WORKSPACE/briefs_doctor/doctor-$(date '+%Y-%m-%d_%H-%M-%S').md"
mkdir -p "$WORKSPACE/briefs_doctor"

PROMPT="Read $WORKSPACE/prompts/DOCTOR.md and follow it exactly. Write your report to: $REPORT"
if [ -n "$ISSUE" ]; then
  PROMPT="$PROMPT

The operator's issue report (investigate this first): $ISSUE"
else
  PROMPT="$PROMPT

No specific issue was reported: run the general health check described in DOCTOR.md."
fi

echo "$(date): Doctor dispatched (issue text: ${#ISSUE} chars). Report target: $REPORT"

rc=0
(cd "$DOCTOR_HOME" && "$CC_BIN" --permission-mode bypassPermissions --model claude-opus-5-5 --verbose --print "$PROMPT") || rc=$?

echo "$(date): CC exited with code $rc"

if [ "$rc" -ne 0 ]; then
  cc_alert "doctor" "$rc" "CC exited nonzero"
elif [ ! -f "$REPORT" ]; then
  cc_alert "doctor" "$rc" "expected output not produced: $REPORT"
  rc=64
else
  $DELIVER_BIN "$REPORT"
fi

echo "$(date): Done (final rc=$rc)"
exit $rc
