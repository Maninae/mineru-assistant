# cc-job-lib.sh — Shared library for Mineru's CC trigger scripts.
#
# SOURCE this file (do not exec). Provides:
#   - idempotent_guard <marker-glob>        : early-exit if today's marker exists
#   - setup_logging <job-name>              : mkdir, rotate, redirect, cd workspace
#   - cc_alert <job> <exit-code> <reason>   : send single-line Telegram alert
#   - run_cc_job <name> <model> <instr> [expected-output-glob]
#                                            : standard run + alert on failure
#   - paused_until_guard <job-name>         : early-exit while cache/paused-jobs/<job>.until
#                                            holds a date that is today or later (see bin/pause-job)
#   - run_with_timeout <secs> <job> -- <cmd...>
#                                            : run <cmd> under a hard wall-clock ceiling; on
#                                              expiry alert, kill the process tree, return 124
#
# Notes:
#   - run_cc_job calls setup_logging + the CC invocation internally. The CC run is
#     capped at CC_JOB_TIMEOUT_SECONDS (default 3600); a trigger script overrides it
#     by assigning the variable after sourcing this file, before run_cc_job.
#   - For non-standard jobs (e.g. consolidation, which needs extra pre-steps and
#     a custom prompt), call setup_logging first, do your work, and use cc_alert
#     directly if needed. Wrap the CC call and any long pre-step in run_with_timeout
#     (CC_JOB_STEP_TIMEOUT_SECONDS, default 600, is the ceiling for non-CC steps).
#   - A hung job is the failure these ceilings exist for: a process blocked in the
#     kernel on a TCC-protected open (macOS tccd wedged) never exits on its own, so
#     without a ceiling no alert fires and the job silently runs for hours.
#
# Conventions assumed:
#   - The sourcing script does NOT pre-cd or pre-redirect. run_cc_job handles both.
#   - WORKSPACE defaults to ${MINERU_HOME:-$HOME/.mineru}. CC_BIN defaults to $HOME/.local/bin/claude-fda.
#   - DELIVER_BIN defaults to "python3 $WORKSPACE/scripts/deliver-output.py".
#   - These env vars are overridable for testing (CC_BIN, DELIVER_BIN, WORKSPACE).
#
# Compatibility: bash 3.2 (macOS default). No associative arrays, no ${var,,}, no
# `wait -n`, no GNU `timeout` (run_with_timeout is the pure-bash replacement).

# Resolve key paths up front; allow env override.
: "${WORKSPACE:=${MINERU_HOME:-$HOME/.mineru}}"
: "${CC_BIN:=$HOME/.local/bin/claude-fda}"
: "${DELIVER_BIN:=python3 $WORKSPACE/scripts/deliver-output.py}"

# Log retention thresholds.
#  - gzip plain logs older than this many days
#  - trash gzipped logs older than this many days
: "${LOG_GZIP_AFTER_DAYS:=14}"
: "${LOG_TRASH_AFTER_DAYS:=90}"

# Hard wall-clock ceilings (seconds). Sized from observed run lengths: no CC job
# has taken more than ~24 min, so 60 min is well clear of a slow-but-healthy run
# while still catching a hung one the same morning. 0 disables a ceiling.
#  - CC_JOB_TIMEOUT_SECONDS      : the CC invocation inside run_cc_job (per-job override
#                                  = assign it in the trigger script before run_cc_job)
#  - CC_JOB_STEP_TIMEOUT_SECONDS : non-CC pre-steps (journal export, contact sweeps)
#  - TIMEOUT_TERM_GRACE_SECONDS  : SIGTERM -> SIGKILL wait after the deadline
#  - TIMEOUT_KILL_GRACE_SECONDS  : SIGKILL -> give-up wait
: "${CC_JOB_TIMEOUT_SECONDS:=3600}"
: "${CC_JOB_STEP_TIMEOUT_SECONDS:=600}"
: "${TIMEOUT_TERM_GRACE_SECONDS:=15}"
: "${TIMEOUT_KILL_GRACE_SECONDS:=10}"

# ----------------------------------------------------------------------------
# _today: today's date in PST-naive form (workspace convention is local time).
_today() { date '+%Y-%m-%d'; }
_timestamp() { date '+%Y-%m-%d_%H-%M-%S'; }

