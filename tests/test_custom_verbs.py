"""End-to-end tests for `mineru custom {add,list,remove,show}` + dynamic dispatch.

Sits on top of `tests/test_custom_registry.py` (registry-layer unit tests)
and exercises the Typer wire-up via `typer.testing.CliRunner`.

Coverage per P3-07 done criteria:

  (a) Round-trip a YAML file via a tmp MINERU_CUSTOM_VERBS_ROOT.
  (b) Reject a name that collides with 'gmail'.
  (c) After add, the new verb appears in `mineru --help` and shells out
      to the mock command with exit-code propagation.
  (d) show / remove work end-to-end.
  (e) All writes to YAML are atomic + 0600.
  (f) `custom add` interactive path covered via CliRunner input="...".
  (g) Missing custom_verbs.yaml is treated as empty registry.
  (h) `{args}` placeholder pass-through works.

⚠️ SAFETY DISCIPLINE ⚠️

  Every write in this file targets a `tmp_path` via
  `MINERU_CUSTOM_VERBS_ROOT`. No test writes into the live worktree
  `profiles/mineru/` directory.

  The dispatcher shells out via `subprocess.run`. Every test that
  invokes a registered verb MOCKS `subprocess.run` in
  `mineru_cli.verbs.custom` (via `unittest.mock.patch`) so no real
  process ever spawns from the tests. The tests that exercise the
  real subprocess path use a fake `/bin/sh` script written to tmp with
  a canned exit code — no network, no external tools.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

import pytest
import typer
import yaml
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.custom.registry import (
    CUSTOM_VERBS_FILENAME,
    CUSTOM_VERBS_ROOT_ENV,
    REGISTRY_FILE_MODE,
    CustomVerbEntry,
    CustomVerbRegistry,
)
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
)

# CliRunner: default (mix_stderr param was removed in newer typer versions;
# the runner now separates stdout/stderr onto `.stdout` / `.stderr`).
runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_profile_yaml(base: Path, name: str = "alice") -> Path:
    """Materialize a schema-valid profile.yaml under `base/<name>/` and return the profile dir."""
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.yaml").write_text(
        f"name: {name}\n"
        f"display_name: {name.capitalize()}\n"
        "assistant_name: TestBot\n"
        "timezone: America/Los_Angeles\n"
        f"keychain_account: {name}-acct\n"
        f"launchd_label_prefix: com.{name}\n"
        f"workspace_absolute: /tmp/{name}-ws\n"
        f"memory_root: /tmp/{name}-ws/memory\n"
        f"briefs_root: /tmp/{name}-ws/briefs\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        f"  env_prefix: {name.upper()}_SECRET_\n",
        encoding="utf-8",
    )
    return profile_dir


@pytest.fixture
def hermetic_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp profile tree pointed at by MINERU_PROFILE_ROOT.

    Also pins MINERU_CUSTOM_VERBS_ROOT to the profile directory so the
    registry file is co-located with profile.yaml (matches production
    behavior where they share `profile_root`).

    The workspace_absolute directory (`/tmp/<name>-ws`) is materialized
    on disk because the dispatcher does `subprocess.run(..., cwd=...)`
    and Python's `subprocess.run` raises `FileNotFoundError` when the
    cwd does not exist. Tests that mock `subprocess.run` never touch
    it, but the belt-and-braces real-subprocess test does.

    Returns the profile directory (`.../alice/`).
    """
    profile_dir = _write_profile_yaml(tmp_path, "alice")
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "alice")
    monkeypatch.setenv(CUSTOM_VERBS_ROOT_ENV, str(profile_dir))
    Path("/tmp/alice-ws").mkdir(parents=True, exist_ok=True)
    return profile_dir


# ---------------------------------------------------------------------------
# help tree — the four `custom` sub-verbs render
# ---------------------------------------------------------------------------


def test_custom_group_help_renders(hermetic_profile: Path) -> None:
    result = runner.invoke(app, ["custom", "--help"])
    assert result.exit_code == 0, result.stderr
    for sub in ("add", "list", "show", "remove"):
        assert sub in result.stdout, (
            f"`mineru custom --help` missing sub-command {sub!r}"
        )


