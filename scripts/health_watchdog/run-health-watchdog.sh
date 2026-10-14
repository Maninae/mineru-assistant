#!/bin/bash
# run-health-watchdog.sh — launchd entry point for the system health watchdog.
#
# bash must stay the launchd job's running program while the probe runs: TCC's Full
# Disk Access grant follows the job's main program, and a python3-rooted job is
# denied chat.db. A bare `bash -c "<one command>"` would exec that command, so this
# script runs python3 as a non-final command and bash waits for it.
WORKSPACE="${MINERU_HOME:-$HOME/.mineru}"
export PYTHONPATH="${PYTHONPATH:-$WORKSPACE/scripts}"
cd "$WORKSPACE" || exit 1
/usr/bin/python3 -m health_watchdog
rc=$?
exit "$rc"