# ----------------------------------------------------------------------------
# idempotent_guard <marker-glob>
#
# If any file matches <marker-glob> (relative to WORKSPACE) AND was modified
# today (local date), echo a skip notice and exit 0.
#
# Example:
#   idempotent_guard "briefs_morning/morning-$(date '+%Y-%m-%d').md"
#
# Globs are expanded by the shell — quote arguments that contain wildcards.
idempotent_guard() {
  local marker="$1"
  if [ -z "$marker" ]; then
    return 0
  fi
  local today
  today=$(_today)

  # Use find to allow glob expansion safely even when no match exists.
  # Convert leading WORKSPACE-relative path to absolute for find.
  local abs_marker="$marker"
  case "$marker" in
    /*) : ;;
    *)  abs_marker="$WORKSPACE/$marker" ;;
  esac

  # Find files matching the glob, modified today (mtime within last 24h).
  # Using -name on the basename, -path otherwise. We use bash globbing first.
  local matches=""
  # Disable globbing inside the test (we want literal) — actually we DO want it,
  # so just let the shell expand.
  for f in $abs_marker; do
    if [ -e "$f" ]; then
      # Compare file mtime date (YYYY-MM-DD) to today.
      local ftime
      ftime=$(date -r "$f" '+%Y-%m-%d' 2>/dev/null) || ftime=""
      if [ "$ftime" = "$today" ]; then
        matches="$f"
        break
      fi
    fi
  done

  if [ -n "$matches" ]; then
    echo "[idempotent_guard] Already ran today: $matches — skipping."
    exit 0
  fi
}

# ----------------------------------------------------------------------------
# _rotate_logs <log_dir>
#
# Inline log retention:
#   * gzip *.log and *.err older than LOG_GZIP_AFTER_DAYS
#   * trash *.gz older than LOG_TRASH_AFTER_DAYS (fall back to leaving in place
#     if `trash` is not installed — NEVER `rm`).
_rotate_logs() {
  local dir="$1"
  [ -d "$dir" ] || return 0

  # gzip plain logs
  find "$dir" -maxdepth 1 -type f \( -name '*.log' -o -name '*.err' \) \
    -mtime "+${LOG_GZIP_AFTER_DAYS}" 2>/dev/null | while read -r f; do
    [ -n "$f" ] && gzip -f "$f" 2>/dev/null || true
  done

  # trash old gzips (NEVER rm)
  if command -v trash >/dev/null 2>&1; then
    find "$dir" -maxdepth 1 -type f -name '*.gz' \
      -mtime "+${LOG_TRASH_AFTER_DAYS}" 2>/dev/null | while read -r f; do
      [ -n "$f" ] && trash "$f" 2>/dev/null || true
    done
  fi
  # If `trash` is missing, we deliberately leave old gzips in place.
  return 0
}

# ----------------------------------------------------------------------------
# _cc_auth_expired_in_log
#
# Returns 0 if the current job log (CC_JOB_LOG_FILE, set by setup_logging)
# carries Claude Code's 401 auth-expiry signature. Patterns are kept specific
# to the CC error text so email/message content a job read can't false-match.
_cc_auth_expired_in_log() {
  [ -n "${CC_JOB_LOG_FILE:-}" ] && [ -r "$CC_JOB_LOG_FILE" ] || return 1
  grep -q -e "Invalid authentication credentials" -e "API Error: 401" \
    "$CC_JOB_LOG_FILE" 2>/dev/null
}

# ----------------------------------------------------------------------------
# _send_failure_alert <job-name> <exit-code> <reason>
#
# Fires a single-line failure alert to Telegram via deliver-output.py --raw.
# Guarded with `|| true` so alert failures cannot mask the real exit code.
#
# When the job log shows CC's 401 auth-expiry signature, the generic message is
# upgraded to the systemic diagnosis: one flat "check logs" line per job is how
# the June 2026 outage stayed un-actioned for days while memories were lost.
_send_failure_alert() {
  local job="$1"
  local code="$2"
  local reason="$3"
  local msg
  if _cc_auth_expired_in_log; then
    msg="🔑 Cron job ${job} failed: Claude Code auth expired (401). ALL cron jobs will fail until CC re-auths. Fix: restart the Mac (see TECHNICAL.md, When Jobs Go Silent)."
  else
    msg="⚠️ Cron job ${job} failed (${reason}, exit ${code}). Check logs/${job}/."
  fi
  # DELIVER_BIN may be a multi-word command (e.g. "python3 .../deliver-output.py").
  # shellcheck disable=SC2086
  $DELIVER_BIN --raw "$msg" 2>/dev/null || true
}

# ----------------------------------------------------------------------------
# _check_expected_output <expected-glob>
#
# Returns 0 if at least one matching file has mtime within the last 2 hours.
# Returns 1 otherwise. Empty glob always returns 0 (no check requested).
_check_expected_output() {
  local glob="$1"
  [ -z "$glob" ] && return 0

  local abs_glob="$glob"
  case "$glob" in
    /*) : ;;
    *)  abs_glob="$WORKSPACE/$glob" ;;
  esac

  # 2 hours = 120 minutes
  for f in $abs_glob; do
    if [ -e "$f" ]; then
      # find returns the path if mtime within last 120 minutes
      local fresh
      fresh=$(find "$f" -maxdepth 0 -mmin -120 2>/dev/null)
      if [ -n "$fresh" ]; then
        return 0
      fi
    fi
  done
  return 1
}

# ----------------------------------------------------------------------------
# paused_until_guard <job-name>
#
# Operator pause switch. If $WORKSPACE/cache/paused-jobs/<job-name>.until exists
# and holds a YYYY-MM-DD that is today or later, log a skip notice and exit 0
# (launchd sees success, no failure alert). Once the date has passed the file is
# trashed so the job resumes on its own. Managed by bin/pause-job.
paused_until_guard() {
  local job="$1"
  local pause_file="$WORKSPACE/cache/paused-jobs/$job.until"
  [ -f "$pause_file" ] || return 0
  local until
  until=$(tr -d '[:space:]' < "$pause_file")
  case "$until" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) : ;;
    *) echo "[paused_until_guard] ignoring malformed pause file $pause_file ('$until')"; return 0 ;;
  esac
  local today
  today=$(_today)
  # ISO dates compare correctly as strings.
  if [ "$today" \> "$until" ]; then
    echo "[paused_until_guard] pause expired ($until); resuming and removing $pause_file"
    if command -v trash >/dev/null 2>&1; then trash "$pause_file" 2>/dev/null || true; else mv "$pause_file" "$pause_file.expired"; fi
    return 0
  fi
  echo "[paused_until_guard] $job is paused until $until — skipping this run."
  exit 0
}

# ----------------------------------------------------------------------------
# _rwt_wait_for_exit <pid> <seconds>
#
# Poll until <pid> is gone or <seconds> have passed. Returns 0 when gone, 1 if
# it is still there. Never blocks in `wait`, so an unkillable child cannot pin us.
_rwt_wait_for_exit() {
  local pid="$1"
  local budget="$2"
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$budget" ]; then
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 0
}

# ----------------------------------------------------------------------------
# _rwt_process_start <pid>
#
# The process's start time as `ps` prints it (empty if the pid is gone). Used to
# tell our child from an unrelated process that recycled its pid after exit.
_rwt_process_start() {
  ps -o lstart= -p "$1" 2>/dev/null | sed 's/^ *//'
}