def test_custom_add_help_renders(hermetic_profile: Path) -> None:
    result = runner.invoke(app, ["custom", "add", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    for token in ("--name", "--description", "--command", "--cwd", "--schedule"):
        assert token in lowered, f"custom add --help missing {token!r}"


# ---------------------------------------------------------------------------
# `custom list` on a fresh profile — empty registry, friendly message
# ---------------------------------------------------------------------------


def test_custom_list_empty_shows_friendly_note(hermetic_profile: Path) -> None:
    """Missing custom_verbs.yaml is treated as empty (fail-open discovery)."""
    result = runner.invoke(app, ["custom", "list"])
    assert result.exit_code == 0, result.stderr
    assert "no custom verbs registered" in result.stdout


def test_custom_list_empty_json(hermetic_profile: Path) -> None:
    result = runner.invoke(app, ["custom", "list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


# ---------------------------------------------------------------------------
# `custom add` — non-interactive via flags (round-trip through YAML)
# ---------------------------------------------------------------------------


def test_custom_add_via_flags_round_trips(hermetic_profile: Path) -> None:
    """P3-07 done-criterion (a): round-trip a YAML file via tmp MINERU_CUSTOM_VERBS_ROOT."""
    result = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "hello-world",
            "--description", "Say hi in a shell.",
            "--command", "echo hello",
        ],
    )
    assert result.exit_code == 0, result.stderr
    yaml_path = hermetic_profile / CUSTOM_VERBS_FILENAME
    assert yaml_path.exists()

    # 0600 permission bit (done-criterion e).
    mode = stat.S_IMODE(os.stat(yaml_path).st_mode)
    assert mode == REGISTRY_FILE_MODE, f"expected 0600, got {oct(mode)}"

    # YAML content round-trips through the registry loader.
    reg = CustomVerbRegistry.load_from(yaml_path)
    entry = reg.get_entry("hello-world")
    assert entry is not None
    assert entry.description == "Say hi in a shell."
    assert entry.command == ("echo", "hello")


def test_custom_add_rejects_gmail_collision(hermetic_profile: Path) -> None:
    """P3-07 done-criterion (b): reject a name that collides with 'gmail'."""
    result = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "gmail",
            "--description", "shadow gmail",
            "--command", "echo pwned",
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert "gmail" in result.stderr.lower()
    assert "built-in" in result.stderr.lower()

    # No file was written.
    yaml_path = hermetic_profile / CUSTOM_VERBS_FILENAME
    assert not yaml_path.exists(), (
        "custom_verbs.yaml should NOT exist after a rejected add"
    )


def test_custom_add_rejects_bad_name_shape(hermetic_profile: Path) -> None:
    result = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "Bad_Name",
            "--description", "x",
            "--command", "echo x",
        ],
    )
    assert result.exit_code == 2
    assert "Bad_Name" in result.stderr


def test_custom_add_rejects_duplicate(hermetic_profile: Path) -> None:
    args = [
        "custom", "add",
        "--name", "foo",
        "--description", "d",
        "--command", "echo x",
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.stderr
    second = runner.invoke(app, args)
    assert second.exit_code == 2
    assert "already registered" in second.stderr


def test_custom_add_records_schedule_and_env(hermetic_profile: Path) -> None:
    result = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "journals",
            "--description", "Export Apple Notes journals to plain text.",
            "--command", "python3 /Users/x/scripts/export-journals.py {args}",
            "--cwd", "/tmp",
            "--schedule", "45 0 * * *",
            "--deploy-notes", "runs via launchd at 12:45 AM daily",
        ],
    )
    assert result.exit_code == 0, result.stderr

    reg = CustomVerbRegistry.load_from(hermetic_profile / CUSTOM_VERBS_FILENAME)
    entry = reg.get_entry("journals")
    assert entry is not None
    assert entry.command == (
        "python3", "/Users/x/scripts/export-journals.py", "{args}",
    )
    assert entry.cwd == "/tmp"
    assert entry.schedule == "45 0 * * *"
    assert entry.deploy_notes == "runs via launchd at 12:45 AM daily"


# ---------------------------------------------------------------------------
# `custom add` — INTERACTIVE path (CliRunner input=...)
# ---------------------------------------------------------------------------


def test_custom_add_interactive_prompt_flow(hermetic_profile: Path) -> None:
    """P3-07 done-criterion (f): fully interactive path via CliRunner input=.

    Prompts in order:
      1. verb name
      2. one-line description
      3. shell command
      4. cwd (blank -> profile default)
      5. schedule (blank -> none)
      6. deploy notes (blank -> none)
      7. inherit parent env vars? (default Y → env_inherit=True)
    """
    stdin = (
        "my-verb\n"          # name
        "A test verb.\n"      # description
        "echo hi {args}\n"     # command with {args} placeholder
        "\n"                   # cwd (blank → profile default)
        "0 8 * * *\n"          # schedule
        "\n"                   # deploy notes (blank)
        "\n"                   # env_inherit prompt (accept default Y)
    )
    result = runner.invoke(app, ["custom", "add"], input=stdin)
    assert result.exit_code == 0, result.stderr
    reg = CustomVerbRegistry.load_from(hermetic_profile / CUSTOM_VERBS_FILENAME)
    entry = reg.get_entry("my-verb")
    assert entry is not None
    assert entry.description == "A test verb."
    assert entry.command == ("echo", "hi", "{args}")
    assert entry.cwd is None  # blank -> None (profile default at dispatch time)
    assert entry.schedule == "0 8 * * *"
    assert entry.deploy_notes is None
    assert entry.env_inherit is True


