"""P3-08 tests — journal-export as custom verb instance #1 + onboarding polish.

Covers three things the P3-08 done-criteria pin:

  (a) The seeded `profiles/mineru/custom_verbs.yaml` loads via
      `CustomVerbRegistry.load(profile)` and exposes a `journal-export`
      entry with the exact schedule / command / cwd the spec requires.
  (b) `mineru journal-export --help` renders in a CliRunner without
      hitting Apple Notes (subprocess.run is patched so no JXA /
      osascript ever fires -- a real invocation would launch Notes.app
      on the developer's Mac).
  (c) A CliRunner dispatch through `mineru journal-export <extra>`
      shells the seeded argv template with the `{args}` placeholder
      replaced by the trailing extras, in the order the spec pins.

Plus the onboarding polish:

  (d) `mineru custom list` shows `journal-export` in its output when the
      seeded profile is active.
  (e) The `render_add_summary` block for a schedule of `45 0 * * *`
      renders the invocation line (`mineru journal-export`), the human
      phrase (`daily at 00:45`), and the two follow-up options (Phase 4
      cron.yaml recommendation + manual plist fallback).

⚠️ SAFETY DISCIPLINE ⚠️

  * `subprocess.run` inside `mineru_cli.verbs.custom` is PATCHED in
    every CliRunner test that dispatches the verb. No real osascript
    invocation is ever made from CI.

  * The tests set both `MINERU_PROFILE_ROOT` and `MINERU_PROFILE` via
    monkeypatch so the resolver is pinned at the real worktree seed
    (`profiles/mineru/`) regardless of any ambient env pollution.

  * `custom.reset_warnings()` runs once per test through an autouse
    fixture so the once-per-process shadow-collision warning gate does
    not carry state across tests when the module was already imported
    by an earlier suite (e.g. test_custom_verbs.py).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.custom.registry import (
    CUSTOM_VERBS_FILENAME,
    CustomVerbEntry,
    CustomVerbRegistry,
)
from mineru_cli.profile import load_active_profile
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
)
from mineru_cli.verbs import custom as custom_verbs

# Runner default: newer Typer versions removed the `mix_stderr` param, so
# the CliRunner separates stdout / stderr onto `.stdout` / `.stderr`.
runner = CliRunner()


# The worktree-scoped paths the P3-08 task pins verbatim. Kept as module
# constants so a rename downstream fails LOUD instead of drifting silently.
# Physical location of the synthetic `mineru` seed shipped under
# tests/fixtures/ (the engine repo ships no live `profiles/` tree).
SEED_PROFILES_DIR = Path(__file__).resolve().parent / "fixtures" / "seed_profile_base"
SEED_PROFILE_DIR = SEED_PROFILES_DIR / "mineru"
SEEDED_REGISTRY_PATH = SEED_PROFILE_DIR / CUSTOM_VERBS_FILENAME
# The workspace + journal-export command path the seed DECLARES (synthetic;
# matches the shipped custom_verbs.yaml). These are asserted against the
# loaded entry, NOT read from disk.
WORKTREE_ROOT = Path("/Users/example/.mineru")
SEEDED_EXPORT_SCRIPT = WORKTREE_ROOT / "scripts" / "export-journals.py"
# The REAL export-journals.py in the engine repo, read by the osascript-safety
# test to confirm the script still targets Notes.app.
REAL_EXPORT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "export-journals.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_shadow_warnings() -> None:
    """Clear the once-per-process warning gate before every test.

    The dispatcher's `_WARNED_MESSAGES` set is process-scoped by design
    (so a busy invocation doesn't double-print), but that means test
    ordering can hide a warning under the "already emitted" branch.
    Resetting up-front makes each test independent.
    """
    custom_verbs.reset_warnings()


@pytest.fixture
def pinned_seed_profile(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the loader at the real worktree seed profile.

    Returns the profile directory (`$MINERU_HOME/profiles/mineru`).
    Every CliRunner test that touches the registry uses this fixture so
    the discovery path is deterministic no matter what env vars the shell
    inherited (a stray `MINERU_PROFILE=alice` from a sibling test suite
    would otherwise silently swap the file the CLI reads).

    We also unset `MINERU_CUSTOM_VERBS_ROOT` so it falls back to the
    profile-derived path (`<profile_root>/custom_verbs.yaml`), which is
    exactly where the P3-08 seed lives.
    """
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(SEED_PROFILES_DIR))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "mineru")
    monkeypatch.delenv("MINERU_CUSTOM_VERBS_ROOT", raising=False)
    return SEED_PROFILE_DIR


