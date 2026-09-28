"""Tests for install-time template rendering (launchd/ + app-deploy/).

Covers step 2 of the 2026-09-03 hydrate fixes:

  - `RECURSIVE_INSTALL_TEMPLATE_DIRS = {"launchd", "app-deploy"}` are
    walked recursively (rendered per-file, NOT symlinked opaque).
  - Filename-token substitution: `LABEL_PREFIX` in a template filename
    is replaced by the operator's `LAUNCHD_PREFIX` context value.
  - Content rendering: a launchd plist and an app-deploy shell script
    template both go through the same render context (a plain launchd
    entry substitutes `{{LAUNCHD_PREFIX}}` in its body; an app-deploy
    template substitutes `{{PERSONA_NAME}}`).
  - `--dry-run` reports launchd/app-deploy actions in the plan output.
  - Filename token with missing context key fails LOUD (`HydrationError`).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.install import (
    RECURSIVE_INSTALL_TEMPLATE_DIRS,
    RECURSIVE_TEMPLATE_DIRS,
    HydrationActionKind,
    HydrationError,
    apply_plan,
    build_plan,
)
from mineru_cli.profile.loader import (
    ENGINE_ROOT_ENV_VAR,
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)


runner = CliRunner()

# Same synthetic connectors fixture used by test_hydrate_verb.py — kept
# in one place so a new connector key added to the real engine tree only
# needs one edit.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SYNTHETIC_CONNECTORS_YAML = (
    _REPO_ROOT / "tests" / "fixtures" / "synthetic-profile" / "connectors.yaml"
)


# --- Constants + shape tests --------------------------------------------


def test_recursive_install_dirs_constant_shape() -> None:
    """The install-dir set names the two dirs that carry per-operator artifacts."""
    assert "launchd" in RECURSIVE_INSTALL_TEMPLATE_DIRS
    assert "app-deploy" in RECURSIVE_INSTALL_TEMPLATE_DIRS
    assert isinstance(RECURSIVE_INSTALL_TEMPLATE_DIRS, frozenset)


def test_install_dirs_do_not_overlap_recursive_template_dirs() -> None:
    """The install-dir set is disjoint from the doc/prompt template set.

    Charter/prompts/recurring get filename-token substitution as a no-op
    (safe), but conceptually they are different concerns — install artifacts
    vs shipped content. Keep the two sets non-overlapping so the doc/CI story
    stays clear.
    """
    assert (
        RECURSIVE_INSTALL_TEMPLATE_DIRS.isdisjoint(RECURSIVE_TEMPLATE_DIRS)
    )


# --- Filename-token substitution ----------------------------------------


def _make_engine_with_install_dirs(tmp_path: Path) -> Path:
    """Engine tree with launchd/ + app-deploy/ carrying representative templates."""
    engine = tmp_path / "engine"
    engine.mkdir()

    launchd = engine / "launchd"
    launchd.mkdir()
    # Filename token + content substitution AND a `{{LAUNCHD_PREFIX}}` in body.
    # The template drops the leading `com.` — `LABEL_PREFIX` substitutes to
    # the WHOLE prefix (`com.example`), so the rendered filename is
    # `com.example.webapp.plist` (no double `com.`).
    (launchd / "LABEL_PREFIX.webapp.plist.template").write_text(
        "<key>Label</key><string>{{LAUNCHD_PREFIX}}.webapp</string>",
        encoding="utf-8",
    )
    # A plain sibling (no LABEL_PREFIX in the name, no .template suffix) —
    # should SYMLINK back to the engine copy.
    (launchd / "plain.txt").write_text("static", encoding="utf-8")

    app_deploy = engine / "app-deploy"
    app_deploy.mkdir()
    (app_deploy / "set-passphrase.sh.template").write_text(
        "#!/bin/bash\n# {{PERSONA_NAME}} setup\n", encoding="utf-8"
    )
    # A binary/plain helper — no substitution, should SYMLINK.
    (app_deploy / "gen-vapid.py").write_text(
        "print('vapid')\n", encoding="utf-8"
    )
    return engine


def _base_context() -> dict:
    """A minimal context with the load-bearing keys the install renderer uses."""
    return {
        "LAUNCHD_PREFIX": "com.example",
        "PERSONA_NAME": "Testora",
        "MINERU_HOME": "/tmp/example",
    }


def test_launchd_filename_token_is_substituted(tmp_path: Path) -> None:
    """`LABEL_PREFIX.webapp.plist.template` renders as `<prefix>.webapp.plist`."""
    engine = _make_engine_with_install_dirs(tmp_path)
    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context=_base_context(),
    )
    render_dests = {
        a.dest for a in plan.actions if a.kind == HydrationActionKind.RENDER
    }
    # The substituted filename is present; the raw `LABEL_PREFIX` name is NOT.
    assert target / "launchd" / "com.example.webapp.plist" in render_dests
    for dest in render_dests:
        assert "LABEL_PREFIX" not in dest.name, (
            f"LABEL_PREFIX literal survived filename substitution: {dest}"
        )
        # No `.template` suffix either.
        assert not dest.name.endswith(".template"), dest


def test_app_deploy_filename_without_token_is_unchanged(tmp_path: Path) -> None:
    """A template with no `LABEL_PREFIX` in its name just drops `.template`."""
    engine = _make_engine_with_install_dirs(tmp_path)
    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context=_base_context(),
    )
    render_dests = {
        a.dest for a in plan.actions if a.kind == HydrationActionKind.RENDER
    }
    assert target / "app-deploy" / "set-passphrase.sh" in render_dests


def test_install_dirs_are_recursive_not_symlinked(tmp_path: Path) -> None:
    """launchd/ + app-deploy/ contribute per-file actions, NOT opaque symlinks.

    Regression guard against the pre-fix state where `build_plan` symlinked
    these dirs whole and no template ever rendered.
    """
    engine = _make_engine_with_install_dirs(tmp_path)
    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context=_base_context(),
    )
    # No SYMLINK action whose dest is the top-level launchd or app-deploy dir
    # (which would be the opaque-symlink regression).
    opaque_symlink_dests = {
        a.dest for a in plan.actions if a.kind == HydrationActionKind.SYMLINK
    }
    assert target / "launchd" not in opaque_symlink_dests
    assert target / "app-deploy" not in opaque_symlink_dests
    # But the plain sibling files under each dir DO symlink.
    assert target / "launchd" / "plain.txt" in opaque_symlink_dests
    assert target / "app-deploy" / "gen-vapid.py" in opaque_symlink_dests
    # And an MKDIR was emitted for each install dir.
    mkdir_dests = {
        a.dest for a in plan.actions if a.kind == HydrationActionKind.MKDIR
    }
    assert target / "launchd" in mkdir_dests
    assert target / "app-deploy" in mkdir_dests


def test_apply_plan_renders_launchd_and_app_deploy_templates(
    tmp_path: Path,
) -> None:
    """End-to-end: apply lays down the substituted filenames + rendered content."""
    engine = _make_engine_with_install_dirs(tmp_path)
    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context=_base_context(),
    )
    apply_plan(plan, dry_run=False)

    # launchd content: the substituted filename exists AND `{{LAUNCHD_PREFIX}}`
    # in the body is replaced with the context value.
    plist = target / "launchd" / "com.example.webapp.plist"
    assert plist.exists()
    body = plist.read_text(encoding="utf-8")
    assert "com.example.webapp" in body
    assert "{{LAUNCHD_PREFIX}}" not in body
    assert "LABEL_PREFIX" not in body

    # app-deploy content: rendered against the same context.
    script = target / "app-deploy" / "set-passphrase.sh"
    assert script.exists()
    assert "Testora setup" in script.read_text(encoding="utf-8")

    # Plain siblings arrived as symlinks with intact content.
    plain = target / "launchd" / "plain.txt"
    assert plain.is_symlink()
    assert plain.read_text(encoding="utf-8") == "static"
    vapid = target / "app-deploy" / "gen-vapid.py"
    assert vapid.is_symlink()
    assert vapid.read_text(encoding="utf-8") == "print('vapid')\n"


def test_filename_token_missing_context_key_fails_loud(tmp_path: Path) -> None:
    """Filename `LABEL_PREFIX` without `LAUNCHD_PREFIX` in context → HydrationError.

    Silent fallback would leave the literal `LABEL_PREFIX` in the on-disk
    plist filename, which would confuse `launchctl` on the operator's box.
    """
    engine = _make_engine_with_install_dirs(tmp_path)
    target = tmp_path / "target"
    context_without_prefix = {"PERSONA_NAME": "X"}  # no LAUNCHD_PREFIX
    with pytest.raises(HydrationError) as exc:
        build_plan(
            engine_root=engine,
            target_root=target,
            context=context_without_prefix,
        )
    msg = str(exc.value)
    assert "LABEL_PREFIX" in msg
    assert "LAUNCHD_PREFIX" in msg


def test_nested_install_dir_walks_recursively(tmp_path: Path) -> None:
    """A subdirectory under launchd/ is walked recursively (not symlinked whole)."""
    engine = tmp_path / "engine"
    engine.mkdir()
    launchd = engine / "launchd"
    launchd.mkdir()
    nested = launchd / "webpush"
    nested.mkdir()
    (nested / "LABEL_PREFIX.push.plist.template").write_text(
        "<Label>{{LAUNCHD_PREFIX}}.push</Label>", encoding="utf-8"
    )

    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context=_base_context(),
    )

    # The nested template renders under target/launchd/webpush/, with LABEL_PREFIX substituted.
    render_dests = {
        a.dest for a in plan.actions if a.kind == HydrationActionKind.RENDER
    }
    assert (
        target / "launchd" / "webpush" / "com.example.push.plist"
    ) in render_dests
    # MKDIR for the nested subdir was emitted.
    mkdir_dests = {
        a.dest for a in plan.actions if a.kind == HydrationActionKind.MKDIR
    }
    assert target / "launchd" / "webpush" in mkdir_dests


# --- --dry-run reports install-dir actions ------------------------------


def _write_synthetic_profile(
    tmp_path: Path, workspace_root: Path, name: str = "installtest"
) -> Path:
    """Materialize a profile.yaml that points at a real workspace root.

    The engine tree is bound separately via `MINERU_ENGINE_ROOT` so the
    verb's engine-root resolution + install-template renderer are the
    load-bearing thing under test.

    Also drops in `<profile>/connectors.yaml` copied from the synthetic
    fixture, matching the shipped verb's requirement that the file exist
    (the loader fails loud on absence because engine templates
    hard-reference connector keys).
    """
    profile_dir = tmp_path / name
    profile_dir.mkdir()
    (profile_dir / "profile.yaml").write_text(
        f"name: {name}\n"
        f"display_name: {name.capitalize()}\n"
        "assistant_name: TestBot\n"
        "timezone: America/Los_Angeles\n"
        f"keychain_account: {name}-acct\n"
        f"launchd_label_prefix: com.{name}\n"
        f"workspace_absolute: {profile_dir}\n"
        f"memory_root: {workspace_root}/memory\n"
        f"briefs_root: {workspace_root}/briefs\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        f"  env_prefix: {name.upper()}_SECRET_\n",
        encoding="utf-8",
    )
    shutil.copyfile(
        _SYNTHETIC_CONNECTORS_YAML, profile_dir / "connectors.yaml"
    )
    return profile_dir


def test_dry_run_output_reports_install_dir_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--dry-run` output surfaces the launchd/ + app-deploy/ actions."""
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
        ENGINE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = _make_engine_with_install_dirs(workspace)

    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(engine))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    _write_synthetic_profile(tmp_path, workspace_root=workspace, name="installtest")

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "installtest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output

    # RENDER lines exist for the substituted launchd filename AND the plain
    # app-deploy filename. The profile's launchd_label_prefix is
    # `com.installtest`, so `LABEL_PREFIX.webapp.plist.template` renders as
    # `com.installtest.webapp.plist`.
    assert str(target / "launchd" / "com.installtest.webapp.plist") in result.output, (
        result.output
    )
    assert str(target / "app-deploy" / "set-passphrase.sh") in result.output, (
        result.output
    )
    # Neither install dir appears as an opaque top-level SYMLINK.
    opaque_launchd = f"{target / 'launchd'}  ->  {engine / 'launchd'}"
    opaque_app_deploy = f"{target / 'app-deploy'}  ->  {engine / 'app-deploy'}"
    assert opaque_launchd not in result.output
    assert opaque_app_deploy not in result.output