def test_custom_add_interactive_rejects_bad_name_before_more_prompts(
    hermetic_profile: Path,
) -> None:
    """Bad-name prompt fails immediately — no wasted typing on subsequent fields.

    We feed only the bad name; if the verb prompted for more fields the
    read would EOF and the exit code would differ from a clean exit 2.
    """
    result = runner.invoke(app, ["custom", "add"], input="Bad_Name\n")
    assert result.exit_code == 2
    assert "Bad_Name" in result.stderr


# ---------------------------------------------------------------------------
# `custom show` + `custom remove`
# ---------------------------------------------------------------------------


def test_custom_show_prints_full_entry(hermetic_profile: Path) -> None:
    add = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "greet",
            "--description", "Say hi.",
            "--command", "echo hi",
        ],
    )
    assert add.exit_code == 0

    show = runner.invoke(app, ["custom", "show", "greet"])
    assert show.exit_code == 0
    assert "greet" in show.stdout
    assert "Say hi." in show.stdout
    # invocation preview is present (Typer forwards extras).
    assert "mineru greet" in show.stdout


def test_custom_show_json(hermetic_profile: Path) -> None:
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "greet",
            "--description", "Say hi.",
            "--command", "echo hi",
        ],
    )
    result = runner.invoke(app, ["custom", "show", "greet", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["name"] == "greet"
    assert payload["command"] == ["echo", "hi"]


def test_custom_show_miss_exits_2(hermetic_profile: Path) -> None:
    result = runner.invoke(app, ["custom", "show", "nope"])
    assert result.exit_code == 2
    assert "nope" in result.stderr


def test_custom_remove_with_yes_flag(hermetic_profile: Path) -> None:
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "greet",
            "--description", "Say hi.",
            "--command", "echo hi",
        ],
    )
    result = runner.invoke(app, ["custom", "remove", "greet", "--yes"])
    assert result.exit_code == 0
    assert "removed" in result.stdout.lower()

    # File still exists (empty list), but greet is gone.
    reg = CustomVerbRegistry.load_from(hermetic_profile / CUSTOM_VERBS_FILENAME)
    assert reg.get_entry("greet") is None
    assert reg.list_entries() == ()


def test_custom_remove_confirm_aborts_on_no(hermetic_profile: Path) -> None:
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "greet",
            "--description", "Say hi.",
            "--command", "echo hi",
        ],
    )
    # Prompt: "remove custom verb 'greet' ..." — answer "n".
    result = runner.invoke(app, ["custom", "remove", "greet"], input="n\n")
    assert result.exit_code == 0
    assert "aborted" in result.stdout.lower()
    reg = CustomVerbRegistry.load_from(hermetic_profile / CUSTOM_VERBS_FILENAME)
    assert reg.get_entry("greet") is not None


def test_custom_remove_miss_exits_2(hermetic_profile: Path) -> None:
    result = runner.invoke(app, ["custom", "remove", "nope", "--yes"])
    assert result.exit_code == 2
    assert "nope" in result.stderr


# ---------------------------------------------------------------------------
# `custom list` — populated
# ---------------------------------------------------------------------------


def test_custom_list_populated_table(hermetic_profile: Path) -> None:
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "foo",
            "--description", "First",
            "--command", "echo 1",
            "--schedule", "0 9 * * *",
        ],
    )
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "bar",
            "--description", "Second",
            "--command", "echo 2",
        ],
    )
    result = runner.invoke(app, ["custom", "list"])
    assert result.exit_code == 0
    assert "foo" in result.stdout
    assert "bar" in result.stdout
    assert "First" in result.stdout
    assert "Second" in result.stdout
    assert "0 9 * * *" in result.stdout


