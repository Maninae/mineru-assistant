# bin_shims — legacy tool shims with an import-failure fallback

This directory holds the thin Bash shims that replace the current
`$MINERU_HOME/bin/<tool>` entry points during Phase 2 of the CLI cutover.
Every shim delegates to `mineru <verb>` when the CLI is healthy and
**falls through to the live original binary** when it isn't. The whole
point is a safety net: a broken `mineru` package must not take down cron
jobs, doctor, or skills.

Reference the 2026-07-25 capability spec: §3.4 (backward compatibility
with `bin/` tools) and Open Q#15 (cutover-day contract).

## Invariant

**Every file in `bin_shims/` is self-contained Bash. No Python dependency
of its own.** The fallback path in particular never imports mineru and
never activates the venv — it just `exec`s the original binary. That is
what keeps the safety net working when the mineru Python is broken.

Corollary: `bin_shims/` may only source files inside `bin_shims/`. Today
that means one shared helper, `_shim_lib.sh`.

## The safety-net contract

Each shim tries two paths in order:

1. **Healthy CLI path.** If `mineru` is on `PATH` and `mineru --help`
   exits 0 within `MINERU_SHIM_HEALTH_TIMEOUT` seconds (default `2`),
   `exec mineru <verb> "$@"`. The user gets the new CLI.

2. **Fallback path.** Otherwise, `exec` the live original binary at
   `FALLBACK_BIN` (e.g. `${MINERU_HOME:-$HOME/.mineru}/bin/msearch`). The user
   gets the exact same behavior they had before the shim existed.

