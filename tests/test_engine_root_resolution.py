"""Tests for `default_engine_root()` — the production engine-tree resolver.

The engine tree ships `charter/`, `prompts/`, `recurring/`, `launchd/`,
`app-deploy/`, `config/`. In a production install the clone lives at
`<workspace_root>/engine/` (a sibling of `profiles/`, NOT the workspace
root itself). This resolver picks the right path so hydrate walks
templates instead of opaquely symlinking `charter/` and rendering nothing.

Covers:
  - `MINERU_ENGINE_ROOT` env override wins (with expanduser + resolve).
  - Default formula `<workspace_root>/engine`.
  - Missing-directory fail-loud behavior (through `build_plan`).
  - Path normalization / traversal segments resolved away.
  - Symlink target followed.
  - `MINERU_ENGINE_ROOT` composes with `MINERU_HOME` / `MINERU_WORKSPACE_ROOT`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mineru_cli.install import HydrationError, build_plan
from mineru_cli.profile.loader import (
    ENGINE_ROOT_ENV_VAR,
    MINERU_HOME_ENV_VAR,
    PROFILE_BASE_DIR_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
    default_engine_root,
    default_workspace_root,
)


def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every env var that steers workspace / engine resolution."""
    for var in (
        ENGINE_ROOT_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        MINERU_HOME_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


# --- Env override wins ----------------------------------------------------


def test_default_engine_root_env_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `MINERU_ENGINE_ROOT` is set, it is returned verbatim (resolved)."""
    _isolate_env(monkeypatch)
    engine = tmp_path / "custom-engine"
    engine.mkdir()
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(engine))

    resolved = default_engine_root()
    assert resolved == engine.resolve()


def test_default_engine_root_env_override_beats_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MINERU_ENGINE_ROOT` wins over the `<workspace>/engine` default."""
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "engine").mkdir()  # the default-formula sibling; must NOT win.

    custom_engine = tmp_path / "custom-engine"
    custom_engine.mkdir()

    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(custom_engine))

    resolved = default_engine_root()
    # The env override, NOT `<workspace>/engine`.
    assert resolved == custom_engine.resolve()
    assert resolved != (workspace / "engine").resolve()


def test_default_engine_root_env_override_expands_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`~/...` in `MINERU_ENGINE_ROOT` expands to `$HOME/...`."""
    _isolate_env(monkeypatch)
    # Rewrite HOME to a real dir so `~/x` expands to something predictable.
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = tmp_path / "my-engine"
    engine.mkdir()
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, "~/my-engine")

    resolved = default_engine_root()
    assert resolved == engine.resolve()
    # Never leaks a literal tilde into the resolved path.
    assert "~" not in str(resolved)


# --- Default formula (<workspace>/engine) --------------------------------


def test_default_engine_root_default_is_workspace_engine_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `MINERU_ENGINE_ROOT`, resolves to `<workspace_root>/engine`."""
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "engine").mkdir()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))

    resolved = default_engine_root()
    assert resolved == (workspace / "engine").resolve()