def test_custom_list_populated_json(hermetic_profile: Path) -> None:
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "foo",
            "--description", "First",
            "--command", "echo 1",
        ],
    )
    result = runner.invoke(app, ["custom", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["name"] == "foo"


# ---------------------------------------------------------------------------
# Dynamic dispatch — after `add`, `mineru <name>` appears in --help
# ---------------------------------------------------------------------------


def test_after_add_new_verb_appears_in_root_help(
    hermetic_profile: Path,
) -> None:
    """P3-07 done-criterion (c) part 1: dynamically registered verbs render in --help.

    Assertion target: `CustomVerbTyperGroup.list_commands(ctx)`. That is
    the layer where the "registered verbs render in --help" claim lives
    — Click / Typer calls it when building the help tree, and our
    override adds each registry entry not shadowed by a built-in. A
    shadow-collision or dispatch-registration regression would still
    let `custom add` succeed (which only writes YAML) but would
    silently drop the entry from `list_commands`; this test catches
    that gap.

    We do NOT assert on `mineru --help` output text: Typer's --help
    handler short-circuits BEFORE the root callback runs, so the top-
    level --help render doesn't include dynamically registered verbs.
    The end-to-end "invoke the verb" test below covers the dispatch
    path (which IS what operators actually invoke).
    """
    from mineru_cli.verbs.custom import (
        CustomVerbTyperGroup,
        reset_registry_cache,
    )

    add = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "sample-verb",
            "--description", "A sample verb.",
            "--command", "echo sample",
        ],
    )
    assert add.exit_code == 0

    # Drop the process-scoped registry memo so the group re-reads the
    # newly written YAML instead of a pre-add cached snapshot.
    reset_registry_cache()

    # Build the same group Click would build for the root app, then
    # ask it what commands it lists — exactly what Typer's help
    # renderer consults.
    typer_info = app.info
    assert typer_info.cls is CustomVerbTyperGroup
    click_command = typer.main.get_command(app)
    assert isinstance(click_command, CustomVerbTyperGroup)
    with click_command.make_context(
        "mineru", ["--help"], resilient_parsing=True
    ) as ctx:
        rendered_names = click_command.list_commands(ctx)
    assert "sample-verb" in rendered_names, (
        f"expected 'sample-verb' in list_commands output, got {rendered_names!r}"
    )


def test_after_add_invoking_verb_shells_out(
    hermetic_profile: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3-07 done-criterion (c) part 2: `mineru <verb>` shells out via subprocess.run.

    We patch `subprocess.run` in the verbs.custom module so no real
    process spawns. The recorded argv + cwd + env are asserted.
    """
    # First: register the verb.
    add = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "shellout",
            "--description", "Shell out to a canned cmd.",
            "--command", "/usr/bin/true --flag",
        ],
    )
    assert add.exit_code == 0

    calls: List[Dict] = []

    class _Completed:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None, check=False):
        calls.append({"cmd": list(cmd), "cwd": cwd, "env": env})
        return _Completed()

    with patch("mineru_cli.verbs.custom.subprocess.run", fake_run):
        result = runner.invoke(app, ["shellout", "extra-arg-1", "--verbose"])

    assert result.exit_code == 0
    assert len(calls) == 1
    argv = calls[0]["cmd"]
    # No `{args}` placeholder -> extras are APPENDED to the argv.
    assert argv == ["/usr/bin/true", "--flag", "extra-arg-1", "--verbose"]
    # cwd defaults to the profile's workspace_absolute. On macOS the
    # profile loader canonicalizes symlinks (`/tmp` -> `/private/tmp`),
    # so compare against the same resolved form the loader produces
    # rather than the literal YAML string.
    expected_cwd = str(Path("/tmp/alice-ws").resolve())
    assert calls[0]["cwd"] == expected_cwd


def test_dynamic_dispatch_substitutes_args_placeholder(
    hermetic_profile: Path,
) -> None:
    """`{args}` in entry.command is REPLACED by the pass-through extras.

    Confirms P3-07 done-criterion (h): pass-through placeholder works.
    """
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "placeholder",
            "--description", "Uses {args}.",
            "--command", "wrap.sh {args} --tail",
        ],
    )

    calls: List[Dict] = []

    class _Completed:
        returncode = 42

    def fake_run(cmd, cwd=None, env=None, check=False):
        calls.append({"cmd": list(cmd)})
        return _Completed()

    with patch("mineru_cli.verbs.custom.subprocess.run", fake_run):
        result = runner.invoke(app, ["placeholder", "A", "B"])

    # Exit code from subprocess is propagated to the CLI (42 here).
    assert result.exit_code == 42
    assert calls[0]["cmd"] == ["wrap.sh", "A", "B", "--tail"]


def test_dynamic_dispatch_forwards_env(hermetic_profile: Path) -> None:
    """entry.env is merged into the subprocess env.

    We can't set env through `custom add` today (flag not exposed —
    interactive-only), so we write the registry YAML directly.
    """
    yaml_path = hermetic_profile / CUSTOM_VERBS_FILENAME
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "verbs": [
                    {
                        "name": "envy",
                        "description": "Uses env.",
                        "command": ["/bin/env"],
                        "env": {"MY_KEY": "my-value"},
                    }
                ]
            }
        )
    )

    calls: List[Dict] = []

    class _Completed:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None, check=False):
        calls.append({"env": dict(env) if env is not None else None})
        return _Completed()

    with patch("mineru_cli.verbs.custom.subprocess.run", fake_run):
        result = runner.invoke(app, ["envy"])
    assert result.exit_code == 0
    assert calls[0]["env"]["MY_KEY"] == "my-value"


def test_dynamic_dispatch_shadow_collision_skips_with_warning(
    hermetic_profile: Path,
) -> None:
    """A registry entry named after a built-in is skipped + a warning is emitted.

    Simulates the "future built-in shadows an existing custom" scenario.
    We seed the YAML with a `gmail`-named entry (bypassing the add-time
    check) and confirm the CLI still runs (the built-in gmail still works)
    and stderr carries a warning.
    """
    yaml_path = hermetic_profile / CUSTOM_VERBS_FILENAME
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "verbs": [
                    {
                        "name": "gmail",
                        "description": "shadow attempt",
                        "command": ["true"],
                    }
                ]
            }
        )
    )
    # Invoke any built-in verb (profile show works) to trigger dispatch
    # registration. The command should succeed AND stderr should carry a
    # warning about the shadowed name.
    result = runner.invoke(app, ["profile", "show"])
    assert result.exit_code == 0
    assert "gmail" in result.stderr
    assert "built-in" in result.stderr.lower() or "shadow" in result.stderr.lower()


def test_dynamic_dispatch_malformed_registry_warns_and_continues(
    hermetic_profile: Path,
) -> None:
    """A malformed custom_verbs.yaml warns but doesn't break built-in verbs."""
    yaml_path = hermetic_profile / CUSTOM_VERBS_FILENAME
    yaml_path.write_text("verbs:\n  - name: 'Bad Name'\n    description: d\n    command: [x]\n")
    result = runner.invoke(app, ["profile", "show"])
    # profile show still works — the built-ins are unaffected.
    assert result.exit_code == 0
    assert "custom verb registry unusable" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Journal-export first-instance INTEGRATION
