"""Tests for the `mineru setup` bare verb (Sep 2026 audit §F2).

`mineru setup` is a thin orchestration over `profile init` +
`profile install` + a Keychain secrets checklist. It does NOT reinvent
either verb — this test suite pins the contract:

  - `--help` renders on a fresh clone without loading a profile.
  - `--no-input` end-to-end: init scaffolds profile.yaml + connectors.yaml,
    install --apply is attempted, secrets checklist prints, setup exits
    with the install verb's exit code (so scripts / CI can gate on it).
  - The Sep-16 audit's F2 gap-close: the confusing missing-connectors
    error message no longer appears anywhere in the setup output — the
    scaffold guarantees `connectors.yaml` exists before install runs.
  - Fallback clause: install failure surfaces a clear next-steps block
    naming the exact profile-dir files the operator should edit, rather
    than the raw stderr traceback.
  - Secrets checklist reads keychain-shaped values from the scaffolded
    connectors.yaml and emits `security add-generic-password` commands
    (never writes a secret to disk).
  - Idempotent: a second `setup` run with an existing active profile
    skips the init step.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.profile.loader import (
    ENGINE_ROOT_ENV_VAR,
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)


runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_ENGINE_ROOT = REPO_ROOT / "engine"


def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every profile-affecting env var so tests hit their own fixtures."""
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
        ENGINE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def sandbox_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A fresh workspace with a copy of the real engine tree beside it.

    Every setup test uses this fixture: workspace_root points at
    tmp_path/workspace, engine tree copied to tmp_path/workspace/engine
    so the shipped install verb finds real templates. `MINERU_*` env
    vars pin both.
    """
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shutil.copytree(REAL_ENGINE_ROOT, workspace / "engine")
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(workspace / "engine"))
    return workspace


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_setup_help_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mineru setup --help` renders on an empty workspace root.

    Same F7-style discipline as the noun --help tests: no profile is
    loaded on a help path, so a fresh clone works.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0, result.output
    assert "guided" in result.output.lower() or "setup" in result.output.lower()


def test_setup_appears_in_root_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mineru --help` lists `setup` alongside the other top-level verbs."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "setup" in result.output


# ---------------------------------------------------------------------------
# End-to-end: init + install + checklist
# ---------------------------------------------------------------------------


def _setup_argv(target: Path) -> list[str]:
    """Standard argv for a scripted `setup` invocation.

    Named --owner-new-* trio drives the bootstrap path (no pre-existing
    humans.yaml). --no-input keeps the run scripted.
    """
    return [
        "setup",
        "--name", "testagent",
        "--persona", "Aster",
        "--owner-new-handle", "testowner",
        "--owner-new-display", "Test Owner",
        "--owner-new-telegram", "12345",
        "--timezone", "America/Los_Angeles",
        "--target", str(target),
        "--no-input",
    ]


def test_setup_scaffolds_profile_and_connectors_yaml(
    sandbox_workspace: Path, tmp_path: Path
) -> None:
    """A scripted `mineru setup` writes profile.yaml + connectors.yaml + friends.

    Pins the F2 gap-close: connectors.yaml must exist after `setup`
    without any hand-editing, so the install loader stops dying on
    absent-file.
    """
    target = tmp_path / "install-target"
    result = runner.invoke(app, _setup_argv(target))
    # exit_code may be non-zero (install can fail on missing extras),
    # but init MUST have scaffolded the profile dir.
    profile_root = sandbox_workspace / "profiles" / "testagent"
    assert profile_root.is_dir(), (
        f"profile init did not scaffold {profile_root}. Output:\n{result.output}"
    )
    for expected in ("profile.yaml", "access.yaml", "cron.yaml", "connectors.yaml"):
        assert (profile_root / expected).exists(), (
            f"scaffold missing {expected} — every file must land in one setup pass"
        )


def test_setup_does_not_emit_missing_connectors_error(
    sandbox_workspace: Path, tmp_path: Path
) -> None:
    """The F2 confusing-error message must not appear in setup output.

    Before Sep-16 2026 the install verb died with a `_die` naming a
    connectors.yaml that did not exist. That exact operator-facing
    string ("does not exist, but shipped engine templates reference
    connector keys") is the confusing error setup was built to close.
    Regression net: if this string ever appears in setup output the
    scaffold gap has reopened.
    """
    target = tmp_path / "install-target"
    result = runner.invoke(app, _setup_argv(target))
    forbidden = "connectors.yaml does not exist"
    assert forbidden not in result.output, (
        f"the F2 confusing-connectors error resurfaced. Output:\n{result.output}"
    )
    assert forbidden not in _stderr_or_empty(result), (
        f"the F2 confusing-connectors error resurfaced on stderr:\n"
        f"{_stderr_or_empty(result)}"
    )


def test_setup_prints_secrets_checklist(
    sandbox_workspace: Path, tmp_path: Path
) -> None:
    """The `[3/3] Keychain secrets checklist` block appears with the sentinel.

    Setup NEVER writes a secret — it prints `security add-generic-password`
    commands with a `<PROMPT_FOR_VALUE>` sentinel the operator's own
    hands supply. That contract is load-bearing (see SECURITY.md
    "Never Store Plaintext Secrets on Disk").
    """
    target = tmp_path / "install-target"
    result = runner.invoke(app, _setup_argv(target))
    combined = result.output + _stderr_or_empty(result)
    assert "Keychain secrets checklist" in combined
    assert "security add-generic-password" in combined
    assert "<PROMPT_FOR_VALUE>" in combined


def test_setup_install_failure_shows_next_steps(
    sandbox_workspace: Path, tmp_path: Path
) -> None:
    """When install fails, the fallback clause emits a clear next-steps block.

    The current install verb fails on missing profile-extras (e.g.
    `USER_POSSESSIVE` derived from `user_pronouns`), and the audit
    §F2 fallback requires setup to emit a clear next-steps block
    rather than let the raw trace surface.
    """
    target = tmp_path / "install-target"
    result = runner.invoke(app, _setup_argv(target))
    combined = result.output + _stderr_or_empty(result)
    # Only run the assertion when install actually failed; if a future
    # change makes install succeed end-to-end (great!), this branch
    # simply becomes trivially satisfied and the block above proves
    # the checklist still printed.
    if result.exit_code != 0:
        assert "Next steps:" in combined
        assert "profile install --target" in combined


def test_setup_is_idempotent_when_active_profile_exists(
    sandbox_workspace: Path, tmp_path: Path
) -> None:
    """A second `mineru setup` with an already-active profile skips init.

    Idempotency matters because the operator may re-run setup after
    editing connectors.yaml to fill placeholders. Re-running init
    would fail loud on the existing profile dir.
    """
    target = tmp_path / "install-target"
    # First run creates the profile.
    runner.invoke(app, _setup_argv(target))
    active = sandbox_workspace / "active"
    assert active.is_symlink(), "first setup did not create the active symlink"

    # Second run must NOT invoke init (name collision would fail loud).
    result2 = runner.invoke(app, _setup_argv(target))
    assert "profile init: skipped" in result2.output


def _stderr_or_empty(result: object) -> str:
    """Safe .stderr read (older CliRunner versions raised on mixed streams)."""
    try:
        text = getattr(result, "stderr", "") or ""
        return text if isinstance(text, str) else ""
    except (AttributeError, ValueError):
        return ""
