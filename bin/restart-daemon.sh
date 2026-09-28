#!/bin/bash
# Restart the Mineru Telegram daemon (Landline).
#
# Thin wrapper: exports Mineru's environment and delegates to the Landline
# repo's deploy/restart.sh, which runs the same gates as the old in-tree
# script (compile-check, import gate, full pytest suite, continuation write,
# launchctl bootout/bootstrap).
#
# Usage:
#   $MINERU_HOME/bin/restart-daemon.sh                       # full gates
#   $MINERU_HOME/bin/restart-daemon.sh --skip-tests          # fast iteration
#   $MINERU_HOME/bin/restart-daemon.sh "custom continuation" # message Claude sees
#
# All four LANDLINE_* seams are env-overridable (defaults derive from
# MINERU_HOME / LAUNCHD_LABEL_PREFIX); export them yourself to point at a
# non-default workspace, sibling repo, or launchd label.
set -euo pipefail

export LANDLINE_WORKSPACE="${LANDLINE_WORKSPACE:-${MINERU_HOME:-$HOME/.mineru}}"
export LANDLINE_REPO="${LANDLINE_REPO:-$HOME/Developer/claude-landline}"
export LANDLINE_LABEL="${LANDLINE_LABEL:-${LAUNCHD_LABEL_PREFIX:-com.mineru.}telegram-daemon}"
export LANDLINE_PLIST="${LANDLINE_PLIST:-$HOME/Library/LaunchAgents/${LANDLINE_LABEL}.plist}"

exec "$LANDLINE_REPO/deploy/restart.sh" "$@"
