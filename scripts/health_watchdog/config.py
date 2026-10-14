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

# Memory: swap used fraction (sysctl vm.swapusage) and memory_pressure free %.
# macOS grows swap on demand in 1 GB steps, so used/total sits near 70% in normal
# operation and flaps across any single line; hence a sustained streak plus a
# CLEAR bar well below the WARN bar (hysteresis) before the check is considered over.
SWAP_USED_WARN_FRACTION = 0.80
SWAP_USED_CLEAR_FRACTION = 0.70
SWAP_USED_CRITICAL_FRACTION = 0.90
MEMORY_FREE_WARN_PERCENT = 15
MEMORY_FREE_CLEAR_PERCENT = 25
MEMORY_FREE_CRITICAL_PERCENT = 5
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
BROWSER_TAB_LIST_LIMIT = 25
BROWSER_TAB_URL_MAX_CHARS = 70
BROWSER_RENDERER_CPU_PERCENT = 50.0
BROWSER_RENDERER_CPU_SUSTAINED_RUNS = 3
BROWSER_TAB_SUMMARY_LIMIT = 8
# Profile Chromium counts as orphaned only when NO browser-server process is in the
# ps table (a slow /health must never authorize a kill), and only after N runs.
BROWSER_SERVER_COMMAND_MARKER = "/browser/server.py"
BROWSER_ORPHAN_SUSTAINED_RUNS = 2

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

# Landline: "Claude silent for Ns, killing process" lines within the window.
LANDLINE_SILENT_WINDOW_SECONDS = 6 * 3600
LANDLINE_SILENT_WARN_COUNT = 2
LANDLINE_LOG_TAIL_BYTES = 2_000_000

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
