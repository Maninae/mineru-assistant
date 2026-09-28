#!/usr/bin/env bash
# Generate annotated memory directory tree for bootstrap context
# Shows description from YAML frontmatter next to each file
# Called by load-extra-context hook at session start

set -euo pipefail

WORKSPACE_DIR="${1:-.}"
MEMORY_DIR="$WORKSPACE_DIR/memory"

if [[ ! -d "$MEMORY_DIR" ]]; then
  echo "Error: memory directory not found at $MEMORY_DIR" >&2
  exit 1
fi

get_description() {
  head -10 "$1" 2>/dev/null | awk '
    /^---$/ { if (in_fm) { found_end=1; exit }; in_fm=1; next }
    in_fm && /^description:/ {
      sub(/^description:[[:space:]]*/, "")
      gsub(/^["'\''"]|["'\''"]$/, "")
      desc=$0; next
    }
    END { if (found_end && desc) print desc }
  '
}

echo "## Current Structure"
echo ""
echo "\`\`\`"

cd "$MEMORY_DIR"

# Use tree if available, post-process to add descriptions
if command -v tree &>/dev/null; then
  # Get pretty tree and full-path tree in parallel, then zip them
  tmp_pretty=$(mktemp)
  tmp_full=$(mktemp)
  tree -I '__pycache__|*.pyc|.DS_Store|daily|monthly' --noreport --dirsfirst . | tail -n +2 > "$tmp_pretty"
  tree -I '__pycache__|*.pyc|.DS_Store|daily|monthly' --noreport --dirsfirst -fi . | tail -n +2 > "$tmp_full"

  paste -d$'\t' "$tmp_pretty" "$tmp_full" | while IFS=$'\t' read -r pline fline; do
    fline="${fline#./}"
    fline=$(echo "$fline" | sed 's/^[[:space:]]*//')
    if [[ "$fline" == *.md ]]; then
      desc=$(get_description "$MEMORY_DIR/$fline")
      if [[ -n "$desc" ]]; then
        echo "${pline}  # ${desc}"
      else
        echo "$pline"
      fi
    else
      echo "$pline"
    fi
  done

  rm -f "$tmp_pretty" "$tmp_full"
else
  # Fallback without tree
  find . -name "*.md" -not -path "./daily/*" -not -path "./monthly/*" -not -name "README.md" | sort | while read -r file; do
    file="${file#./}"
    depth=$(echo "$file" | tr -cd '/' | wc -c)
    indent=""
    for ((i=0; i<depth; i++)); do indent="${indent}    "; done
    basename=$(basename "$file")
    desc=$(get_description "$MEMORY_DIR/$file")
    if [[ -n "$desc" ]]; then
      echo "${indent}|-- ${basename}  # ${desc}"
    else
      echo "${indent}|-- ${basename}"
    fi
  done
fi

# Summary lines for excluded dirs
daily_count=$(find ./daily -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
monthly_count=$(find ./monthly -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
echo "|-- daily/  # ${daily_count} session logs"
echo "|-- monthly/  # ${monthly_count} monthly summaries"

echo "\`\`\`"
