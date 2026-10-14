# scripts/health_watchdog/ — System Health Watchdog

A launchd job (`<prefix>.health-watchdog`, every 300 s) that notices a wedged Mac and says so loudly: a stuck `tccd` freezing protected-file reads, memory/swap pressure, sustained load, hung scheduled jobs (which it kills), a runaway stealth-browser renderer (which it stops), stealth-browser tab piles and week-idle tabs (which it only lists), a wedged OrbStack VM (which it can restart), repeated Landline silent-kills, and low disk. One Telegram message per run, per-check cooldowns, a "recovered" line when a check clears, everything also logged locally.

**Python 3.9 only.** launchd runs it under `/usr/bin/python3`. No `X | None`, no `match`, `typing.Optional/List/Dict` in annotations. Stdlib only. After any edit: `/usr/bin/python3 -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('scripts/health_watchdog/*.py')]"`.

## Run it

```bash
cd $MINERU_HOME && DRY_RUN=1 PYTHONPATH=$MINERU_HOME/scripts /usr/bin/python3 -m health_watchdog   # print, change nothing
PYTHONPATH=$MINERU_HOME/scripts /usr/bin/python3 -m health_watchdog                                # real run (what launchd does)
bin/pause-job health-watchdog 2026-12-31                                                           # pause through a date
launchctl bootout gui/$(id -u)/<prefix>.health-watchdog                                           # stop entirely
```

`DRY_RUN=1` prints the message it would send and the pids it would kill, and does not persist state (so streak counters are untouched).

## Architecture

```
run-health-watchdog.sh launchd entry (bash stays the job's program; see TCC invariant)
__main__.py            orchestrator: logging, pause guard, run order, deliver, persist
   ├── process_table.py     ps + launchctl list -> ProcessInfo rows, label->pid, tree helpers
   ├── plist_inventory.py   LaunchAgents plists -> which labels are KeepAlive services
   ├── system_probes.py     swap / memory_pressure / load / ncpu / disk / tccd cpu -> SystemSnapshot
   ├── tcc_probe.py         bounded sqlite3 read of a TCC-protected file -> ok | hung | error
   ├── landline_health.py   count "Claude silent for" lines in the daemon log window
   ├── browser_hygiene.py   /health + /tabs, tab timestamps, renderer processes, `bin/browser action=stop`
   ├── orbstack_health.py   helper ps stats, `orb status` -> bounded `docker version`, `orb stop`/`orb start`
   ├── hung_jobs.py         SELECTOR (pure) + kill_job_trees (TERM, wait, KILL, wait)
   ├── evaluate.py          thresholds -> Finding (pure; streaks via state.bump_streak)
   ├── evaluate_orbstack.py OrbStack wedge / stuck-exit / restart decision (pure, same rules)
   ├── state.py             JSON state, cooldown + recovery decisions (pure)
   ├── alerting.py          message format, deliver-output.py --raw, bounded osascript fallback
   ├── findings.py          Finding / Severity
   ├── subprocess_deadline.py  run_with_deadline(): poll, TERM, KILL, else ABANDON
   └── config.py            every threshold, path, deadline, allowlist
```

Dependencies flow downward only: `__main__` -> probes/actions -> `process_table`/`subprocess_deadline` -> `config`. `evaluate.py`, `evaluate_orbstack.py` and `state.py` never run a command, which is what makes the threshold and cooldown logic unit-testable.

## Invariants

- **Nothing may hang the watchdog.** Every child goes through `run_with_deadline`; a child that survives SIGKILL is abandoned, never awaited. The TCC probe records its abandoned pid in state and refuses to start another while that pid lives (no piling onto a frozen tccd).
- **The plist must launch through `/bin/bash` via the wrapper script.** Under launchd, TCC's Full Disk Access grant follows the job's main program: with `/bin/bash` as the program, `sqlite3` and `/usr/bin/python3` children can read `~/Library/Messages/chat.db`; with `/usr/bin/python3` as the program every reader is denied (verified 2026-10-13 with scratch agents). The template runs `/bin/bash .../run-health-watchdog.sh`, and the script runs python3 as a non-final command so bash stays alive around it (`bash -c "<one command>"` execs the command away, which bash 3.2 does). Homebrew python and `imsg` fail either way, so keep the probe on `/usr/bin/sqlite3`. A probe `error` finding right after an install means this invariant was broken, not that tccd is stuck.
- **False-kill is worse than false-negative.** The hung-job selector only picks (a) a running non-KeepAlive `<prefix>.*` label's pid from `launchctl list`, or (b) an orphan (ppid 1, no TTY, our user) whose command names a script a scheduled plist launches (`plist_inventory.job_script_paths`), one of the engine's firewall helpers (`JOB_COMMAND_MARKERS`), or a headless `claude --print` carrying the `recurring/` job-prompt marker; and only once the root is older than `MAX_JOB_AGE_SECONDS` (3 h). It never touches a KeepAlive/allowlisted service tree, anything with a TTY (or under a TTY process), another user's process, a hand-run or nohup'd workspace script, or itself. Widen `SERVICE_*` allowlists freely; narrow the job markers only with a test.
- **SIGKILL survivors are remembered, not re-signalled.** `state["survivors"]` maps pid to start time (pid-reuse guard); later runs skip those pids and raise one standing CRITICAL (`hung_jobs_survivor`) under the normal 30 min cooldown, which recovers when the processes finally disappear.
- **Anti-flap.** Memory and load latch on at their WARN bar and release only under a lower CLEAR bar (`state["flags"]`), and must hold for `*_SUSTAINED_RUNS` before alerting. "Recovered" lines are sent only for checks whose last sent alert was CRITICAL, once per sent alert; cooldown records survive recovery, so a WARN that flaps stays silent.
- **Actions bypass cooldowns and never "recover".** A kill, a browser stop, an OrbStack restart, or terminating orphaned profile Chromium is reported every time it happens (`Finding.action_taken`). A kill that reports survivors also starts the `hung_jobs_survivor` cooldown (`Finding.also_cools`), so the first survivor costs one CRITICAL, not two.
- **Orphaned Chromium is decided from the process table only.** Profile Chromium counts as orphaned when no `/browser/server.py` process exists in `ps` (the 5 s `/health` probe never authorizes a kill, since it can time out under load with the server alive) and only after `BROWSER_ORPHAN_SUSTAINED_RUNS` consecutive runs.
- **`docker` is never called unless `orb status` says Running.** Any `docker` call auto-launches a stopped OrbStack, so the probe order is `orb status` (bounded; a hang counts as hung) and only then `docker version`. Stopped is a deliberate state (or the app is not set to start at login): no finding, no auto-start. Test: `test_docker_is_never_invoked_when_orb_status_is_stopped`.
- **OrbStack restarts are opt-in and rationed.** Only with `orbstack_auto_restart: true` in the per-install config, at most once per `ORBSTACK_RESTART_COOLDOWN_S`, and never while a helper is stuck in exit state.
- **A finding may carry its own cooldown** (`Finding.cooldown_seconds`); the daily tab notices use 24 h instead of the WARN default.
- **A crash of the watchdog itself** logs the traceback and sends one short Telegram note (bounded, best effort), exit code 1.
- **No per-install literals** (names, home paths): the engine's leak-gate tests scan this directory. Derive paths from `MINERU_HOME` / `HOME`.
- **Pause file** `cache/paused-jobs/health-watchdog.until` uses the same date semantics as `cc-job-lib.sh`, so `bin/pause-job` works unchanged.

