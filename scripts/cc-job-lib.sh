# cc-job-lib.sh — Shared library for Mineru's CC trigger scripts.
#
# SOURCE this file (do not exec). Provides:
#   - idempotent_guard <marker-glob>        : early-exit if today's marker exists
#   - setup_logging <job-name>              : mkdir, rotate, redirect, cd workspace
#   - cc_alert <job> <exit-code> <reason>   : send single-line Telegram alert
#   - run_cc_job <name> <model> <instr> [expected-output-glob]
#                                            : standard run + alert on failure
#
# Notes:
#   - run_cc_job calls setup_logging + the CC invocation internally.
#   - For non-standard jobs (e.g. consolidation, which needs extra pre-steps and
#     a custom prompt), call setup_logging first, do your work, and use cc_alert
#     directly if needed.
#
# Conventions assumed:
#   - The sourcing script does NOT pre-cd or pre-redirect. run_cc_job handles both.
#   - WORKSPACE defaults to ${MINERU_HOME:-$HOME/.mineru}. CC_BIN defaults to $HOME/.local/bin/claude-fda.
#   - DELIVER_BIN defaults to "python3 $WORKSPACE/scripts/deliver-output.py".
#   - These env vars are overridable for testing (CC_BIN, DELIVER_BIN, WORKSPACE).
#
# Compatibility: bash 3.2 (macOS default). No associative arrays, no ${var,,}.

# Resolve key paths up front; allow env override.
: "${WORKSPACE:=${MINERU_HOME:-$HOME/.mineru}}"
: "${CC_BIN:=$HOME/.local/bin/claude-fda}"
: "${DELIVER_BIN:=python3 $WORKSPACE/scripts/deliver-output.py}"

# Log retention thresholds.
#  - gzip plain logs older than this many days
#  - trash gzipped logs older than this many days
: "${LOG_GZIP_AFTER_DAYS:=14}"
: "${LOG_TRASH_AFTER_DAYS:=90}"

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
#      using the standard "Read <instruction-file> and execute the job." prompt.
#   5. After the run: if exit nonzero, or expected-output-glob given but no matching
#      file with mtime within last 2 hours, fire a Telegram failure alert.
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
  echo "[$(_timestamp)] CC model=$model instruction=$instr"

  local rc=0
  # Run CC. Note: $CC_BIN may be overridden in tests (e.g. /bin/echo).
  "$CC_BIN" --permission-mode bypassPermissions --model "$model" --verbose --print \
    "Read $instr and execute the job." || rc=$?

  echo "[$(_timestamp)] CC exited with code $rc"

  # Check expected output.
  if [ "$rc" -ne 0 ]; then
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