# ----------------------------------------------------------------------------
# _rwt_forward_signal <pgid>
#
# Trap body while run_with_timeout has a child running: pass the stop signal on
# to the child's whole process group, then exit the way a signalled shell would.
_rwt_forward_signal() {
  kill -TERM -- "-$1" 2>/dev/null || true
  exit 143
}

# ----------------------------------------------------------------------------
# run_with_timeout <seconds> <job-name> -- <command...>
#
# Run <command> under a hard wall-clock ceiling. A normal exit returns the
# command's own exit code. Past the deadline, in this order:
#   1. fire the Telegram failure alert (the operator must hear about a hung job
#      even if nothing below manages to kill it),
#   2. SIGTERM the command's whole process group, wait TIMEOUT_TERM_GRACE_SECONDS,
#   3. SIGKILL the group, wait TIMEOUT_KILL_GRACE_SECONDS,
#   4. return 124 whether or not the child could be reaped, with
#      RUN_WITH_TIMEOUT_FIRED=1 so callers skip their own "exited nonzero" alert.
#
# How it works (bash 3.2; macOS ships no GNU `timeout`):
#   - The command starts in the background with job control on (`set -m`), which
#     gives it its own process group, so `kill -- -<pid>` reaches the whole tree
#     (claude and every tool subprocess under it).
#   - This shell polls `kill -0` once a second instead of blocking in `wait`.
#     bash reaps exited children on SIGCHLD, so the probe turns false on exit, and
#     `wait` is only ever called on a child already known to be dead. A child stuck
#     in an uninterruptible kernel wait (a tccd freeze) therefore cannot pin the
#     script: it is left behind, logged, and 124 is returned anyway.
#   - Before killing, the child's `ps` start time is re-checked against the one
#     recorded at spawn, so a recycled pid is never mistaken for a hung child.
#   - INT/TERM/HUP to this shell (launchd unload, Ctrl-C) are forwarded to the
#     child's group; the traps are installed for the duration and reset after.
#
# <seconds> = 0 runs the command in the foreground with no ceiling. The child
# lives in a background process group, so it cannot read from a terminal
# (irrelevant under launchd, where stdin is /dev/null). When a child dies by
# signal, bash prints one "Terminated: 15" line on stderr (the .err log).
run_with_timeout() {
  local limit="${1:-}"
  local job="${2:-}"
  shift 2 2>/dev/null || true
  if [ "${1:-}" = "--" ]; then
    shift
  fi
  if [ -z "$limit" ] || [ -z "$job" ] || [ $# -eq 0 ]; then
    echo "run_with_timeout: usage: run_with_timeout <seconds> <job-name> -- <command...>" >&2
    return 2
  fi
  case "$limit" in
    *[!0-9]*)
      echo "run_with_timeout: <seconds> must be a non-negative integer, got '$limit'" >&2
      return 2 ;;
  esac
  RUN_WITH_TIMEOUT_FIRED=0

  local rc=0
  if [ "$limit" -eq 0 ]; then
    "$@" || rc=$?
    return $rc
  fi

  # Job control on just for the fork, so the child gets its own process group.
  local monitor_was_on=0
  case "$-" in *m*) monitor_was_on=1 ;; esac
  set -m
  "$@" &
  local child=$!
  if [ "$monitor_was_on" -eq 0 ]; then
    set +m
  fi
  local child_start
  child_start=$(_rwt_process_start "$child")

  trap "_rwt_forward_signal $child" INT TERM HUP
  local started=$SECONDS
  local timed_out=0
  while kill -0 "$child" 2>/dev/null; do
    if [ $((SECONDS - started)) -ge "$limit" ]; then
      timed_out=1
      break
    fi
    sleep 1
  done
  trap - INT TERM HUP

  # Still alive at the deadline, but is it still OUR child? (pid recycling)
  if [ "$timed_out" -eq 1 ] && [ -n "$child_start" ] \
     && [ "$(_rwt_process_start "$child")" != "$child_start" ]; then
    timed_out=0
  fi
  if [ "$timed_out" -eq 0 ]; then
    wait "$child" || rc=$?
    return $rc
  fi

  RUN_WITH_TIMEOUT_FIRED=1
  local limit_text="${limit}s"
  if [ "$limit" -ge 60 ]; then
    limit_text="$(( (limit + 30) / 60 )) min"
  fi
  echo "[$(_timestamp)] run_with_timeout: $job exceeded ${limit}s; alerting, then killing process group $child"
  _send_failure_alert "$job" 124 \
    "timed out after ${limit_text} and was killed: likely hung, possibly a macOS permission/tccd freeze"

  kill -TERM -- "-$child" 2>/dev/null || true
  if ! _rwt_wait_for_exit "$child" "$TIMEOUT_TERM_GRACE_SECONDS"; then
    kill -KILL -- "-$child" 2>/dev/null || true
    if ! _rwt_wait_for_exit "$child" "$TIMEOUT_KILL_GRACE_SECONDS"; then
      echo "[$(_timestamp)] run_with_timeout: pid $child survived SIGKILL (uninterruptible kernel wait?); leaving it and returning 124"
      return 124
    fi
  fi
  # The child is dead; reap it so no job-table entry lingers. Cannot block.
  wait "$child" 2>/dev/null || true
  echo "[$(_timestamp)] run_with_timeout: process group $child is gone"
  return 124
}

