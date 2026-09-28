"""F7 tests for the single-active-profile loader.

Focused on the F7 done-criteria contracts (a broader suite lives in
`test_profile.py`; this file exists to make the F7 claims individually
grep-visible):

  - `--profile <name>` overrides everything.
  - `MINERU_PROFILE` overrides the default.
  - Default profile name is `mineru` (the shipped seed profile).
  - `mineru --profile bogus profile show` exits non-zero and the error
    message names the missing `profile.yaml` path so the user knows
    exactly which file to create.
  - `load_active_profile` populates every required field from a valid
    `profile.yaml` and rejects a missing required field by name.
  - The loader never touches `$MINERU_HOME`: the default base is
    worktree-scoped.

Discipline:
  - Uses `MINERU_PROFILE_ROOT` (test-only override) + tmp_path to
    build ephemeral profile trees. No test writes under `$MINERU_HOME`.
  - Uses `typer.testing.CliRunner` for the CLI end-to-end assertions.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.profile import (
    Profile,
    ProfileError,
    default_profiles_base_dir,
    load_active_profile,
    resolve_profile_name,
)
from mineru_cli.profile.loader import (
    CURRENT_SYMLINK_NAME,
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)


runner = CliRunner()


# --- Helpers ---------------------------------------------------------------


def _write_valid_profile_yaml(base: Path, name: str) -> Path:
    """Materialize a schema-valid `<base>/<name>/profile.yaml` and return its path."""
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_yaml = profile_dir / "profile.yaml"
    # `assistant_name: TestBot` (NOT the seed profile's `Mineru`) so this
    # fixture proves the field is genuinely parameterized. A hardcoded
    # `"Mineru"` here would mask a regression that pinned the persona name.
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
        "  backends:\n"
        "    - env\n"
        "    - keychain\n"
        f"  env_prefix: {name.upper()}_SECRET_\n"
    )
    profile_yaml.write_text(body, encoding="utf-8")
    return profile_yaml


# --- Name resolution (--profile > MINERU_PROFILE > default) ---------------


def test_resolve_profile_name_explicit_flag_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--profile foo` beats `MINERU_PROFILE=bar` beats the default."""
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "from-env")
    assert resolve_profile_name("from-flag") == "from-flag"


def test_resolve_profile_name_env_beats_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "from-env")
    assert resolve_profile_name(None) == "from-env"


def test_resolve_profile_name_no_default_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Phase 1: no flag + no env + no `current` symlink -> fail loud.

    The old silent default is gone; a machine with no active
    profile MUST say so.
    """
    monkeypatch.delenv(PROFILE_NAME_ENV_VAR, raising=False)
    monkeypatch.delenv(PROFILE_BASE_DIR_ENV_VAR, raising=False)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    with pytest.raises(ProfileError) as exc:
        resolve_profile_name(None)
    assert "no active profile" in str(exc.value)
    assert CURRENT_SYMLINK_NAME in str(exc.value)


def test_resolve_profile_name_empty_flag_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--profile ''` should not shadow the env / symlink resolution."""
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "from-env")
    assert resolve_profile_name("") == "from-env"


# --- Default base directory (worktree-scoped, never $MINERU_HOME) ------------


def test_default_profiles_base_dir_defaults_under_mineru_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Without an override env, the default base is `<MINERU_HOME>/profiles`.

    Genericized replacement for the old worktree-scoped default: every
    user's profiles base derives from their own MINERU_HOME seam.
    """
    monkeypatch.delenv(PROFILE_BASE_DIR_ENV_VAR, raising=False)
    monkeypatch.delenv(WORKSPACE_ROOT_ENV_VAR, raising=False)
    monkeypatch.setenv("MINERU_HOME", str(tmp_path))
    assert default_profiles_base_dir() == tmp_path / "profiles"


def test_default_profiles_base_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    assert default_profiles_base_dir() == tmp_path.resolve()


# --- load_active_profile happy path ---------------------------------------


def test_load_active_profile_populates_foundation_fields(
    tmp_path: Path,
) -> None:
    _write_valid_profile_yaml(tmp_path, "alice")
    profile = load_active_profile("alice", base_dir=tmp_path)
    assert isinstance(profile, Profile)
    assert profile.name == "alice"
    assert profile.display_name == "Alice"
    # Asserts against the fixture value, NOT the seed profile's "Mineru" —
    # the point is to prove `assistant_name` is a genuinely parameterized field.
    assert profile.assistant_name == "TestBot"
    assert profile.timezone == "America/Los_Angeles"
    assert profile.keychain_account == "alice-acct"
    assert profile.launchd_label_prefix == "com.alice"
    # Paths are resolved to absolute (tmp/ may resolve to /private/tmp on macOS).
    assert profile.workspace_absolute.is_absolute()
    assert profile.memory_root.is_absolute()
    assert profile.briefs_root.is_absolute()
    assert profile.journal_apple_notes_folder == "Daily Journals"
    assert profile.secrets_backends == ["env", "keychain"]
    assert profile.secrets_env_prefix == "ALICE_SECRET_"
    assert profile.profile_yaml_path == (tmp_path / "alice" / "profile.yaml")


def test_load_active_profile_resolves_via_current_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A `current -> profiles/<name>` symlink at the workspace root drives
    flagless resolution (no silent default).

    Self-contained in tmp since the engine repo ships no live `profiles/`
    tree; still proves resolution goes through the symlink, not a default.
    """
    monkeypatch.delenv(PROFILE_BASE_DIR_ENV_VAR, raising=False)
    monkeypatch.delenv(PROFILE_NAME_ENV_VAR, raising=False)
    ws = tmp_path.resolve()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(ws))
    (ws / "profiles").mkdir()
    _write_valid_profile_yaml(ws / "profiles", "mineru")
    os.symlink("profiles/mineru", ws / CURRENT_SYMLINK_NAME)
    profile = load_active_profile(None)
    assert profile.name == "mineru"
    assert profile.keychain_account == "mineru-acct"


