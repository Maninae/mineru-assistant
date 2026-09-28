#!/usr/bin/env bash
# _shim_lib.sh — shared Bash helper sourced by every legacy shim in bin_shims/.
#
# Purpose (safety net for Phase 2 of the cutover, §3.4 + Open Q#15 of the
# 2026-07-25 capability spec): every legacy bin/<tool> shim delegates to
# `mineru <verb>` when the CLI is healthy, and falls THROUGH to the live
# original binary when it isn't. A broken mineru package must not take down
# cron jobs, doctor, or skills that call the legacy bin path.
#
# INVARIANT: every file under bin_shims/ is self-contained Bash. No Python.
# No sourcing files outside bin_shims/. The fallback path in particular
# never touches the mineru package or its venv — it just exec's the
# original binary. That is what makes the safety net work when mineru's
# Python is broken.
#
# The health probe is deliberately shallow: `mineru --help` with a 2s cap.
# The root Typer app renders --help without touching the msearch / gog /
# imsg engines, so a healthy-help probe passes even if a specific wrapper
# subpackage is later broken. If the probe hangs or errors, we fall
# through to the original binary — the shim NEVER hangs forever.
#
# ---- Authoring a new shim ---------------------------------------------------
#
# Copy bin_shims/msearch as a template. A new shim is ~5 lines of real code:
#
#   #!/usr/bin/env bash
#   set -euo pipefail
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=./_shim_lib.sh
#   source "$SCRIPT_DIR/_shim_lib.sh"
#   MINERU_VERB=(gmail)                      # arg tail of `mineru ...`
#   FALLBACK_BIN=${MINERU_HOME:-$HOME/.mineru}/bin/gog-firewall
#   shim_run "$@"
#
# - MINERU_VERB (Bash array): the noun (and optional sub-noun) `mineru ...`
#   is dispatched with when healthy. The user's argv is appended verbatim.
# - FALLBACK_BIN (string): absolute path to the live original binary. Must
#   exist on disk — this file is what serves cron/doctor/skills when the
#   CLI is unavailable.
# - Nothing else. The lib does the health probe, the timeout cap, and the
#   dispatch. Shims stay one-liners.

# Timeout (seconds) for the mineru health probe. 2s per the F6 contract.
: "${MINERU_SHIM_HEALTH_TIMEOUT:=2}"
# This value is interpolated into the `bash -c` health probe below, so it must
# be a bare positive integer. Reject anything else (defends against a shell
# metacharacter payload smuggled in via the env var — e.g. an inherited
# cron/subprocess environment setting it to "2; rm -rf ...").
#
# `0` is explicitly rejected too: `perl -e 'alarm 0'` clears the pending
# alarm, which would let a hung `mineru --help` block the shim forever —
# breaking the "shim NEVER hangs forever" invariant declared at the top
# of this file. Any zero/empty/non-digit value snaps back to the 2s default.
case "${MINERU_SHIM_HEALTH_TIMEOUT}" in
    ''|0|*[!0-9]*) MINERU_SHIM_HEALTH_TIMEOUT=2 ;;
esac

# Perl is guaranteed on macOS at /usr/bin/perl; coreutils' `timeout` /
# `gtimeout` are NOT. We invoke perl by absolute path so a stripped-down
# cron/launchd PATH (e.g. one that omits /usr/bin) can't silently downgrade
# the probe to "perl missing → exit 127 → fall through to legacy binary",
# which would defeat Phase 2 rollout (the new CLI would look permanently
# unhealthy). Perl's `alarm` gives a portable timeout cap with a non-zero
# exit on SIGALRM (perl returns 142 = 128 + SIGALRM), which the probe
# treats as "unhealthy" and falls through to the fallback.
#
# The whole probe runs inside a `bash -c` subshell whose stderr is dumped,
# so a SIGALRM-killed perl doesn't print "Alarm clock: 14" to the shim's
# stderr. The exit code of the subshell (0 healthy / non-zero unhealthy)
# is all we need.
MINERU_SHIM_PERL_BIN=/usr/bin/perl
mineru_healthy() {
    command -v mineru >/dev/null 2>&1 || return 1
    [[ -x "$MINERU_SHIM_PERL_BIN" ]] || return 1
    { bash -c "'${MINERU_SHIM_PERL_BIN}' -e 'alarm shift; exec @ARGV or exit 127' \
        \"${MINERU_SHIM_HEALTH_TIMEOUT}\" mineru --help >/dev/null 2>&1"; } 2>/dev/null
}