# ---------------------------------------------------------------------------
# (a) Seeded YAML round-trips through the registry loader
# ---------------------------------------------------------------------------


def test_seeded_registry_file_exists_on_disk() -> None:
    """The seed file must be committed at the exact worktree path."""
    assert SEEDED_REGISTRY_PATH.exists(), (
        f"P3-08 seed missing: expected {SEEDED_REGISTRY_PATH} to exist "
        "with a `journal-export` entry."
    )


def test_seeded_journal_export_appears_in_registry(
    pinned_seed_profile: Path,
) -> None:
    """P3-08 done-criterion (a): registry.list_entries() includes journal-export.

    Loads the profile via the real loader (no mocks) so we exercise the
    same code path that `mineru <verb>` will take in production.
    """
    profile = load_active_profile(None)
    registry = CustomVerbRegistry.load(profile)
    names = [e.name for e in registry.list_entries()]
    assert "journal-export" in names, (
        f"seeded registry loaded {names!r}; expected `journal-export` to "
        "be present."
    )


def test_seeded_entry_shape_matches_p3_08_contract(
    pinned_seed_profile: Path,
) -> None:
    """Every load-bearing field on the seeded entry is asserted individually.

    Pinned explicitly (not a snapshot-diff) so a copy tweak in the YAML
    that changes ONLY the description surfaces as a specific line
    failure, not a whole-file blob mismatch.
    """
    profile = load_active_profile(None)
    registry = CustomVerbRegistry.load(profile)
    entry = registry.get_entry("journal-export")
    assert entry is not None
    # Description mentions the *what* + *where* per the task spec.
    assert "Apple Notes" in entry.description
    assert "Daily Journals" in entry.description
    assert "journal_exports/" in entry.description
    # argv template: script path, then the `{args}` placeholder in order.
    assert entry.command == (
        str(SEEDED_EXPORT_SCRIPT),
        "{args}",
    )
    # cwd is the worktree workspace_absolute (absolute path, matches
    # the seeded profile's `workspace_absolute`).
    assert entry.cwd == str(WORKTREE_ROOT)
    # Informational schedule -- matches the 12:45 AM row in TECHNICAL.md.
    assert entry.schedule == "45 0 * * *"
    # deploy_notes points the operator at the launchd plist that Phase 4
    # will materialize; must reference both the plist path AND
    # `mineru cron install` so future-you knows where the recipe lives.
    assert entry.deploy_notes is not None
    assert "com.mineru.export-journals.plist" in entry.deploy_notes
    assert "mineru cron install" in entry.deploy_notes


# ---------------------------------------------------------------------------
# (b) `mineru journal-export --help` renders without spawning JXA
# ---------------------------------------------------------------------------


def test_mineru_journal_export_help_renders_without_shellout(
    pinned_seed_profile: Path,
) -> None:
    """P3-08 done-criterion (b): `--help` renders without touching Apple Notes.

    The dispatcher's `subprocess.run` is patched to a sentinel that
    fails LOUD if anything calls it. A `--help` invocation must never
    reach the callback -- Click short-circuits at option parsing --
    so the mock stays untouched at the end.
    """
    call_log: List[Dict[str, Any]] = []

    def _forbid_run(*args: Any, **kwargs: Any) -> None:
        call_log.append({"args": args, "kwargs": kwargs})
        raise AssertionError(
            "subprocess.run was invoked during a `--help` render -- the "
            "help short-circuit is regressed and a real osascript call "
            "would fire on the developer's Mac."
        )

    with patch("mineru_cli.verbs.custom.subprocess.run", _forbid_run):
        result = runner.invoke(app, ["journal-export", "--help"])

    assert result.exit_code == 0, result.stderr
    # The entry's description is used as the Click short help; assert on
    # a distinctive substring so a rename in the seed fails loud.
    assert "Apple Notes" in result.stdout
    assert call_log == []


# ---------------------------------------------------------------------------
# (c) Dispatch with mocked subprocess.run: argv matches the seeded template
# ---------------------------------------------------------------------------