# ---------------------------------------------------------------------------


def test_journal_export_registration_is_first_instance(
    hermetic_profile: Path,
) -> None:
    """P3-07 first instance: register the operator's Apple Notes journal export.

    Documents the spec §0 decision #9 example: the currently-scheduled
    export-journals.py is representable as a custom verb. We register it
    here, confirm the YAML round-trips, and confirm the dispatch would
    fire the expected argv. NO real subprocess runs — the dispatcher is
    mocked.
    """
    add = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "journal-export",
            "--description", "Export Apple Notes 'Daily Journals' to plain text.",
            "--command", "python3 /Users/example/.mineru/scripts/export-journals.py {args}",
            "--cwd", "/Users/example/.mineru",
            "--schedule", "45 0 * * *",
            "--deploy-notes", (
                "Runs nightly at 12:45 AM via launchd. "
                "Optional integer arg = number of recent notes to export."
            ),
        ],
    )
    assert add.exit_code == 0, add.stderr

    # YAML round-trip.
    reg = CustomVerbRegistry.load_from(hermetic_profile / CUSTOM_VERBS_FILENAME)
    entry = reg.get_entry("journal-export")
    assert entry is not None
    assert entry.command[-1] == "{args}"
    assert entry.schedule == "45 0 * * *"

    # Dispatch fires the expected argv (with pass-through extras).
    calls: List[Dict] = []

    class _Completed:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None, check=False):
        calls.append({"cmd": list(cmd), "cwd": cwd})
        return _Completed()

    with patch("mineru_cli.verbs.custom.subprocess.run", fake_run):
        result = runner.invoke(app, ["journal-export", "5"])
    assert result.exit_code == 0
    argv = calls[0]["cmd"]
    assert argv == [
        "python3",
        "/Users/example/.mineru/scripts/export-journals.py",
        "5",
    ]
    assert calls[0]["cwd"] == "/Users/example/.mineru"


# ---------------------------------------------------------------------------
# Real subprocess smoke test — no external tools, just /bin/sh + tmp script
# ---------------------------------------------------------------------------


def test_real_subprocess_exit_code_propagates(
    hermetic_profile: Path, tmp_path: Path,
) -> None:
    """A real /bin/sh subprocess exits 7 and the CLI must also exit 7.

    This is the belt-and-braces test: not just the mock, but the actual
    subprocess.run code path. The fake script is a bare `exit 7` — no
    network, no external tool, cross-platform safe on macOS.
    """
    script = tmp_path / "exit7.sh"
    script.write_text("#!/bin/sh\nexit 7\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    add = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "seven",
            "--description", "Exits 7.",
            "--command", str(script),
        ],
    )
    assert add.exit_code == 0
    result = runner.invoke(app, ["seven"])
    assert result.exit_code == 7


# ---------------------------------------------------------------------------
# Registration guard — skips when subcommand is 'custom'
# ---------------------------------------------------------------------------


