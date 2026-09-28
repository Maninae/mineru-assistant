#!/usr/bin/env python3
"""
Mineru Browser Server — Configuration & Logging

Module-level constants and logger. Imported by all other browser/* modules.
"""

import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Workspace root — the master path seam. Every workspace-relative browser
# path (the persistent Chromium profile, the tabs-snapshot cache) derives
# from this. `MINERU_HOME` lets a non-default install, or a second profile
# on the same machine, relocate the whole workspace by exporting a different
# value. The default is a per-user `.mineru` directory under the home
# folder, resolved for whoever runs the server (never a hardcoded username).
MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))

# Server port. Defaults to 9471 (the live production port). The env knob
# `MINERU_BROWSER_PORT` lets tests bind a SEPARATE port so they never
# collide with the live server on 9471. Never bind to 0.0.0.0; the server
# listens on 127.0.0.1 only (see server.py::main).
PORT = int(os.environ.get("MINERU_BROWSER_PORT", "9471"))
PID_FILE = "/tmp/mineru-browser-server.pid"

# Optional PID-file override for the same "tests never touch the live
# server" story. The default keeps prod behavior exactly as-is; tests
# opt in by setting the env var to a tmp path.
_PID_FILE_OVERRIDE = os.environ.get("MINERU_BROWSER_PID_FILE")
if _PID_FILE_OVERRIDE:
    PID_FILE = _PID_FILE_OVERRIDE
LOG_DIR = MINERU_HOME / "logs" / "browser-server"
HEADLESS = os.environ.get("MINERU_BROWSER_HEADLESS", "false").lower() in ("1", "true", "yes")
CDP_ENDPOINT = os.environ.get("MINERU_BROWSER_CDP", "")  # e.g. "http://localhost:9222"
CLOAK_EXECUTABLE = os.path.expanduser(
    "~/.cloakbrowser/chromium-145.0.7632.109.2/Chromium.app/Contents/MacOS/Chromium"
)
USE_CLOAK = os.environ.get("MINERU_BROWSER_USE_CLOAK", "true").lower() != "false"
PERSISTENT_PROFILE = MINERU_HOME / "cache" / "cloakbrowser-profile"

# ---------------------------------------------------------------------------
# P3-05: Live-tabs SSE + debounced on-disk snapshot
# ---------------------------------------------------------------------------

# The on-disk cache the crash-recovery layer reads (spec §4.3 layer 4).
# `mineru browser tabs` falls back to this file when the live server is
# down. Env override lets tests point at a `tmp_path` and lets a
# non-standard install (e.g. on `/Volumes/vega/`) relocate the file.
# Constant name kept as `TABS_SNAPSHOT_PATH` for import-side ergonomics;
# the env knob is namespaced `MINERU_BROWSER_TABS_SNAPSHOT_PATH` to
# match the rest of the browser env vars.
TABS_SNAPSHOT_PATH = Path(
    os.environ.get(
        "MINERU_BROWSER_TABS_SNAPSHOT_PATH",
        str(MINERU_HOME / "cache" / "browser-tabs.json"),
    )
)

# SSE heartbeat interval (spec §4.3): a `:heartbeat\n\n` comment fires
# on every subscriber's stream when the event queue has been idle for
# this many seconds. 15 s is the standard SSE heartbeat cadence — long
# enough that a live browsing session's real events dominate the
# stream, short enough that a stale connection surfaces within tens of
# seconds. Tests bump this way down (e.g. 0.2 s) so a heartbeat assert
# doesn't dominate wall-clock time.
SSE_HEARTBEAT_SECONDS = float(
    os.environ.get("MINERU_BROWSER_SSE_HEARTBEAT_SECONDS", "15")
)

# Debounce window (ms) for the on-disk snapshot writer. 200 ms is the
# spec §4.3 default; tests can zero it via
# `MINERU_BROWSER_TABS_DEBOUNCE_MS=0` for synchronous writes.
TABS_SNAPSHOT_DEBOUNCE_MS = int(
    os.environ.get("MINERU_BROWSER_TABS_DEBOUNCE_MS", "200")
)

# Interactive roles that get ref IDs
INTERACTIVE_ROLES = frozenset({
    "button", "checkbox", "combobox", "link", "listbox", "menuitem",
    "menuitemcheckbox", "menuitemradio", "option", "radio", "searchbox",
    "slider", "spinbutton", "switch", "tab", "textbox", "treeitem",
})

# Roles to include in tree output (interactive + structural)
VISIBLE_ROLES = INTERACTIVE_ROLES | frozenset({
    "heading", "paragraph", "list", "listitem", "table", "row", "cell",
    "columnheader", "rowheader", "img", "figure", "navigation", "main",
    "banner", "contentinfo", "complementary", "form", "region", "article",
    "dialog", "alert", "alertdialog", "status", "group", "toolbar",
    "separator", "menu", "menubar", "tablist", "tabpanel", "tree",
    "treegrid", "grid", "gridcell", "rowgroup",
})

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOG_DIR / "server.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(log_file)),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("browser-server")
