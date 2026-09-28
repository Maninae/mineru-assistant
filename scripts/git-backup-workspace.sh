#!/bin/bash
# git-backup-workspace.sh — Commit and push all workspace changes.
# Called at the end of daily consolidation.

set -euo pipefail

cd "${MINERU_HOME:-$HOME/.mineru}"

DATE="${1:-$(date -v-1d '+%Y-%m-%d')}"

git add -A
git commit -m "Daily backup: $DATE" || echo "Nothing to commit"
git push origin HEAD
