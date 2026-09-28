"""Shared test config.

Adds the repository root to `sys.path` so tests can `import browser.*`
without an editable install. The `mineru_cli` package is picked up via
its declared setuptools entry (pyproject `[tool.setuptools.packages.find]`
resolves `mineru_cli*`), but `browser/` sits at the repo root as a
standalone package that the browser server ships with, not part of the
`mineru_cli` package. This conftest bridges the gap for tests.

Also provides a default active profile for the whole suite (see
`_default_active_profile`): the engine repo ships no live `profiles/` tree
or `current` symlink, so CLI verb tests that invoke `mineru` without
setting up a profile need a default the way the source worktree got one
from its committed `current -> profiles/mineru` symlink.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Synthetic `mineru` seed profile shipped under tests/fixtures/. Stands in
# for the source worktree's committed `current -> profiles/mineru` symlink.
_SEED_PROFILE_BASE = _REPO_ROOT / "tests" / "fixtures" / "seed_profile_base"


@pytest.fixture(autouse=True)
def _default_active_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point default profile resolution at the synthetic seed for every test.

    Sets `MINERU_PROFILE=mineru` + `MINERU_PROFILE_ROOT=<seed base>` so a
    flagless `load_active_profile(None)` resolves the shipped seed. This
    replaces the worktree's committed `current` symlink, which the engine
    repo does not ship. Tests that need a different profile (or none)
    override these env vars with their own monkeypatch — the same fixture
    instance, so the per-test calls run after this setup and win.

    Also delete the three per-profile runtime env keys that
    `load_active_profile()` exports into `os.environ` on first hydration
    (`MINERU_HOME`, `MINERU_KEYCHAIN_ACCOUNT`, `MINERU_INJECT_QUEUE_DIR`).
    Without this cleanup a prior test that hydrated a profile would leak
    its `MINERU_HOME` into the next test, and any test that constructs a
    profile via `load_active_profile(..., base_dir=tmp_path)` after that
    leak would see the LEAKED value as `default_workspace_root()` and
    trip the shared-root validation (Fix 1 loader guard). Step-5 audit
    fix 2 is deliberate about mutating `os.environ` — but tests need a
    clean slate around each case.
    """
    monkeypatch.setenv("MINERU_PROFILE", "mineru")
    monkeypatch.setenv("MINERU_PROFILE_ROOT", str(_SEED_PROFILE_BASE))
    monkeypatch.delenv("MINERU_HOME", raising=False)
    monkeypatch.delenv("MINERU_KEYCHAIN_ACCOUNT", raising=False)
    monkeypatch.delenv("MINERU_INJECT_QUEUE_DIR", raising=False)
