"""Shared setup for the Mineru web-app pytest suite.

Every test file imports this first so `$MINERU_HOME/app` (the app modules) and
`$MINERU_HOME/` (for `lib.tldr`, which `feeds` re-exports) land on `sys.path`
whether the tests run via `python3 -m pytest tests/` or standalone via
`python3 tests/test_x.py`.

The `SyntheticWorkspace` helper builds a tempdir tree with a couple of
brief feeds + library sources, then rewires the loaded app modules'
module-level attributes so the code walks the synthetic tree instead of
the operator's real $MINERU_HOME workspace. Tear-down restores every attribute so
one test can't leak state into another.

Python 3.9-compatible. Stdlib only.
"""

import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List


# MINERU_HOME points to the WORKTREE ROOT (the repo checkout this test file
# lives inside), NOT the live `$MINERU_HOME/` workspace. When this helper file
# was authored it lived at `$MINERU_HOME/tests/` and the two were the same
# thing, but this same file now also ships inside the `mineru-cli` worktree
# — where the live workspace and the worktree have diverged and the worktree
# carries its own newer copy of `app/`, `browser/`, and `lib/`.
#
# Anchoring MINERU_HOME to the worktree root (via __file__) has two effects:
#   1. Tests import the worktree's own `app/`, `browser/`, `lib/` modules —
#      i.e. the code under test — instead of whatever version happens to
#      live in the live `$MINERU_HOME` directory.
#   2. We never insert `$MINERU_HOME` at `sys.path[0]`, so packages present in
#      both trees (notably `browser/`) resolve to the worktree copy, which
#      is the version pytest is asserting against.
#
# In the live workspace this still resolves to `$MINERU_HOME`, because that IS
# the parent-of-parent of `$MINERU_HOME/tests/_app_test_setup.py`. So the file
# is now workspace-agnostic: it points to whichever repo it currently lives in.
MINERU_HOME = Path(__file__).resolve().parent.parent
APP_DIR = MINERU_HOME / "app"

# App modules live under <MINERU_HOME>/app/; lib/tldr.py lives one level up.
for entry in (str(APP_DIR), str(MINERU_HOME)):
    if entry not in sys.path:
        sys.path.insert(0, entry)


# --- Synthetic-workspace helper ---------------------------------------------