## State and logs

| What | Where |
|------|-------|
| Rotating log | `$MINERU_HOME/logs/health-watchdog/watchdog.log` (+ launchd `stdout.log` / `stderr.log`) |
| State (streaks, cooldowns, active checks, stuck probe pid) | `$MINERU_HOME/cache/health-watchdog-state.json` |
| Plist template | `engine/launchd/LABEL_PREFIX.health-watchdog.plist.template` |
| Per-install overrides (optional) | `$MINERU_HOME/config/health-watchdog.json`: `offload_volume`, `job_max_age_overrides_seconds`, `extra_service_command_fragments`, `extra_job_command_markers` (private scripts jobs spawn indirectly), `orbstack_auto_restart` (default false); starter at `engine/config/health-watchdog.example.json` |

## Adding a check

1. Probe in the module that owns the data source (or a new `<thing>_probe.py` if it is a new source); return plain data, run commands only through `run_with_deadline`.
2. Threshold -> `Finding` in `evaluate.py`; constants in `config.py`; sustained checks use `bump_streak`.
3. Wire it in `__main__.collect_findings`, add a `RECOVERED_TITLES` entry in `alerting.py`.
4. Tests: `tests/test_health_watchdog_probes.py` (parser), `tests/test_health_watchdog_orbstack.py` (OrbStack probe order + logic), `tests/test_health_watchdog_alerting_logic.py` (threshold + cooldown), `tests/test_health_watchdog_hung_jobs.py` (selector). Run with `uv run --with pytest pytest tests/test_health_watchdog_*.py`.

## Stealth-browser tabs: surfaced, not reaped

Tabs opened by interactive sessions may legitimately sit open for hours, overnight, or days, so tab count never stops the browser. Instead:

| Condition | Finding | Action | Cooldown |
|-----------|---------|--------|----------|
| More than `BROWSER_TABS_HEADSUP` (20) tabs | WARN listing up to 25 tabs as `<age> · <url>` | none | 24 h |
| Tabs whose `lastActivityAt` is older than `BROWSER_IDLE_TAB_DAYS` (7) | WARN listing `targetId · idle Nd · url`, ending in a reply hint | none (the user replies to close) | 24 h |
| A renderer at or above 50% CPU for 3 runs | browser stopped | `bin/browser action=stop` | action, always reported |
| Profile Chromium with no browser-server process, 2 runs | orphans terminated | TERM/KILL | action, always reported |

Tab data comes from `/tabs` (`openedAt`, `lastActivityAt`, `openedBy`, `url`, ISO 8601 with offset). The browser server logs every tab close (`Closed tab ... (explicit close)` / `Tab ... closed by page`) in `logs/browser-server/server.log`, so a disappearing tab can be traced.

## A wedged OrbStack: what it looks like and what to do

When the host runs short of RAM, macOS swaps out the OrbStack VM's memory and the VM stops making progress.

- **Symptoms:** every `docker` CLI call hangs; the `OrbStack Helper` process sits at 300-400% CPU doing no real work; `~/.orbstack/log/vmgr.log` fills with `rcu_sched detected stalls on CPUs/tasks` (about one every 5 minutes); host swap is near full and load climbs, which starves everything else (tccd included).
- **Fix:** `orb stop && orb start` restarts the VM and is enough. The watchdog does this itself when `orbstack_auto_restart` is true. A Mac reboot is only needed when the helper is stuck in exit state (ps STAT contains `E`, rss 0, shown as `(OrbStack Helper)`; SIGKILL cannot remove it, and the watchdog raises a CRITICAL saying so) or when the Mac has been up for months.
- **Prevention:** cap the VM's memory (`orb config set memory_mib <MiB>`) so it can never take a large share of host RAM; a VM that slowly grows to its ceiling over months of uptime is the usual path into this state.

