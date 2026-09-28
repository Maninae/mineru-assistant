"""Tests for the `mineru profile install` verb (Phase 2 chunk 1).

The verb was renamed from `hydrate` to `install` on 2026-09-16 (see
`reports/2026-09-16-mineru-cli-naming-consolidation-audit.md` §1). The
old `hydrate` spelling still works as a hidden Typer alias for ~90
days; these tests continue to exercise it under the alias to lock in
the backward-compat contract, and add fresh coverage for the canonical
`install` name + the new `--apply` flag (which replaces `--no-dry-run`).

Covers:
  - `mineru profile install --help` and the hidden `mineru profile hydrate --help`
    both exit 0 without loading a profile (lazy-hydration pattern:
    verb body never runs on a help path).
  - A bogus `--profile` fails loud when the verb actually executes.
  - `--dry-run` against a synthetic `--target` prints a plan and
    creates NOTHING under `--target`.
  - The verb walks the ENGINE tree (`MINERU_ENGINE_ROOT` / <ws>/engine),
    NOT the workspace root — regression guard against the earlier bug
    where `engine_root=active.workspace_absolute` symlinked charter/
    opaque and nothing rendered.
  - `--engine-root` CLI override.
  - End-to-end against the repo's REAL `engine/` tree (charter/prompts
    templates come out as RENDER actions, not opaque symlinks).
  - `_load_profile_connectors` shipped-verb path: happy path (a
    connector-derived var renders), plus every failure branch
    (absent / empty / malformed / non-mapping → the exact _die frame).
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

# The real engine tree shipped by this repo — used for the end-to-end
# verb test that proves charter/ and prompts/ actually render, not just
# through the API layer (`test_engine_templates_render_clean.py`).
REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_ENGINE_ROOT = REPO_ROOT / "engine"

# The synthetic connectors fixture. Every hydrate test that materializes a
# profile writes THIS file into `<profile>/connectors.yaml` so the shipped
# verb has all the connector keys the engine templates reference. Kept in
# one place so a new key added to the real engine tree only needs one edit.
SYNTHETIC_CONNECTORS_YAML = (
    REPO_ROOT / "tests" / "fixtures" / "synthetic-profile" / "connectors.yaml"
)


def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip all profile-affecting env so the resolver hits our fixtures."""
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
        ENGINE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def _write_synthetic_profile(
    tmp_path: Path,
    workspace_root: Path,
    name: str = "hydratetest",
    write_connectors: bool = True,
) -> Path:
    """Materialize a profile.yaml whose `workspace_absolute` is a real workspace.

    Historical trap (fixed 2026-09-03): this used to set
    `workspace_absolute` to the engine tree itself, so the verb (which
    once passed `workspace_absolute` to `build_plan` as engine_root)
    walked "the right" tree by accident, hiding the real bug where
    engine_root should be `<workspace>/engine`. The fix pins the two
    concepts as distinct: the profile's `workspace_absolute` is the
    workspace root; the engine tree lives at `<workspace>/engine/` (or
    wherever `MINERU_ENGINE_ROOT` points).

    Also drops in a `connectors.yaml` copied from the synthetic fixture
    (unless `write_connectors=False`, used by the tests that exercise the
    absent-file `_die` branch). This keeps every happy-path test aligned
    with the shipped verb's requirement that `<profile>/connectors.yaml`
    exists — the loader fails loud on absence because engine templates
    hard-reference connector keys.
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
    if write_connectors:
        shutil.copyfile(
            SYNTHETIC_CONNECTORS_YAML, profile_dir / "connectors.yaml"
        )
    return profile_dir


def _make_synthetic_engine(tmp_path: Path) -> Path:
    """A minimal engine tree for the verb-level dry-run smoke."""
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "IDENTITY.md.template").write_text(
        "# {{USER_NAME}}", encoding="utf-8"
    )
    (engine / "plain.txt").write_text("static", encoding="utf-8")
    return engine


# --- --help exits 0 without loading a profile ---------------------------


def test_hydrate_help_exits_zero_without_loading_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mineru profile hydrate --help` prints usage and NEVER hits the loader.

    Lazy hydration invariant (see test_lazy_profile_hydration.py): the
    verb body is not called on a help path, so `load_active_profile`
    must not fire.
    """
    _isolate_env(monkeypatch)

    call_count = {"n": 0}

    from mineru_cli.profile import loader as loader_module

    original = loader_module.load_active_profile

    def counting_load(*args, **kwargs):  # noqa: ANN001
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(loader_module, "load_active_profile", counting_load)

    result = runner.invoke(app, ["profile", "hydrate", "--help"])
    assert result.exit_code == 0, result.output
    assert "hydrate" in result.output.lower()
    assert "--target" in result.output
    assert "--dry-run" in result.output
    assert call_count["n"] == 0, (
        "profile hydrate --help must not load the active profile "
        f"(load_active_profile was called {call_count['n']} time(s))"
    )