# ----------------------------------------------------------------------------
# setup_logging <job-name>
#
# Public helper: create logs/<job>/, rotate old files, redirect stdout/stderr
# to timestamped log+err files, and cd to WORKSPACE.
#
# Idempotent within a single shell invocation — but typically called once
# per script. Use this when you can't call run_cc_job directly (e.g. you need
# a custom CC prompt or extra pre-steps).
setup_logging() {
  local job="$1"
  if [ -z "$job" ]; then
    echo "setup_logging: usage: setup_logging <job-name>" >&2
    return 2
  fi
  local log_dir="$WORKSPACE/logs/$job"
  local ts
  ts=$(_timestamp)
  mkdir -p "$log_dir"
  _rotate_logs "$log_dir"
  # Global so the failure-alert path can grep this run's log for the CC 401
  # auth-expiry signature (see _cc_auth_expired_in_log).
  CC_JOB_LOG_FILE="$log_dir/$ts.log"
  exec >"$CC_JOB_LOG_FILE" 2>"$log_dir/$ts.err"
  echo "[$ts] Starting $job"
  cd "$WORKSPACE"
}

# ----------------------------------------------------------------------------
# cc_alert <job-name> <exit-code> <reason>
#
# Public helper: fire a single-line failure alert to Telegram. Always returns 0
# so callers can chain it without affecting their own exit code.
cc_alert() {
  _send_failure_alert "$1" "$2" "$3"
  return 0
}