Why this exists (from §3.4 + Open Q#15): during Phase 2 the legacy
`bin/<tool>` entry points become `exec mineru <verb>` shims. If `mineru`
itself later fails to import (bad refactor, missing dep, venv broken,
Landline daemon shipping a partial upgrade), every legacy caller would
break — cron, doctor, skills. The fallback makes the shim a true safety
net: mineru broken → cron still runs, doctor still works, skills still
succeed.

### Why `mineru --help` is a safe probe

The root Typer app renders `--help` without touching any live engine
(msearch, gog-firewall, imsg-firewall). The probe is fast (~150ms on a
warm cache) and, crucially, doesn't fail just because a specific wrapper
subpackage is broken — it only fails if the root `mineru_cli` package
itself is broken, which is exactly the failure mode the fallback exists
to survive.

The 2-second timeout is enforced by `perl -e 'alarm ...; exec ...'`
(perl is guaranteed on macOS; coreutils `timeout` / `gtimeout` are not).
A hung `mineru --help` triggers SIGALRM at 2s, perl exits 142, the probe
returns non-zero, and the shim falls through to the original binary. The
shim **never hangs forever**.

## Authoring a new shim

Copy `msearch` as a template. A new shim is ~5 lines of real code:

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_shim_lib.sh
source "$SCRIPT_DIR/_shim_lib.sh"

MINERU_VERB=(gmail)                              # noun (+ sub-noun) to dispatch
FALLBACK_BIN=${MINERU_HOME:-$HOME/.mineru}/bin/gog-firewall  # live original binary

shim_run "$@"
```

Two knobs, nothing else:

- `MINERU_VERB` (Bash array): the argv tail `mineru ...` is dispatched
  with when healthy. The user's argv is appended verbatim, so
  `msearch tags` → `mineru memory tags`. Multi-word verbs work via array
  syntax (e.g. `MINERU_VERB=(memory search)` if a future shim needs it).
- `FALLBACK_BIN` (string): resolved path to the live original binary,
  defaulting under `$MINERU_HOME` (`${MINERU_HOME:-$HOME/.mineru}/bin/<tool>`).
  Must exist and be executable — this is the file that serves
  cron/doctor/skills when the CLI is unavailable.

`_shim_lib.sh` handles the health probe, the 2s timeout cap, and the
dispatch. Do NOT reimplement any of that in the shim itself; keep them
one-liners so the safety-net logic has one home.

## Roster (grows during Phase 2)

| Shim | Kind | Dispatch | `FALLBACK_BIN` |
|---|---|---|---|
| `msearch` | one-liner (`shim_run`) | `mineru memory` | `$MINERU_HOME/bin/msearch` |
| `gog-firewall` | **polymorphic** (`shim_route`) | see routing table below | `$MINERU_HOME/bin/gog-firewall` |
| `imsg-firewall` | one-liner (`shim_run`) | `mineru imessage` | `$MINERU_HOME/bin/imsg-firewall` |
| `monarch` | one-liner (`shim_run`) | `mineru finance` | `$MINERU_HOME/bin/monarch` (user-provided; see note) |
| _(future)_ `browser`, ... | ... | ... | ... |

Additional shims land as their `mineru <verb>` wire-up is completed.

> **`monarch` fallback note:** the engine does not ship `bin/monarch` (it was an
> install-local symlink into a sibling `monarch-money-cli` repo). Its shim
> fallback only fires when the mineru CLI is unavailable AND the user has
> provided their own `$MINERU_HOME/bin/monarch`.

### `gog-firewall` is POLYMORPHIC — landmine callout

`gog-firewall`'s first argv is itself a verb (`gog-firewall gmail search
foo`, `gog-firewall calendar events bar`, `gog-firewall drive ls ...`,
...). The Phase-4 cutover doc (§7 LANDMINE) explicitly forbids a naive
`MINERU_VERB=(gmail)` shim: that would silently misroute every
non-gmail call to `mineru gmail`. The shim MUST route on `$1`, and
that is why it uses `shim_route` + a local `route()` function instead
of the one-liner `shim_run` path.

Routing table (kept in lock-step with the shim itself):

| `gog-firewall <arg1>` | → | `mineru <noun>` |
|---|---|---|
| `gmail`    | → | `mineru gmail` |
| `calendar` | → | `mineru calendar` |
| `drive`    | → | `mineru drive` |
| `docs`     | → | `mineru docs` |
| `sheets`   | → | `mineru sheets` |
| `contacts` | → | `mineru contacts` |
| `tasks`    | → | `mineru tasks` |
| `people`   | → | `mineru people` |
| `groups`   | → | `mineru groups` |
| _anything else_ | → | live `$MINERU_HOME/bin/gog-firewall` (compound-safety-net) |

An unknown verb (e.g. `gog-firewall auth status`) falls through to the
live fallback with the ORIGINAL argv intact — never an error. This is
the compound-safety-net principle: a route we haven't wired to mineru
yet must not block a legacy caller.

## Testing a shim

- **Healthy path:** run `bin_shims/<name> ...` with mineru on PATH. The
  output must match `mineru <verb> ...` byte-for-byte, since the shim
  just `exec`s that command.
- **Fallback path:** poison PATH (`PATH=/broken bin_shims/<name> ...`)
  or point at a broken venv. The shim must exec the original binary and
  succeed.
- **Timeout path:** shadow `mineru` on PATH with a script that sleeps
  longer than `MINERU_SHIM_HEALTH_TIMEOUT`. The probe must time out and
  the shim must fall through to the original binary within ~2s + epsilon.
- **Polymorphic path (`gog-firewall` only):** every routing key in the
  table above must reach the matching `mineru <noun>`, and one unknown
  key must fall through to the live fallback without error.

The pytest suite in `tests/test_shims.py` exercises every path for
`msearch`, `gog-firewall`, `imsg-firewall`, and `monarch` end-to-end
using real subprocesses.

## Install-time health-probe smoke test

After installing a new shim (or shipping a mineru upgrade), verify the
health probe still works. This reinforces the LANDMINE (§7 of the
Phase-4 cutover doc) that `mineru --help` must NEVER fail — every shim
depends on it as the "healthy CLI" signal.

```bash
# 1. Probe the CLI directly. Must exit 0 within 2s.
time mineru --help >/dev/null && echo "probe OK"

# 2. Confirm each shim's healthy path resolves. --help is a safe smoke
#    test — no engines touched, no writes attempted, no external calls.
for shim in msearch gog-firewall imsg-firewall monarch; do
    printf "%-15s → " "$shim"
    if [[ "$shim" == "gog-firewall" ]]; then
        # Polymorphic: pass a real routing verb.
        bin_shims/$shim gmail --help >/dev/null 2>&1 && echo OK || echo FAIL
    else
        bin_shims/$shim --help >/dev/null 2>&1 && echo OK || echo FAIL
    fi
done
```

A `FAIL` anywhere means the shim's routing (or the CLI's `--help`)
regressed. Do NOT ship a cutover with any FAIL — the shim's whole job
is to preserve the legacy contract, and a failing `--help` breaks the
health probe every downstream shim depends on.
