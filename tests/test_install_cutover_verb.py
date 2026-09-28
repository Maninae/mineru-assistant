"""Verb- and loader-level tests for installing into a shared workspace root.

Covers the cutover mechanics fixes:
  - C1: `runtime_root: shared` opt-in lets `workspace_absolute` equal the
    shared root; the guard compares resolved paths on both sides.
  - C2: the engine root resolves to `<workspace>/engine` even after a
    profile load exported `MINERU_HOME = profiles/<name>`.
  - C5: a warning when --target differs from `workspace_absolute`.
  - S1: `mineru setup` with the default target is not refused because
    `profile init` populated the root (`active`, `people.yaml`, `profiles/`).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.profile.loader import (
    ENGINE_ROOT_ENV_VAR,
    MINERU_HOME_ENV_VAR,
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
    ProfileError,
    load_active_profile,
)

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_ENGINE_ROOT = REPO_ROOT / "engine"
SYNTHETIC_CONNECTORS_YAML = REPO_ROOT / "tests" / "fixtures" / "synthetic-profile" / "connectors.yaml"

# Profile extras the shipped templates reference (what `profile init` leaves out).
TEMPLATE_EXTRAS_YAML = """user_pronouns: they/them
user_full_name: Test Owner
user_location: Testville
user_dev_root: /tmp/dev
user_claude_home: /tmp/claude
persona_emoji: "*"
persona_origin: test fixture
persona_kind: assistant
"""


def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every env var that steers workspace, profile, or engine lookup."""
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
        ENGINE_ROOT_ENV_VAR,
        MINERU_HOME_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def write_profile(
    profiles_dir: Path,
    name: str,
    *,
    workspace_absolute: Path,
    runtime_root: Optional[str] = None,
    extras: str = "",
) -> Path:
    """Write a schema-valid `profiles_dir/<name>/profile.yaml` + connectors."""
    profile_dir = profiles_dir / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"name: {name}",
        f"display_name: {name.capitalize()}",
        "assistant_name: TestBot",
        "timezone: America/Los_Angeles",
        f"keychain_account: {name}-kc",
        f"launchd_label_prefix: com.{name}",
        f"workspace_absolute: {workspace_absolute}",
        f"memory_root: {workspace_absolute}/memory",
        f"briefs_root: {workspace_absolute}",
        "journal_apple_notes_folder: Daily Journals",
        "secrets:",
        "  backends: [env, keychain]",
        f"  env_prefix: {name.upper()}_SECRET_",
    ]
    if runtime_root is not None:
        lines.append(f"runtime_root: {runtime_root}")
    (profile_dir / "profile.yaml").write_text("\n".join(lines) + "\n" + extras, encoding="utf-8")
    shutil.copyfile(SYNTHETIC_CONNECTORS_YAML, profile_dir / "connectors.yaml")
    return profile_dir


def all_output(result: object) -> str:
    """stdout + stderr of a CliRunner result, tolerant of CliRunner versions."""
    stderr = ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (AttributeError, ValueError):
        pass
    return getattr(result, "output", "") + (stderr if isinstance(stderr, str) else "")