def test_mineru_journal_export_dispatch_argv_and_cwd(
    pinned_seed_profile: Path,
) -> None:
    """P3-08 done-criterion (c): argv[0] is the script + extras appended.

    We assert every load-bearing dispatch invariant in one place:
      * argv[0] is the seeded script path (no `python3` prefix).
      * The `{args}` placeholder is REPLACED (not appended AFTER a
        literal `{args}` token) by the trailing CliRunner extras.
      * cwd matches the seeded workspace directory.
      * env includes the current process env (dispatcher merges, does
        not replace, so PATH remains visible).
    """
    calls: List[Dict[str, Any]] = []

    class _Completed:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None, check=False):
        calls.append({"cmd": list(cmd), "cwd": cwd, "env": env})
        return _Completed()

    with patch("mineru_cli.verbs.custom.subprocess.run", fake_run):
        result = runner.invoke(app, ["journal-export", "5", "--verbose"])

    assert result.exit_code == 0, result.stderr
    assert len(calls) == 1
    argv = calls[0]["cmd"]
    # argv[0] is the SEEDED script path -- the P3-08 contract is that
    # `{args}` expands to the extras and is NOT itself in argv.
    assert argv[0] == str(SEEDED_EXPORT_SCRIPT)
    # Extras appear in the exact order the operator passed them, in place
    # of the `{args}` placeholder.
    assert argv == [
        str(SEEDED_EXPORT_SCRIPT),
        "5",
        "--verbose",
    ]
    # cwd -- the seeded absolute path.
    assert calls[0]["cwd"] == str(WORKTREE_ROOT)
    # env: the dispatcher copies os.environ + merges the entry's own env
    # (empty here). We only assert on a well-known key that must survive.
    assert calls[0]["env"] is not None
    assert "PATH" in calls[0]["env"]


def test_mineru_journal_export_dispatch_no_extras(
    pinned_seed_profile: Path,
) -> None:
    """`{args}` with zero extras -> the placeholder is silently dropped.

    The dispatcher iterates entry.command and REPLACES `{args}` with
    whatever's in ctx.args. When there are no extras, the replacement
    is an empty list and the placeholder disappears from argv. Assert
    the resulting argv has NO literal `{args}` string surviving.
    """
    calls: List[Dict[str, Any]] = []

    class _Completed:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None, check=False):
        calls.append({"cmd": list(cmd)})
        return _Completed()

    with patch("mineru_cli.verbs.custom.subprocess.run", fake_run):
        result = runner.invoke(app, ["journal-export"])

    assert result.exit_code == 0, result.stderr
    argv = calls[0]["cmd"]
    assert argv == [str(SEEDED_EXPORT_SCRIPT)]
    assert "{args}" not in argv, (
        "the `{args}` placeholder leaked into the shelled-out argv -- "
        "the dispatcher's substitution regressed."
    )


# ---------------------------------------------------------------------------
# (d) `mineru custom list` shows journal-export
# ---------------------------------------------------------------------------


def test_mineru_custom_list_shows_journal_export(
    pinned_seed_profile: Path,
) -> None:
    """P3-08 done-criterion: `custom list` renders the seeded entry."""
    result = runner.invoke(app, ["custom", "list"])
    assert result.exit_code == 0, result.stderr
    assert "journal-export" in result.stdout
    # Schedule column carries the raw cron string (the human phrase
    # only shows up in the `add`-time summary, per the task's spec).
    assert "45 0 * * *" in result.stdout


def test_mineru_custom_show_journal_export_prints_full_entry(
    pinned_seed_profile: Path,
) -> None:
    """`custom show journal-export` renders the full entry + invocation preview."""
    result = runner.invoke(app, ["custom", "show", "journal-export"])
    assert result.exit_code == 0, result.stderr
    assert "journal-export" in result.stdout
    assert str(SEEDED_EXPORT_SCRIPT) in result.stdout
    assert "45 0 * * *" in result.stdout
    # Invocation preview line -- the operator's copy-paste target.
    assert "mineru journal-export" in result.stdout


# ---------------------------------------------------------------------------
# (e) Onboarding summary block -- unit-tested against a fixed input
# ---------------------------------------------------------------------------


def _fixed_journal_entry() -> CustomVerbEntry:
    """Fixed CustomVerbEntry that mirrors the on-disk seed 1:1.

    Reused across every onboarding-summary test so a copy tweak that
    changes ONLY the summary layout (not the seed) is what fails.
    """
    return CustomVerbEntry(
        name="journal-export",
        description=(
            "Export the operator's Apple Notes Daily Journals folder to plain "
            "text (atomic swap into journal_exports/)."
        ),
        command=(str(SEEDED_EXPORT_SCRIPT), "{args}"),
        cwd=str(WORKTREE_ROOT),
        schedule="45 0 * * *",
        deploy_notes=(
            "Nightly launchd job at 00:45 America/Los_Angeles.\n"
            "Target plist: ~/Library/LaunchAgents/com.mineru.export-journals.plist"
        ),
        env={},
    )


