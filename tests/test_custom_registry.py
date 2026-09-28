"""Unit tests for the user-defined custom-verb registry (P3-07).

These tests exercise the pure-Python registry layer
(`mineru_cli.custom.registry`) — no Typer, no CliRunner, no subprocess.
Verb-level end-to-end tests live in `tests/test_custom_verbs.py`.

Coverage:

  - CustomVerbEntry.from_mapping: happy path + every validation branch.
  - registry_path_for_profile: env override, missing profile_root.
  - Registry.load_from: missing file -> empty; malformed YAML raises;
    duplicate verb name in YAML raises; empty `verbs:` returns empty.
  - Registry.add_entry: appends; rejects name-shape violations;
    rejects built-in collisions; rejects duplicate names.
  - Registry.remove_entry: happy path; miss raises loud.
  - Registry.save_to: writes atomically at 0600; round-trips a
    previously-saved registry byte-perfectly through load.
  - validate_no_collision: reserved-verb collision surfaces the exact
    name in the error message.
  - _atomic_write_600: mode bit is 0600; write is atomic under a crash
    surrogate (raise between open + rename leaves the target intact).

⚠️ SAFETY DISCIPLINE ⚠️

  Every write in this file targets a `tmp_path` via
  `MINERU_CUSTOM_VERBS_ROOT`. No test writes into the live worktree
  `profiles/mineru/` directory (the seed profile ships without a
  `custom_verbs.yaml`, so accidentally writing one would be visible in
  git — the env override is the guard-rail).

  No test invokes the shelled-out `command` of any entry: the registry
  is a pure serialization/validation layer and never spawns subprocesses.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from mineru_cli.custom.registry import (
    CUSTOM_VERBS_FILENAME,
    CUSTOM_VERBS_ROOT_ENV,
    CustomVerbEntry,
    CustomVerbError,
    CustomVerbRegistry,
    NAME_PATTERN,
    REGISTRY_FILE_MODE,
    _atomic_write_600,
    _validate_name_shape,
    builtin_verb_names,
    registry_path_for_profile,
    validate_no_collision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_mapping(**overrides):
    """Return a minimal valid entry mapping, patchable via overrides."""
    base = {
        "name": "hello-world",
        "description": "Say hi.",
        "command": ["echo", "hi"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# NAME_PATTERN + _validate_name_shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["a", "abc", "a-b", "a-b-c", "foo123", "journal-export"],
)
def test_name_pattern_accepts_valid_names(name: str) -> None:
    assert NAME_PATTERN.match(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-foo",  # starts with hyphen
        "1foo",  # starts with digit
        "Foo",  # uppercase
        "foo bar",  # space
        "foo_bar",  # underscore not allowed
        "foo/bar",
        "foo.bar",
    ],
)
def test_name_pattern_rejects_invalid_names(name: str) -> None:
    assert not NAME_PATTERN.match(name)


def test_validate_name_shape_error_names_the_input() -> None:
    with pytest.raises(CustomVerbError) as exc:
        _validate_name_shape("Bad_Name")
    assert "Bad_Name" in str(exc.value)


# ---------------------------------------------------------------------------
# CustomVerbEntry.from_mapping — validation branches
# ---------------------------------------------------------------------------


def test_from_mapping_happy_path() -> None:
    entry = CustomVerbEntry.from_mapping(
        _valid_mapping(
            cwd="/tmp",
            schedule="0 9 * * *",
            deploy_notes="run daily at 9am\nsecond line",
            env={"FOO": "bar"},
        )
    )
    assert entry.name == "hello-world"
    assert entry.description == "Say hi."
    assert entry.command == ("echo", "hi")
    assert entry.cwd == "/tmp"
    assert entry.schedule == "0 9 * * *"
    assert entry.deploy_notes == "run daily at 9am\nsecond line"
    assert entry.env == {"FOO": "bar"}


def test_from_mapping_optional_fields_default_to_none() -> None:
    entry = CustomVerbEntry.from_mapping(_valid_mapping())
    assert entry.cwd is None
    assert entry.schedule is None
    assert entry.deploy_notes is None
    assert entry.env == {}


def test_from_mapping_rejects_non_mapping() -> None:
    with pytest.raises(CustomVerbError):
        CustomVerbEntry.from_mapping(["not", "a", "mapping"])


def test_from_mapping_rejects_missing_name() -> None:
    with pytest.raises(CustomVerbError) as exc:
        CustomVerbEntry.from_mapping({"description": "x", "command": ["y"]})
    assert "name" in str(exc.value)


def test_from_mapping_rejects_bad_name_shape() -> None:
    with pytest.raises(CustomVerbError) as exc:
        CustomVerbEntry.from_mapping(_valid_mapping(name="Bad Name"))
    assert "Bad Name" in str(exc.value)


def test_from_mapping_rejects_missing_description() -> None:
    with pytest.raises(CustomVerbError) as exc:
        CustomVerbEntry.from_mapping(
            {"name": "hi", "command": ["echo"]}
        )
    assert "description" in str(exc.value)


def test_from_mapping_rejects_empty_command() -> None:
    with pytest.raises(CustomVerbError) as exc:
        CustomVerbEntry.from_mapping(_valid_mapping(command=[]))
    assert "command" in str(exc.value)


def test_from_mapping_rejects_non_string_command_element() -> None:
    with pytest.raises(CustomVerbError):
        CustomVerbEntry.from_mapping(_valid_mapping(command=["echo", 42]))


def test_from_mapping_rejects_relative_cwd() -> None:
    with pytest.raises(CustomVerbError) as exc:
        CustomVerbEntry.from_mapping(_valid_mapping(cwd="relative/path"))
    assert "cwd" in str(exc.value)
    assert "absolute" in str(exc.value)


def test_from_mapping_rejects_env_non_string_value() -> None:
    with pytest.raises(CustomVerbError):
        CustomVerbEntry.from_mapping(_valid_mapping(env={"FOO": 123}))


def test_from_mapping_rejects_env_non_mapping() -> None:
    with pytest.raises(CustomVerbError):
        CustomVerbEntry.from_mapping(_valid_mapping(env=["FOO=bar"]))


# ---------------------------------------------------------------------------
# to_yaml_dict — round-trip integrity
# ---------------------------------------------------------------------------


def test_to_yaml_dict_only_emits_set_fields() -> None:
    """Optional fields left unset stay out of the YAML dict."""
    entry = CustomVerbEntry.from_mapping(_valid_mapping())
    dumped = entry.to_yaml_dict()
    assert dumped == {
        "name": "hello-world",
        "description": "Say hi.",
        "command": ["echo", "hi"],
    }
    # cwd / schedule / deploy_notes / env NOT in the dict
    for k in ("cwd", "schedule", "deploy_notes", "env"):
        assert k not in dumped


def test_to_yaml_dict_emits_all_set_fields() -> None:
    entry = CustomVerbEntry.from_mapping(
        _valid_mapping(
            cwd="/tmp",
            schedule="0 9 * * *",
            deploy_notes="notes",
            env={"FOO": "bar"},
        )
    )
    dumped = entry.to_yaml_dict()
    assert dumped["cwd"] == "/tmp"
    assert dumped["schedule"] == "0 9 * * *"
    assert dumped["deploy_notes"] == "notes"
    assert dumped["env"] == {"FOO": "bar"}


# ---------------------------------------------------------------------------
# registry_path_for_profile
# ---------------------------------------------------------------------------


class _FakeProfile:
    """Minimal Profile stand-in for tests that only need `profile_root`."""

    def __init__(self, profile_root: Path) -> None:
        self.profile_root = profile_root
        self.workspace_absolute = profile_root


def test_registry_path_uses_profile_root_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CUSTOM_VERBS_ROOT_ENV, raising=False)
    profile = _FakeProfile(tmp_path)
    resolved = registry_path_for_profile(profile)
    assert resolved == tmp_path / CUSTOM_VERBS_FILENAME


def test_registry_path_env_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CUSTOM_VERBS_ROOT_ENV, str(tmp_path))
    profile = _FakeProfile(Path("/should-not-be-used"))
    resolved = registry_path_for_profile(profile)
    # `.resolve()` on macOS turns /tmp into /private/tmp; assert on the
    # basename + parent shape rather than exact equality.
    assert resolved.name == CUSTOM_VERBS_FILENAME
    assert resolved.parent.resolve() == tmp_path.resolve()


def test_registry_path_missing_profile_root_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CUSTOM_VERBS_ROOT_ENV, raising=False)

    class _Empty:
        pass

    with pytest.raises(CustomVerbError) as exc:
        registry_path_for_profile(_Empty())
    assert "profile_root" in str(exc.value)


# ---------------------------------------------------------------------------
# builtin_verb_names — pulls the real app tree
# ---------------------------------------------------------------------------


def test_builtin_verb_names_contains_current_nouns() -> None:
    """The set must include every noun currently registered on the app."""
    names = builtin_verb_names()
    for expected in ("gmail", "telegram", "custom", "browser", "profile"):
        assert expected in names, f"built-in verb {expected!r} missing from {sorted(names)}"


def test_validate_no_collision_surfaces_the_name() -> None:
    with pytest.raises(CustomVerbError) as exc:
        validate_no_collision("gmail", {"gmail", "telegram"})
    assert "gmail" in str(exc.value)
    assert "built-in" in str(exc.value)


def test_validate_no_collision_passes_for_novel_name() -> None:
    # Should not raise
    validate_no_collision("sam-custom", {"gmail", "telegram"})


# ---------------------------------------------------------------------------
# CustomVerbRegistry.load_from
# ---------------------------------------------------------------------------


def test_load_from_missing_file_is_empty(tmp_path: Path) -> None:
    """Fail-open on discovery — a fresh install must not crash."""
    reg = CustomVerbRegistry.load_from(tmp_path / "does-not-exist.yaml")
    assert reg.list_entries() == ()


def test_load_from_empty_verbs_list(tmp_path: Path) -> None:
    path = tmp_path / "custom_verbs.yaml"
    path.write_text("verbs: []\n")
    reg = CustomVerbRegistry.load_from(path)
    assert reg.list_entries() == ()


def test_load_from_bad_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "custom_verbs.yaml"
    path.write_text("verbs: [not: valid: yaml\n")
    with pytest.raises(CustomVerbError) as exc:
        CustomVerbRegistry.load_from(path)
    assert "YAML" in str(exc.value) or "yaml" in str(exc.value)


def test_load_from_non_mapping_top_level_raises(tmp_path: Path) -> None:
    path = tmp_path / "custom_verbs.yaml"
    path.write_text("- just a list\n")
    with pytest.raises(CustomVerbError) as exc:
        CustomVerbRegistry.load_from(path)
    assert "mapping" in str(exc.value)


def test_load_from_verbs_not_a_list_raises(tmp_path: Path) -> None:
    path = tmp_path / "custom_verbs.yaml"
    path.write_text("verbs: not-a-list\n")
    with pytest.raises(CustomVerbError) as exc:
        CustomVerbRegistry.load_from(path)
    assert "list" in str(exc.value)


def test_load_from_duplicate_names_raises(tmp_path: Path) -> None:
    path = tmp_path / "custom_verbs.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "verbs": [
                    _valid_mapping(name="dup"),
                    _valid_mapping(name="dup"),
                ]
            }
        )
    )
    with pytest.raises(CustomVerbError) as exc:
        CustomVerbRegistry.load_from(path)
    assert "duplicate" in str(exc.value).lower()
    assert "dup" in str(exc.value)


def test_load_from_schema_invalid_entry_raises(tmp_path: Path) -> None:
    """A YAML that parses but violates the schema fails LOUD (not silent)."""
    path = tmp_path / "custom_verbs.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "verbs": [
                    {"name": "Bad Name", "description": "d", "command": ["x"]},
                ]
            }
        )
    )
    with pytest.raises(CustomVerbError):
        CustomVerbRegistry.load_from(path)


# ---------------------------------------------------------------------------
# CustomVerbRegistry.add_entry / remove_entry
# ---------------------------------------------------------------------------


def test_add_entry_appends() -> None:
    reg = CustomVerbRegistry()
    entry = CustomVerbEntry.from_mapping(_valid_mapping(name="foo"))
    new = reg.add_entry(entry, builtin_names={"gmail"})
    assert len(new.list_entries()) == 1
    assert new.get_entry("foo") == entry


def test_add_entry_rejects_collision_with_builtin() -> None:
    reg = CustomVerbRegistry()
    entry = CustomVerbEntry.from_mapping(_valid_mapping(name="gmail"))
    with pytest.raises(CustomVerbError) as exc:
        reg.add_entry(entry, builtin_names={"gmail", "telegram"})
    assert "gmail" in str(exc.value)


def test_add_entry_rejects_bad_name_shape() -> None:
    reg = CustomVerbRegistry()
    # Bypass from_mapping so we can construct an entry with an intentionally
    # invalid name and verify `add_entry` catches it.
    entry = CustomVerbEntry(
        name="Bad_Name",
        description="x",
        command=("echo",),
    )
    with pytest.raises(CustomVerbError):
        reg.add_entry(entry, builtin_names=set())


def test_add_entry_rejects_duplicate() -> None:
    reg = CustomVerbRegistry()
    entry = CustomVerbEntry.from_mapping(_valid_mapping(name="foo"))
    reg2 = reg.add_entry(entry, builtin_names=set())
    with pytest.raises(CustomVerbError):
        reg2.add_entry(entry, builtin_names=set())


def test_remove_entry_happy() -> None:
    reg = CustomVerbRegistry()
    entry = CustomVerbEntry.from_mapping(_valid_mapping(name="foo"))
    reg2 = reg.add_entry(entry, builtin_names=set())
    reg3 = reg2.remove_entry("foo")
    assert reg3.list_entries() == ()


def test_remove_entry_miss_raises() -> None:
    reg = CustomVerbRegistry()
    with pytest.raises(CustomVerbError) as exc:
        reg.remove_entry("does-not-exist")
    assert "does-not-exist" in str(exc.value)


# ---------------------------------------------------------------------------
# save_to + round-trip
# ---------------------------------------------------------------------------


def test_save_to_writes_file_at_0600(tmp_path: Path) -> None:
    reg = CustomVerbRegistry()
    entry = CustomVerbEntry.from_mapping(_valid_mapping(name="foo"))
    reg2 = reg.add_entry(entry, builtin_names=set())
    target = tmp_path / "custom_verbs.yaml"
    written = reg2.save_to(target)
    assert written == target
    mode = stat.S_IMODE(os.stat(written).st_mode)
    assert mode == REGISTRY_FILE_MODE, f"expected mode 0600, got {oct(mode)}"


def test_save_to_round_trips_through_load(tmp_path: Path) -> None:
    reg = CustomVerbRegistry(path=tmp_path / "custom_verbs.yaml")
    entry1 = CustomVerbEntry.from_mapping(
        _valid_mapping(
            name="one",
            cwd="/tmp",
            schedule="0 9 * * *",
            deploy_notes="notes\nsecond line",
            env={"FOO": "bar"},
        )
    )
    entry2 = CustomVerbEntry.from_mapping(_valid_mapping(name="two"))
    reg = reg.add_entry(entry1, builtin_names=set())
    reg = reg.add_entry(entry2, builtin_names=set())
    reg.save_to()

    reloaded = CustomVerbRegistry.load_from(tmp_path / "custom_verbs.yaml")
    assert reloaded.get_entry("one") == entry1
    assert reloaded.get_entry("two") == entry2


def test_save_to_no_path_raises() -> None:
    reg = CustomVerbRegistry()
    with pytest.raises(CustomVerbError) as exc:
        reg.save_to()
    assert "path" in str(exc.value)


def test_save_to_empty_registry_writes_empty_verbs_list(tmp_path: Path) -> None:
    reg = CustomVerbRegistry(path=tmp_path / "custom_verbs.yaml")
    reg.save_to()
    content = (tmp_path / "custom_verbs.yaml").read_text()
    parsed = yaml.safe_load(content)
    assert parsed == {"verbs": []}


# ---------------------------------------------------------------------------
# _atomic_write_600 — permission + atomicity
# ---------------------------------------------------------------------------


def test_atomic_write_600_creates_file_at_0600(tmp_path: Path) -> None:
    target = tmp_path / "test.yaml"
    _atomic_write_600(target, "hello world\n")
    assert target.exists()
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == REGISTRY_FILE_MODE
    assert target.read_text() == "hello world\n"


def test_atomic_write_600_replaces_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "test.yaml"
    _atomic_write_600(target, "first\n")
    _atomic_write_600(target, "second\n")
    assert target.read_text() == "second\n"
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == REGISTRY_FILE_MODE


def test_atomic_write_600_creates_parent_dir(tmp_path: Path) -> None:
    """Missing intermediate directory is created (mkdir parents=True)."""
    target = tmp_path / "nested" / "deep" / "test.yaml"
    _atomic_write_600(target, "hi\n")
    assert target.exists()


def test_atomic_write_600_does_not_leave_temp_file(tmp_path: Path) -> None:
    """Happy path: only the target file remains, no `.tmp-*` cruft."""
    target = tmp_path / "test.yaml"
    _atomic_write_600(target, "hi\n")
    siblings = list(tmp_path.iterdir())
    assert siblings == [target]


def test_atomic_write_600_cleans_up_temp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write failure MUST remove the sibling temp file (no cruft)."""
    target = tmp_path / "test.yaml"

    # Force os.replace to raise, simulating a rename failure after the
    # temp file was written. The cleanup path should remove the temp.
    real_replace = os.replace

    def fake_replace(src, dst):
        # Sanity: the temp really did land on disk before we sabotage.
        assert Path(src).exists()
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fake_replace)
    with pytest.raises(OSError):
        _atomic_write_600(target, "hi\n")

    # Restore before checking siblings so the assertion isn't polluted
    # by a lingering monkeypatch.
    monkeypatch.setattr(os, "replace", real_replace)

    # The target should NOT exist, and no `.tmp-*` file should remain.
    assert not target.exists()
    leftovers = [
        p
        for p in tmp_path.iterdir()
        if p.name.startswith("test.yaml.tmp-")
    ]
    assert leftovers == [], (
        f"expected no temp cruft; found: {[p.name for p in leftovers]}"
    )