class SyntheticWorkspace:
    """Builds a temporary MINERU_HOME-shaped tree and monkey-patches app
    modules to walk it instead of the real workspace.

    Layout inside the tempdir:
        <tmp>/briefs_alpha/       - a fake feed with the id "alpha"
        <tmp>/briefs_beta/        - a fake feed with the id "beta"
        <tmp>/reports/            - library source "reports"
        <tmp>/creations/          - library source "creations"
        <tmp>/state/              - app state dir (seen ledger, unlock tokens)

    Usage:
        ws = SyntheticWorkspace()
        ws.setup()                                    # in setUp()
        ws.write_brief("alpha", "morning-1.md", "# Hello\\n\\nTLDR: hi\\n")
        ...
        ws.teardown()                                 # in tearDown()
    """

    def __init__(self):
        self.tmpdir_obj: tempfile.TemporaryDirectory = None
        self.root: Path = None
        self.state_dir: Path = None
        self.original_state: Dict[str, object] = {}

    # --- lifecycle ----------------------------------------------------------

    def setup(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="mineru-tests-")
        self.root = Path(self.tmpdir_obj.name)
        for sub in ("briefs_alpha", "briefs_beta", "reports", "creations", "state"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.state_dir = self.root / "state"

        # Rewire the modules that read config-derived globals at request time.
        import config
        import feeds
        import search
        import library
        import http_helpers
        import handlers
        import seen_ledger
        import unlock_gate

        # Snapshot originals so teardown() can restore them cleanly.
        self.original_state["config.MINERU_HOME"] = config.MINERU_HOME
        self.original_state["config.LIBRARY_SOURCES"] = dict(config.LIBRARY_SOURCES)
        self.original_state["config.FEED_REGISTRY"] = list(config.FEED_REGISTRY)
        self.original_state["config.FEED_BY_ID"] = dict(config.FEED_BY_ID)
        self.original_state["config.READ_ALLOWLIST"] = list(config.READ_ALLOWLIST)
        self.original_state["config.STATE_DIR"] = config.STATE_DIR
        self.original_state["config.SEEN_LEDGER_PATH"] = config.SEEN_LEDGER_PATH
        self.original_state["config.UNLOCK_TOKENS_PATH"] = config.UNLOCK_TOKENS_PATH
        self.original_state["config.UNLOCK_LOCKOUT_PATH"] = config.UNLOCK_LOCKOUT_PATH

        # Build the synthetic registry: two brief feeds.
        synthetic_registry: List[Dict] = [
            {"id": "alpha", "dirs": ["briefs_alpha"],
             "display_name": "Alpha", "emoji": "A", "accent": "a"},
            {"id": "beta", "dirs": ["briefs_beta"],
             "display_name": "Beta", "emoji": "B", "accent": "b"},
        ]
        synthetic_by_id = {feed["id"]: feed for feed in synthetic_registry}
        synthetic_library = {
            "reports": (self.root / "reports").resolve(),
            "creations": (self.root / "creations").resolve(),
        }
        synthetic_allowlist = [
            (self.root / "briefs_alpha").resolve(),
            (self.root / "briefs_beta").resolve(),
            (self.root / "reports").resolve(),
            (self.root / "creations").resolve(),
        ]

        # Patch config first, then re-point every module that already imported
        # the old references. Bare mutation isn't enough because names like
        # `search.FEED_REGISTRY` were bound at module-load time.
        config.MINERU_HOME = self.root
        config.LIBRARY_SOURCES = synthetic_library
        config.FEED_REGISTRY = synthetic_registry
        config.FEED_BY_ID = synthetic_by_id
        config.READ_ALLOWLIST = synthetic_allowlist
        config.STATE_DIR = self.state_dir
        config.SEEN_LEDGER_PATH = self.state_dir / "seen.json"
        config.UNLOCK_TOKENS_PATH = self.state_dir / "unlock-tokens.json"
        config.UNLOCK_LOCKOUT_PATH = self.state_dir / "unlock-lockout.json"

        for module_name in ("feeds", "search", "library", "http_helpers",
                            "handlers", "seen_ledger", "unlock_gate", "pulse_freshness"):
            module = sys.modules.get(module_name)
            if module is None:
                continue
            if hasattr(module, "MINERU_HOME"):
                module.MINERU_HOME = self.root
            if hasattr(module, "LIBRARY_SOURCES"):
                module.LIBRARY_SOURCES = synthetic_library
            if hasattr(module, "FEED_REGISTRY"):
                module.FEED_REGISTRY = synthetic_registry
            if hasattr(module, "FEED_BY_ID"):
                module.FEED_BY_ID = synthetic_by_id
            if hasattr(module, "READ_ALLOWLIST"):
                module.READ_ALLOWLIST = synthetic_allowlist
            if hasattr(module, "STATE_DIR"):
                module.STATE_DIR = self.state_dir
            if hasattr(module, "SEEN_LEDGER_PATH"):
                module.SEEN_LEDGER_PATH = self.state_dir / "seen.json"
            if hasattr(module, "UNLOCK_TOKENS_PATH"):
                module.UNLOCK_TOKENS_PATH = self.state_dir / "unlock-tokens.json"
            if hasattr(module, "UNLOCK_LOCKOUT_PATH"):
                module.UNLOCK_LOCKOUT_PATH = self.state_dir / "unlock-lockout.json"

        # Also refresh handlers.FEED_IDS (built at module load from FEED_REGISTRY).
        handlers_module = sys.modules.get("handlers")
        if handlers_module is not None:
            handlers_module.FEED_IDS = {feed["id"] for feed in synthetic_registry}

        # Clear any TTL caches that could return real-workspace results.
        feeds.invalidate_feed_summary_cache()
        feeds.invalidate_today_items_cache()
        search.invalidate_search_cache()

    def teardown(self):
        import config
        import handlers

        config.MINERU_HOME = self.original_state["config.MINERU_HOME"]
        config.LIBRARY_SOURCES = self.original_state["config.LIBRARY_SOURCES"]
        config.FEED_REGISTRY = self.original_state["config.FEED_REGISTRY"]
        config.FEED_BY_ID = self.original_state["config.FEED_BY_ID"]
        config.READ_ALLOWLIST = self.original_state["config.READ_ALLOWLIST"]
        config.STATE_DIR = self.original_state["config.STATE_DIR"]
        config.SEEN_LEDGER_PATH = self.original_state["config.SEEN_LEDGER_PATH"]
        config.UNLOCK_TOKENS_PATH = self.original_state["config.UNLOCK_TOKENS_PATH"]
        config.UNLOCK_LOCKOUT_PATH = self.original_state["config.UNLOCK_LOCKOUT_PATH"]

        for module_name in ("feeds", "search", "library", "http_helpers",
                            "handlers", "seen_ledger", "unlock_gate", "pulse_freshness"):
            module = sys.modules.get(module_name)
            if module is None:
                continue
            if hasattr(module, "MINERU_HOME"):
                module.MINERU_HOME = config.MINERU_HOME
            if hasattr(module, "LIBRARY_SOURCES"):
                module.LIBRARY_SOURCES = config.LIBRARY_SOURCES
            if hasattr(module, "FEED_REGISTRY"):
                module.FEED_REGISTRY = config.FEED_REGISTRY
            if hasattr(module, "FEED_BY_ID"):
                module.FEED_BY_ID = config.FEED_BY_ID
            if hasattr(module, "READ_ALLOWLIST"):
                module.READ_ALLOWLIST = config.READ_ALLOWLIST
            if hasattr(module, "STATE_DIR"):
                module.STATE_DIR = config.STATE_DIR
            if hasattr(module, "SEEN_LEDGER_PATH"):
                module.SEEN_LEDGER_PATH = config.SEEN_LEDGER_PATH
            if hasattr(module, "UNLOCK_TOKENS_PATH"):
                module.UNLOCK_TOKENS_PATH = config.UNLOCK_TOKENS_PATH
            if hasattr(module, "UNLOCK_LOCKOUT_PATH"):
                module.UNLOCK_LOCKOUT_PATH = config.UNLOCK_LOCKOUT_PATH

        handlers_module = sys.modules.get("handlers")
        if handlers_module is not None:
            handlers_module.FEED_IDS = {feed["id"] for feed in config.FEED_REGISTRY}

        import feeds
        import search
        feeds.invalidate_feed_summary_cache()
        feeds.invalidate_today_items_cache()
        search.invalidate_search_cache()

        if self.tmpdir_obj is not None:
            self.tmpdir_obj.cleanup()
        self.tmpdir_obj = None

    # --- filesystem builders -----------------------------------------------

    def write_brief(self, feed_id: str, relpath: str, text: str,
                    mtime: float = None) -> Path:
        """Write a brief file under briefs_<feed_id>/, creating parents as needed."""
        feed_dir_name = "briefs_" + feed_id
        target = self.root / feed_dir_name / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(target, (mtime, mtime))
        return target

    def write_library_file(self, source: str, relpath: str, text: str = "",
                           binary: bytes = None, mtime: float = None) -> Path:
        """Write a library file under <source>/<relpath>."""
        target = self.root / source / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if binary is not None:
            target.write_bytes(binary)
        else:
            target.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(target, (mtime, mtime))
        return target


# --- Timestamp source (deterministic in tests) --------------------------------

class FrozenTime:
    """Context-managed monkey-patch of `time.time` to return a fixed value.

    Used to make status-classification and lockout math deterministic without
    depending on wall-clock at test time.
    """

    def __init__(self, module, fixed_now: float):
        self.module = module
        self.fixed_now = fixed_now
        self.original = None

    def __enter__(self):
        self.original = self.module.time.time
        self.module.time.time = lambda: self.fixed_now
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.module.time.time = self.original