# --- Real engine tree exercises the install-render path -----------------


def test_apply_plan_against_real_engine_tree_renders_launchd_plists(
    tmp_path: Path,
) -> None:
    """The repo's real `engine/launchd/` renders every plist with substituted names.

    Uses a synthetic context so the substituted filename is predictable
    (`com.substitutetest.<jobname>.plist`), and only checks a couple of
    known job names to keep the assertion tight if the roster shifts.
    """
    repo_engine = Path(__file__).resolve().parent.parent / "engine"
    if not (repo_engine / "launchd").exists():
        pytest.skip("real engine/launchd/ not shipped in this checkout")

    target = tmp_path / "target"
    context = {
        "LAUNCHD_PREFIX": "com.substitutetest",
        "PERSONA_NAME": "Substitute",
        "PERSONA_NAME_LOWER": "substitute",
        "USER_NAME": "Sub",
        "USER_TIMEZONE": "America/Los_Angeles",
        "MINERU_HOME": "/tmp/subhome",
        "MEMORY_ROOT": "/tmp/subhome/memory",
        "BRIEFS_ROOT": "/tmp/subhome",
        "USER_HOME": "/tmp/subuser",
        "KEYCHAIN_ACCOUNT": "sub",
        "SECRETS_ENV_PREFIX": "SUB_",
    }
    # Walk ONLY the launchd/ dir to keep this focused (a full-engine walk
    # is what `test_engine_templates_render_clean.py` covers via the API).
    plan = build_plan(
        engine_root=repo_engine,
        target_root=target,
        context=context,
    )
    # Filter the plan to actions inside launchd/ for the assertions.
    launchd_render_dests = [
        a.dest for a in plan.actions
        if a.kind == HydrationActionKind.RENDER
        and "launchd" in a.dest.parts
    ]
    assert launchd_render_dests, "no launchd renders in the plan"
    for dest in launchd_render_dests:
        assert "LABEL_PREFIX" not in dest.name, (
            f"literal LABEL_PREFIX survived filename substitution: {dest}"
        )
        # Every substituted plist name carries the operator prefix.
        assert dest.name.startswith("com.substitutetest.")
        assert dest.name.endswith(".plist")
