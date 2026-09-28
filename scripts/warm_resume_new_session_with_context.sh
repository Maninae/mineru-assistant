#!/usr/bin/env bash
# warm_resume_new_session_with_context.sh
# Produces structured context for a new session agent to read.
# Last 3 days of consolidated daily memories + today's session fragments.
#
# Only picks up files matching:
#   YYYY-MM-DD.md          (consolidated daily)
#   YYYY-MM-DD-*.md        (slugged session fragments)
#   YYYY-MM-DD_HH-MM-SS.md (timestamped session fragments)
# Skips: .raw.md, .denoise.log, and anything else
set -euo pipefail

MEMORY_DIR="${MINERU_HOME:-$HOME/.mineru}/memory/daily"
TZ="America/Los_Angeles"
export TZ

DAY_OF_WEEK=$(date +"%A")
DATE_FULL=$(date +"%B %-d, %Y")
TIME_NOW=$(date +"%-I:%M %p %Z")

# Strict pattern: only YYYY-MM-DD.md (10-char date stem)
is_consolidated() {
  local base="$1"
  [[ "$base" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
}

# Strict pattern: YYYY-MM-DD-*.md or YYYY-MM-DD_HH-MM-SS.md
is_session_fragment() {
  local base="$1"
  [[ "$base" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}[-_] ]]
}

cat <<EOF
<session_warmup>
<current_time>${DAY_OF_WEEK}, ${DATE_FULL} — ${TIME_NOW}</current_time>

<instructions>
You are resuming from a fresh session. Below is your recent history — the last 3 days
of consolidated daily memories and any session fragments from today. Read all of it.
This is your continuity bridge; use it to orient yourself before responding.
</instructions>
EOF

# --- Last 3 days of consolidated daily memories ---
TODAY=$(date +"%Y-%m-%d")

# Calculate 3 days ago for the cutoff
THREE_DAYS_AGO=$(date -v-3d +"%Y-%m-%d")

CONSOLIDATED_FILES=()
for f in "${MEMORY_DIR}"/????-??-??.md; do
  [ -f "$f" ] || continue
  base=$(basename "$f" .md)
  is_consolidated "$base" || continue
  # Skip today (shown separately) and anything older than 3 days
  [ "$base" = "$TODAY" ] && continue
  [ "$base" \< "$THREE_DAYS_AGO" ] && continue
  CONSOLIDATED_FILES+=("$f")
done

IFS=$'\n' SORTED=($(printf '%s\n' "${CONSOLIDATED_FILES[@]}" | sort -r | head -3))
unset IFS

echo ""
echo "<recent_days count=\"${#SORTED[@]}\">"

for (( i=${#SORTED[@]}-1; i>=0; i-- )); do
  f="${SORTED[$i]}"
  d=$(basename "$f" .md)
  day_name=$(date -j -f "%Y-%m-%d" "$d" +"%A" 2>/dev/null || echo "")
  echo ""
  echo "<day date=\"${d}\" day=\"${day_name}\">"
  cat "$f"
  echo ""
  echo "</day>"
done

echo "</recent_days>"

# --- Today's sessions (fragments only, not the consolidated file) ---
TODAY_FILES=()
for f in "${MEMORY_DIR}/${TODAY}"*.md; do
  [ -f "$f" ] || continue
  fname=$(basename "$f")
  base="${fname%.md}"

  # Skip artifacts
  case "$fname" in *.raw.md|*.denoise.log) continue ;; esac

  # Skip the consolidated file (shown above if it existed)
  is_consolidated "$base" && continue

  # Must be a valid session fragment
  is_session_fragment "$base" || continue

  TODAY_FILES+=("$f")
done

if [ ${#TODAY_FILES[@]} -gt 0 ]; then
  echo ""
  echo "<today count=\"${#TODAY_FILES[@]}\">"
  for f in "${TODAY_FILES[@]}"; do
    fname=$(basename "$f")
    echo ""
    echo "<session file=\"${fname}\">"
    cat "$f"
    echo ""
    echo "</session>"
  done
  echo "</today>"
else
  echo ""
  echo "<today count=\"0\">No sessions yet today.</today>"
fi

echo ""
echo "</session_warmup>"
