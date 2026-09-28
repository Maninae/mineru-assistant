#!/bin/bash
# trigger-daily-consolidation-claude-code.sh
# Daily memory consolidation via Claude Code.
# Called by launchd at 1:00 AM daily.
#
# Special steps before the CC invocation:
#   * Pre-export Apple Notes journals (bash has TCC access; CC does not).
#   * Extract Claude Code session transcripts into memory/daily/ fragments.
# These pre-steps are kept inline; CC prompt is custom-built (with a YESTERDAY
# date pin), so we don't use run_cc_job here. We do use the shared lib for log
# setup, retention, and failure alerting.

set -euo pipefail
. "$(dirname "$0")/cc-job-lib.sh"

# Optional: pass a date as $1 (e.g. 2026-03-18), defaults to yesterday.
YESTERDAY="${1:-$(date -v-1d '+%Y-%m-%d')}"
CONSOLIDATED="$WORKSPACE/memory/daily/${YESTERDAY}.md"

# Idempotency guard (before log redirect, so it shows up in launchd log too).
if [ -f "$CONSOLIDATED" ]; then
  echo "$(date): Consolidated file already exists: $CONSOLIDATED — skipping."
  exit 0
fi

# Now set up per-run timestamped logs and cd to workspace.
setup_logging "daily-consolidation"

# Pre-export Apple Notes journals — skip if a recent export is on disk.
EXPORT_DIR="${MINERU_JOURNAL_EXPORTS_DIR:-$WORKSPACE/journal_exports}"
export MINERU_JOURNAL_EXPORTS_DIR="$EXPORT_DIR"
RECENT_EXPORTS=$(find "$EXPORT_DIR" -maxdepth 1 -name "*.txt" -mmin -180 2>/dev/null | wc -l | tr -d ' ')
if [ "$RECENT_EXPORTS" -gt 0 ]; then
  echo "$(date): Found $RECENT_EXPORTS recent journal exports (< 3 hours old) — skipping re-export."
else
  echo "$(date): No recent exports found. Exporting daily journals from Apple Notes..."
  if ! /usr/bin/python3 scripts/export-journals.py 30; then
    echo "$(date): WARNING: Journal export failed, continuing without journals"
    cc_alert "daily-consolidation" "1" "journal export step failed (continuing)"
  fi
fi

# Extract Claude Code session transcripts into memory/daily/ fragments.
echo "$(date): Extracting Claude Code sessions for $YESTERDAY..."
python3 scripts/extract-cc-sessions.py --since "$YESTERDAY" || {
  echo "$(date): WARNING: CC session extraction failed, continuing without CC sessions"
}

PROMPT="$(cat recurring/consolidate-daily-memories.md)

IMPORTANT: The target date for this consolidation run is $YESTERDAY. Use this date everywhere instead of computing yesterday's date yourself."

rc=0
"$CC_BIN" --permission-mode bypassPermissions --model claude-sonnet-5 --verbose --print \
  "$PROMPT" || rc=$?

echo "$(date): CC exited with code $rc"

# Expected output: memory/daily/<YESTERDAY>.md should now exist (and be fresh).
if [ "$rc" -ne 0 ]; then
  cc_alert "daily-consolidation" "$rc" "CC exited nonzero"
elif [ ! -f "$CONSOLIDATED" ]; then
  cc_alert "daily-consolidation" "$rc" "expected output not produced: memory/daily/${YESTERDAY}.md"
  rc=64
fi

echo "$(date): Done (final rc=$rc)"
exit $rc