def test_humanize_schedule_daily_at_00_45() -> None:
    """`45 0 * * *` renders as the exact phrase the P3-08 spec pins."""
    assert custom_verbs.humanize_schedule("45 0 * * *") == "daily at 00:45"


def test_humanize_schedule_weekly_and_monthly_and_fallback() -> None:
    """Weekly / monthly patterns render the expected English; unknown falls through."""
    assert custom_verbs.humanize_schedule("30 9 * * 1") == "weekly on Monday at 09:30"
    assert (
        custom_verbs.humanize_schedule("0 7 15 * *")
        == "monthly on the 15th at 07:00"
    )
    # Cron accepts 7 as Sunday.
    assert (
        custom_verbs.humanize_schedule("0 12 * * 7")
        == "weekly on Sunday at 12:00"
    )
    # Ranges / lists / steps -> raw fallback.
    assert custom_verbs.humanize_schedule("*/5 * * * *") == "*/5 * * * *"
    assert custom_verbs.humanize_schedule("0 9-17 * * 1-5") == "0 9-17 * * 1-5"
    # Wrong field count -> raw fallback.
    assert custom_verbs.humanize_schedule("45 0 * *") == "45 0 * *"
    # Empty / non-string -> raw fallback.
    assert custom_verbs.humanize_schedule("") == ""


def test_render_add_summary_pins_all_three_p3_08_items() -> None:
    """Fixed-input regression: the P3-08 summary block has invocation, human
    phrase, and both scheduling options.

    Uses `SUMMARY_*_PREFIX` module constants so a stylistic tweak that
    changes the trailing text stays green (the operator's contract is
    the LEADING prefix + the payload it carries), while a rename ("*
    invoke:" -> "* run:") fails loud.
    """
    entry = _fixed_journal_entry()
    lines = custom_verbs.render_add_summary(
        entry,
        target_path="/tmp/pretend/registry.yaml",
        profile_name="mineru",
    )
    joined = "\n".join(lines)

    # Header + exact number of load-bearing marker prefixes present.
    assert lines[0] == "next steps:"

    # (1) invocation line: `mineru journal-export` (no [...args] noise).
    invoke_lines = [l for l in lines if l.startswith(custom_verbs.SUMMARY_INVOKE_PREFIX)]
    assert invoke_lines == [f"{custom_verbs.SUMMARY_INVOKE_PREFIX}mineru journal-export"]

    # (2) schedule line: the raw cron string + the human phrase in parens.
    schedule_lines = [l for l in lines if l.startswith(custom_verbs.SUMMARY_SCHEDULE_PREFIX)]
    assert schedule_lines == [
        f"{custom_verbs.SUMMARY_SCHEDULE_PREFIX}45 0 * * *  (daily at 00:45)"
    ]

    # (3) two follow-up options -- header + option A + option B all present.
    assert custom_verbs.SUMMARY_SCHEDULE_HEADER in lines
    option_a_lines = [
        l for l in lines if l.startswith(custom_verbs.SUMMARY_SCHEDULE_OPTION_A_PREFIX)
    ]
    option_b_lines = [
        l for l in lines if l.startswith(custom_verbs.SUMMARY_SCHEDULE_OPTION_B_PREFIX)
    ]
    assert len(option_a_lines) == 1
    assert len(option_b_lines) == 1
    # Option A must reference cron.yaml (Phase 4 auto-install path) AND
    # the profile name that was passed in.
    assert "profiles/mineru/cron.yaml" in option_a_lines[0]
    assert "mineru cron install" in option_a_lines[0]
    # Option B references deploy_notes since the fixed entry has them.
    assert "deploy_notes" in option_b_lines[0]

    # File-write receipt line -- the operator's cat target.
    file_lines = [l for l in lines if l.startswith(custom_verbs.SUMMARY_FILE_PREFIX)]
    assert file_lines == [
        f"{custom_verbs.SUMMARY_FILE_PREFIX}/tmp/pretend/registry.yaml"
    ]

    # Cheap end-to-end check the block is human-readable (no accidental
    # `None` / repr leaks).
    assert "None" not in joined


def test_render_add_summary_no_schedule_branch() -> None:
    """No-schedule branch renders the fallback line + no options block.

    Guards against a regression that would silently print the schedule
    options for an entry with NO schedule -- the operator would be told
    to install a non-existent cron entry.
    """
    entry = CustomVerbEntry(
        name="quick-echo",
        description="Just an echo.",
        command=("echo", "hi"),
        cwd=None,
        schedule=None,
        deploy_notes=None,
    )
    lines = custom_verbs.render_add_summary(
        entry, target_path="/tmp/x.yaml", profile_name="alice"
    )
    # The no-schedule branch prints exactly one schedule line and NO
    # follow-up options header/pair.
    schedule_lines = [
        l for l in lines if l.startswith(custom_verbs.SUMMARY_SCHEDULE_PREFIX)
    ]
    assert len(schedule_lines) == 1
    assert "(none)" in schedule_lines[0]
    assert custom_verbs.SUMMARY_SCHEDULE_HEADER not in lines
    assert not [
        l for l in lines if l.startswith(custom_verbs.SUMMARY_SCHEDULE_OPTION_A_PREFIX)
    ]