# --- Fail-loud on missing profile.yaml -----------------------------------


def test_load_active_profile_missing_names_exact_path(tmp_path: Path) -> None:
    """`profile.yaml not found` message names the exact absolute path.

    That path is the fix-it hint — the user knows where to `touch` /
    which directory to `mkdir`. A vague error would waste their time.
    """
    with pytest.raises(ProfileError) as exc:
        load_active_profile("bogus", base_dir=tmp_path)
    msg = str(exc.value)
    expected_path = tmp_path / "bogus" / "profile.yaml"
    assert "bogus" in msg
    assert str(expected_path) in msg
    assert "not found" in msg


def test_cli_profile_show_bogus_profile_exits_nonzero_naming_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mineru --profile bogus profile show` exits non-zero + names the missing file.

    This is the F7 done-criterion for the profile loader: fail loud with
    a concrete pointer to the missing YAML.
    """
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["--profile", "bogus", "profile", "show"])
    assert result.exit_code != 0, (
        f"expected non-zero exit; got {result.exit_code}\n{result.output}"
    )
    output = result.output
    # The user-visible error must (a) mention the profile name, (b) name
    # profile.yaml, and (c) contain the missing-path phrasing.
    assert "bogus" in output
    assert "profile.yaml" in output
    assert "not found" in output
    # And ideally the exact absolute path (allow the /private/tmp macOS
    # prefix by matching on `<tmp_basename>/bogus/profile.yaml`).
    tail = str(Path(tmp_path.name) / "bogus" / "profile.yaml")
    assert tail in output or str(tmp_path / "bogus" / "profile.yaml") in output


def test_cli_bogus_env_profile_also_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MINERU_PROFILE=bogus` (no --profile flag) fails loud too."""
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "env-bogus")
    result = runner.invoke(app, ["profile", "show"])
    assert result.exit_code != 0
    assert "env-bogus" in result.output


# --- Fail-loud on missing required field ---------------------------------


@pytest.mark.parametrize(
    "missing_field",
    [
        "display_name",
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
    tmp_path: Path, missing_field: str,
) -> None:
    """Every foundation-required field is called out by NAME on absence.

    The loader must not silently accept a missing field and default it
    — the operator would ship with a wrong profile.
    """
    profile_yaml = _write_valid_profile_yaml(tmp_path, "alice")
    # Rewrite the yaml with one line removed.
    body = profile_yaml.read_text()
    new_body = re.sub(rf"^{missing_field}:.*\n", "", body, flags=re.MULTILINE)
    assert body != new_body, f"failed to strip {missing_field!r} from fixture"
    profile_yaml.write_text(new_body)

    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    assert missing_field in str(exc.value), (
        f"error message must name the missing field {missing_field!r}: "
        f"got {exc.value!s}"
    )


# --- CLI: happy-path `profile show` with --profile override --------------


def test_cli_profile_show_with_explicit_profile_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mineru --profile alice profile show` reads the alice profile."""
    _write_valid_profile_yaml(tmp_path, "alice")
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["--profile", "alice", "profile", "show"])
    assert result.exit_code == 0, result.output
    # Field names + values from the fixture must all appear.
    assert "alice" in result.stdout
    assert "alice-acct" in result.stdout
    assert "ALICE_SECRET_" in result.stdout


def test_cli_profile_show_json_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mineru --profile alice profile show --json` returns valid JSON."""
    import json

    _write_valid_profile_yaml(tmp_path, "alice")
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    result = runner.invoke(
        app, ["--profile", "alice", "profile", "show", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "alice"
    assert payload["secrets_backends"] == ["env", "keychain"]
    assert payload["secrets_env_prefix"] == "ALICE_SECRET_"


# --- Root-level flag propagation to loader (--profile is real) -----------


def test_cli_profile_flag_beats_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--profile alice` wins over `MINERU_PROFILE=bob`."""
    _write_valid_profile_yaml(tmp_path, "alice")
    _write_valid_profile_yaml(tmp_path, "bob")
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "bob")
    result = runner.invoke(app, ["--profile", "alice", "profile", "show"])
    assert result.exit_code == 0
    # `alice-acct` is alice's, `bob-acct` is bob's — verify alice wins.
    assert "alice-acct" in result.stdout
    assert "bob-acct" not in result.stdout


def test_cli_env_profile_wins_over_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MINERU_PROFILE=bob` wins over the shipped `mineru` seed when no flag is given."""
    _write_valid_profile_yaml(tmp_path, "bob")
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "bob")
    result = runner.invoke(app, ["profile", "show"])
    assert result.exit_code == 0
    assert "bob-acct" in result.stdout