# --- bogus --profile fails loud on execution ----------------------------


def test_hydrate_bogus_profile_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executing with `--profile bogus` (no such profile) exits non-zero."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "bogus-nonexistent",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
    # Target was never created — the profile loader failed first.
    assert not target.exists()


def test_hydrate_verb_level_bogus_profile_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verb-level `--profile bogus` (not root) also fails loud."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "profile",
            "hydrate",
            "--profile",
            "bogus-nonexistent",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
    assert not target.exists()


# --- dry-run prints a plan and creates nothing --------------------------


def _prepare_workspace_and_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Set up a two-directory layout: workspace root + sibling engine tree.

    Lays down a real `<workspace>/engine/` under `tmp_path` and binds
    `MINERU_ENGINE_ROOT` at the sibling engine directory. This is the
    fixed shape a real install has, and it prevents any accidental
    conflation of the workspace root and the engine tree.

    Returns `(workspace_root, engine_root)`.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = _make_synthetic_engine(workspace)
    # Bind the env-seam engine override AND the workspace root, so the
    # verb resolves the exact engine tree we just laid down.
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(engine))
    return workspace, engine


def test_hydrate_dry_run_prints_plan_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `--dry-run` invocation prints a plan without touching --target."""
    _isolate_env(monkeypatch)
    workspace, engine = _prepare_workspace_and_engine(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "HydrationPlan" in result.output
    assert "MKDIR" in result.output
    assert "RENDER" in result.output
    assert not target.exists()


def test_hydrate_dry_run_output_renders_symlinks_dest_arrow_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI dry-run surfaces the `dest -> source` SYMLINK shape end-to-end.

    Guards the render layout at the verb-boundary so a future edit that
    flips the shape back (or a regression in the `plan.render()` -
    `apply_plan(dry_run=True)` - `typer.echo` chain) is caught by the
    CLI test, not just the unit test.
    """
    _isolate_env(monkeypatch)
    workspace, engine = _prepare_workspace_and_engine(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SYMLINK" in result.output
    # `plain.txt` renders as `<target>/plain.txt -> <engine>/plain.txt`.
    expected = f"{target / 'plain.txt'}  ->  {engine / 'plain.txt'}"
    assert expected in result.output, result.output
    # The old (source -> dest) shape must not appear.
    old_shape = f"{engine / 'plain.txt'}  ->  {target / 'plain.txt'}"
    assert old_shape not in result.output, result.output


def test_hydrate_requires_target_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing `--target` fails with a usage error (safety guard)."""
    _isolate_env(monkeypatch)
    workspace, _engine = _prepare_workspace_and_engine(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    result = runner.invoke(
        app,
        ["--profile", "hydratetest", "profile", "hydrate", "--dry-run"],
    )
    # Click / Typer exits with 2 on a missing required option.
    assert result.exit_code != 0


# --- End-to-end: real engine/ tree renders (not opaque symlinks) --------


def test_hydrate_against_real_engine_dry_run_renders_charter_and_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`profile hydrate --dry-run` against the repo's REAL engine/ tree
    emits RENDER actions for charter/ and prompts/ (not opaque SYMLINKs),
    and flattens charter/ contents to the workspace root.

    Mirror of what `test_engine_templates_render_clean.py` proves at the
    API layer, but through the CLI verb — the load-bearing bug this
    guards against was `engine_root=active.workspace_absolute` (which
    made hydrate walk the WRONG tree, symlinking charter/ opaque and
    rendering nothing). Additionally, per split-manifest §3 the charter
    templates FLATTEN to the workspace root (SOUL.md, AGENTS.md,
    CLAUDE.md, etc. at `~/.mineru/`, NOT `~/.mineru/charter/`), so this
    test asserts a known charter template's rendered dest is at
    `<target>/<NAME>.md`, not under `<target>/charter/`.
    """
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Bind the workspace root to a sandbox and MINERU_ENGINE_ROOT to the
    # real engine tree so the resolver picks the real templates up.
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(REAL_ENGINE_ROOT))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "HydrationPlan" in result.output
    # RENDER actions exist for at least one template each from charter/
    # and prompts/ — proof that build_plan walked them recursively and did
    # NOT emit a single opaque SYMLINK for the whole directory.
    assert "RENDER" in result.output
    # Neither charter/ nor prompts/ appears as an opaque SYMLINK — the
    # regression this test guards against was `charter -> <engine>/charter`.
    assert f"{target / 'charter'}  ->  {REAL_ENGINE_ROOT / 'charter'}" not in result.output, (
        "charter/ appeared as an opaque SYMLINK — the regression this test guards "
        "against just re-landed."
    )
    assert f"{target / 'prompts'}  ->  {REAL_ENGINE_ROOT / 'prompts'}" not in result.output, (
        "prompts/ appeared as an opaque SYMLINK — regression."
    )
    # Charter FLATTENS: SOUL.md renders at <target>/SOUL.md (workspace root),
    # NOT at <target>/charter/SOUL.md.
    assert str(target / "SOUL.md") in result.output, result.output
    assert f"{target / 'charter' / 'SOUL.md'}" not in result.output, (
        "charter template did not flatten to workspace root — split-manifest "
        "§3 requires charter files at TOP LEVEL."
    )
    # Prompts still mirror under <target>/prompts/.
    assert str(target / "prompts") in result.output, result.output