def test_render_add_summary_no_deploy_notes_branch_names_launchd_path() -> None:
    """When deploy_notes is empty, option B still tells the operator WHERE.

    The manual-plist option must include the launchd-agents directory
    and the verb name so the operator knows what file to create even
    if they haven't back-filled deploy_notes yet.
    """
    entry = CustomVerbEntry(
        name="nightly-thing",
        description="Runs nightly.",
        command=("/usr/bin/true",),
        schedule="0 3 * * *",
        deploy_notes=None,
    )
    lines = custom_verbs.render_add_summary(
        entry, target_path="/tmp/x.yaml", profile_name="mineru"
    )
    option_b_lines = [
        l for l in lines if l.startswith(custom_verbs.SUMMARY_SCHEDULE_OPTION_B_PREFIX)
    ]
    assert len(option_b_lines) == 1
    body = option_b_lines[0]
    assert "~/Library/LaunchAgents/" in body
    assert "nightly-thing" in body


# ---------------------------------------------------------------------------
# `custom add` end-to-end: the polished summary lands in stdout
# ---------------------------------------------------------------------------


def test_custom_add_end_to_end_prints_polished_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full `custom add ... --schedule '45 0 * * *'` renders the polished summary.

    Uses a hermetic tmp profile (NOT the real mineru seed) so the write
    lands in tmp_path -- we don't want the test to touch the seeded
    file. Verifies stdout carries every summary marker prefix, plus
    the human-schedule phrase for the P3-08 example input.
    """
    # Build a schema-valid ephemeral profile.
    profile_dir = tmp_path / "test-user"
    profile_dir.mkdir()
    (profile_dir / "profile.yaml").write_text(
        "name: test-user\n"
        "display_name: Test\n"
        "assistant_name: TestBot\n"
        "timezone: America/Los_Angeles\n"
        "keychain_account: test-acct\n"
        "launchd_label_prefix: com.test\n"
        f"workspace_absolute: {profile_dir}\n"
        f"memory_root: {tmp_path}/memory\n"
        f"briefs_root: {tmp_path}/briefs\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        "  env_prefix: TEST_SECRET_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "test-user")
    monkeypatch.setenv("MINERU_CUSTOM_VERBS_ROOT", str(profile_dir))

    result = runner.invoke(
        app,
        [
            "custom", "add",
            "--name", "journal-export",
            "--description", "Export journals.",
            "--command", f"{SEEDED_EXPORT_SCRIPT} {{args}}",
            "--cwd", str(WORKTREE_ROOT),
            "--schedule", "45 0 * * *",
            "--deploy-notes", "Launchd at 00:45.",
        ],
    )
    assert result.exit_code == 0, result.stderr
    stdout = result.stdout
    assert "next steps:" in stdout
    assert f"{custom_verbs.SUMMARY_INVOKE_PREFIX}mineru journal-export" in stdout
    # The literal cron + human phrase are both present.
    assert "45 0 * * *  (daily at 00:45)" in stdout
    # Scheduling options are both offered.
    assert custom_verbs.SUMMARY_SCHEDULE_HEADER in stdout
    assert "profiles/test-user/cron.yaml" in stdout
    assert "deploy_notes" in stdout


# ---------------------------------------------------------------------------
# Safety: no test in this file ever hits osascript / Apple Notes
# ---------------------------------------------------------------------------


def test_no_real_osascript_ever_fires_in_this_module() -> None:
    """Static invariant: the export script reads the local Notes SQLite store
    and never drives Notes.app over AppleEvents.

    The dispatcher tests above all patch subprocess.run; this test pins that
    the real script has no osascript / JXA path left that an unpatched run
    could hit.
    """
    body = REAL_EXPORT_SCRIPT.read_text(encoding="utf-8")
    assert "NoteStore.sqlite" in body, (
        "export script no longer reads the Notes SQLite store -- update this "
        "safety test's expectation."
    )
    assert "osascript" not in body and "Application('Notes')" not in body, (
        "export script shells out to Notes.app again -- tests must mock it."
    )
