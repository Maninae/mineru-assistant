"""Tests for the Phase 1.5 profile onboarding flow.

Covers `mineru_cli.profile.onboarding` + `mineru profile init`:

  - Profile-name uniqueness enforced ATOMICALLY via `os.makedirs
    (exist_ok=False)` (duplicate name fails with a clear message).
  - Name shape + reserved-name rejection (lowercase kebab-case,
    workspace collision, RESERVED_PROFILE_NAMES).
  - Owner registration appends to humans.yaml (atomic tmp+replace).
  - Scaffold correctness: profile.yaml round-trips through the loader;
    access.yaml validates owner-only against the humans registry;
    memory/, briefs/, cache/, logs/ dirs land on disk.
  - Atomic cleanup on injected mid-scaffold failure (no partial dir
    remains, humans.yaml still untouched by that call).
  - Bootstrap case: zero profiles + no humans.yaml → creates humans.yaml
    AND flips the `current` symlink automatically.
  - No-auto-switch in the normal case (`activate=False` on a workspace
    that already has an active profile).
  - `--no-input` fails when a required field is missing.
  - `mineru profile init --help` renders on an empty workspace root.
  - The Google walkthrough shells out to a mocked `gog auth add <email>`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from mineru_cli.access.loader import load_access_config
from mineru_cli.access.schema import AccessTier
from mineru_cli.app import app
from mineru_cli.humans import HumansRegistry, load_humans_registry
from mineru_cli.humans.schema import Human
from mineru_cli.profile import (
    ACTIVE_SYMLINK_NAME,
    CURRENT_SYMLINK_NAME,
    current_symlink_path,
    load_active_profile,
)
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)
from mineru_cli.profile.onboarding import (
    CONNECTORS_PLACEHOLDER_TOKEN,
    DEFAULT_JOURNAL_APPLE_NOTES_FOLDER,
    DEFAULT_SECRETS_BACKENDS,
    NewHuman,
    OnboardingError,
    PROFILE_DIR_SUFFIX_HEX_LEN,
    ProfileSpec,
    RESERVED_PROFILE_NAMES,
    ScaffoldResult,
    build_gog_auth_command,
    check_persona_collision,
    create_profile,
    default_env_prefix_from_name,
    default_persona_from_name,
    format_gog_command_string,
    machine_timezone,
    run_gog_auth_add,
    validate_new_human,
    validate_persona,
    validate_profile_name,
    validate_timezone,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin workspace_root at tmp_path; no pre-existing profiles or humans.yaml."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    return tmp_path


@pytest.fixture
def workspace_with_sam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Workspace with a pre-existing sam human + a pre-existing 'sam' profile.

    Lets tests exercise the non-bootstrap branch (existing registry file,
    existing active profile → no auto-switch when activate=False). Uses
    the CANONICAL `people.yaml` (post-2026-09-16 audit §2A F3); the
    fallback path via a legacy `humans.yaml` is covered by dedicated
    tests further down.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    (tmp_path / "people.yaml").write_text(
        "humans:\n"
        "  sam:\n"
        "    telegram_id: 111\n"
        "    display_name: \"Sam Rivera\"\n",
        encoding="utf-8",
    )
    # Materialize a real, loadable sam profile so we exercise the
    # "already has profiles" path (bootstrap-case detection depends on
    # the presence of prior profile.yaml files).
    sam_dir = tmp_path / "profiles" / "sam"
    sam_dir.mkdir(parents=True)
    (sam_dir / "profile.yaml").write_text(
        "name: sam\n"
        "display_name: Sam\n"
        "assistant_name: Mineru\n"
        "timezone: America/Los_Angeles\n"
        "keychain_account: sam\n"
        "launchd_label_prefix: com.sam\n"
        f"workspace_absolute: {tmp_path}\n"
        f"memory_root: {tmp_path}/profiles/sam/memory\n"
        f"briefs_root: {tmp_path}/profiles/sam/briefs\n"
        f"journal_apple_notes_folder: Daily Journals\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        "  env_prefix: SAM_SECRET_\n",
        encoding="utf-8",
    )
    (sam_dir / "access.yaml").write_text(
        "owner: sam\n"
        "authorized:\n"
        "  - {human: sam, tier: owner}\n",
        encoding="utf-8",
    )
    # Point `current` at sam so we can prove init does NOT auto-flip it.
    os.symlink("profiles/sam", tmp_path / CURRENT_SYMLINK_NAME)
    return tmp_path


def _bare_spec(name: str, owner: str = "sam") -> ProfileSpec:
    """Return a valid ProfileSpec that references an EXISTING owner."""
    return ProfileSpec(
        name=name,
        persona=name.capitalize(),
        owner_handle=owner,
        timezone="America/Los_Angeles",
    )


def _bootstrap_spec(
    name: str = "alice", handle: str = "alice", tg_id: int = 42
) -> ProfileSpec:
    """Return a ProfileSpec with an inline-added owner (bootstrap flow)."""
    return ProfileSpec(
        name=name,
        persona="Alice",
        owner_handle=handle,
        timezone="America/Los_Angeles",
        new_human=NewHuman(
            handle=handle, display_name="Alice Smith", telegram_id=tg_id
        ),
    )


# ---------------------------------------------------------------------------
# Helper-function unit tests
# ---------------------------------------------------------------------------


def test_default_persona_from_name_titlecases_and_dehyphenates() -> None:
    assert default_persona_from_name("sam") == "Sam"
    assert default_persona_from_name("gemini-scout") == "Gemini Scout"
    assert default_persona_from_name("multi-word-name") == "Multi Word Name"


def test_default_env_prefix_from_name_upper_underscores() -> None:
    assert default_env_prefix_from_name("sam") == "SAM_SECRET_"
    assert default_env_prefix_from_name("gemini-scout") == "GEMINI_SCOUT_SECRET_"


def test_machine_timezone_returns_nonempty_string() -> None:
    tz = machine_timezone()
    assert isinstance(tz, str)
    assert tz.strip() != ""


def test_build_gog_auth_command_matches_verified_form() -> None:
    """Verified 2026-08-28 against `gog auth add --help`: form is
    `gog auth add <email>` (email is a positional arg, not a flag).

    A `--` separator between `auth add` and the email keeps an email that
    happens to start with `-` from being parsed as a gog flag."""
    cmd = build_gog_auth_command("me@example.com")
    assert cmd == ["/opt/homebrew/bin/gog", "auth", "add", "--", "me@example.com"]
    assert format_gog_command_string("me@example.com") == (
        "/opt/homebrew/bin/gog auth add -- me@example.com"
    )


def test_build_gog_auth_command_rejects_malformed_email() -> None:
    """Obviously-bad values fail loud before the shell-out (no `@`, empty,
    whitespace, missing dot in the domain half)."""
    for bad in ("", "no-at-sign", "spaces in@example.com", "user@nodot", "user@"):
        with pytest.raises(OnboardingError) as exc:
            build_gog_auth_command(bad)
        assert "email" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_profile_name_rejects_uppercase(workspace: Path) -> None:
    with pytest.raises(OnboardingError) as exc:
        validate_profile_name("Bad_Name", workspace)
    assert "Bad_Name" in str(exc.value)
    assert "kebab" in str(exc.value).lower()


def test_validate_profile_name_rejects_leading_dash(workspace: Path) -> None:
    with pytest.raises(OnboardingError) as exc:
        validate_profile_name("-flag", workspace)
    assert "-flag" in str(exc.value)


def test_validate_profile_name_rejects_reserved(workspace: Path) -> None:
    for reserved in sorted(RESERVED_PROFILE_NAMES):
        with pytest.raises(OnboardingError) as exc:
            validate_profile_name(reserved, workspace)
        assert reserved in str(exc.value)
        assert "reserved" in str(exc.value).lower()


def test_validate_profile_name_rejects_workspace_collision(
    workspace: Path,
) -> None:
    (workspace / "some-existing-file").write_text("x", encoding="utf-8")
    with pytest.raises(OnboardingError) as exc:
        validate_profile_name("some-existing-file", workspace)
    assert "some-existing-file" in str(exc.value)
    assert "workspace" in str(exc.value).lower()


def test_validate_profile_name_allows_good_name(workspace: Path) -> None:
    # No raise.
    validate_profile_name("gemini-scout", workspace)
    validate_profile_name("sam", workspace)
    validate_profile_name("a1", workspace)


def test_validate_persona_rejects_empty() -> None:
    with pytest.raises(OnboardingError):
        validate_persona("")
    with pytest.raises(OnboardingError):
        validate_persona("   ")


def test_validate_timezone_rejects_empty() -> None:
    with pytest.raises(OnboardingError):
        validate_timezone("")


def test_validate_new_human_rejects_bad_shape() -> None:
    with pytest.raises(OnboardingError):
        validate_new_human(NewHuman("bad handle", "X", 1))
    with pytest.raises(OnboardingError):
        validate_new_human(NewHuman("alice", "", 1))
    with pytest.raises(OnboardingError):
        # bool subclasses int; rejected explicitly.
        validate_new_human(NewHuman("alice", "Alice", True))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# create_profile: uniqueness (atomic), scaffold shape, rollback
# ---------------------------------------------------------------------------


def test_create_profile_bootstrap_flow_scaffolds_and_activates(
    workspace: Path,
) -> None:
    """Zero profiles + no humans.yaml → creates humans.yaml AND flips `current`."""
    spec = _bootstrap_spec()
    result = create_profile(spec)

    # Scaffold correctness.
    assert result.profile_root.exists()
    assert result.profile_yaml_path.exists()
    assert result.access_yaml_path.exists()
    for sub in ("memory", "briefs", "cache", "logs"):
        assert (result.profile_root / sub).is_dir()

    # humans.yaml appeared with the inline-added human.
    humans_yaml = workspace / "people.yaml"
    assert humans_yaml.exists()
    reg = load_humans_registry(path=humans_yaml)
    assert "alice" in reg
    assert reg.get("alice").telegram_id == 42
    assert result.humans_yaml_written is True

    # Bootstrap auto-activated.
    assert result.activated is True
    current = current_symlink_path(workspace)
    assert os.path.islink(current)
    assert Path(os.readlink(current)).name == "alice"

    # profile.yaml round-trips through the loader.
    loaded = load_active_profile("alice", base_dir=workspace / "profiles")
    assert loaded.name == "alice"
    assert loaded.assistant_name == "Alice"
    assert loaded.journal_apple_notes_folder == DEFAULT_JOURNAL_APPLE_NOTES_FOLDER
    assert loaded.secrets_backends == list(DEFAULT_SECRETS_BACKENDS)
    assert loaded.secrets_env_prefix == "ALICE_SECRET_"

    # access.yaml validates against the humans registry (owner-only).
    access = load_access_config(loaded, reg)
    assert access.owner == "alice"
    assert len(access.authorized) == 1
    assert access.authorized[0].tier == AccessTier.OWNER


def test_create_profile_collision_allocates_hash_suffix(
    workspace_with_sam: Path,
) -> None:
    """A second `create_profile(name='sam')` no longer fails — the
    reservation policy allocates a 6-hex suffix so many agents can share
    the same requested name (and persona)."""
    spec = _bare_spec("sam")
    result = create_profile(spec)

    # Suffixed name matches `<name>-<6hex>`; original seed dir is untouched.
    assert re.fullmatch(r"sam-[0-9a-f]{6}", result.profile_name), (
        f"expected suffixed name, got {result.profile_name!r}"
    )
    assert result.profile_root.exists()
    assert result.profile_root.name == result.profile_name

    # The new profile.yaml carries the SUFFIXED name (and matching keychain);
    # the persona / display_name reflect the original request.
    body = yaml.safe_load(result.profile_yaml_path.read_text(encoding="utf-8"))
    assert body["name"] == result.profile_name
    assert body["display_name"] == spec.persona
    assert body["assistant_name"] == spec.persona
    assert body["keychain_account"] == result.profile_name

    # Original seed profile dir at profiles/sam is left completely alone.
    original = workspace_with_sam / "profiles" / "sam"
    assert original.is_dir()
    original_body = yaml.safe_load(
        (original / "profile.yaml").read_text(encoding="utf-8")
    )
    assert original_body["name"] == "sam"


def test_create_profile_collision_engages_on_the_atomic_syscall_not_a_pre_check(
    workspace_with_sam: Path,
) -> None:
    """Simulate a racer: intercept only the BARE `profiles/brand-new`
    reservation and force `FileExistsError`, exactly the behavior a
    concurrent create would produce between our validation and our
    scaffold. The suffixed retry must engage off the atomic syscall
    (not a pre-check), leaving us with a suffixed profile dir instead
    of an error.
    """
    spec = _bare_spec("brand-new")
    real_makedirs = os.makedirs

    def racy_makedirs(path, *args, **kwargs):
        # Only intercept the bare profile-dir create — let the suffixed
        # retries land normally through the real syscall.
        if kwargs.get("exist_ok") is False and str(path).endswith(
            "/profiles/brand-new"
        ):
            raise FileExistsError(path)
        return real_makedirs(path, *args, **kwargs)

    with patch("mineru_cli.profile.onboarding.os.makedirs", side_effect=racy_makedirs):
        result = create_profile(spec)

    # Bare name was never created (patched to raise); a suffixed dir landed.
    assert not (workspace_with_sam / "profiles" / "brand-new").exists()
    assert re.fullmatch(r"brand-new-[0-9a-f]{6}", result.profile_name), (
        f"expected suffixed name, got {result.profile_name!r}"
    )
    assert result.profile_root.exists()


def test_create_profile_collision_exhaustion_fails_loud_and_rolls_back(
    workspace_with_sam: Path,
) -> None:
    """When EVERY reservation attempt loses the race, `create_profile`
    raises `OnboardingError` (not `FileExistsError`) and leaves no stray
    profile dir behind."""
    spec = _bare_spec("cursed")

    def always_taken(path, *args, **kwargs):
        # Reject any exist_ok=False call — this is the reservation
        # syscall. `exist_ok=True` calls (parent-dir mkdirs, scaffold
        # subdirs) pass through so the setup around the reservation
        # loop still works.
        if kwargs.get("exist_ok") is False:
            raise FileExistsError(path)
        # Ignore exist_ok kwarg on the passthrough — real behavior is
        # idempotent for the parent-dir case.
        return None

    with patch("mineru_cli.profile.onboarding.os.makedirs", side_effect=always_taken):
        with pytest.raises(OnboardingError) as exc:
            create_profile(spec)

    assert "could not reserve" in str(exc.value).lower()
    # No candidate dir ever landed on disk.
    profiles_dir = workspace_with_sam / "profiles"
    assert not any(
        p.name.startswith("cursed") for p in profiles_dir.iterdir()
    ), sorted(p.name for p in profiles_dir.iterdir())


def test_collision_suffixed_profile_round_trips_through_loader(
    workspace_with_sam: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a suffixed reservation lands, `load_active_profile(<suffixed>)`
    returns a Profile whose name-derived namespacing (launchd label prefix,
    secrets env prefix) is built from the SUFFIXED name — not the requested
    one. This is the invariant that keeps two profiles named after the same
    persona from colliding on Keychain / launchd fleets.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace_with_sam))

    spec = _bare_spec("sam")
    result = create_profile(spec)
    suffixed = result.profile_name
    assert re.fullmatch(r"sam-[0-9a-f]{6}", suffixed)

    profile = load_active_profile(suffixed)
    assert profile.name == suffixed
    assert profile.launchd_label_prefix == f"com.{suffixed}"
    assert profile.secrets_env_prefix == default_env_prefix_from_name(suffixed)
    # Persona / display_name unchanged — the suffix is a namespacing
    # concern, not a persona rename.
    assert profile.assistant_name == spec.persona
    assert profile.display_name == spec.persona


def test_create_profile_no_collision_keeps_bare_name(
    workspace: Path,
) -> None:
    """A fresh name (no existing dir) reserves the bare basename with no
    suffix — collision-only policy leaves greenfield names alone."""
    # Bootstrap: no humans.yaml yet, so use the inline-owner spec.
    spec = _bootstrap_spec(name="greenfield", handle="green", tg_id=101)
    result = create_profile(spec)
    assert result.profile_name == "greenfield"
    assert result.profile_root.name == "greenfield"


def test_profile_dir_suffix_policy_always_on(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping `PROFILE_DIR_SUFFIX_ONLY_ON_COLLISION` off makes EVERY new
    profile dir carry a 6-hex suffix — locks the single-policy-constant
    design so future edits keep both branches in one place."""
    monkeypatch.setattr(
        "mineru_cli.profile.onboarding.PROFILE_DIR_SUFFIX_ONLY_ON_COLLISION",
        False,
    )
    spec = _bootstrap_spec(name="fresh", handle="freshie", tg_id=202)
    result = create_profile(spec)
    hex_chars = PROFILE_DIR_SUFFIX_HEX_LEN
    assert re.fullmatch(rf"fresh-[0-9a-f]{{{hex_chars}}}", result.profile_name), (
        f"expected always-on suffix, got {result.profile_name!r}"
    )