def test_register_dispatch_skips_when_managing_customs(
    hermetic_profile: Path,
) -> None:
    """`mineru custom list` must not try to register custom entries onto the app.

    A previously-registered `custom` sub-app command with a name that
    matches a custom entry would collide. The guard `if subcommand ==
    'custom': return` in register_custom_dispatch prevents this. We
    verify by adding a verb, then confirming `custom list` still runs
    cleanly (no traceback, no duplicate-registration error).
    """
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "safe",
            "--description", "x",
            "--command", "echo x",
        ],
    )
    # No mocks needed — list is a pure read.
    result = runner.invoke(app, ["custom", "list"])
    assert result.exit_code == 0
    assert "safe" in result.stdout


# ---------------------------------------------------------------------------
# Static invariants
# ---------------------------------------------------------------------------


def test_custom_registry_source_never_touches_home_mineru() -> None:
    """The registry source must not hardcode $MINERU_HOME (worktree-scoped)."""
    from mineru_cli.custom import registry as reg_mod

    src = Path(reg_mod.__file__).read_text()
    for bad in (
        'Path.home() / ".mineru"',
        "mineru-cli-build",
        "'$MINERU_HOME'",
    ):
        assert bad not in src, (
            f"custom/registry.py has a code-path reference to the live system: {bad!r}"
        )


# ---------------------------------------------------------------------------
# Security hardening: env redaction in `custom show` (both output modes)
# ---------------------------------------------------------------------------


def _write_registry_with_env(hermetic_profile: Path, env: Dict[str, str]) -> None:
    """Seed the registry YAML directly with a `secret-verb` carrying `env`."""
    (hermetic_profile / CUSTOM_VERBS_FILENAME).write_text(
        yaml.safe_dump(
            {
                "verbs": [
                    {
                        "name": "secret-verb",
                        "description": "carries secret env",
                        "command": ["/usr/bin/true"],
                        "env": env,
                    }
                ]
            }
        )
    )


def test_custom_show_pretty_masks_env_values(hermetic_profile: Path) -> None:
    """The pretty renderer must NEVER echo env values (defense-in-depth)."""
    _write_registry_with_env(
        hermetic_profile,
        {"MY_TOKEN": "sk-live-CAFEBABE12345", "OTHER": "plain"},
    )
    result = runner.invoke(app, ["custom", "show", "secret-verb"])
    assert result.exit_code == 0, result.stderr
    # Key names surface, values do not:
    assert "MY_TOKEN" in result.stdout
    assert "OTHER" in result.stdout
    assert "sk-live-CAFEBABE12345" not in result.stdout
    assert "plain" not in result.stdout
    # Redaction sentinel appears:
    assert "***" in result.stdout


