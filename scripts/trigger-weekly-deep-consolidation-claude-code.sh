#!/bin/bash
# trigger-weekly-deep-consolidation-claude-code.sh
# Weekly deep consolidation via Claude Code.
# Called by launchd Sunday nights at 11:00 PM.
#
# Uses a custom prompt (no "Read X and execute the job." pattern), so we don't
# call run_cc_job — but we do use the shared lib for log setup, retention, and
# failure alerting. This brings the previously-unredirected job onto the same
# logs/<job>/ layout as everything else.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

setup_logging "weekly-deep-consolidation"

PROMPT="$(cat recurring/weekly-deep-consolidation.md)"

rc=0
"$CC_BIN" --permission-mode bypassPermissions --model claude-opus-5-5 --verbose --print \
  "$PROMPT" || rc=$?

echo "$(date): CC exited with code $rc"

if [ "$rc" -ne 0 ]; then
  cc_alert "weekly-deep-consolidation" "$rc" "CC exited nonzero"
fi

echo "$(date): Done (final rc=$rc)"
exit $rc
