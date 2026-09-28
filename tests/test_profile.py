"""Tests for the profile layer (Phase 1 multi-profile framework).

Covers:
  - `resolve_profile_name`: --profile > MINERU_PROFILE > `current`
    symlink > fail-loud (no silent default).
  - `load_active_profile`: reads a valid profile.yaml, populates every
    foundation field, resolves paths to absolute, preserves extras.
  - Fail-loud on missing profile.yaml, missing required field, non-mapping
    top level, malformed YAML.
  - `secrets_config_from_profile`: F2's `SecretsResolver` picks up the
    profile's backend order, env prefix, and keychain account.
  - `mineru profile show`: pretty table and --json shapes.
  - `mineru --profile bogus profile show`: exit code 2, message names the
    missing path.
  - Seed `profiles/mineru/profile.yaml` in the worktree exists and loads.
  - `current` symlink resolution: pointer file drives the resolver when
    flag/env are absent.
  - `mineru profile use <name>` verb: atomically re-points the `current`
    symlink at an existing profile; refuses non-existent targets and
    refuses to clobber a non-symlink file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.profile import (
    ACTIVE_SYMLINK_NAME,
    CURRENT_SYMLINK_NAME,
    Profile,
    ProfileError,
    current_symlink_path,
    default_profiles_base_dir,
    default_workspace_root,
    load_active_profile,
    resolve_profile_name,
    secrets_config_from_profile,
)
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)
from mineru_cli.profile.switching import switch_active_profile
from mineru_cli.secrets import SecretsConfig, build_resolver


# Synthetic seed profile shipped under tests/fixtures/ (the engine repo does
# NOT ship a live `profiles/` tree). Used by the seed-existence test below.
SEED_PROFILE_BASE = Path(__file__).resolve().parent / "fixtures" / "seed_profile_base"


# ----------------------- helpers ---------------------------------------


def _write_profile(base: Path, name: str, body: str) -> Path:
    """Materialize a profile.yaml under `<base>/<name>/` and return its path."""
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_yaml = profile_dir / "profile.yaml"
    profile_yaml.write_text(body, encoding="utf-8")
    return profile_yaml


def _minimal_body(name: str = "alice", **overrides) -> str:
    """Minimal-required profile.yaml body; overrides can drop a field.

    Missing fields are simulated by passing `field=None` in overrides.
    `assistant_name` defaults to `TestBot` so `Profile.assistant_name` is
    demonstrably a parameterized field.
    """
    fields = {
        "name": name,
        "display_name": name.capitalize(),
        "assistant_name": "TestBot",
        "timezone": "America/Los_Angeles",
        "keychain_account": f"{name}-acct",
        "launchd_label_prefix": f"com.{name}",
        "workspace_absolute": f"/tmp/{name}-workspace",
        "memory_root": f"/tmp/{name}-workspace/memory",
        "briefs_root": f"/tmp/{name}-workspace/briefs",
        "journal_apple_notes_folder": "Daily Journals",
    }
    fields.update(overrides)
    lines = [f"{k}: {v}" for k, v in fields.items() if v is not None]
    lines.append("secrets:")
    lines.append("  backends:")
    lines.append("    - env")
    lines.append("    - keychain")
    lines.append("  env_prefix: ALICE_SECRET_")
    return "\n".join(lines) + "\n"


def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every profile-resolution env var so the test starts clean."""
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


# ----------------------- resolve_profile_name --------------------------


def test_resolve_profile_name_explicit_beats_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "from-env")
    assert resolve_profile_name("from-flag") == "from-flag"


def test_resolve_profile_name_env_beats_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    # A stray `current` symlink should NOT win over an env var.
    (tmp_path / "profiles").mkdir()
    _write_profile(tmp_path / "profiles", "sym", _minimal_body("sym"))
    os.symlink("profiles/sym", tmp_path / CURRENT_SYMLINK_NAME)
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "from-env")
    assert resolve_profile_name(None) == "from-env"


def test_resolve_profile_name_no_active_profile_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No flag, no env, no `current` symlink -> fail loud, no silent default."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    with pytest.raises(ProfileError) as exc:
        resolve_profile_name(None)
    msg = str(exc.value)
    assert "no active profile" in msg
    # The error names all three resolution surfaces so the user knows
    # what to fix.
    assert "--profile" in msg
    assert PROFILE_NAME_ENV_VAR in msg
    assert CURRENT_SYMLINK_NAME in msg