def test_hydrate_engine_root_cli_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--engine-root PATH` wins over MINERU_ENGINE_ROOT."""
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    # Env-seam engine points at one tree.
    env_engine_parent = tmp_path / "env-engine-parent"
    env_engine_parent.mkdir()
    env_engine = _make_synthetic_engine(env_engine_parent)
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(env_engine))

    # Override engine tree — different content so we can tell which one won.
    override_engine = tmp_path / "override-engine"
    override_engine.mkdir()
    (override_engine / "OVERRIDE_MARKER.txt").write_text("override", encoding="utf-8")

    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--engine-root",
            str(override_engine),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    # The override tree's marker file appears in the plan; the env-seam tree's
    # files do NOT.
    assert "OVERRIDE_MARKER.txt" in result.output
    assert "IDENTITY.md" not in result.output, (
        "env-seam engine (which has IDENTITY.md.template) leaked past the "
        "CLI --engine-root override; the override didn't win."
    )


def test_hydrate_engine_root_missing_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolved engine_root that doesn't exist fails loud (HydrationError)."""
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    # NOTE: `MINERU_ENGINE_ROOT` NOT set, and `<workspace>/engine/` does
    # not exist. build_plan must reject it.

    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code != 0, result.output
    # The verb wraps HydrationError as a `[ERROR] profile hydrate: ...` frame.
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "does not exist" in combined, combined
    assert not target.exists()


# --- connectors.yaml wiring (rehearsal must-fix #1 regression coverage) --
#
# The shipped verb loads `<profile>/connectors.yaml` and passes it to
# `build_render_context`. Prior to Sep 16 2026 the verb hard-coded
# `connectors=None`, which broke every real hydrate on the first
# connector-derived variable (`TAILSCALE_HOSTNAME`, ...) — a Rule-8
# bypass site because the CTX assembly unit tests never exercised the
# CLI path. These tests promote the reviewer's rehearsal probes into a
# permanent guard: happy path + every failure branch of
# `_load_profile_connectors`.


def _write_engine_needing_connector_key(engine_dir: Path, key: str) -> None:
    """Lay a minimal engine tree with one template that references `key`.

    The template renders to `<target>/EXPECTED.md` and contains the raw
    `key`-derived value, so tests can grep the plan for both the RENDER
    action and the resolved value.
    """
    (engine_dir / "EXPECTED.md.template").write_text(
        f"connector-value: {{{{{key}}}}}\n", encoding="utf-8"
    )


def test_hydrate_verb_reads_connectors_yaml_and_renders_connector_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy-path: the verb loads `<profile>/connectors.yaml` and a template
    referencing a connector key resolves against that value.

    Rule-8 regression guard for must-fix #1. If this test passes but the
    real verb still hard-codes `connectors=None`, `render_template` would
    raise ValueError on `{{TAILSCALE_HOSTNAME}}` and the CLI would exit
    non-zero — which the assertion below catches directly.
    """
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = workspace / "engine"
    engine.mkdir()
    _write_engine_needing_connector_key(engine, "TAILSCALE_HOSTNAME")
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(engine))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--no-dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    rendered = (target / "EXPECTED.md").read_text(encoding="utf-8")
    # The synthetic connectors fixture pins `TAILSCALE_HOSTNAME: zephyr.tail0000ex.ts.net`.
    assert "connector-value: zephyr.tail0000ex.ts.net" in rendered, rendered


def test_hydrate_verb_dies_loud_when_connectors_yaml_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent `<profile>/connectors.yaml` → `_die` with an operator-facing
    frame that names the missing file AND explains why. Previously the
    verb silently returned `None` here and then bombed downstream with
    an unhelpful `unknown variable {{DAEMON_PERSONA_NAME}}` error.
    """
    _isolate_env(monkeypatch)
    workspace, _engine = _prepare_workspace_and_engine(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    # `write_connectors=False` → the profile deliberately lacks connectors.yaml
    # so this branch of `_load_profile_connectors` is exercised.
    _write_synthetic_profile(
        tmp_path,
        workspace_root=workspace,
        name="hydratetest",
        write_connectors=False,
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 2, result.output
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "connectors.yaml" in combined, combined
    assert "does not exist" in combined, combined
    # The message must guide the operator toward the schema, not just the fact.
    assert "UPPERCASE" in combined, combined


def test_hydrate_verb_treats_empty_connectors_yaml_as_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Present-but-empty `connectors.yaml` → `{}`, hydrate proceeds. Renders
    only bomb if a template happens to reference a connector key that's
    then missing — this test uses a synthetic engine that doesn't."""
    _isolate_env(monkeypatch)
    workspace, engine = _prepare_workspace_and_engine(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    profile_dir = _write_synthetic_profile(
        tmp_path,
        workspace_root=workspace,
        name="hydratetest",
        write_connectors=False,
    )
    (profile_dir / "connectors.yaml").write_text("", encoding="utf-8")

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "HydrationPlan" in result.output


def test_hydrate_verb_dies_loud_on_malformed_connectors_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Present-and-malformed `connectors.yaml` → `_die` naming the file."""
    _isolate_env(monkeypatch)
    workspace, _engine = _prepare_workspace_and_engine(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    profile_dir = _write_synthetic_profile(
        tmp_path,
        workspace_root=workspace,
        name="hydratetest",
        write_connectors=False,
    )
    # Unmatched bracket + control chars — definitely not valid YAML.
    (profile_dir / "connectors.yaml").write_text(
        "USER_PRIMARY_EMAIL: [oops\n\t\x00broken\n", encoding="utf-8"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 2, result.output
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "connectors.yaml" in combined, combined
    assert "not valid YAML" in combined, combined


def test_hydrate_verb_dies_loud_when_connectors_yaml_is_not_a_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A top-level YAML sequence (or scalar) in `connectors.yaml` → `_die`
    naming the type mismatch."""
    _isolate_env(monkeypatch)
    workspace, _engine = _prepare_workspace_and_engine(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    profile_dir = _write_synthetic_profile(
        tmp_path,
        workspace_root=workspace,
        name="hydratetest",
        write_connectors=False,
    )
    # Valid YAML, but a top-level list rather than the required mapping.
    (profile_dir / "connectors.yaml").write_text(
        "- USER_PRIMARY_EMAIL: sam@example.test\n- TAILSCALE_HOSTNAME: x\n",
        encoding="utf-8",
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 2, result.output
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "connectors.yaml" in combined, combined
    assert "YAML mapping" in combined, combined


# --- Rename (2026-09-16): `hydrate` -> `install`, `--no-dry-run` -> `--apply` ---
#
# The canonical name is now `install`; `hydrate` remains as a hidden Typer
# alias for ~90 days, with a stderr deprecation notice on execution. The
# `--apply` flag is the canonical opt-in write; `--no-dry-run` is a hidden
# alias flag that flips `dry_run=False` and emits its own deprecation notice.
# These tests pin the rename contract so a future edit can't silently drop
# either the alias or the notice.


def test_install_verb_help_renders_and_shows_apply_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mineru profile install --help` exits 0 and mentions `--apply`."""
    _isolate_env(monkeypatch)
    result = runner.invoke(app, ["profile", "install", "--help"])
    assert result.exit_code == 0, result.output
    assert "install" in result.output.lower()
    assert "--target" in result.output
    assert "--apply" in result.output
    # `--no-dry-run` is hidden, so it must NOT appear in --help output.
    assert "--no-dry-run" not in result.output


def test_install_dry_run_prints_plan_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canonical `install` verb honors dry-run default (same shape as old `hydrate`)."""
    _isolate_env(monkeypatch)
    workspace, _engine = _prepare_workspace_and_engine(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "install",
            "--target",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "HydrationPlan" in result.output
    assert not target.exists()


def test_install_apply_flag_writes_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--apply` on `install` writes the plan (replaces the old `--no-dry-run`)."""
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = workspace / "engine"
    engine.mkdir()
    _write_engine_needing_connector_key(engine, "TAILSCALE_HOSTNAME")
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(engine))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "install",
            "--target",
            str(target),
            "--apply",
        ],
    )
    assert result.exit_code == 0, result.output
    rendered = (target / "EXPECTED.md").read_text(encoding="utf-8")
    assert "zephyr.tail0000ex.ts.net" in rendered, rendered


def test_hydrate_alias_still_works_and_emits_deprecation_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hidden `hydrate` alias still dispatches to the install body, and
    emits one `DEPRECATED:` stderr line naming both the old and new names.
    """
    _isolate_env(monkeypatch)
    workspace, _engine = _prepare_workspace_and_engine(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "hydrate",
            "--target",
            str(target),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "DEPRECATED:" in combined, combined
    assert "profile hydrate" in combined
    assert "profile install" in combined


def test_no_dry_run_alias_flag_still_writes_and_emits_deprecation_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hidden `--no-dry-run` flag still forces apply mode and fires the
    per-flag deprecation notice on stderr.
    """
    _isolate_env(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = workspace / "engine"
    engine.mkdir()
    _write_engine_needing_connector_key(engine, "TAILSCALE_HOSTNAME")
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace))
    monkeypatch.setenv(ENGINE_ROOT_ENV_VAR, str(engine))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    _write_synthetic_profile(
        tmp_path, workspace_root=workspace, name="hydratetest"
    )

    target = tmp_path / "sandbox-target"
    result = runner.invoke(
        app,
        [
            "--profile",
            "hydratetest",
            "profile",
            "install",
            "--target",
            str(target),
            "--no-dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    # Target got written (proves --no-dry-run still flips apply mode).
    assert (target / "EXPECTED.md").exists(), result.output
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "DEPRECATED:" in combined, combined
    assert "--no-dry-run" in combined
    assert "--apply" in combined