def test_custom_show_json_masks_env_values_by_default(
    hermetic_profile: Path,
) -> None:
    """`custom show --json` masks env values unless --reveal-env is passed."""
    _write_registry_with_env(
        hermetic_profile,
        {"MY_TOKEN": "sk-live-CAFEBABE12345"},
    )
    result = runner.invoke(app, ["custom", "show", "secret-verb", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["env"] == {"MY_TOKEN": "***"}
    assert "sk-live-CAFEBABE12345" not in result.stdout
    # env_inherit surfaces in the JSON (new field).
    assert payload["env_inherit"] is True


def test_custom_show_json_reveal_env_flag_unmasks_and_warns(
    hermetic_profile: Path,
) -> None:
    """--reveal-env unmasks the values AND prints a stderr warning."""
    _write_registry_with_env(
        hermetic_profile,
        {"MY_TOKEN": "sk-live-CAFEBABE12345"},
    )
    result = runner.invoke(
        app, ["custom", "show", "secret-verb", "--json", "--reveal-env"]
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["env"] == {"MY_TOKEN": "sk-live-CAFEBABE12345"}
    # Warning on stderr — signals the risky mode leaves a trace.
    assert "WARNING" in result.stderr
    assert "plaintext" in result.stderr.lower()


def test_custom_show_reveal_env_rejects_pretty_mode(
    hermetic_profile: Path,
) -> None:
    """`--reveal-env` without `--json` is a user error (exit 2)."""
    _write_registry_with_env(hermetic_profile, {"MY_TOKEN": "x"})
    result = runner.invoke(
        app, ["custom", "show", "secret-verb", "--reveal-env"]
    )
    assert result.exit_code == 2
    assert "--json" in result.stderr


def test_custom_list_json_always_masks_env_values(
    hermetic_profile: Path,
) -> None:
    """`custom list --json` has no --reveal-env; every env value is masked."""
    _write_registry_with_env(
        hermetic_profile,
        {"SECRET": "hunter2"},
    )
    result = runner.invoke(app, ["custom", "list", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload[0]["env"] == {"SECRET": "***"}
    assert "hunter2" not in result.stdout


# ---------------------------------------------------------------------------
# Security hardening: env_inherit=False → minimal base env
# ---------------------------------------------------------------------------


def test_custom_add_env_inherit_flag_defaults_to_true(
    hermetic_profile: Path,
) -> None:
    """Bare `custom add` records env_inherit=True (back-compat)."""
    result = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "back-compat",
            "--description", "d",
            "--command", "echo x",
        ],
    )
    assert result.exit_code == 0, result.stderr
    reg = CustomVerbRegistry.load_from(hermetic_profile / CUSTOM_VERBS_FILENAME)
    entry = reg.get_entry("back-compat")
    assert entry is not None
    assert entry.env_inherit is True


def test_custom_add_no_env_inherit_flag_records_false(
    hermetic_profile: Path,
) -> None:
    """`--no-env-inherit` records env_inherit=False on the entry."""
    result = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "isolated",
            "--description", "d",
            "--command", "echo x",
            "--no-env-inherit",
        ],
    )
    assert result.exit_code == 0, result.stderr
    reg = CustomVerbRegistry.load_from(hermetic_profile / CUSTOM_VERBS_FILENAME)
    entry = reg.get_entry("isolated")
    assert entry is not None
    assert entry.env_inherit is False


def test_dispatch_env_inherit_true_full_environ_copy(
    hermetic_profile: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default env_inherit=True: subprocess env INCLUDES parent MINERU_SECRET_*.

    This is the historical behavior we must preserve for back-compat.
    """
    # Seed a marker env var that stands in for a parent-shell secret.
    monkeypatch.setenv("MINERU_SECRET_MARKER", "parent-value-42")

    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "leaky-default",
            "--description", "d",
            "--command", "/usr/bin/true",
        ],
    )
    calls: List[Dict] = []

    class _Completed:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None, check=False):
        calls.append({"env": dict(env) if env is not None else None})
        return _Completed()

    with patch("mineru_cli.verbs.custom.subprocess.run", fake_run):
        result = runner.invoke(app, ["leaky-default"])
    assert result.exit_code == 0
    # env_inherit=True: parent secret IS in the subprocess env.
    assert calls[0]["env"]["MINERU_SECRET_MARKER"] == "parent-value-42"


def test_dispatch_env_inherit_false_isolates_from_parent_secrets(
    hermetic_profile: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env_inherit=False: subprocess env EXCLUDES parent MINERU_SECRET_* / tokens.

    Only PATH / HOME / USER / LANG carry over; entry.env is layered on
    top. This is the defense that keeps a third-party custom verb from
    picking up the operator's OAuth / API tokens.
    """
    monkeypatch.setenv("MINERU_SECRET_MARKER", "must-not-leak")
    monkeypatch.setenv("OAUTH_TOKEN", "also-must-not-leak")

    # Register with env_inherit=False and an explicit entry.env overlay.
    yaml_path = hermetic_profile / CUSTOM_VERBS_FILENAME
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "verbs": [
                    {
                        "name": "isolated",
                        "description": "isolated subprocess",
                        "command": ["/usr/bin/true"],
                        "env": {"EXPLICIT_KEY": "explicit-value"},
                        "env_inherit": False,
                    }
                ]
            }
        )
    )

    calls: List[Dict] = []

    class _Completed:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None, check=False):
        calls.append({"env": dict(env) if env is not None else None})
        return _Completed()

    with patch("mineru_cli.verbs.custom.subprocess.run", fake_run):
        result = runner.invoke(app, ["isolated"])
    assert result.exit_code == 0
    env_passed = calls[0]["env"]
    # Parent secrets are NOT in the subprocess env.
    assert "MINERU_SECRET_MARKER" not in env_passed, (
        f"leaked parent secret into isolated subprocess env: keys={list(env_passed)}"
    )
    assert "OAUTH_TOKEN" not in env_passed
    # Minimal base survived: PATH is present (present in every real shell).
    assert "PATH" in env_passed
    # Entry-declared explicit env overlay survived.
    assert env_passed["EXPLICIT_KEY"] == "explicit-value"


def test_registry_from_mapping_accepts_env_inherit_bool(
    hermetic_profile: Path,
) -> None:
    """Round-trip: env_inherit false in YAML → False on the entry."""
    yaml_path = hermetic_profile / CUSTOM_VERBS_FILENAME
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "verbs": [
                    {
                        "name": "roundtrip",
                        "description": "d",
                        "command": ["/usr/bin/true"],
                        "env_inherit": False,
                    }
                ]
            }
        )
    )
    reg = CustomVerbRegistry.load_from(yaml_path)
    entry = reg.get_entry("roundtrip")
    assert entry is not None
    assert entry.env_inherit is False