# --- No writes under $MINERU_HOME (paranoid grep) ---------------------------


def test_loader_source_uses_mineru_home_seam_not_username_path() -> None:
    """The loader derives its default workspace root from the MINERU_HOME
    seam, never a hardcoded username'd absolute path.

    Genericized invariant: `os.environ.get("MINERU_HOME", ...)` is the
    correct default (and `Path.home() / ".mineru"` is its legitimate
    fallback, so that is NOT forbidden). What IS forbidden is a hardcoded
    `/Users/<name>` literal that leaks a username and breaks off-machine.
    """
    from mineru_cli.profile import loader as loader_module

    src = Path(loader_module.__file__).read_text()
    # No hardcoded real-user workspace path — anything shaped like
    # `/Users/<name>/.mineru` or `/home/<name>/.mineru` (concrete
    # username in a hard-coded absolute default) is a leak. The
    # regex uses a generic character class so this meta-test carries
    # no specific username of its own.
    forbidden_pattern = re.compile(r"/(Users|home)/[A-Za-z0-9._-]+/\.mineru")
    match = forbidden_pattern.search(src)
    assert match is None, (
        f"loader.py hardcodes a username'd workspace path: {match.group(0)!r}"
    )
    # The MINERU_HOME seam IS present (the correct default source): the env
    # name appears (as the `MINERU_HOME_ENV_VAR` constant) and is read via
    # `os.environ.get`.
    assert '"MINERU_HOME"' in src, (
        "loader.py must name the MINERU_HOME env seam for its default root"
    )
    assert "os.environ.get" in src, (
        "loader.py must read the MINERU_HOME env seam via os.environ.get"
    )


# --- launchd_label_prefix traversal guard (Fix 1b, Finding 12) -----------


@pytest.mark.parametrize(
    "bad_prefix",
    [
        # The literal audit example — traversal into ~/Library/LaunchAgents/.
        "../../../Library/LaunchAgents/com.evil",
        # Explicit path separators.
        "com/evil",
        "com\\evil",
        # Leading `..` (relative traversal shorthand).
        "..com.evil",
        # Leading dot (a hidden filename is not a valid launchd label).
        ".hidden",
        # Empty string / whitespace-only.
        "",
        "   ",
        # Shell metacharacters that must never enter a filename.
        "com.evil;rm -rf /",
        "com.evil$(whoami)",
        # Leading digit (spec-violating and pattern-rejected).
        "9com.evil",
    ],
)
def test_load_active_profile_rejects_traversal_in_launchd_label_prefix(
    tmp_path: Path, bad_prefix: str,
) -> None:
    """Cat-B critical (2026-09-04 step-5 audit, Finding 12).

    A hand-edited profile.yaml with a traversal/metacharacter payload in
    `launchd_label_prefix` must fail loud AT LOAD TIME, before any
    hydration walks the engine tree. The hydrator's dest-inside-target
    assertion is the last line of defense; this loader-side reject
    surfaces a clean, actionable "your profile is malformed" error
    instead of a plan-time traceback.
    """
    profile_yaml = _write_valid_profile_yaml(tmp_path, "alice")
    body = profile_yaml.read_text()
    # Rewrite ONLY the launchd_label_prefix line.
    new_body = re.sub(
        r"^launchd_label_prefix:.*\n",
        f"launchd_label_prefix: {bad_prefix!r}\n",
        body,
        flags=re.MULTILINE,
    )
    assert body != new_body, "failed to substitute launchd_label_prefix in fixture"
    profile_yaml.write_text(new_body)

    with pytest.raises(ProfileError) as exc:
        load_active_profile("alice", base_dir=tmp_path)
    msg = str(exc.value)
    assert "launchd_label_prefix" in msg, (
        f"loader must name the offending field: got {msg!r}"
    )


def test_load_active_profile_accepts_default_com_prefix(tmp_path: Path) -> None:
    """The onboarding-generated `com.<name>` prefix matches the safe pattern."""
    profile_yaml = _write_valid_profile_yaml(tmp_path, "installtest")
    body = profile_yaml.read_text()
    # Sanity-check: fixture is using the onboarding-shaped default.
    assert "launchd_label_prefix: com.installtest" in body
    # No raise — the fixture loads cleanly.
    profile = load_active_profile("installtest", base_dir=tmp_path)
    assert profile.launchd_label_prefix == "com.installtest"