def test_resolve_profile_name_current_symlink_wins_over_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`current -> profiles/alice` resolves to `alice` when flag+env absent."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    (tmp_path / "profiles").mkdir()
    _write_profile(tmp_path / "profiles", "alice", _minimal_body("alice"))
    os.symlink("profiles/alice", tmp_path / CURRENT_SYMLINK_NAME)
    assert resolve_profile_name(None) == "alice"


def test_resolve_profile_name_current_symlink_regular_file_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A regular file at the `current` path is a loud error, not a fallback."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    (tmp_path / CURRENT_SYMLINK_NAME).write_text("sam\n")
    with pytest.raises(ProfileError) as exc:
        resolve_profile_name(None)
    assert "not a symlink" in str(exc.value)


# ----------------------- default_workspace_root ------------------------


def test_default_workspace_root_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    assert default_workspace_root() == tmp_path.resolve()


def test_default_workspace_root_legacy_profile_root_env_is_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Legacy MINERU_PROFILE_ROOT (without WORKSPACE_ROOT) is treated as
    workspace root, so tmp_path-based fixtures place humans.yaml + current
    alongside the per-profile dirs."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    assert default_workspace_root() == tmp_path.resolve()
    # And profiles base dir is ALSO the same path (legacy semantics).
    assert default_profiles_base_dir() == tmp_path.resolve()


def test_default_profiles_base_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    assert default_profiles_base_dir() == tmp_path.resolve()


def test_default_workspace_root_defaults_to_mineru_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Default workspace root is the MINERU_HOME seam (default $MINERU_HOME).

    A custom MINERU_HOME redirects it; unset falls back to $MINERU_HOME. This
    is the genericized replacement for the old worktree-scoped default —
    every user's workspace root now derives from their own MINERU_HOME.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv("MINERU_HOME", str(tmp_path))
    assert default_workspace_root() == tmp_path
    monkeypatch.delenv("MINERU_HOME", raising=False)
    assert default_workspace_root() == Path.home() / ".mineru"


def test_default_profiles_base_dir_defaults_under_mineru_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Default profiles base is `<MINERU_HOME>/profiles`."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv("MINERU_HOME", str(tmp_path))
    assert default_profiles_base_dir() == tmp_path / "profiles"


# ----------------------- current_symlink_path --------------------------