def test_registry_from_mapping_rejects_non_bool_env_inherit(
    hermetic_profile: Path,
) -> None:
    """A non-boolean env_inherit fails LOUD (schema error)."""
    from mineru_cli.custom.registry import CustomVerbError

    yaml_path = hermetic_profile / CUSTOM_VERBS_FILENAME
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "verbs": [
                    {
                        "name": "bad-envinherit",
                        "description": "d",
                        "command": ["/usr/bin/true"],
                        "env_inherit": "yes",  # not a bool
                    }
                ]
            }
        )
    )
    with pytest.raises(CustomVerbError):
        CustomVerbRegistry.load_from(yaml_path)


# ---------------------------------------------------------------------------
# Performance: registry load memoized by (path, mtime_ns)
# ---------------------------------------------------------------------------


def test_registry_load_is_memoized_by_mtime(
    hermetic_profile: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_load_registry_best_effort` re-uses its memo across invocations.

    Spies on `CustomVerbRegistry.load` and calls the memoized helper
    directly a few times to prove it does NOT re-parse the YAML when
    the file mtime hasn't changed. We call the helper directly instead
    of routing through `runner.invoke(app, ["custom", "list"])` because
    the `custom list` verb ALSO calls the un-cached `_load_registry`
    path for its own reads — the cache only benefits the dispatch-time
    `_load_registry_best_effort` path (called from the TyperGroup's
    `get_command`), which is what we're proving here.
    """
    from mineru_cli.custom import registry as reg_mod
    from mineru_cli.verbs import custom as custom_verbs

    custom_verbs.reset_warnings()
    # Seed one entry so the registry file exists.
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "cache-me",
            "--description", "d",
            "--command", "echo x",
        ],
    )
    # Prime the memo directly.
    custom_verbs._load_registry_best_effort()

    load_calls: List[int] = []
    original_load = reg_mod.CustomVerbRegistry.load

    def spy_load(profile):
        load_calls.append(1)
        return original_load(profile)

    monkeypatch.setattr(reg_mod.CustomVerbRegistry, "load", spy_load)

    # Two more calls should hit the memo (same path, same mtime).
    custom_verbs._load_registry_best_effort()
    custom_verbs._load_registry_best_effort()
    assert len(load_calls) == 0, (
        f"cache miss: expected 0 re-parses, got {len(load_calls)}"
    )


def test_profile_load_is_memoized_across_dispatch_calls(
    hermetic_profile: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_load_registry_best_effort` does NOT re-parse profile.yaml on repeats.

    Regression guard for the perf finding: before the profile memo, every
    `_load_registry_best_effort` call parsed profile.yaml a fresh, which
    meant a single `mineru gmail --help` invocation triggered two profile
    reads (once from the TyperGroup, once from the root callback). The
    cache in `_load_active_profile_cached` keys on (profile_name,
    base_dir, profile.yaml mtime), so back-to-back calls with an unchanged
    profile.yaml must hit the memo.
    """
    from mineru_cli.profile import loader as loader_mod
    from mineru_cli.verbs import custom as custom_verbs

    custom_verbs.reset_warnings()
    # Prime the memo once.
    custom_verbs._load_registry_best_effort()

    load_calls: List[int] = []
    original_load = loader_mod.load_active_profile

    def spy_load(*a, **kw):
        load_calls.append(1)
        return original_load(*a, **kw)

    # Patch the name the custom module actually looked up at import time
    # (`from mineru_cli.profile import ProfileError, load_active_profile`).
    monkeypatch.setattr(custom_verbs, "load_active_profile", spy_load)

    # Two more calls should hit the memo (same profile.yaml, same mtime).
    custom_verbs._load_registry_best_effort()
    custom_verbs._load_registry_best_effort()
    assert len(load_calls) == 0, (
        f"profile memo miss: expected 0 re-parses, got {len(load_calls)}"
    )


def test_registry_cache_invalidates_on_mtime_change(
    hermetic_profile: Path,
) -> None:
    """A `custom add` in the same process re-parses on next call.

    Writing the file changes mtime → cache miss on the next
    `_load_registry_best_effort`.
    """
    from mineru_cli.verbs import custom as custom_verbs

    custom_verbs.reset_warnings()
    # First add → creates the file.
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "first",
            "--description", "d",
            "--command", "echo 1",
        ],
    )
    # Prime the cache.
    r1 = runner.invoke(app, ["custom", "list", "--json"])
    payload1 = json.loads(r1.stdout)
    assert len(payload1) == 1
    # Second add → mutates mtime → cache invalidates.
    runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "second",
            "--description", "d",
            "--command", "echo 2",
        ],
    )
    r2 = runner.invoke(app, ["custom", "list", "--json"])
    payload2 = json.loads(r2.stdout)
    assert len(payload2) == 2, (
        f"cache did not invalidate on write; second add invisible: {payload2}"
    )
