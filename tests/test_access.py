"""Tests for the per-profile access allowlist (Phase 1).

Covers:
  - `load_access_config`: valid access.yaml -> populated `AccessConfig`
    with owner + authorized list, cross-validated against humans registry.
  - Owner materialized implicitly when omitted from `authorized:`.
  - Fail-loud on missing file, unknown owner handle, unknown authorized
    handle, unknown tier, duplicate humans, tier=owner for non-owner.
  - `AccessTier` enum: `(str, Enum)` with `OWNER` and (deferred) `GUEST`
    members; string values are stable.
  - `mineru access show`: pretty + JSON; requires an active profile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mineru_cli.access import (
    AccessConfig,
    AccessError,
    AccessTier,
    load_access_config,
)
from mineru_cli.app import app
from mineru_cli.humans.schema import Human, HumansRegistry
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)


runner = CliRunner()


def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def _humans(*handles: str) -> HumansRegistry:
    """Build a HumansRegistry from a list of handles (assigns synthetic ids)."""
    entries = {
        h: Human(handle=h, telegram_id=1000 + i, display_name=h.capitalize())
        for i, h in enumerate(handles)
    }
    return HumansRegistry(entries_by_handle=entries)


def _profile(tmp_path: Path, name: str = "alice") -> "Profile":
    """Materialize a valid profile.yaml and load it."""
    from mineru_cli.profile import load_active_profile

    body = (
        f"name: {name}\n"
        f"display_name: {name.capitalize()}\n"
        "assistant_name: TestBot\n"
        "timezone: America/Los_Angeles\n"
        f"keychain_account: {name}-acct\n"
        f"launchd_label_prefix: com.{name}\n"
        f"workspace_absolute: /tmp/{name}-workspace\n"
        f"memory_root: /tmp/{name}-workspace/memory\n"
        f"briefs_root: /tmp/{name}-workspace/briefs\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        f"  env_prefix: {name.upper()}_SECRET_\n"
    )
    (tmp_path / name).mkdir()
    (tmp_path / name / "profile.yaml").write_text(body, encoding="utf-8")
    return load_active_profile(name, base_dir=tmp_path)


# --- AccessTier enum ---------------------------------------------------


def test_access_tier_is_str_enum() -> None:
    """AccessTier is `(str, Enum)` per the project Python style guide."""
    assert issubclass(AccessTier, str)
    assert AccessTier.OWNER.value == "owner"
    # GUEST is present as a documented DEFERRED slot.
    assert AccessTier.GUEST.value == "guest"


# --- load_access_config: happy paths ----------------------------------


def test_load_access_config_owner_only(tmp_path: Path) -> None:
    profile = _profile(tmp_path, "alice")
    humans = _humans("alice")
    access_yaml = profile.profile_root / "access.yaml"
    access_yaml.write_text(
        "owner: alice\n"
        "authorized:\n"
        "  - {human: alice, tier: owner}\n",
        encoding="utf-8",
    )
    config = load_access_config(profile, humans)
    assert isinstance(config, AccessConfig)
    assert config.profile_name == "alice"
    assert config.owner == "alice"
    assert len(config.authorized) == 1
    assert config.authorized[0].human == "alice"
    assert config.authorized[0].tier == AccessTier.OWNER
    assert config.guest_entries() == []


def test_load_access_config_materializes_owner_when_omitted(
    tmp_path: Path,
) -> None:
    """The owner is always present as an OWNER entry, even if the operator
    forgot to list themselves in `authorized:`."""
    profile = _profile(tmp_path, "alice")
    humans = _humans("alice")
    access_yaml = profile.profile_root / "access.yaml"
    access_yaml.write_text("owner: alice\n", encoding="utf-8")
    config = load_access_config(profile, humans)
    assert config.owner == "alice"
    assert len(config.authorized) == 1
    owner_entry = config.owner_entry()
    assert owner_entry.human == "alice"
    assert owner_entry.tier == AccessTier.OWNER


def test_load_access_config_tolerates_zero_guests(tmp_path: Path) -> None:
    """Phase 1: an owner-only allowlist is a valid, common shape."""
    profile = _profile(tmp_path, "alice")
    humans = _humans("alice", "bob")  # bob is registered but not authorized
    access_yaml = profile.profile_root / "access.yaml"
    access_yaml.write_text(
        "owner: alice\nauthorized:\n  - {human: alice, tier: owner}\n",
        encoding="utf-8",
    )
    config = load_access_config(profile, humans)
    assert config.guest_entries() == []


def test_load_access_config_accepts_guest_tier_entry(tmp_path: Path) -> None:
    """A guest-tier entry is TOLERATED at load time; enforcement is deferred."""
    profile = _profile(tmp_path, "alice")
    humans = _humans("alice", "bob")
    access_yaml = profile.profile_root / "access.yaml"
    access_yaml.write_text(
        "owner: alice\n"
        "authorized:\n"
        "  - {human: alice, tier: owner}\n"
        "  - {human: bob, tier: guest}\n",
        encoding="utf-8",
    )
    config = load_access_config(profile, humans)
    guests = config.guest_entries()
    assert len(guests) == 1
    assert guests[0].human == "bob"
    assert guests[0].tier == AccessTier.GUEST


# --- load_access_config: fail-loud paths ------------------------------


def test_load_access_config_missing_file_names_path(tmp_path: Path) -> None:
    profile = _profile(tmp_path, "alice")
    with pytest.raises(AccessError) as exc:
        load_access_config(profile, _humans("alice"))
    assert "access.yaml" in str(exc.value)
    assert str(profile.profile_root / "access.yaml") in str(exc.value)


def test_load_access_config_missing_owner(tmp_path: Path) -> None:
    profile = _profile(tmp_path, "alice")
    (profile.profile_root / "access.yaml").write_text("authorized: []\n")
    with pytest.raises(AccessError) as exc:
        load_access_config(profile, _humans("alice"))
    assert "owner" in str(exc.value)


def test_load_access_config_owner_missing_from_humans_registry(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path, "alice")
    (profile.profile_root / "access.yaml").write_text("owner: ghost\n")
    with pytest.raises(AccessError) as exc:
        load_access_config(profile, _humans("alice"))
    assert "ghost" in str(exc.value)
    assert "humans.yaml" in str(exc.value)


def test_load_access_config_unknown_authorized_human(tmp_path: Path) -> None:
    profile = _profile(tmp_path, "alice")
    (profile.profile_root / "access.yaml").write_text(
        "owner: alice\n"
        "authorized:\n"
        "  - {human: alice, tier: owner}\n"
        "  - {human: ghost, tier: guest}\n",
    )
    with pytest.raises(AccessError) as exc:
        load_access_config(profile, _humans("alice"))
    assert "ghost" in str(exc.value)


def test_load_access_config_unknown_tier(tmp_path: Path) -> None:
    profile = _profile(tmp_path, "alice")
    (profile.profile_root / "access.yaml").write_text(
        "owner: alice\n"
        "authorized:\n"
        "  - {human: alice, tier: admin}\n",
    )
    with pytest.raises(AccessError) as exc:
        load_access_config(profile, _humans("alice"))
    assert "admin" in str(exc.value)


def test_load_access_config_duplicate_human(tmp_path: Path) -> None:
    profile = _profile(tmp_path, "alice")
    humans = _humans("alice", "bob")
    (profile.profile_root / "access.yaml").write_text(
        "owner: alice\n"
        "authorized:\n"
        "  - {human: alice, tier: owner}\n"
        "  - {human: bob, tier: guest}\n"
        "  - {human: bob, tier: guest}\n",
    )
    with pytest.raises(AccessError) as exc:
        load_access_config(profile, humans)
    assert "bob" in str(exc.value)
    assert "more than once" in str(exc.value)


def test_load_access_config_rejects_owner_tier_for_non_owner(
    tmp_path: Path,
) -> None:
    """Only the profile owner may carry tier=owner."""
    profile = _profile(tmp_path, "alice")
    humans = _humans("alice", "bob")
    (profile.profile_root / "access.yaml").write_text(
        "owner: alice\n"
        "authorized:\n"
        "  - {human: bob, tier: owner}\n",
    )
    with pytest.raises(AccessError) as exc:
        load_access_config(profile, humans)
    assert "bob" in str(exc.value)
    assert "owner" in str(exc.value)


def test_load_access_config_rejects_non_mapping_top_level(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path, "alice")
    (profile.profile_root / "access.yaml").write_text("just a scalar\n")
    with pytest.raises(AccessError) as exc:
        load_access_config(profile, _humans("alice"))
    assert "mapping" in str(exc.value)


# --- Seed access.yaml in the worktree ---------------------------------


def test_seed_access_yaml_fixture_is_owner_only() -> None:
    """The synthetic `mineru` seed shipped under tests/fixtures/ carries an
    owner-only access.yaml (the engine ships no live `profiles/` tree)."""
    access_yaml = (
        Path(__file__).resolve().parent
        / "fixtures" / "seed_profile_base" / "mineru" / "access.yaml"
    )
    assert access_yaml.exists()


# --- CLI: mineru access show ------------------------------------------


def test_cli_access_show_pretty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    profile = _profile(tmp_path, "alice")
    # Write a humans.yaml under the workspace root and set the profile
    # base dir env so the CLI hits our fixtures.
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    (tmp_path / "humans.yaml").write_text(
        "humans:\n"
        "  alice:\n"
        "    telegram_id: 1\n"
        "    display_name: Alice\n"
    )
    (profile.profile_root / "access.yaml").write_text(
        "owner: alice\nauthorized:\n  - {human: alice, tier: owner}\n"
    )
    result = runner.invoke(app, ["--profile", "alice", "access", "show"])
    assert result.exit_code == 0, result.output
    assert "profile:" in result.output
    assert "alice" in result.output
    assert "owner:" in result.output
    assert "(owner)" in result.output
    assert "0 guests" in result.output or "guest" in result.output


def test_cli_access_show_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    profile = _profile(tmp_path, "alice")
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    (tmp_path / "humans.yaml").write_text(
        "humans:\n"
        "  alice:\n"
        "    telegram_id: 1\n"
        "    display_name: Alice\n"
    )
    (profile.profile_root / "access.yaml").write_text(
        "owner: alice\n"
        "authorized:\n  - {human: alice, tier: owner}\n"
    )
    result = runner.invoke(
        app, ["--profile", "alice", "access", "show", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["profile_name"] == "alice"
    assert payload["owner"] == "alice"
    assert payload["authorized"] == [{"human": "alice", "tier": "owner"}]