def test_create_profile_rolls_back_partial_dir_on_mid_scaffold_failure(
    workspace_with_sam: Path,
) -> None:
    """Inject failure AFTER files are written; assert dir is cleaned up."""
    spec = _bare_spec("half-built")
    called: List[Path] = []

    def blow_up(profile_root: Path) -> None:
        called.append(profile_root)
        # Sanity: files exist mid-scaffold before we blow up.
        assert profile_root.exists()
        assert (profile_root / "profile.yaml").exists()
        raise RuntimeError("simulated mid-scaffold failure")

    with pytest.raises(RuntimeError, match="simulated"):
        create_profile(spec, fail_after_scaffold_for_test=blow_up)

    # The hook fired, and the entire dir was rolled back.
    assert called == [workspace_with_sam / "profiles" / "half-built"]
    assert not (workspace_with_sam / "profiles" / "half-built").exists()
    # humans.yaml was NOT touched by this call.
    original = (
        "humans:\n"
        "  sam:\n"
        "    telegram_id: 111\n"
        "    display_name: \"Sam Rivera\"\n"
    )
    assert (workspace_with_sam / "people.yaml").read_text() == original


def test_create_profile_rolls_back_dir_but_keeps_humans_when_switch_fails(
    workspace: Path,
) -> None:
    """Bootstrap flow with an inline-added human: if `switch_active_profile`
    fails AFTER humans.yaml was already committed, the profile dir rolls
    back but humans.yaml is intentionally LEFT in place (the added human
    is thin and harmless; re-running init with the same handle will find
    them). Locks the "humans committed then switch fails" branch that
    lived unwritten before this test."""
    spec = _bootstrap_spec(name="switchfail", handle="stan", tg_id=303)

    def boom(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("simulated activation failure")

    # Bootstrap path unconditionally activates, so a failure inside
    # switch_active_profile fires the caller's rollback branch. Patch
    # AFTER humans.yaml commits (rollback preserves humans by design).
    humans_yaml = workspace / "people.yaml"
    with patch(
        "mineru_cli.profile.onboarding.switch_active_profile",
        side_effect=boom,
    ):
        with pytest.raises(RuntimeError, match="simulated activation failure"):
            create_profile(spec)

    # Profile dir was rolled back.
    assert not (workspace / "profiles" / "switchfail").exists()
    # humans.yaml survived: `stan` is present so a retry finds them.
    assert humans_yaml.exists()
    reg = load_humans_registry(path=humans_yaml)
    assert "stan" in reg
    assert reg.get("stan").telegram_id == 303
    # `current` symlink never got wired up.
    assert not (workspace / CURRENT_SYMLINK_NAME).exists()


def test_create_profile_unknown_owner_rejected(workspace: Path) -> None:
    # No humans.yaml at all → owner cannot exist.
    spec = _bare_spec("neo", owner="ghost")
    with pytest.raises(OnboardingError) as exc:
        create_profile(spec)
    assert "ghost" in str(exc.value)
    assert "not found" in str(exc.value).lower()


def test_create_profile_inline_add_rejects_existing_handle(
    workspace_with_sam: Path,
) -> None:
    """Cannot inline-add `sam` when humans.yaml already has `sam`."""
    spec = ProfileSpec(
        name="conflict",
        persona="Conflict",
        owner_handle="sam",
        timezone="America/Los_Angeles",
        new_human=NewHuman("sam", "Sam Rivera", 111),
    )
    with pytest.raises(OnboardingError) as exc:
        create_profile(spec)
    assert "already registered" in str(exc.value).lower()
    # Rollback: no partial profile dir.
    assert not (workspace_with_sam / "profiles" / "conflict").exists()


def test_create_profile_no_auto_switch_when_prior_profile_exists(
    workspace_with_sam: Path,
) -> None:
    """A non-bootstrap init leaves `current` unchanged unless activate=True."""
    before = os.readlink(workspace_with_sam / CURRENT_SYMLINK_NAME)
    spec = _bare_spec("second")
    result = create_profile(spec)
    assert result.activated is False
    after = os.readlink(workspace_with_sam / CURRENT_SYMLINK_NAME)
    assert before == after


def test_create_profile_activate_flag_flips_current_symlink(
    workspace_with_sam: Path,
) -> None:
    spec = _bare_spec("second")
    result = create_profile(spec, activate=True)
    assert result.activated is True
    target = Path(
        os.readlink(workspace_with_sam / CURRENT_SYMLINK_NAME)
    ).name
    assert target == "second"


def test_create_profile_writes_google_account_field(
    workspace_with_sam: Path,
) -> None:
    spec = ProfileSpec(
        name="ga-profile",
        persona="GAProfile",
        owner_handle="sam",
        timezone="America/Los_Angeles",
        google_account="assistant@example.com",
    )
    result = create_profile(spec)
    data = yaml.safe_load(result.profile_yaml_path.read_text())
    assert data["google_account"] == "assistant@example.com"
    # Round-trips through the loader onto the first-class Profile field
    # (`google_account` was promoted from `extras` to a top-level dataclass
    # attribute so `run_gog_firewall(ctx=ctx)` can inject `--account=`
    # per profile without an `extras`-shape lookup at every call site).
    loaded = load_active_profile(
        "ga-profile", base_dir=workspace_with_sam / "profiles"
    )
    assert loaded.google_account == "assistant@example.com"
    # And it must NOT also land in `extras` — one canonical home per field.
    assert "google_account" not in loaded.extras


# ---------------------------------------------------------------------------
# check_persona_collision
# ---------------------------------------------------------------------------


def test_check_persona_collision_finds_duplicate(
    workspace_with_sam: Path,
) -> None:
    hits = check_persona_collision(
        "Mineru", workspace_with_sam / "profiles"
    )
    assert "sam" in hits


def test_check_persona_collision_empty_on_no_matches(
    workspace_with_sam: Path,
) -> None:
    assert (
        check_persona_collision(
            "Nobody-Uses-This", workspace_with_sam / "profiles"
        )
        == []
    )


# ---------------------------------------------------------------------------
# CLI: mineru profile init
# ---------------------------------------------------------------------------


def test_cli_init_help_renders_on_empty_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mineru profile init --help` MUST render on a fresh workspace
    root with no humans.yaml + no `current` symlink."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["profile", "init", "--help"])
    assert result.exit_code == 0, result.stderr
    for token in (
        "--name",
        "--persona",
        "--owner",
        "--owner-new-handle",
        "--timezone",
        "--google-account",
        "--activate",
        "--skip-integrations",
        "--no-input",
    ):
        assert token in result.stdout, f"missing flag {token!r}"


def test_cli_init_no_input_missing_name_fails(workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--no-input",
            "--persona", "X",
            "--owner", "someone",
        ],
    )
    assert result.exit_code == 2
    assert "--name" in result.stderr


def test_cli_init_no_input_missing_owner_fails(workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--no-input",
            "--name", "solo",
            "--persona", "Solo",
        ],
    )
    assert result.exit_code == 2
    combined = (result.stderr or "") + (result.stdout or "")
    assert "owner" in combined.lower()


def test_cli_init_full_flags_bootstrap_scaffolds_end_to_end(
    workspace: Path,
) -> None:
    """Fully scripted bootstrap: no prompts, no integrations,
    inline-add the owner human."""
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--name", "gemini-scout",
            "--persona", "Gemini Scout",
            "--owner-new-handle", "vic",
            "--owner-new-display", "Vic Rivera",
            "--owner-new-telegram", "999",
            "--timezone", "America/Los_Angeles",
            "--no-input",
            "--skip-integrations",
        ],
    )
    assert result.exit_code == 0, result.stderr

    profile_root = workspace / "profiles" / "gemini-scout"
    assert profile_root.is_dir()
    assert (profile_root / "profile.yaml").exists()
    assert (profile_root / "access.yaml").exists()
    assert (profile_root / "cron.yaml").exists()
    for sub in ("memory", "briefs", "cache", "logs"):
        assert (profile_root / sub).is_dir()

    reg = load_humans_registry(path=workspace / "people.yaml")
    assert "vic" in reg
    assert reg.get("vic").telegram_id == 999

    # Bootstrap auto-activated: the `active` symlink now exists.
    assert os.path.islink(workspace / ACTIVE_SYMLINK_NAME)


def test_cli_init_duplicate_name_allocates_hash_suffix(
    workspace_with_sam: Path,
) -> None:
    """Second init with the same requested name succeeds with a suffixed
    dir; the summary + stderr surface the actual created name so the
    operator sees the collision was resolved (not swallowed)."""
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--name", "sam",
            "--persona", "X",
            "--owner", "sam",
            "--no-input",
            "--skip-integrations",
        ],
    )
    assert result.exit_code == 0, result.output
    # The suffixed name (`sam-<6hex>`) must appear in the summary.
    assert re.search(r"sam-[0-9a-f]{6}", result.output), result.output
    # The collision note is emitted to stderr and names both requested + final.
    assert "'sam' was taken" in result.stderr
    assert "created 'sam-" in result.stderr
    # Both the pre-existing sam profile AND the new suffixed profile
    # exist. Locks the "duplicate init preserves the pre-existing profile"
    # invariant: a rebrand-era regression that silently overwrote the
    # original would fail this test.
    profiles_dir = workspace_with_sam / "profiles"
    suffixed = [
        p.name for p in profiles_dir.iterdir()
        if p.is_dir() and re.fullmatch(r"sam-[0-9a-f]{6}", p.name)
    ]
    assert len(suffixed) == 1, sorted(p.name for p in profiles_dir.iterdir())
    original = profiles_dir / "sam"
    assert original.is_dir()
    original_body = yaml.safe_load(
        (original / "profile.yaml").read_text(encoding="utf-8")
    )
    assert original_body["name"] == "sam"


def test_cli_init_reserved_name_rejected(workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--name", "current",
            "--persona", "X",
            "--owner", "someone",
            "--no-input",
            "--skip-integrations",
        ],
    )
    assert result.exit_code == 2
    assert "reserved" in result.stderr.lower()


def test_cli_init_owner_new_partial_flags_rejected(workspace: Path) -> None:
    """`--owner-new-handle` without display+telegram fails cleanly."""
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--name", "partial",
            "--persona", "P",
            "--owner-new-handle", "alice",
            "--no-input",
            "--skip-integrations",
        ],
    )
    assert result.exit_code == 2
    assert "must all be provided together" in result.stderr.lower()
    # Rollback: no dir was created (validation failed before the atomic
    # scaffold gate).
    assert not (workspace / "profiles" / "partial").exists()


def test_cli_init_owner_and_owner_new_mutually_exclusive(
    workspace_with_sam: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--name", "confused",
            "--persona", "X",
            "--owner", "sam",
            "--owner-new-handle", "alice",
            "--owner-new-display", "Alice",
            "--owner-new-telegram", "1",
            "--no-input",
            "--skip-integrations",
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr.lower()


def test_cli_init_google_account_walkthrough_mocks_gog(
    workspace_with_sam: Path,
) -> None:
    """--google-account is stored on the profile; walkthrough is offered.

    We fake a TTY (CliRunner's stdin is normally not a TTY) so the
    interactive branch fires, drive the confirm prompt with `input=`,
    and mock `subprocess.run` so no real `gog` invocation happens.
    """
    with patch("mineru_cli.verbs.profile._stdin_is_tty", return_value=True), \
         patch("mineru_cli.profile.onboarding.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        # Every non-defaulted required field is supplied as a flag so
        # the only remaining prompt is the Google-walkthrough confirm.
        result = runner.invoke(
            app,
            [
                "profile", "init",
                "--name", "ga",
                "--persona", "GA",
                "--owner", "sam",
                "--timezone", "America/Los_Angeles",
                "--google-account", "ga@example.com",
                # Deliberately NOT --no-input so the walkthrough prompt fires.
            ],
            input="y\n",  # confirm the walkthrough
        )
    assert result.exit_code == 0, result.stderr

    # Profile carries the google_account field.
    data = yaml.safe_load(
        (workspace_with_sam / "profiles" / "ga" / "profile.yaml").read_text()
    )
    assert data["google_account"] == "ga@example.com"

    # Mocked subprocess was invoked with the verified command shape.
    mock_run.assert_called_once()
    call_args = mock_run.call_args.args[0]
    assert call_args == build_gog_auth_command("ga@example.com")


def test_cli_init_google_walkthrough_deferred_when_declined(
    workspace_with_sam: Path,
) -> None:
    """Declining the interactive walkthrough prints the ready-to-copy command."""
    with patch("mineru_cli.verbs.profile._stdin_is_tty", return_value=True), \
         patch("mineru_cli.profile.onboarding.subprocess.run") as mock_run:
        result = runner.invoke(
            app,
            [
                "profile", "init",
                "--name", "gd",
                "--persona", "GD",
                "--owner", "sam",
                "--timezone", "America/Los_Angeles",
                "--google-account", "gd@example.com",
            ],
            input="n\n",
        )
    assert result.exit_code == 0, result.stderr
    mock_run.assert_not_called()
    assert format_gog_command_string("gd@example.com") in result.stdout


def test_cli_init_google_walkthrough_deferred_when_non_tty(
    workspace_with_sam: Path,
) -> None:
    """Non-TTY caller with --google-account still records the field and
    defers the walkthrough with a printed command (never blocks on a
    prompt)."""
    with patch(
        "mineru_cli.profile.onboarding.subprocess.run"
    ) as mock_run:
        result = runner.invoke(
            app,
            [
                "profile", "init",
                "--name", "gnt",
                "--persona", "GNT",
                "--owner", "sam",
                "--google-account", "gnt@example.com",
                "--no-input",
            ],
        )
    assert result.exit_code == 0, result.stderr
    mock_run.assert_not_called()
    assert format_gog_command_string("gnt@example.com") in result.stdout


def test_cli_init_skip_integrations_never_prompts_for_google(
    workspace_with_sam: Path,
) -> None:
    """With --skip-integrations, the Google walkthrough is never offered."""
    with patch(
        "mineru_cli.profile.onboarding.subprocess.run"
    ) as mock_run:
        result = runner.invoke(
            app,
            [
                "profile", "init",
                "--name", "sk",
                "--persona", "SK",
                "--owner", "sam",
                "--google-account", "sk@example.com",
                "--skip-integrations",
                "--no-input",
            ],
        )
    assert result.exit_code == 0, result.stderr
    mock_run.assert_not_called()
    # But the google_account field is still recorded.
    data = yaml.safe_load(
        (workspace_with_sam / "profiles" / "sk" / "profile.yaml").read_text()
    )
    assert data["google_account"] == "sk@example.com"


def test_cli_init_summary_names_created_files(
    workspace_with_sam: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--name", "sum-profile",
            "--persona", "SumBot",
            "--owner", "sam",
            "--no-input",
            "--skip-integrations",
        ],
    )
    assert result.exit_code == 0, result.stderr
    for expected in ("profile.yaml", "access.yaml", "memory/", "briefs/", "sum-profile"):
        assert expected in result.stdout


# ---------------------------------------------------------------------------
# Direct API smoke: run_gog_auth_add
# ---------------------------------------------------------------------------


def test_run_gog_auth_add_returns_subprocess_exit_code() -> None:
    fake = MagicMock(returncode=7)
    calls: List[List[str]] = []

    def runner_fn(cmd):
        calls.append(cmd)
        return fake

    rc = run_gog_auth_add("me@example.com", runner=runner_fn)
    assert rc == 7
    assert calls == [build_gog_auth_command("me@example.com")]


# ---------------------------------------------------------------------------
# Presentation-layer degradation (TTY-aware polish)
# ---------------------------------------------------------------------------


def test_cli_init_no_input_output_has_no_ansi_escapes(
    workspace_with_sam: Path,
) -> None:
    """Scripted (--no-input) output must be pure ASCII / no ANSI cruft.

    Guards the "TTY-AWARE degrade cleanly" contract: pipe / --no-input /
    non-TTY = plain and parseable, ready for `awk` / `grep` / a launchd
    log tail. A regression that leaks a raw `\\x1b[...m` sequence into
    scripted output would fire this test.
    """
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--name", "plain",
            "--persona", "PlainBot",
            "--owner", "sam",
            "--no-input",
            "--skip-integrations",
        ],
    )
    assert result.exit_code == 0, result.stderr
    # No CSI escape sequence in either stream.
    assert "\x1b[" not in result.stdout
    assert "\x1b[" not in result.stderr
    # Sanity: the summary content survives the polish (frame + key rows).
    for expected in ("created profile: plain", "profile.yaml", "memory/"):
        assert expected in result.stdout


def test_cli_init_error_frame_no_ansi_in_scripted_mode(
    workspace: Path,
) -> None:
    """Styled `[ERROR]` frame degrades to plain `[ERROR] <message>` when
    stdout is not a TTY."""
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--name", "Bad_Name",
            "--persona", "X",
            "--owner", "someone",
            "--no-input",
            "--skip-integrations",
        ],
    )
    assert result.exit_code == 2
    # The bracketed `[ERROR]` marker is always present (never relies on
    # color alone — accessibility rule).
    assert "[ERROR]" in result.stderr
    # No ANSI escape in the scripted error frame.
    assert "\x1b[" not in result.stderr


def test_cli_init_summary_names_next_steps(
    workspace_with_sam: Path,
) -> None:
    """The final `Next steps:` block includes copy-pasteable commands
    (cli-ux-patterns §9 done screens)."""
    result = runner.invoke(
        app,
        [
            "profile", "init",
            "--name", "next-steps-check",
            "--persona", "NBot",
            "--owner", "sam",
            "--no-input",
            "--skip-integrations",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert "Next steps:" in result.stdout
    assert "mineru profile show --profile next-steps-check" in result.stdout
    assert "mineru access show --profile next-steps-check" in result.stdout
    # Since we did NOT --activate, and workspace_with_sam has an
    # existing sam profile, the summary should point at `profile use`.
    assert "mineru profile use next-steps-check" in result.stdout


def test_run_gog_auth_add_default_uses_patched_subprocess_run() -> None:
    """When `runner` is omitted, subprocess.run is resolved at call time so
    patches on `mineru_cli.profile.onboarding.subprocess.run` take effect.

    Regression: an earlier `runner=subprocess.run` default-arg pattern
    captured the real subprocess.run at import time, defeating the patch
    and opening a real OAuth browser during CI.
    """
    with patch(
        "mineru_cli.profile.onboarding.subprocess.run"
    ) as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        rc = run_gog_auth_add("me@example.com")
    assert rc == 0
    mock_run.assert_called_once_with(
        build_gog_auth_command("me@example.com")
    )


# ---------------------------------------------------------------------------
# Connectors starter (Sep 2026 gap-close)
# ---------------------------------------------------------------------------
#
# The Sep-16 2026 CLI naming-consolidation audit (§F2) flagged that
# `profile init` scaffolded `profile.yaml`, `access.yaml`, `cron.yaml` but
# NOT `connectors.yaml`, so every fresh `install --apply` died on the
# first template connector reference with a `_die` message pointing at a
# file that did not exist. `_write_scaffold_files` now writes a starter
# `connectors.yaml` (see `_render_connectors_yaml_starter`) with every
# required UPPERCASE key present and each value a `REPLACE_ME__*`
# placeholder. These tests pin the contract: file present, contains the
# required keys, is loadable YAML, and each value carries the sentinel
# token so `mineru setup` (and any future audit) can spot unedited
# placeholders.


# Every UPPERCASE connector key an engine template hard-references
# (see `grep -rhoE '\{\{[A-Z_]+\}\}' engine/` + spec §2.5). A new key
# added to the shipped engine tree AND missing from the scaffold breaks
# `install --apply` on a fresh profile, so pinning the required set
# here is the load-bearing check.
REQUIRED_CONNECTOR_KEYS = frozenset(
    {
        "USER_PRIMARY_EMAIL",
        "PERSONAL_CALENDAR_ID",
        "FAMILY_CALENDAR_ID",
        "CHURCH_CALENDAR_ID",
        "TAILSCALE_HOSTNAME",
        "TELEGRAM_BOT_SERVICE",
        "DAEMON_PERSONA_NAME",
        "CHURCH_NAME",
        "FINANCE_CLI_PRODUCT",
        "FINANCE_CLI_BINARY_NAME",
        "FINANCE_CLI_SUBCOMMANDS",
    }
)


def test_create_profile_scaffolds_connectors_yaml(workspace: Path) -> None:
    """The scaffold writes `<profile_root>/connectors.yaml` with the
    required UPPERCASE keys, and `ScaffoldResult` surfaces the path.

    Reasserts the connectors-scaffold gap flagged in §F2 of the Sep-16
    2026 audit is closed: absence of this file was the shipped-verb
    error every fresh `install --apply` hit before this change.
    """
    result = create_profile(_bootstrap_spec())
    assert result.connectors_yaml_path is not None, (
        "ScaffoldResult.connectors_yaml_path must be populated"
    )
    assert result.connectors_yaml_path.exists()
    assert result.connectors_yaml_path == result.profile_root / "connectors.yaml"

    body = yaml.safe_load(result.connectors_yaml_path.read_text(encoding="utf-8"))
    assert isinstance(body, dict), (
        "connectors.yaml must be a YAML mapping at the top level "
        "(the install loader hard-rejects any other shape)."
    )
    missing = REQUIRED_CONNECTOR_KEYS - body.keys()
    assert not missing, (
        f"scaffold missing required connector keys {missing}; a fresh "
        "`mineru profile install --apply` will bomb on the first "
        "connector reference in an engine template."
    )


def test_scaffolded_connectors_values_carry_placeholder_sentinel(
    workspace: Path,
) -> None:
    """Every scalar in the scaffolded file starts with the placeholder
    sentinel so `mineru setup` (or an audit) can grep for unedited
    values after an install."""
    result = create_profile(_bootstrap_spec())
    assert result.connectors_yaml_path is not None
    body = yaml.safe_load(result.connectors_yaml_path.read_text(encoding="utf-8"))

    # Scalar values (calendar IDs, service names, hostnames) all carry
    # the sentinel. List-shaped values (FINANCE_CLI_SUBCOMMANDS) contain
    # dicts whose `cmd:` value carries it — everything else in the list
    # entry (e.g. `comment:`) is prose and doesn't need to.
    for key, value in body.items():
        if isinstance(value, str):
            assert CONNECTORS_PLACEHOLDER_TOKEN in value, (
                f"scalar {key!r} should carry the "
                f"{CONNECTORS_PLACEHOLDER_TOKEN!r} sentinel "
                "(so `mineru setup` can detect unedited placeholders)."
            )
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict) and "cmd" in entry:
                    assert CONNECTORS_PLACEHOLDER_TOKEN in entry["cmd"]


def test_scaffolded_connectors_is_valid_yaml_mapping(workspace: Path) -> None:
    """The scaffold parses as YAML AND passes the install loader's
    top-level-mapping check (dict, not a scalar/list/None).

    Regression guard against a docstring-only "template" that starts
    with `#` comments only and parses to `None`, which the install
    loader tolerates but which would mask a broken scaffold.
    """
    result = create_profile(_bootstrap_spec())
    assert result.connectors_yaml_path is not None
    body = yaml.safe_load(result.connectors_yaml_path.read_text(encoding="utf-8"))
    assert isinstance(body, dict) and body, (
        "scaffolded connectors.yaml must parse to a non-empty dict"
    )


def test_scaffolded_connectors_satisfies_shipped_install_loader(
    workspace: Path,
) -> None:
    """Round-trip through the real install-verb loader: a freshly
    scaffolded profile passes `_load_profile_connectors` (used by
    `profile install`) without tripping the `_die` for absent /
    malformed / non-mapping files.
    """
    from mineru_cli.verbs.profile import _load_profile_connectors

    result = create_profile(_bootstrap_spec())
    connectors = _load_profile_connectors(result.profile_root)
    assert isinstance(connectors, dict) and connectors
    assert REQUIRED_CONNECTOR_KEYS.issubset(connectors.keys())
