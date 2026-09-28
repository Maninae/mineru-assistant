#!/bin/bash
# run-group-members-sweep.sh
# Refreshes contact names and detects new iMessage group chats.
# Pure bash — no LLM needed.
# Called by launchd at 11:00 PM daily.

set -uo pipefail

PYTHON=/usr/bin/python3
WORKSPACE="${MINERU_HOME:-$HOME/.mineru}"
LOG_DIR="$WORKSPACE/logs/group-members-sweep"
TIMESTAMP=$(date '+%Y-%m-%d_%H-%M-%S')
DATE_PST=$(TZ=America/Los_Angeles date '+%Y-%m-%d')

mkdir -p "$LOG_DIR"
exec > "$LOG_DIR/$TIMESTAMP.log" 2>&1

echo "[$TIMESTAMP] Starting group members sweep"

cd "$WORKSPACE"

# Step 1: Refresh contact map (non-fatal — sweep uses existing map if this fails)
echo "--- Refreshing contacts ---"
if ! $PYTHON scripts/build_contact_map.py; then
  echo "WARNING: Contact map refresh failed (continuing with existing map)"
fi

# Step 2: Sweep group members
echo "--- Sweeping group members ---"
$PYTHON scripts/sweep_group_members.py --update --save-report

# Step 3: Save cron log entry
CRON_LOG_DIR="$WORKSPACE/logs/cron/group-members-sweep"
mkdir -p "$CRON_LOG_DIR"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Group members sweep complete"
  echo "See full output in: $LOG_DIR/$TIMESTAMP.log"
} >> "$CRON_LOG_DIR/$DATE_PST.log"

echo "[$(date '+%Y-%m-%d_%H-%M-%S')] Done"
