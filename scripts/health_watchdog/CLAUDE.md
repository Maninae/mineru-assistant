# scripts/health_watchdog/ — System Health Watchdog

A launchd job (`<prefix>.health-watchdog`, every 300 s) that notices a wedged Mac and says so loudly: a stuck `tccd` freezing protected-file reads, memory/swap pressure, sustained load, hung scheduled jobs (which it kills), a leaked stealth-browser tab pile (which it stops), repeated Landline silent-kills, and low disk. One Telegram message per run, per-check cooldowns, a "recovered" line when a check clears, everything also logged locally.

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
__main__.py            orchestrator: logging, pause guard, run order, deliver, persist
   ├── process_table.py     ps + launchctl list -> ProcessInfo rows, label->pid, tree helpers
   ├── plist_inventory.py   LaunchAgents plists -> which labels are KeepAlive services
   ├── system_probes.py     swap / memory_pressure / load / ncpu / disk / tccd cpu -> SystemSnapshot
   ├── tcc_probe.py         bounded sqlite3 read of a TCC-protected file -> ok | hung | error
   ├── landline_health.py   count "Claude silent for" lines in the daemon log window
   ├── browser_hygiene.py   /health + /tabs, renderer processes, `bin/browser action=stop`
   ├── hung_jobs.py         SELECTOR (pure) + kill_job_trees (TERM, wait, KILL, wait)
   ├── evaluate.py          thresholds -> Finding (pure; streaks via state.bump_streak)
   ├── state.py             JSON state, cooldown + recovery decisions (pure)
   ├── alerting.py          message format, deliver-output.py --raw, bounded osascript fallback
   ├── findings.py          Finding / Severity
   ├── subprocess_deadline.py  run_with_deadline(): poll, TERM, KILL, else ABANDON
   └── config.py            every threshold, path, deadline, allowlist
```

Dependencies flow downward only: `__main__` -> probes/actions -> `process_table`/`subprocess_deadline` -> `config`. `evaluate.py` and `state.py` never run a command, which is what makes the threshold and cooldown logic unit-testable.

## Invariants

- **Nothing may hang the watchdog.** Every child goes through `run_with_deadline`; a child that survives SIGKILL is abandoned, never awaited. The TCC probe records its abandoned pid in state and refuses to start another while that pid lives (no piling onto a frozen tccd).
- **The plist must launch through `/bin/bash`.** Under launchd, TCC's Full Disk Access grant follows the job's main program: with `/bin/bash` as the program, `sqlite3` and `/usr/bin/python3` children can read `~/Library/Messages/chat.db`; with `/usr/bin/python3` as the program every reader is denied (verified 2026-10-13 with scratch agents). The template therefore runs `bash -c "/usr/bin/python3 -m health_watchdog"` with no `exec`. Homebrew python and `imsg` fail either way, so keep the probe on `/usr/bin/sqlite3`. A probe `error` finding right after an install means this invariant was broken, not that tccd is stuck.
- **False-kill is worse than false-negative.** The hung-job selector only picks (a) a running non-KeepAlive `<prefix>.*` label's pid from `launchctl list`, or (b) an orphan (ppid 1, no TTY, our user) whose command names a workspace script, an imsg firewall binary, or a headless `claude --print` carrying the `recurring/` job-prompt marker; and only once the root is older than `MAX_JOB_AGE_SECONDS` (3 h). It never touches a KeepAlive/allowlisted service tree, anything with a TTY (or under a TTY process), another user's process, or itself. Widen `SERVICE_*` allowlists freely; narrow the job markers only with a test.
- **Actions bypass cooldowns and never "recover".** A kill or a browser stop is reported every time it happens (`Finding.action_taken`).
- **No per-install literals** (names, home paths): the engine's leak-gate tests scan this directory. Derive paths from `MINERU_HOME` / `HOME`.
- **Pause file** `cache/paused-jobs/health-watchdog.until` uses the same date semantics as `cc-job-lib.sh`, so `bin/pause-job` works unchanged.

## State and logs

| What | Where |
|------|-------|
| Rotating log | `$MINERU_HOME/logs/health-watchdog/watchdog.log` (+ launchd `stdout.log` / `stderr.log`) |
| State (streaks, cooldowns, active checks, stuck probe pid) | `$MINERU_HOME/cache/health-watchdog-state.json` |
| Plist template | `engine/launchd/LABEL_PREFIX.health-watchdog.plist.template` |

## Adding a check

1. Probe in the module that owns the data source (or a new `<thing>_probe.py` if it is a new source); return plain data, run commands only through `run_with_deadline`.
2. Threshold -> `Finding` in `evaluate.py`; constants in `config.py`; sustained checks use `bump_streak`.
3. Wire it in `__main__.collect_findings`, add a `RECOVERED_TITLES` entry in `alerting.py`.
4. Tests: `tests/test_health_watchdog_probes.py` (parser), `tests/test_health_watchdog_alerting_logic.py` (threshold + cooldown), `tests/test_health_watchdog_hung_jobs.py` (selector). Run with `uv run --with pytest pytest tests/test_health_watchdog_*.py`.