def test_current_symlink_path_default_workspace_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`current_symlink_path` (kept as a back-compat public alias post-2026-09-16
    rename) now returns the canonical `active` pointer path.
    """
    _isolate_env(monkeypatch)
    path = current_symlink_path()
    assert path.name == ACTIVE_SYMLINK_NAME
    assert path.parent == default_workspace_root()


def test_current_symlink_path_explicit_workspace_root(
    tmp_path: Path,
) -> None:
    """Post-rename: `current_symlink_path(ws)` == `ws / 'active'`."""
    path = current_symlink_path(tmp_path)
    assert path == tmp_path / ACTIVE_SYMLINK_NAME


# ----------------------- load_active_profile ---------------------------


def test_load_active_profile_populates_every_foundation_field(
    tmp_path: Path,
) -> None:
    _write_profile(tmp_path, "alice", _minimal_body("alice"))
    profile = load_active_profile("alice", base_dir=tmp_path)
    assert isinstance(profile, Profile)
    assert profile.name == "alice"
    assert profile.display_name == "Alice"
    assert profile.assistant_name == "TestBot"
    assert profile.timezone == "America/Los_Angeles"
    assert profile.keychain_account == "alice-acct"
    assert profile.launchd_label_prefix == "com.alice"
    assert profile.workspace_absolute.is_absolute()
    assert profile.memory_root.is_absolute()
    assert profile.briefs_root.is_absolute()
    assert profile.journal_apple_notes_folder == "Daily Journals"
    assert profile.secrets_backends == ["env", "keychain"]
    assert profile.secrets_env_prefix == "ALICE_SECRET_"
    assert profile.profile_yaml_path == (tmp_path / "alice" / "profile.yaml")


def test_load_active_profile_preserves_extras(tmp_path: Path) -> None:
    body = _minimal_body("alice") + (
        "\ncharter:\n"
        "  identity: prompts/IDENTITY.md\n"
        "connectors:\n"
        "  google:\n"
        "    primary_account: alice@example.com\n"
    )
    _write_profile(tmp_path, "alice", body)
    profile = load_active_profile("alice", base_dir=tmp_path)
    assert "charter" in profile.extras
    assert profile.extras["charter"] == {"identity": "prompts/IDENTITY.md"}
    assert profile.extras["connectors"]["google"]["primary_account"] == (
        "alice@example.com"
    )


def test_load_active_profile_missing_file_names_path(tmp_path: Path) -> None:
    with pytest.raises(ProfileError) as exc:
        load_active_profile("ghost", base_dir=tmp_path)
    msg = str(exc.value)
    assert "ghost" in msg
    expected = tmp_path / "ghost" / "profile.yaml"
    assert str(expected) in msg


@pytest.mark.parametrize(
    "missing_field",
    [
        "display_name",
        "assistant_name",
        "timezone",
        "keychain_account",
        "launchd_label_prefix",
        "workspace_absolute",
        "memory_root",
        "briefs_root",
        "journal_apple_notes_folder",
    ],
)
def test_load_active_profile_missing_required_field_names_field(
    tmp_path: Path, missing_field: str
) -> None:
    body = _minimal_body("alice", **{missing_field: None})
    _write_profile(tmp_path, "alice", body)
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert missing_field in str(exc.value)


def test_load_active_profile_missing_name_field_names_field(
    tmp_path: Path,
) -> None:
    body = "\n".join(
        [
            "display_name: Alice",
            "assistant_name: TestBot",
            "timezone: America/Los_Angeles",
            "keychain_account: alice-acct",
            "launchd_label_prefix: com.alice",
            "workspace_absolute: /tmp/alice-workspace",
            "memory_root: /tmp/alice-workspace/memory",
            "briefs_root: /tmp/alice-workspace/briefs",
            "journal_apple_notes_folder: Daily Journals",
            "secrets:",
            "  backends:",
            "    - env",
            "    - keychain",
            "  env_prefix: ALICE_SECRET_",
        ]
    ) + "\n"
    _write_profile(tmp_path, "alice", body)
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert "'name'" in str(exc.value)


def test_load_active_profile_missing_secrets_block_names_dotted_field(
    tmp_path: Path,
) -> None:
    body = "\n".join(
        [
            "name: alice",
            "display_name: Alice",
            "assistant_name: TestBot",
            "timezone: America/Los_Angeles",
            "keychain_account: a",
            "launchd_label_prefix: com.a",
            "workspace_absolute: /tmp/a",
            "memory_root: /tmp/a/m",
            "briefs_root: /tmp/a/b",
            "journal_apple_notes_folder: J",
        ]
    ) + "\n"
    _write_profile(tmp_path, "alice", body)
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert "secrets.backends" in str(exc.value)


def test_load_active_profile_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    _write_profile(tmp_path, "alice", "just a scalar string\n")
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert "mapping" in str(exc.value)


def test_load_active_profile_rejects_malformed_yaml(tmp_path: Path) -> None:
    _write_profile(tmp_path, "alice", ":\n  - bad\n  broken: [\n")
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert "not valid YAML" in str(exc.value) or "profile.yaml" in str(exc.value)


def test_load_active_profile_name_mismatch_fails_loud(tmp_path: Path) -> None:
    _write_profile(tmp_path, "alice", _minimal_body(name="wrong-name-in-file"))
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert "alice" in str(exc.value)
    assert "wrong-name-in-file" in str(exc.value)


def test_load_active_profile_rejects_empty_secrets_backends(tmp_path: Path) -> None:
    body = "\n".join(
        [
            "name: alice",
            "display_name: Alice",
            "assistant_name: TestBot",
            "timezone: America/Los_Angeles",
            "keychain_account: alice-acct",
            "launchd_label_prefix: com.alice",
            "workspace_absolute: /tmp/alice-workspace",
            "memory_root: /tmp/alice-workspace/memory",
            "briefs_root: /tmp/alice-workspace/briefs",
            "journal_apple_notes_folder: Daily Journals",
            "secrets:",
            "  backends: []",
            "  env_prefix: ALICE_SECRET_",
        ]
    ) + "\n"
    _write_profile(tmp_path, "alice", body)
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert "secrets.backends" in str(exc.value)
    assert "non-empty" in str(exc.value)


def test_load_active_profile_rejects_unknown_secrets_backend(tmp_path: Path) -> None:
    body = _minimal_body("alice").replace(
        "    - env\n    - keychain\n",
        "    - env\n    - kchain\n",
    )
    _write_profile(tmp_path, "alice", body)
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert "kchain" in str(exc.value)
    assert "expected" in str(exc.value)


def test_load_active_profile_rejects_non_mapping_secrets_block(tmp_path: Path) -> None:
    body = "\n".join(
        [
            "name: alice",
            "display_name: Alice",
            "assistant_name: TestBot",
            "timezone: America/Los_Angeles",
            "keychain_account: alice-acct",
            "launchd_label_prefix: com.alice",
            "workspace_absolute: /tmp/alice-workspace",
            "memory_root: /tmp/alice-workspace/memory",
            "briefs_root: /tmp/alice-workspace/briefs",
            "journal_apple_notes_folder: Daily Journals",
            "secrets:",
            "  - env",
            "  - keychain",
        ]
    ) + "\n"
    _write_profile(tmp_path, "alice", body)
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    msg = str(exc.value)
    assert "must be a mapping" in msg
    assert "list" in msg


@pytest.mark.parametrize(
    "bad_value",
    ["", "foo/bar", "./relative"],
)
def test_load_active_profile_rejects_relative_workspace_absolute(
    tmp_path: Path, bad_value: str
) -> None:
    body = _minimal_body("alice", workspace_absolute=bad_value)
    _write_profile(tmp_path, "alice", body)
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert "workspace_absolute" in str(exc.value)


def test_load_active_profile_rejects_non_string_timezone(tmp_path: Path) -> None:
    body = _minimal_body("alice").replace(
        "timezone: America/Los_Angeles",
        "timezone: 42",
    )
    _write_profile(tmp_path, "alice", body)
    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert "timezone" in str(exc.value)
    assert "string" in str(exc.value)


@pytest.mark.parametrize(
    "bad_name",
    ["../evil", "foo/bar", ".", ".."],
)
def test_resolve_profile_name_rejects_traversal_in_flag(bad_name: str) -> None:
    with pytest.raises(ProfileError) as exc:
        resolve_profile_name(bad_name)
    assert "invalid profile name" in str(exc.value)


def test_resolve_profile_name_rejects_traversal_in_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "../evil")
    with pytest.raises(ProfileError) as exc:
        resolve_profile_name(None)
    assert "invalid profile name" in str(exc.value)
    assert PROFILE_NAME_ENV_VAR in str(exc.value)


# ----------------------- F2 composition end-to-end ---------------------


def test_secrets_config_from_profile_carries_all_three_knobs(
    tmp_path: Path,
) -> None:
    _write_profile(tmp_path, "alice", _minimal_body("alice"))
    profile = load_active_profile("alice", base_dir=tmp_path)
    config = secrets_config_from_profile(profile)
    assert isinstance(config, SecretsConfig)
    assert config.backends == ["env", "keychain"]
    assert config.env_prefix == "ALICE_SECRET_"
    assert config.keychain_account == "alice-acct"


def test_profile_derived_resolver_uses_profile_env_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_profile(tmp_path, "alice", _minimal_body("alice"))
    profile = load_active_profile("alice", base_dir=tmp_path)
    resolver = build_resolver(secrets_config_from_profile(profile))
    monkeypatch.setenv("ALICE_SECRET_MY_KEY", "the-value")
    result = resolver.resolve("my-key")
    assert result.present is True
    assert result.value == "the-value"
    monkeypatch.setenv("MINERU_SECRET_MY_KEY", "wrong-value")
    result2 = resolver.resolve("my-key")
    assert result2.value == "the-value"


# ----------------------- CLI: mineru profile show ----------------------


runner = CliRunner()


def test_cli_profile_show_pretty_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch)
    _write_profile(tmp_path, "alice", _minimal_body("alice"))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["--profile", "alice", "profile", "show"])
    assert result.exit_code == 0, result.output
    assert "name" in result.stdout and "alice" in result.stdout
    assert "keychain_account" in result.stdout
    assert "alice-acct" in result.stdout
    assert "secrets_backends" in result.stdout
    assert "ALICE_SECRET_" in result.stdout


def test_cli_profile_show_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch)
    _write_profile(tmp_path, "alice", _minimal_body("alice"))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    result = runner.invoke(
        app, ["--profile", "alice", "profile", "show", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "alice"
    assert payload["secrets_backends"] == ["env", "keychain"]
    assert payload["secrets_env_prefix"] == "ALICE_SECRET_"
    assert isinstance(payload["profile_yaml_path"], str)


def test_cli_profile_show_bogus_profile_exits_nonzero_naming_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["--profile", "bogus", "profile", "show"])
    assert result.exit_code != 0
    output = result.output
    assert "bogus" in output
    assert "not found" in output
    assert "profile.yaml" in output
    tail_ok = str(tmp_path.name) in output or str(
        tmp_path / "bogus" / "profile.yaml"
    ) in output
    assert tail_ok


# ----------------------- Seed profile ---------------------------------


def test_seed_profile_fixture_ships() -> None:
    """A synthetic `mineru` seed ships under tests/fixtures/ for the suite."""
    seed = SEED_PROFILE_BASE / "mineru" / "profile.yaml"
    assert seed.exists(), f"seed profile fixture missing: {seed}"


def test_flagless_resolution_via_active_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """With no flag / env, an `active -> profiles/<name>` symlink at the
    workspace root drives resolution (NOT a silent default).

    Self-contained: builds a tmp workspace + symlink rather than relying on
    a committed seed, since the engine repo ships no live `profiles/` tree.
    """
    _isolate_env(monkeypatch)
    ws = tmp_path.resolve()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(ws))
    (ws / "profiles").mkdir()
    _write_profile(ws / "profiles", "mineru", _minimal_body("mineru"))
    os.symlink("profiles/mineru", ws / ACTIVE_SYMLINK_NAME)

    active = current_symlink_path(default_workspace_root())
    assert active == ws / ACTIVE_SYMLINK_NAME
    assert os.path.islink(active)
    assert Path(os.readlink(active)).name == "mineru"

    profile = load_active_profile(None)
    assert profile.name == "mineru"
    assert profile.secrets_backends == ["env", "keychain"]


def test_flagless_resolution_falls_back_to_legacy_current_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Legacy fallback: a pre-2026-09-16 workspace has only a `current`
    symlink (no `active`); the loader must still resolve it.
    """
    _isolate_env(monkeypatch)
    ws = tmp_path.resolve()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(ws))
    (ws / "profiles").mkdir()
    _write_profile(ws / "profiles", "mineru", _minimal_body("mineru"))
    # Only the LEGACY name exists — canonical `active` is absent.
    os.symlink("profiles/mineru", ws / CURRENT_SYMLINK_NAME)
    assert not (ws / ACTIVE_SYMLINK_NAME).exists()

    profile = load_active_profile(None)
    assert profile.name == "mineru"


