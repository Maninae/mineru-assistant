"""Thresholds, paths, deadlines, and allowlists for the health watchdog.

Python 3.9 compatible (launchd runs this under /usr/bin/python3): no `X | None`,
no `match`, typing.Optional/List/Dict instead of builtin generics in annotations.

Every tunable lives here as a named constant. Paths derive from MINERU_HOME (the
workspace) and HOME so the package carries no per-install literals.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
LAUNCHD_PREFIX = os.environ.get("MINERU_LAUNCHD_PREFIX", "com.mineru")
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

WATCHDOG_JOB_NAME = "health-watchdog"
LOG_DIR = MINERU_HOME / "logs" / WATCHDOG_JOB_NAME
LOG_FILE = LOG_DIR / "watchdog.log"
LOG_ROTATE_BYTES = 1_000_000
LOG_ROTATE_COUNT = 3
STATE_FILE = MINERU_HOME / "cache" / "health-watchdog-state.json"
# Same pause convention as cc-job-lib.sh: `bin/pause-job health-watchdog YYYY-MM-DD`.
PAUSE_FILE = MINERU_HOME / "cache" / "paused-jobs" / (WATCHDOG_JOB_NAME + ".until")

DELIVER_SCRIPT = MINERU_HOME / "scripts" / "deliver-output.py"
BROWSER_CLI = MINERU_HOME / "bin" / "browser"
BROWSER_TABS_SNAPSHOT = MINERU_HOME / "cache" / "browser-tabs.json"
BROWSER_PROFILE_DIR = MINERU_HOME / "cache" / "cloakbrowser-profile"
BROWSER_PORT = int(os.environ.get("MINERU_BROWSER_PORT", "9471"))
LANDLINE_DAEMON_LOG = MINERU_HOME / "logs" / "telegram-daemon" / "daemon.log"

# TCC probe: a read-only sqlite query against Messages' chat.db. Under launchd the
# Full Disk Access grant follows the job's MAIN program: with /bin/bash as the
# program every Apple-signed child (sqlite3, /usr/bin/python3) can read chat.db;
# with /usr/bin/python3 as the program all of them are denied. Hence the plist runs
# `/bin/bash run-health-watchdog.sh` like every other job. Homebrew python and imsg
# fail either way.
TCC_PROBE_DB = Path.home() / "Library" / "Messages" / "chat.db"
TCC_PROBE_SQLITE_BIN = "/usr/bin/sqlite3"
TCC_PROBE_QUERY = "select count(*) from chat;"
TCC_FIX_TEXT = (
    "Fix: run `sudo killall tccd` (safe, launchd respawns it) or reboot."
)

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() not in ("", "0", "false", "no")

# Deadlines (seconds). Every subprocess the watchdog starts is bounded by one.
TCC_PROBE_DEADLINE_SECONDS = 20
QUICK_COMMAND_DEADLINE_SECONDS = 10
DELIVER_DEADLINE_SECONDS = 60
BROWSER_HTTP_DEADLINE_SECONDS = 5
BROWSER_STOP_DEADLINE_SECONDS = 30
NOTIFY_DEADLINE_SECONDS = 5
KILL_TERM_GRACE_SECONDS = 10
KILL_KILL_GRACE_SECONDS = 5

# tccd CPU (ps %cpu, a decaying ~1 min average) sustained across runs.
TCCD_CPU_WARN_PERCENT = 50.0
TCCD_CPU_SUSTAINED_RUNS = 3

# Memory: memory_pressure free % is the signal; swap used fraction (sysctl vm.swapusage)
# only counts while free memory is also getting tight.
# - macOS never pages swap back in until the pages are touched, so swap stays near full
#   for days after one busy moment while half the RAM sits free. Swap alone is not pressure.
# - Each bar has a looser CLEAR bar (hysteresis) plus a sustained streak, so a metric
#   sitting on a line does not flap.
SWAP_USED_WARN_FRACTION = 0.80
SWAP_USED_CLEAR_FRACTION = 0.70  # used only when the free-memory probe failed
SWAP_USED_CRITICAL_FRACTION = 0.90
MEMORY_FREE_WARN_PERCENT = 15
MEMORY_FREE_CLEAR_PERCENT = 25
MEMORY_FREE_CRITICAL_PERCENT = 5
# High swap warns only at or below this free %, and releases only above the CLEAR bar.
MEMORY_FREE_SWAP_GATE_PERCENT = 30
MEMORY_FREE_SWAP_GATE_CLEAR_PERCENT = 40
MEMORY_SUSTAINED_RUNS = 2

# Load: 5-minute average divided by core count, sustained across runs, with the
# same hysteresis (stays "bad" until it drops under the CLEAR bar).
LOAD_PER_CORE_WARN = 1.0
LOAD_PER_CORE_CLEAR = 0.8
LOAD_PER_CORE_CRITICAL = 1.5
LOAD_SUSTAINED_RUNS = 3

# A hung-job pid that survived SIGKILL is remembered (pid + start time, so a reused
# pid is not mistaken for it) and not re-signalled; its alert follows the CRITICAL
# cooldown instead of firing every run.
SURVIVOR_START_TOLERANCE_SECONDS = 120

# Hung jobs: normal CC jobs finish in 1-25 min and the largest plist TimeOut is 2 h.
MAX_JOB_AGE_SECONDS = 3 * 3600
# Per-job overrides keyed by label suffix (the part after LAUNCHD_PREFIX + ".").
JOB_MAX_AGE_OVERRIDES_SECONDS = {}  # type: dict

# Browser hygiene. Tab piles are SURFACED, never reaped: tabs may legitimately sit
# open for hours or days, so only a runaway renderer or orphaned Chromium is acted on.
BROWSER_TABS_HEADSUP = 20
BROWSER_TABS_HEADSUP_COOLDOWN_S = 24 * 3600
BROWSER_IDLE_TAB_DAYS = 7
BROWSER_IDLE_TABS_COOLDOWN_S = 24 * 3600
# Tab lines show host + the last N chars of the path (the distinguishing end, no query).
BROWSER_TAB_PATH_TAIL_CHARS = 40
BROWSER_RENDERER_CPU_PERCENT = 50.0
BROWSER_RENDERER_CPU_SUSTAINED_RUNS = 3
BROWSER_TAB_SUMMARY_LIMIT = 8  # also caps each tab notice's list
# Profile Chromium counts as orphaned only when NO browser-server process is in the
# ps table (a slow /health must never authorize a kill), and only after N runs.
BROWSER_SERVER_COMMAND_MARKER = "/browser/server.py"
BROWSER_ORPHAN_SUSTAINED_RUNS = 2
# Real page-open probe: /health answers 200 even when the server's Python cannot
# import playwright, so a live server is asked to open (and close) about:blank.
# Only with 0 tabs (never alongside a session's tabs); every 30 min while passing,
# every run while failing; alert after N failures in a row.
BROWSER_OPEN_PROBE_URL = "about:blank"
BROWSER_OPEN_PROBE_INTERVAL_SECONDS = 30 * 60
BROWSER_OPEN_PROBE_DEADLINE_SECONDS = 60  # covers a cold Chromium launch
BROWSER_OPEN_PROBE_SUSTAINED_RUNS = 2
BROWSER_OPEN_PROBE_ERROR_MAX_CHARS = 200

# OrbStack (the Docker/Linux VM). A VM whose memory got swapped out spins its helper
# at 300-400% CPU doing no real work, and every `docker` call hangs. Any `docker` call
# also AUTO-LAUNCHES a stopped OrbStack, so docker is probed only after `orb status`
# says Running. Binaries are looked up by absolute path: launchd's PATH has no
# /opt/homebrew or /usr/local.
ORBSTACK_APP_PATH = Path("/Applications/OrbStack.app")
ORB_BIN_CANDIDATES = (
    "/opt/homebrew/bin/orb",
    "/usr/local/bin/orb",
    str(ORBSTACK_APP_PATH / "Contents" / "MacOS" / "bin" / "orb"),
)
DOCKER_BIN_CANDIDATES = (
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    str(Path.home() / ".orbstack" / "bin" / "docker"),
    str(ORBSTACK_APP_PATH / "Contents" / "MacOS" / "xbin" / "docker"),
)
ORBSTACK_HELPER_COMMAND_MARKER = "OrbStack Helper"
ORBSTACK_STATUS_DEADLINE_S = 30
ORBSTACK_DOCKER_PROBE_DEADLINE_S = 30
ORBSTACK_RESTART_DEADLINE_S = 120
ORBSTACK_HELPER_CPU_WARN_PERCENT = 200.0
ORBSTACK_CPU_SUSTAINED_RUNS = 3
ORBSTACK_HUNG_SUSTAINED_RUNS = 2
# A helper stuck in exit state (ps STAT contains "E") ignores SIGKILL; only a reboot clears it.
ORBSTACK_STUCK_EXIT_SUSTAINED_RUNS = 2
ORBSTACK_RESTART_COOLDOWN_S = 3600
# The stuck-exit CRITICAL repeats until a reboot, so it re-alerts every 6 h, not every 30 min.
ORBSTACK_STUCK_EXIT_COOLDOWN_S = 6 * 3600
# After `orb start`, dockerd needs time: re-probe up to N times, this far apart, within the budget.
ORBSTACK_RESTART_PROBE_ATTEMPTS = 4
ORBSTACK_RESTART_PROBE_INTERVAL_S = 15
ORBSTACK_RESTART_VERIFY_BUDGET_S = 120

# Landline: "Claude silent for Ns, killing process" lines within the window.
LANDLINE_SILENT_WINDOW_SECONDS = 6 * 3600
LANDLINE_SILENT_WARN_COUNT = 2
LANDLINE_LOG_TAIL_BYTES = 2_000_000

# Missed runs: a scheduled job whose newest sign of life is older than its longest
# scheduled gap plus grace raised a CRITICAL (so a "ran again" recovery line follows).
# Grace = max(gap x FACTOR, gap + MIN); the 12 h cooldown keeps a dead daily job to
# two pings a day.
MISSED_RUN_GRACE_FACTOR = 1.5
MISSED_RUN_MIN_GRACE_SECONDS = 4 * 3600
MISSED_RUN_COOLDOWN_SECONDS = 12 * 3600
# The Pulse job registry (label suffix -> display name + freshness signal), shared with app/.
LAUNCHD_JOBS_REGISTRY_FILE = Path(
    os.environ.get("MINERU_LAUNCHD_JOBS_FILE", str(MINERU_HOME / "config" / "launchd-jobs.json"))
)
PAUSED_JOBS_DIR = MINERU_HOME / "cache" / "paused-jobs"
JOB_LOGS_DIR = MINERU_HOME / "logs"

# Disk on /.
DISK_FREE_WARN_GB = 10.0
DISK_FREE_CRITICAL_GB = 5.0

TOP_PROCESS_COUNT = 5

# Alert cooldowns per check; an escalation (WARN -> CRITICAL) bypasses them.
WARN_COOLDOWN_SECONDS = 2 * 3600
CRITICAL_COOLDOWN_SECONDS = 30 * 60

# ---------------------------------------------------------------------------
# Hung-job selector allowlists. False-kill is worse than false-negative, so
# every list here widens the protected set, never the kill set.

# Label suffixes that are services, even if a plist lacks KeepAlive.
SERVICE_LABEL_ALLOWLIST = (
    "telegram-daemon",
    "webapp",
    "daemon-watchdog",
    WATCHDOG_JOB_NAME,
)
# Command-line fragments that mark a long-running service process (and its tree).
SERVICE_COMMAND_ALLOWLIST = (
    "-m landline",
    "/app/server.py",
    "/browser/server.py",
    "Chromium",
    "cloakbrowser",
)
# Command-line fragments that mark an ORPHAN as part of a scheduled job: the engine's
# firewall helpers that jobs spawn. The scheduled scripts themselves are derived at
# runtime from the plists' ProgramArguments (plist_inventory.job_script_paths), so a
# hand-run or nohup'd workspace script is never mistaken for a job.
JOB_COMMAND_MARKERS = (
    "/bin/imsg-firewall",
    "/bin/imsg-named",
    "/bin/gog-firewall",
    "/scripts/boundary_validator.py",
    "/scripts/sweep_group_members.py",
    "/scripts/build_contact_map.py",
    "/scripts/extract-cc-sessions.py",
    "/scripts/export-journals.py",  # launched via run-pre-export-journals.sh, so not in its plist argv
)
# run_cc_job's prompt is "Read recurring/<job>.md and execute the job.", so a
# headless `claude --print` carrying this marker is a scheduled CC job.
CC_JOB_PROMPT_MARKER = "recurring/"
CC_BINARY_NAMES = ("claude", "claude-fda")
NO_TTY = "??"

# ---------------------------------------------------------------------------
# Optional per-install overrides: $MINERU_HOME/config/health-watchdog.json
# (starter: engine/config/health-watchdog.example.json). Same engine-vs-profile
# split the other scripts use, so operator specifics never land in engine code.
PRIVATE_CONFIG_PATH = Path(
    os.environ.get("MINERU_HEALTH_WATCHDOG_CONFIG", str(MINERU_HOME / "config" / "health-watchdog.json"))
)
PRIVATE_CONFIG_ERROR = ""  # set when the file exists but cannot be used; __main__ logs it


def load_private_config(path: Path) -> Tuple[Dict[str, Any], str]:
    """(values, error) from the optional JSON file; a missing file is simply ({}, "")."""
    if not path.exists():
        return {}, ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError) as load_error:
        return {}, "%s: %s" % (path, load_error)
    if not isinstance(loaded, dict):
        return {}, "%s: top level must be a JSON object" % path
    return loaded, ""


_private, PRIVATE_CONFIG_ERROR = load_private_config(PRIVATE_CONFIG_PATH)

# Where big files should go instead of /; empty string = no hint in disk alerts.
OFFLOAD_VOLUME = str(_private.get("offload_volume", "") or "")
OFFLOAD_VOLUME_HINT = ("%s is the place for big files." % OFFLOAD_VOLUME) if OFFLOAD_VOLUME else ""
JOB_MAX_AGE_OVERRIDES_SECONDS.update({
    str(suffix): int(seconds)
    for suffix, seconds in dict(_private.get("job_max_age_overrides_seconds", {}) or {}).items()
})
SERVICE_COMMAND_ALLOWLIST = SERVICE_COMMAND_ALLOWLIST + tuple(
    str(fragment) for fragment in list(_private.get("extra_service_command_fragments", []) or [])
)
# Private scripts that scheduled jobs spawn indirectly (so they are not in any plist's
# ProgramArguments) can be named here to make their orphans killable too.
JOB_COMMAND_MARKERS = JOB_COMMAND_MARKERS + tuple(
    str(marker) for marker in list(_private.get("extra_job_command_markers", []) or [])
)
# Off by default: the generic engine never restarts someone's VM unasked. Only a JSON
# `true` enables it (a string "false" must not).
ORBSTACK_AUTO_RESTART = _private.get("orbstack_auto_restart", False) is True
# Missed-run check scope. The workspace prefix is always watched; other launchd
# prefixes the operator owns (personal scripts) are added here. Labels are skipped by
# full label; freshness maps a full label to a registry-shaped spec ({"kind": ...},
# paths absolute, "~/"-relative, or relative to MINERU_HOME), which wins over the
# registry. Extra plist dirs are scanned after LaunchAgents (a label found twice keeps
# the LaunchAgents copy); a plist only counts when its label is loaded in launchd.
MISSED_RUN_LABEL_PREFIXES = (LAUNCHD_PREFIX,) + tuple(
    str(prefix) for prefix in list(_private.get("missed_run_extra_label_prefixes", []) or [])
)
MISSED_RUN_SKIP_LABELS = frozenset(
    ["%s.%s" % (LAUNCHD_PREFIX, WATCHDOG_JOB_NAME)]
    + [str(label) for label in list(_private.get("missed_run_skip_labels", []) or [])]
)
MISSED_RUN_FRESHNESS_OVERRIDES = {
    str(label): dict(spec)
    for label, spec in dict(_private.get("missed_run_freshness", {}) or {}).items()
    if isinstance(spec, dict)
}  # type: Dict[str, Dict[str, Any]]
MISSED_RUN_EXTRA_PLIST_DIRS = tuple(
    Path(os.path.expanduser(str(directory)))
    for directory in list(_private.get("missed_run_extra_plist_dirs", []) or [])
)
