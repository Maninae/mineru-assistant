#!/bin/bash
# run-group-members-sweep.sh
# Refreshes contact names and detects new iMessage group chats.
# Pure bash — no LLM needed.
# Called by launchd at 11:00 PM daily.

set -uo pipefail
# Shared lib for run_with_timeout + cc_alert only; this script keeps its own logging.
. "$(dirname "$0")/cc-job-lib.sh"

PYTHON=/usr/bin/python3
WORKSPACE="${MINERU_HOME:-$HOME/.mineru}"
LOG_DIR="$WORKSPACE/logs/group-members-sweep"
TIMESTAMP=$(date '+%Y-%m-%d_%H-%M-%S')
DATE_PST=$(TZ=America/Los_Angeles date '+%Y-%m-%d')

mkdir -p "$LOG_DIR"
exec > "$LOG_DIR/$TIMESTAMP.log" 2>&1

echo "[$TIMESTAMP] Starting group members sweep"

cd "$WORKSPACE"

# Both steps read TCC-guarded stores (Contacts, Messages chat.db); a wedged tccd
# blocks those opens forever, so each step runs under a hard ceiling that alerts.

# Step 1: Refresh contact map (non-fatal — sweep uses existing map if this fails)
echo "--- Refreshing contacts ---"
if ! run_with_timeout "$CC_JOB_STEP_TIMEOUT_SECONDS" "group-members-sweep" -- \
     $PYTHON scripts/build_contact_map.py; then
  echo "WARNING: Contact map refresh failed (continuing with existing map)"
fi

# Step 2: Sweep group members
echo "--- Sweeping group members ---"
rc=0
run_with_timeout "$CC_JOB_STEP_TIMEOUT_SECONDS" "group-members-sweep" -- \
  $PYTHON scripts/sweep_group_members.py --update --save-report || rc=$?
# A timed-out step has already alerted inside run_with_timeout.
if [ "$rc" -ne 0 ] && [ "${RUN_WITH_TIMEOUT_FIRED:-0}" -eq 0 ]; then
  cc_alert "group-members-sweep" "$rc" "sweep exited nonzero"
fi

# Step 3: Save cron log entry
CRON_LOG_DIR="$WORKSPACE/logs/cron/group-members-sweep"
mkdir -p "$CRON_LOG_DIR"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Group members sweep finished (rc=$rc)"
  echo "See full output in: $LOG_DIR/$TIMESTAMP.log"
} >> "$CRON_LOG_DIR/$DATE_PST.log"

echo "[$(date '+%Y-%m-%d_%H-%M-%S')] Done (final rc=$rc)"
exit $rc