# ----------------------------------------------------------------------------
# run_cc_job <job-name> <model> <instruction-file> [expected-output-glob]
#
# The standard wrapper:
#   1. Set up LOG_DIR = WORKSPACE/logs/<job-name>; rotate old logs.
#   2. Redirect stdout/stderr to timestamped log/err files.
#   3. cd to WORKSPACE.
#   4. Run claude-fda with --permission-mode bypassPermissions, --model, --verbose, --print
#      using the standard "Read <instruction-file> and execute the job." prompt,
#      under run_with_timeout with CC_JOB_TIMEOUT_SECONDS as the ceiling.
#   5. After the run: if exit nonzero, or expected-output-glob given but no matching
#      file with mtime within last 2 hours, fire a Telegram failure alert. A timeout
#      has already alerted inside run_with_timeout and returns 124.
#   6. Exit with the underlying CC exit code (alert never masks it).
#
# Caller is expected to have set -euo pipefail BEFORE sourcing — we leave shell
# options alone here.
run_cc_job() {
  local job="$1"
  local model="$2"
  local instr="$3"
  local expected="${4:-}"

  if [ -z "$job" ] || [ -z "$model" ] || [ -z "$instr" ]; then
    echo "run_cc_job: usage: run_cc_job <job-name> <model> <instruction-file> [expected-output-glob]" >&2
    return 2
  fi

  setup_logging "$job"
  paused_until_guard "$job"
  echo "[$(_timestamp)] CC model=$model instruction=$instr timeout=${CC_JOB_TIMEOUT_SECONDS}s"

  local rc=0
  # Run CC under the hard ceiling. Note: $CC_BIN may be overridden in tests (e.g. /bin/echo).
  run_with_timeout "$CC_JOB_TIMEOUT_SECONDS" "$job" -- \
    "$CC_BIN" --permission-mode bypassPermissions --model "$model" --verbose --print \
    "Read $instr and execute the job." || rc=$?

  echo "[$(_timestamp)] CC exited with code $rc"

  # Check expected output. A timeout already alerted, so it needs no second one.
  if [ "${RUN_WITH_TIMEOUT_FIRED:-0}" -eq 1 ]; then
    :
  elif [ "$rc" -ne 0 ]; then
    _send_failure_alert "$job" "$rc" "CC exited nonzero"
  elif [ -n "$expected" ]; then
    if ! _check_expected_output "$expected"; then
      _send_failure_alert "$job" "$rc" "no recent output matched '$expected'"
      # Surface the missing-output failure to launchd as well.
      rc=64
    fi
  fi

  echo "[$(_timestamp)] Done (final rc=$rc)"
  return $rc
}