# Dispatch entry point for every shim. Reads MINERU_VERB (array) and
# FALLBACK_BIN (string) from the caller's scope, then exec's the right
# target. `exec` replaces the shim process so exit codes, signals, and
# stdio semantics match the target one-for-one.
shim_run() {
    if [[ -z "${FALLBACK_BIN:-}" ]]; then
        echo "shim_lib: FALLBACK_BIN is unset (bug in the shim, not in mineru)" >&2
        exit 78
    fi
    if [[ -z "${MINERU_VERB+x}" ]]; then
        echo "shim_lib: MINERU_VERB is unset (bug in the shim, not in mineru)" >&2
        exit 78
    fi

    if mineru_healthy; then
        exec mineru "${MINERU_VERB[@]}" "$@"
    fi

    # Fallback path: mineru missing or unhealthy. Use the live original
    # binary directly so cron/doctor/skills keep working.
    if [[ ! -x "$FALLBACK_BIN" ]]; then
        echo "shim_lib: fallback binary missing or not executable: $FALLBACK_BIN" >&2
        echo "shim_lib: (mineru CLI was also unavailable — no safe path forward)" >&2
        exit 127
    fi
    exec "$FALLBACK_BIN" "$@"
}

# Polymorphic dispatch entry point for tools whose FIRST argv position is
# itself a verb (e.g. `gog-firewall gmail search ...`, `gog-firewall
# calendar events ...`). Unlike `shim_run`, the mineru noun depends on
# arg1, so the shim provides a `route <arg1>` Bash function that
# populates the `ROUTED_VERB` array global with the mineru noun (and
# optional sub-noun) to dispatch to. An empty ROUTED_VERB means
# "unknown verb, fall through to the fallback with the ORIGINAL argv
# intact" — that is the compound-safety-net principle: a route we
# haven't wired to mineru yet MUST NOT block a legacy caller.
#
# Contract:
#   shim_route <fallback_bin> <arg1> <rest...>
#
# The shim must define, before calling this helper:
#
#   route() {
#       # $1 == arg1. Set ROUTED_VERB=(mineru noun...) on hit,
#       # or ROUTED_VERB=() on miss.
#       case "$1" in
#           gmail)    ROUTED_VERB=(gmail) ;;
#           calendar) ROUTED_VERB=(calendar) ;;
#           *)        ROUTED_VERB=() ;;
#       esac
#   }
#
# Health-probe + timeout semantics are IDENTICAL to `shim_run` — the
# probe is `mineru --help` with the same 2s cap, so a broken mineru
# still falls through cleanly regardless of which verb was requested.
shim_route() {
    if [[ $# -lt 1 ]]; then
        echo "shim_lib: shim_route needs at least <fallback_bin>" >&2
        exit 78
    fi
    local fallback="$1"
    shift
    if ! declare -F route >/dev/null 2>&1; then
        echo "shim_lib: polymorphic shim must define a route() function" >&2
        exit 78
    fi

    # Reset the routing output; `route` populates it, we read it.
    ROUTED_VERB=()
    local first_arg="${1:-}"
    route "$first_arg"

    # Healthy CLI + known route → exec `mineru <verb> <rest...>`.
    # We consume the routing arg (first_arg) since the mineru noun
    # replaces it; the rest of argv is forwarded verbatim.
    if [[ ${#ROUTED_VERB[@]} -gt 0 ]] && mineru_healthy; then
        shift || true  # drop first_arg; no-op when argv was empty
        exec mineru "${ROUTED_VERB[@]}" "$@"
    fi

    # Fall-through path: mineru unhealthy OR route unknown. Pass the
    # ORIGINAL argv (first_arg + rest) to the fallback so its own verb
    # dispatch still works — the fallback IS a polymorphic tool too.
    if [[ ! -x "$fallback" ]]; then
        echo "shim_lib: fallback binary missing or not executable: $fallback" >&2
        echo "shim_lib: (mineru CLI was also unavailable — no safe path forward)" >&2
        exit 127
    fi
    exec "$fallback" "$@"
}