def test_flagless_resolution_prefers_active_over_legacy_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When both `active` and `current` exist, `active` wins."""
    _isolate_env(monkeypatch)
    ws = tmp_path.resolve()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(ws))
    (ws / "profiles").mkdir()
    _write_profile(ws / "profiles", "alice", _minimal_body("alice"))
    _write_profile(ws / "profiles", "bob", _minimal_body("bob"))
    os.symlink("profiles/alice", ws / ACTIVE_SYMLINK_NAME)
    os.symlink("profiles/bob", ws / CURRENT_SYMLINK_NAME)

    profile = load_active_profile(None)
    assert profile.name == "alice"


# ----------------------- Positive tests for `profile use` --------------


def _setup_two_profiles(base: Path) -> None:
    """Materialize `alice` + `bob` under `base/profiles/` for switching tests."""
    profiles = base / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    _write_profile(profiles, "alice", _minimal_body("alice"))
    _write_profile(profiles, "bob", _minimal_body("bob"))


def test_switch_active_profile_creates_current_symlink(
    tmp_path: Path,
) -> None:
    _setup_two_profiles(tmp_path)
    current = switch_active_profile(
        "alice", workspace_root=tmp_path, profiles_base_dir=tmp_path / "profiles"
    )
    assert os.path.islink(current)
    target = os.readlink(current)
    assert Path(target).name == "alice"


def test_switch_active_profile_atomically_replaces_existing(
    tmp_path: Path,
) -> None:
    _setup_two_profiles(tmp_path)
    switch_active_profile(
        "alice", workspace_root=tmp_path, profiles_base_dir=tmp_path / "profiles"
    )
    switch_active_profile(
        "bob", workspace_root=tmp_path, profiles_base_dir=tmp_path / "profiles"
    )
    active = tmp_path / ACTIVE_SYMLINK_NAME
    assert Path(os.readlink(active)).name == "bob"


def test_switch_active_profile_rejects_missing_target(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProfileError) as exc:
        switch_active_profile(
            "ghost",
            workspace_root=tmp_path,
            profiles_base_dir=tmp_path / "profiles",
        )
    assert "ghost" in str(exc.value)
    assert "profile.yaml" in str(exc.value)


def test_switch_active_profile_refreshes_stale_legacy_current_symlink(
    tmp_path: Path,
) -> None:
    """Migration-window bookkeeping: when a legacy `current` symlink is
    already in the workspace, `switch_active_profile` refreshes it to
    the new target so the fallback path can never return a stale name
    if `active` is later deleted.
    """
    _setup_two_profiles(tmp_path)
    # Simulate a pre-2026-09-16 workspace: only the legacy `current`
    # symlink exists, pointing at alice.
    os.symlink("profiles/alice", tmp_path / CURRENT_SYMLINK_NAME)
    switch_active_profile(
        "bob", workspace_root=tmp_path, profiles_base_dir=tmp_path / "profiles"
    )
    # `active` is the canonical write target.
    assert Path(os.readlink(tmp_path / ACTIVE_SYMLINK_NAME)).name == "bob"
    # `current` also updated so a legacy-fallback reader sees bob too.
    assert Path(os.readlink(tmp_path / CURRENT_SYMLINK_NAME)).name == "bob"


def test_switch_active_profile_leaves_no_legacy_current_untouched(
    tmp_path: Path,
) -> None:
    """A fresh workspace (no `current`) is NOT retrofitted with one — we
    only refresh a legacy symlink if it exists, never create it.
    """
    _setup_two_profiles(tmp_path)
    switch_active_profile(
        "alice", workspace_root=tmp_path, profiles_base_dir=tmp_path / "profiles"
    )
    assert (tmp_path / ACTIVE_SYMLINK_NAME).exists()
    # No pre-existing legacy symlink → we don't plant one.
    assert not (tmp_path / CURRENT_SYMLINK_NAME).exists()


def test_switch_active_profile_rejects_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(ProfileError) as exc:
        switch_active_profile(
            "../evil",
            workspace_root=tmp_path,
            profiles_base_dir=tmp_path / "profiles",
        )
    assert "invalid profile name" in str(exc.value)


def test_switch_active_profile_refuses_to_overwrite_regular_file(
    tmp_path: Path,
) -> None:
    """A non-symlink at the `active` path is a loud refusal, not a clobber."""
    _setup_two_profiles(tmp_path)
    (tmp_path / ACTIVE_SYMLINK_NAME).write_text("do not clobber\n")
    with pytest.raises(ProfileError) as exc:
        switch_active_profile(
            "alice",
            workspace_root=tmp_path,
            profiles_base_dir=tmp_path / "profiles",
        )
    assert "refusing to overwrite non-symlink" in str(exc.value)


def test_switch_active_profile_replaces_dangling_symlink(
    tmp_path: Path,
) -> None:
    """A dangling `active` symlink (target deleted) is safe to replace."""
    _setup_two_profiles(tmp_path)
    os.symlink("profiles/does-not-exist", tmp_path / ACTIVE_SYMLINK_NAME)
    switch_active_profile(
        "alice",
        workspace_root=tmp_path,
        profiles_base_dir=tmp_path / "profiles",
    )
    assert Path(os.readlink(tmp_path / ACTIVE_SYMLINK_NAME)).name == "alice"


def test_switch_active_profile_symlinked_profile_dir_preserves_name(
    tmp_path: Path,
) -> None:
    """A `profiles/<name>` that is itself a symlink to an external dir MUST
    store the profile NAME in `current`, not the symlink's external
    resolved path.

    Fable adversarial review Finding 5: `_relative_target` used to
    `.resolve()` `profile_dir`, so a symlinked `profiles/alice ->
    /some/external/x` stored the ABSOLUTE external path. The reader
    later extracted `Path(target).name` = `"x"` and tried to load
    `<base>/x/profile.yaml`, which doesn't exist — silent activation
    failure. The fix uses the un-resolved path so the stored basename
    stays `"alice"`, and `load_active_profile("alice")` reads through
    the symlink correctly.
    """
    # Materialize the real profile at an EXTERNAL location outside the
    # workspace root, then plant a symlink at profiles/alice pointing
    # at it.
    external = tmp_path.parent / f"external-{tmp_path.name}"
    external.mkdir(exist_ok=True)
    external_alice = external / "alice-real"
    external_alice.mkdir(exist_ok=True)
    (external_alice / "profile.yaml").write_text(
        _minimal_body("alice"), encoding="utf-8"
    )

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    # symlink profiles/alice -> /external/alice-real
    os.symlink(str(external_alice), str(profiles / "alice"))

    current = switch_active_profile(
        "alice",
        workspace_root=tmp_path,
        profiles_base_dir=profiles,
    )
    assert os.path.islink(current)
    target = os.readlink(current)
    # The stored target's LAST component MUST be the profile name,
    # not the external symlink target's basename (`alice-real`).
    assert Path(target).name == "alice", (
        f"stored symlink target {target!r} does not end in 'alice'; "
        "the pre-fix `.resolve()` followed the symlink and broke the "
        "reader's `Path(target).name` extraction."
    )
    # And the loader can pick up the profile via the stored target.
    reloaded = load_active_profile("alice", base_dir=profiles)
    assert reloaded.name == "alice"
    # Cleanup: the external dir was created outside tmp_path so the
    # tmp fixture won't auto-clean it. Best-effort teardown.
    try:
        (external_alice / "profile.yaml").unlink()
        external_alice.rmdir()
        external.rmdir()
    except OSError:
        pass


def test_cli_profile_use_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mineru profile use <name>` writes the symlink and the next
    `mineru profile show` picks up the new active profile."""
    _isolate_env(monkeypatch)
    _setup_two_profiles(tmp_path)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    # Activate alice via the CLI.
    result = runner.invoke(app, ["profile", "use", "alice"])
    assert result.exit_code == 0, result.output
    assert "active profile: alice" in result.output
    # Now a fresh invocation with no --profile / env resolves alice via
    # the symlink.
    result = runner.invoke(app, ["profile", "show"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.stdout
    assert "alice-acct" in result.stdout


def test_cli_profile_use_rejects_missing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch)
    _setup_two_profiles(tmp_path)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["profile", "use", "ghost"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_cli_profile_active_reports_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical `profile active` verb prints the active profile name
    on a clean stdout (no deprecation noise)."""
    _isolate_env(monkeypatch)
    _setup_two_profiles(tmp_path)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    runner.invoke(app, ["profile", "use", "bob"])
    result = runner.invoke(app, ["profile", "active"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "bob"


def test_cli_profile_active_no_symlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`profile active` with no pointer symlink exits non-zero + names both
    the canonical `active` path AND the legacy `current` fallback path so
    the operator sees what the loader looked for."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["profile", "active"])
    assert result.exit_code != 0
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "no `active` symlink" in combined
    assert "legacy `current`" in combined


def test_cli_profile_current_alias_still_works_and_emits_deprecation_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hidden `profile current` alias dispatches identically AND fires
    the one-line DEPRECATED stderr notice on use.
    """
    _isolate_env(monkeypatch)
    _setup_two_profiles(tmp_path)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    runner.invoke(app, ["profile", "use", "bob"])
    result = runner.invoke(app, ["profile", "current"])
    assert result.exit_code == 0, result.output
    # Body still prints the active name somewhere in the combined output.
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "bob" in combined
    assert "DEPRECATED:" in combined
    assert "profile current" in combined
    assert "profile active" in combined
