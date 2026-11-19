#!/bin/bash
# run-pre-export-journals.sh [limit]
# Exports the newest Apple Notes journals to journal_exports/ (scripts/export-journals.py).
# Plain Python, no LLM. Called by launchd shortly before the nightly consolidation.
#
# - bash stays the job's main program and python runs as a child: under launchd the
#   TCC grant that lets a job read the Notes store follows the job's main program, and
#   /usr/bin/python3 (Apple-signed, stable signature) is the reader that holds it.
# - A non-zero exit sends one Telegram alert (cc_alert); a hang is cut off at
#   CC_JOB_STEP_TIMEOUT_SECONDS and alerts from inside run_with_timeout.
# - Output stays on stdout/stderr, which the plist captures in logs/pre-export-journals/.

set -uo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

JOB_NAME="pre-export-journals"
EXPORT_LIMIT="${1:-30}"

paused_until_guard "$JOB_NAME"
cd "$WORKSPACE"

rc=0
run_with_timeout "$CC_JOB_STEP_TIMEOUT_SECONDS" "$JOB_NAME" -- \
  /usr/bin/python3 "$WORKSPACE/scripts/export-journals.py" "$EXPORT_LIMIT" || rc=$?
if [ "$rc" -ne 0 ] && [ "${RUN_WITH_TIMEOUT_FIRED:-0}" -eq 0 ]; then
  cc_alert "$JOB_NAME" "$rc" "journal export exited nonzero"
fi
exit $rc
