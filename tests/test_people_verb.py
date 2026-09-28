"""Tests for the machine-level human registry (Phase 1).

Covers:
  - `load_humans_registry`: valid file -> populated `HumansRegistry`
    with lookup and iteration behaving correctly.
  - Fail-loud on missing registry file, non-mapping top-level, missing
    `humans:` block, empty humans block, missing entry fields, invalid
    telegram_id type, bad handle, etc.
  - `mineru people list`: pretty table + JSON shapes (canonical verb).
  - `mineru people path`: prints the workspace-root-relative path.
  - `mineru humans list` / `mineru humans path`: HIDDEN 2026-09-16 audit
    §2A alias — still dispatches AND emits the DEPRECATED notice.
  - Filename resolution (2026-09-16 audit §2A F3): the loader prefers
    the canonical `people.yaml`, falls back to a legacy `humans.yaml`,
    and the resolver is transparent to callers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.humans import (
    HumansError,
    HumansRegistry,
    default_humans_yaml_path,
    default_people_yaml_path,
    legacy_humans_yaml_path,
    load_humans_registry,
    resolve_registry_yaml_path,
)
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


def _valid_body() -> str:
    return (
        "humans:\n"
        "  sam:\n"
        "    telegram_id: 123456789\n"
        "    display_name: \"Sam Rivera\"\n"
        "  mira:\n"
        "    telegram_id: 987654321\n"
        "    display_name: \"Mira Rivera\"\n"
    )


def _write_humans(tmp_path: Path, body: str, *, filename: str = "people.yaml") -> Path:
    """Write the given YAML body to the workspace root under `filename`.

    Default is the CANONICAL `people.yaml` (post-2026-09-16 audit §2A
    F3 rename). Tests that need to exercise the LEGACY `humans.yaml`
    fallback pass `filename="humans.yaml"` explicitly.
    """
    path = tmp_path / filename
    path.write_text(body, encoding="utf-8")
    return path


# --- load_humans_registry ----------------------------------------------


def test_load_humans_registry_populates_entries(tmp_path: Path) -> None:
    _write_humans(tmp_path, _valid_body())
    registry = load_humans_registry(path=tmp_path / "people.yaml")
    assert isinstance(registry, HumansRegistry)
    assert len(registry) == 2
    assert "sam" in registry
    assert "mira" in registry
    sam = registry.get("sam")
    assert sam.handle == "sam"
    assert sam.telegram_id == 123456789
    assert sam.display_name == "Sam Rivera"
    assert registry.handles() == ["sam", "mira"]


def test_load_humans_registry_iteration_preserves_yaml_order(
    tmp_path: Path,
) -> None:
    _write_humans(tmp_path, _valid_body())
    registry = load_humans_registry(path=tmp_path / "people.yaml")
    handles = [h.handle for h in registry]
    assert handles == ["sam", "mira"]


def test_load_humans_registry_missing_file_names_path(tmp_path: Path) -> None:
    with pytest.raises(HumansError) as exc:
        load_humans_registry(path=tmp_path / "does-not-exist.yaml")
    assert str(tmp_path / "does-not-exist.yaml") in str(exc.value)


def test_load_humans_registry_rejects_non_mapping_top_level(
    tmp_path: Path,
) -> None:
    _write_humans(tmp_path, "just a scalar\n")
    with pytest.raises(HumansError) as exc:
        load_humans_registry(path=tmp_path / "people.yaml")
    assert "mapping" in str(exc.value)


def test_load_humans_registry_rejects_missing_humans_block(
    tmp_path: Path,
) -> None:
    _write_humans(tmp_path, "other_key: 42\n")
    with pytest.raises(HumansError) as exc:
        load_humans_registry(path=tmp_path / "people.yaml")
    assert "humans" in str(exc.value)
    assert "missing" in str(exc.value)


def test_load_humans_registry_rejects_empty_humans_block(
    tmp_path: Path,
) -> None:
    _write_humans(tmp_path, "humans: {}\n")
    with pytest.raises(HumansError) as exc:
        load_humans_registry(path=tmp_path / "people.yaml")
    assert "empty" in str(exc.value)


def test_load_humans_registry_rejects_bad_handle_chars(
    tmp_path: Path,
) -> None:
    _write_humans(
        tmp_path,
        "humans:\n"
        "  \"bad handle\":\n"
        "    telegram_id: 1\n"
        "    display_name: X\n",
    )
    with pytest.raises(HumansError) as exc:
        load_humans_registry(path=tmp_path / "people.yaml")
    assert "bad handle" in str(exc.value)


def test_load_humans_registry_rejects_missing_telegram_id(
    tmp_path: Path,
) -> None:
    _write_humans(
        tmp_path,
        "humans:\n"
        "  sam:\n"
        "    display_name: Sam\n",
    )
    with pytest.raises(HumansError) as exc:
        load_humans_registry(path=tmp_path / "people.yaml")
    assert "telegram_id" in str(exc.value)


def test_load_humans_registry_rejects_non_integer_telegram_id(
    tmp_path: Path,
) -> None:
    _write_humans(
        tmp_path,
        "humans:\n"
        "  sam:\n"
        "    telegram_id: \"123\"\n"
        "    display_name: Sam\n",
    )
    with pytest.raises(HumansError) as exc:
        load_humans_registry(path=tmp_path / "people.yaml")
    assert "telegram_id" in str(exc.value)
    assert "integer" in str(exc.value)


def test_load_humans_registry_rejects_bool_as_telegram_id(
    tmp_path: Path,
) -> None:
    """`True` is a subclass of int in Python; the loader rejects explicitly."""
    _write_humans(
        tmp_path,
        "humans:\n"
        "  sam:\n"
        "    telegram_id: true\n"
        "    display_name: Sam\n",
    )
    with pytest.raises(HumansError) as exc:
        load_humans_registry(path=tmp_path / "people.yaml")
    assert "telegram_id" in str(exc.value)


def test_load_humans_registry_rejects_empty_display_name(
    tmp_path: Path,
) -> None:
    _write_humans(
        tmp_path,
        "humans:\n"
        "  sam:\n"
        "    telegram_id: 1\n"
        "    display_name: \"\"\n",
    )
    with pytest.raises(HumansError) as exc:
        load_humans_registry(path=tmp_path / "people.yaml")
    assert "display_name" in str(exc.value)


# --- default path resolution + people.yaml/humans.yaml fallback ---------
#
# 2026-09-16 audit §2A F3 file rename: the canonical registry filename
# is `people.yaml`; the legacy `humans.yaml` is READ as a fallback so
# pre-rename workspaces keep resolving. These tests pin the resolver
# semantics (prefer canonical; fall back to legacy; canonical default
# for a fresh workspace) so a regression fails here loud.


def test_default_people_yaml_path_is_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`default_people_yaml_path` is the CANONICAL post-rename target."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    assert default_people_yaml_path() == tmp_path.resolve() / "people.yaml"


def test_default_humans_yaml_path_is_legacy_alias_pointing_at_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`default_humans_yaml_path` is the pre-rename alias.

    Post-rename it points at the canonical `people.yaml` path so
    pre-rename imports (`from mineru_cli.humans import
    default_humans_yaml_path`) still land on the currently-used file.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    assert default_humans_yaml_path() == default_people_yaml_path()
    assert default_humans_yaml_path() == tmp_path.resolve() / "people.yaml"


def test_legacy_humans_yaml_path_is_the_pre_rename_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`legacy_humans_yaml_path` names the legacy fallback filename."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    assert legacy_humans_yaml_path() == tmp_path.resolve() / "humans.yaml"


def test_resolve_registry_prefers_people_yaml_when_both_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    _write_humans(tmp_path, _valid_body(), filename="people.yaml")
    _write_humans(tmp_path, _valid_body(), filename="humans.yaml")
    resolved = resolve_registry_yaml_path()
    assert resolved == tmp_path.resolve() / "people.yaml"


def test_resolve_registry_falls_back_to_humans_yaml_when_only_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    _write_humans(tmp_path, _valid_body(), filename="humans.yaml")
    resolved = resolve_registry_yaml_path()
    assert resolved == tmp_path.resolve() / "humans.yaml"


def test_resolve_registry_defaults_to_canonical_when_nothing_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh workspace: resolver returns the CANONICAL path.

    The caller then tries to open it and gets a fail-loud
    `HumansError` naming the canonical path — the right file to
    create.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    resolved = resolve_registry_yaml_path()
    assert resolved == tmp_path.resolve() / "people.yaml"


def test_load_humans_registry_reads_via_fallback_when_only_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full loader reads the legacy filename identically to the canonical.

    Load-bearing regression for the access-seam contract: the resolved
    `HumansRegistry` MUST be identical regardless of which filename is
    on disk, so the downstream `resolve_allowlist_ids` -> Keychain
    payload never changes across the rename.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    _write_humans(tmp_path, _valid_body(), filename="humans.yaml")
    registry = load_humans_registry()
    assert registry.handles() == ["sam", "mira"]
    assert registry.get("sam").telegram_id == 123456789


def test_load_humans_registry_prefers_people_yaml_when_both_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both files exist, only `people.yaml` is read.

    A pre-rename `humans.yaml` that was left behind (or that the
    writer refreshed via the mirror) must not shadow the canonical
    file. The test writes DIFFERENT contents to each file and asserts
    the loader returns the canonical entries only.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    # people.yaml — canonical, one handle
    _write_humans(
        tmp_path,
        (
            "humans:\n"
            "  canonical_only:\n"
            "    telegram_id: 111\n"
            "    display_name: Canonical\n"
        ),
        filename="people.yaml",
    )
    # humans.yaml — legacy, DIFFERENT handle
    _write_humans(
        tmp_path,
        (
            "humans:\n"
            "  legacy_only:\n"
            "    telegram_id: 222\n"
            "    display_name: Legacy\n"
        ),
        filename="humans.yaml",
    )
    registry = load_humans_registry()
    assert registry.handles() == ["canonical_only"]
    assert "legacy_only" not in registry


# --- CLI: mineru people list / path (canonical, 2026-09-16 audit §2A F3) -


def test_cli_people_list_pretty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    _write_humans(tmp_path, _valid_body())
    result = runner.invoke(app, ["people", "list"])
    assert result.exit_code == 0, result.output
    assert "sam" in result.stdout
    assert "mira" in result.stdout
    assert "123456789" in result.stdout
    assert "Sam Rivera" in result.stdout


def test_cli_people_list_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    _write_humans(tmp_path, _valid_body())
    result = runner.invoke(app, ["people", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # JSON key stays `"humans"` for back-compat with pre-rename consumers.
    assert [h["handle"] for h in payload["humans"]] == ["sam", "mira"]
    assert payload["humans"][0]["telegram_id"] == 123456789


def test_cli_people_list_missing_file_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["people", "list"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_cli_people_path_prints_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mineru people path` prints the canonical `people.yaml` path."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["people", "path"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == str(tmp_path.resolve() / "people.yaml")


def test_cli_people_path_warns_when_only_legacy_humans_yaml_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If only the legacy `humans.yaml` exists, the verb warns on stderr.

    stdout stays the canonical path (the operator's copy-paste target);
    stderr names the legacy path the loader will actually read so the
    operator knows to migrate.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    _write_humans(tmp_path, _valid_body(), filename="humans.yaml")
    result = runner.invoke(app, ["people", "path"])
    assert result.exit_code == 0, result.output
    # Canonical path in output; the fallback note also lands (stderr
    # is merged into result.output by CliRunner default).
    assert str(tmp_path.resolve() / "people.yaml") in result.output
    assert "legacy" in result.output
    assert "humans.yaml" in result.output


# --- CLI: mineru humans list / path (HIDDEN 2026-09-16 audit §2A alias) --
#
# Standard 90-day compat window. Both verbs still dispatch through the
# same canonical body AND fire the shared DEPRECATED stderr notice. Pins
# the dispatch equivalence AND the notice wording so a regression on
# either fails here loud.


def test_cli_humans_alias_list_still_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    _write_humans(tmp_path, _valid_body())
    result = runner.invoke(app, ["humans", "list"])
    assert result.exit_code == 0, result.output
    # Same output the canonical verb produces.
    assert "sam" in result.stdout
    assert "Sam Rivera" in result.stdout


def test_cli_humans_alias_list_emits_deprecation_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    _write_humans(tmp_path, _valid_body())
    result = runner.invoke(app, ["humans", "list"])
    assert result.exit_code == 0
    # CliRunner merges stderr into `output` by default; the notice uses
    # the shared `DEPRECATED:` prefix so a single grep across every
    # rename in the batch catches it.
    assert "DEPRECATED" in result.output
    assert "mineru humans list" in result.output
    assert "mineru people list" in result.output


def test_cli_humans_alias_list_json_still_dispatches_and_emits_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    _write_humans(tmp_path, _valid_body())
    result = runner.invoke(app, ["humans", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert "DEPRECATED" in result.output
    # The JSON payload itself must still parse cleanly — the notice
    # goes to stderr and is separated from stdout in real usage; here
    # CliRunner merges them, so we search for the JSON payload directly.
    # (Look for the fresh JSON block AFTER the notice.)
    assert '"handle": "sam"' in result.output


def test_cli_humans_alias_path_still_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mineru humans path` (deprecated) still prints the canonical
    people.yaml path — the alias delegates to the same body."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["humans", "path"])
    assert result.exit_code == 0, result.output
    # Delegates through the canonical `path` body, which now prints the
    # canonical `people.yaml` location (see the file-rename note in the
    # 2026-09-16 audit §2A F3).
    assert str(tmp_path.resolve() / "people.yaml") in result.output


def test_cli_humans_alias_path_emits_deprecation_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    result = runner.invoke(app, ["humans", "path"])
    assert "DEPRECATED" in result.output
    assert "mineru humans path" in result.output
    assert "mineru people path" in result.output


def test_humans_alias_hidden_from_root_help() -> None:
    """`mineru --help` should NOT list the deprecated `humans` alias."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Structural pin: the hidden alias's Typer registration carries
    # hidden=True. (A rendered-help substring check would false-positive
    # on the connector help strings that still mention "humans".)
    for group in app.registered_groups:
        if group.name == "humans":
            assert group.hidden is True, (
                "the `humans` alias group must be hidden from `mineru --help`"
            )
            break
    else:
        raise AssertionError("hidden `humans` alias sub-app not registered")


# --- Seed people.yaml in the worktree ---------------------------------


def test_human_registry_loads_a_registered_owner(tmp_path: Path) -> None:
    """A valid people.yaml loads and registers its entries.

    The engine ships no live registry file (it is per-user, gitignored),
    so this builds a synthetic one rather than asserting a committed file.
    """
    registry_yaml = tmp_path / "people.yaml"
    registry_yaml.write_text(
        "humans:\n"
        "  sam:\n"
        "    telegram_id: 123456789\n"
        '    display_name: "Sam Rivera"\n',
        encoding="utf-8",
    )
    registry = load_humans_registry(path=registry_yaml)
    assert "sam" in registry