def make_shared_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace root whose `engine` is a symlink to this repo's engine/."""
    isolate_env(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    os.symlink(REPO_ENGINE_ROOT, workspace / "engine")
    monkeypatch.setenv(MINERU_HOME_ENV_VAR, str(workspace))
    return workspace


# --- C1: runtime_root opt-in ------------------------------------------------


def test_shared_root_rejected_without_opt_in_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    write_profile(tmp_path, "owner", workspace_absolute=tmp_path)

    with pytest.raises(ProfileError) as exc:
        load_active_profile("owner", base_dir=tmp_path)
    assert "collapses onto the shared workspace root" in str(exc.value)
    assert "runtime_root: shared" in str(exc.value)


def test_shared_root_accepted_with_runtime_root_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    write_profile(tmp_path, "owner", workspace_absolute=tmp_path, runtime_root="shared")

    profile = load_active_profile("owner", base_dir=tmp_path)
    assert profile.workspace_absolute == tmp_path.resolve()


def test_runtime_root_rejects_unknown_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    write_profile(tmp_path, "owner", workspace_absolute=tmp_path / "owner", runtime_root="yes")

    with pytest.raises(ProfileError) as exc:
        load_active_profile("owner", base_dir=tmp_path)
    assert "runtime_root" in str(exc.value)


def test_shared_root_guard_sees_through_symlinked_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MINERU_HOME spelled through a symlink (the `/tmp` vs `/private/tmp`
    case) must still collide with the resolved `workspace_absolute`."""
    isolate_env(monkeypatch)
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    link_root = tmp_path / "link-root"
    os.symlink(real_root, link_root)
    monkeypatch.setenv(MINERU_HOME_ENV_VAR, str(link_root))
    write_profile(tmp_path / "profiles", "owner", workspace_absolute=link_root)

    with pytest.raises(ProfileError) as exc:
        load_active_profile("owner", base_dir=tmp_path / "profiles")
    assert "collapses onto the shared workspace root" in str(exc.value)


# --- C2 + C5: engine root and target warning --------------------------------


def test_install_finds_workspace_engine_despite_profile_home_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --engine-root, `workspace_absolute = profiles/x`: the plan walks
    `<ws>/engine` (the repo), on the first run and on a second in-process
    run after the profile export already rewrote MINERU_HOME."""
    workspace = make_shared_workspace(tmp_path, monkeypatch)
    write_profile(workspace / "profiles", "x", workspace_absolute=workspace / "profiles" / "x")

    for _ in range(2):
        result = runner.invoke(app, ["--profile", "x", "profile", "install", "--target", str(workspace)])
        assert result.exit_code == 0, all_output(result)
        assert "engine_root does not exist" not in all_output(result)
        assert str(REPO_ENGINE_ROOT.resolve() / "charter") in result.output


def test_install_warns_when_target_differs_from_workspace_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = make_shared_workspace(tmp_path, monkeypatch)
    write_profile(workspace / "profiles", "x", workspace_absolute=workspace / "profiles" / "x")

    result = runner.invoke(app, ["--profile", "x", "profile", "install", "--target", str(workspace)])
    assert result.exit_code == 0, all_output(result)
    assert "differs from the profile's workspace_absolute" in all_output(result)


def test_install_into_runtime_root_does_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = make_shared_workspace(tmp_path, monkeypatch)
    write_profile(workspace / "profiles", "owner", workspace_absolute=workspace, runtime_root="shared")

    result = runner.invoke(app, ["--profile", "owner", "profile", "install", "--target", str(workspace)])
    assert result.exit_code == 0, all_output(result)
    assert "differs from the profile's workspace_absolute" not in all_output(result)


# --- S1: setup with the default target ---------------------------------------


def test_setup_default_target_installs_over_init_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`profile init` leaves `active`, `people.yaml`, `profiles/` in the
    root; installing into that same root must not be refused for it."""
    workspace = make_shared_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    setup_argv = [
        "setup",
        "--name", "aster",
        "--persona", "Aster",
        "--owner-new-handle", "owner",
        "--owner-new-display", "Owner",
        "--owner-new-telegram", "12345",
        "--timezone", "America/Los_Angeles",
        "--no-input",
    ]
    first = runner.invoke(app, setup_argv)
    assert "non-empty" not in all_output(first)
    assert "engine_root does not exist" not in all_output(first)

    profile_yaml = workspace / "profiles" / "aster" / "profile.yaml"
    profile_yaml.write_text(profile_yaml.read_text(encoding="utf-8") + TEMPLATE_EXTRAS_YAML, encoding="utf-8")
    result = runner.invoke(app, ["--profile", "aster", "profile", "install", "--target", str(workspace), "--apply"])
    assert result.exit_code == 0, all_output(result)
    assert "applied (" in result.output
    assert (workspace / "bin").is_dir() and not (workspace / "bin").is_symlink()
    assert (workspace / "AGENTS.md").is_file()

    again = runner.invoke(app, ["--profile", "aster", "profile", "install", "--target", str(workspace), "--apply"])
    assert again.exit_code == 0, all_output(again)
    assert "0 created, 0 updated" in again.output
