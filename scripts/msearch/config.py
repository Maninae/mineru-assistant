"""Constants, paths, and configuration for msearch."""

from __future__ import annotations

import os
from pathlib import Path

DAILY_DIR = "memory/daily"
MEMORY_DIR = "memory"
REPORTS_DIR = "reports"
CACHE_PATH = "memory/tags-index.json"
OLLAMA_MODEL = "gemma3:12b"
OLLAMA_URL = "http://localhost:11434"
DEFAULT_TOP_N = 10
CACHE_TTL_SECONDS = 300


def resolve_paths(workspace_root: str | None = None) -> dict[str, Path]:
    """Resolve all paths relative to the workspace root.

    Args:
        workspace_root: Override for workspace root. Defaults to the
            $MINERU_HOME env var (or the standard workspace when unset).

    Returns:
        Dict with keys: root, daily_dir, memory_dir, reports_dir, cache_path.
    """
    root = Path(workspace_root) if workspace_root else Path(
        os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
    root = root.resolve()
    return {
        "root": root,
        "daily_dir": root / DAILY_DIR,
        "memory_dir": root / MEMORY_DIR,
        "reports_dir": root / REPORTS_DIR,
        "cache_path": root / CACHE_PATH,
    }