def test_default_engine_root_composes_with_mineru_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MINERU_HOME` steers the workspace default, which steers engine default.

    A downstream user with a custom `MINERU_HOME=/opt/foo` gets
    `default_engine_root() == /opt/foo/engine` — one env seam, all
    subsystems align.
    """
    _isolate_env(monkeypatch)
    workspace = tmp_path / "custom-home"
    workspace.mkdir()
    (workspace / "engine").mkdir()
    monkeypatch.setenv(MINERU_HOME_ENV_VAR, str(workspace))

    # Sanity: the workspace-root default matches what we set.
    assert default_workspace_root() == workspace
    # And engine root drops the `/engine` sibling underneath.
    assert default_engine_root() == (workspace / "engine").resolve()


def test_default_engine_root_read_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-session change to `MINERU_ENGINE_ROOT` is picked up on next call.

    Guards against a lazy `default_engine_root` implementation that snapshots
    the env once at import time. The `MINERU_HOME` seam pattern the sibling
    helpers follow requires call-time reads.
    """
    _isolate_env(monkeypatch)
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()

    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(first))
    assert default_engine_root() == first.resolve()

    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(second))
    assert default_engine_root() == second.resolve()


# --- Missing-directory fail-loud (through build_plan) -------------------


def test_default_engine_root_does_not_check_existence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`default_engine_root()` returns a path even when the dir doesn't exist.

    Mirrors `default_workspace_root()` — the helper is a pure resolver;
    the fail-loud path is at consumer time (`build_plan`). This lets
    `--help` and non-hydrate verbs run on a fresh checkout without a
    dangling env var blowing them up.
    """
    _isolate_env(monkeypatch)
    ghost = tmp_path / "does" / "not" / "exist"
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(ghost))

    # No exception even though `ghost` doesn't exist.
    resolved = default_engine_root()
    # `resolve()` normalizes the path even without existence.
    assert isinstance(resolved, Path)
    assert resolved.name == "exist"


def test_build_plan_fails_loud_on_missing_engine_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build_plan(engine_root=<missing>)` raises HydrationError.

    This is the load-bearing failure mode for the resolver: the helper
    doesn't stat the disk, so the "did you forget to clone the engine?"
    error surfaces here, at the plan-build boundary, with a clear message.
    """
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    # NOTE: `<workspace>/engine` is intentionally NOT created.

    engine = default_engine_root()
    assert not engine.exists()

    with pytest.raises(HydrationError) as exc:
        build_plan(
            engine_root=engine,
            target_root=tmp_path / "target",
            context={},
        )
    assert "does not exist" in str(exc.value)
    assert str(engine) in str(exc.value)


def test_build_plan_fails_loud_when_engine_root_is_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `MINERU_ENGINE_ROOT` pointing at a file (not a dir) fails loud."""
    _isolate_env(monkeypatch)
    engine_file = tmp_path / "not-a-dir"
    engine_file.write_text("scalar", encoding="utf-8")
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(engine_file))

    engine = default_engine_root()
    with pytest.raises(HydrationError) as exc:
        build_plan(
            engine_root=engine,
            target_root=tmp_path / "target",
            context={},
        )
    assert "not a directory" in str(exc.value)


# --- Path normalization / traversal rejection ---------------------------


def test_default_engine_root_normalizes_traversal_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`..` segments in `MINERU_ENGINE_ROOT` collapse (via `resolve()`).

    A path like `<tmp>/a/../engine` normalizes to `<tmp>/engine` so a
    stray traversal segment can never smuggle a hydrate write into an
    unexpected parent dir.
    """
    _isolate_env(monkeypatch)
    real_engine = tmp_path / "engine"
    real_engine.mkdir()
    (tmp_path / "a").mkdir()
    weird = tmp_path / "a" / ".." / "engine"

    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(weird))
    resolved = default_engine_root()
    # `..` collapsed; the final path has no `..` left.
    assert ".." not in resolved.parts
    assert resolved == real_engine.resolve()


def test_default_engine_root_default_formula_normalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default `<workspace>/engine` path is resolved (traversal-safe)."""
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "engine").mkdir()
    # A workspace override with a `..` in it still normalizes.
    (tmp_path / "b").mkdir()
    weird_workspace = tmp_path / "b" / ".." / "workspace"
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(weird_workspace))

    resolved = default_engine_root()
    assert ".." not in resolved.parts
    assert resolved == (workspace / "engine").resolve()


# --- Symlink resolution -------------------------------------------------


def test_default_engine_root_env_follows_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink in `MINERU_ENGINE_ROOT` is resolved to its physical target.

    Guards against a downstream consumer thinking two hydrates against
    the same symlink alias were writing to two different engine trees.
    """
    _isolate_env(monkeypatch)
    real_engine = tmp_path / "real-engine"
    real_engine.mkdir()
    alias = tmp_path / "alias-engine"
    os.symlink(real_engine, alias)

    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(alias))
    resolved = default_engine_root()
    assert resolved == real_engine.resolve()
    # The alias itself is a symlink, but the resolved path is not.
    assert not resolved.is_symlink()


def test_default_engine_root_default_formula_follows_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked `<workspace>/engine` is followed too.

    Common case: an operator wants to keep the engine clone on a
    fast SSD (`/opt/engine`) but leave the workspace on the default
    disk. They `ln -s /opt/engine ~/.mineru/engine`, and hydrate must
    resolve through the symlink so plan/apply see the physical tree.
    """
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_engine = tmp_path / "physical-engine"
    real_engine.mkdir()
    os.symlink(real_engine, workspace / "engine")

    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    resolved = default_engine_root()
    assert resolved == real_engine.resolve()
    assert not resolved.is_symlink()
