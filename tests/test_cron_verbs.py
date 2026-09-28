"""Tests for `mineru cron` read-only verbs (P4-03).

⚠️⚠️ HARD SAFETY POSTURE — READ TWICE ⚠️⚠️

  NO test in this file EVER writes to `~/Library/LaunchAgents`. Every
  test seeds `MINERU_LAUNCHD_DIR` to a `tmp_path` sub-directory so the
  real launchd files are neither read nor written.

  NO test in this file EVER invokes `launchctl`. The verbs under test
  do not call it either — a grep in this file for `launchctl` should
  be empty by contract.

  NO test in this file EVER runs a real recurring job. `mineru cron edit`
  is exercised with $EDITOR pointed at `/bin/true` so the subprocess
  returns immediately without opening a UI.

  NO test in this file EVER touches the live workspace at
  `$MINERU_HOME/`. Every profile fixture writes a workspace
  under `tmp_path`.

Coverage:

  * `mineru --help` still renders with the cron sub-app installed
    (LANDMINE smoke test).
  * `mineru cron --help` renders and enumerates the six read verbs.
  * `list` renders a table + `--json` payload against a small cron.yaml
    fixture and a tmp launchd dir. `live` and `enabled_mismatch`
    columns respond to plist presence/absence.
  * `status` shows humanized schedule, last-log ts, expected-output
    freshness (fresh + stale + not requested), diff summary vs
    installed plist.
  * `logs` tails the newest log file and rejects a missing directory
    with exit 1. Follow mode is NOT exercised (no infinite polling in
    the test suite).
  * `edit`:
      - refuses a path under the live `$MINERU_HOME/recurring/`
        (Phase 4 rewrite is gated).
      - refuses a `kind='script'` job (no instruction file).
      - happy path: `EDITOR=/bin/true` returns exit 0 against a
        workspace-scoped instruction file.
  * `plist`:
      - stdout mode prints the rendered XML.
      - `--out` outside the launchd dir writes the file.
      - `--out` inside the launchd dir is REFUSED with exit 2
        (LANDMINE guard).
  * `diff`:
      - "not installed" -> friendly header + full rendered body.
      - "clean" -> single-line "clean" note.
      - "differs" -> unified diff output.
  * Import-time side effects: importing `mineru_cli.verbs.cron` must
    NOT read `~/Library/LaunchAgents` and must NOT parse cron.yaml.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
)
from mineru_cli.verbs import cron as cron_verb


runner = CliRunner()


# ---------------------------------------------------------------------------
# Profile / cron.yaml / launchd-dir fixtures
# ---------------------------------------------------------------------------


CRON_YAML_FIXTURE = """\
defaults:
  default_model: claude-opus-4-6
  default_timeout: 1800
  default_env:
    PATH: /usr/local/bin:/usr/bin:/bin
    HOME: /tmp
  default_working_directory: ""

jobs:

  - name: morning-brief
    kind: llm
    schedule: "0 7 * * *"
    model: claude-opus-4-6
    instruction: recurring/morning-brief.md
    expected_output_glob: briefs_morning/morning-*.md
    idempotency_marker: briefs_morning/morning-{today}.md
    timeout_seconds: 3600

  - name: cleanup-retention
    kind: script
    schedule: "7 4 2 * *"
    program_args:
      - /bin/bash
      - /tmp/testws/scripts/cleanup-retention.sh
    env:
      PATH: /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin

  - name: prompts-alignment
    kind: llm
    schedule:
      - "0 22 * * 2"
      - "0 22 * * 5"
    model: claude-opus-4-6
    instruction: recurring/prompts-alignment.md
"""


def _write_profile_yaml(base: Path, name: str, workspace: Path) -> Path:
    """Materialize a schema-valid profile.yaml + workspace tree under `base`."""
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "briefs").mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.yaml").write_text(
        f"name: {name}\n"
        f"display_name: {name.capitalize()}\n"
        "assistant_name: TestBot\n"
        "timezone: America/Los_Angeles\n"
        f"keychain_account: {name}-acct\n"
        f"launchd_label_prefix: com.{name}\n"
        f"workspace_absolute: {workspace}\n"
        f"memory_root: {workspace}/memory\n"
        f"briefs_root: {workspace}/briefs\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        f"  env_prefix: {name.upper()}_SECRET_\n",
        encoding="utf-8",
    )
    (profile_dir / "cron.yaml").write_text(CRON_YAML_FIXTURE, encoding="utf-8")
    return profile_dir


@pytest.fixture
def hermetic_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[Path, Path, Path]:
    """Materialize a profile + cron.yaml + tmp launchd dir + workspace tree.

    Returns (profile_dir, workspace_dir, launchd_dir).
    """
    profile_base = tmp_path / "profiles"
    workspace = tmp_path / "workspace"
    launchd_dir = tmp_path / "launchagents"
    profile_dir = _write_profile_yaml(profile_base, "hermes", workspace)
    launchd_dir.mkdir(parents=True, exist_ok=True)

    # Recurring instruction files (workspace-scoped, NEVER live) so
    # `edit` has something legitimate to point at.
    recurring_dir = workspace / "recurring"
    recurring_dir.mkdir(parents=True, exist_ok=True)
    (recurring_dir / "morning-brief.md").write_text(
        "# morning-brief\ninstruction body\n", encoding="utf-8"
    )
    (recurring_dir / "prompts-alignment.md").write_text(
        "# prompts-alignment\ninstruction body\n", encoding="utf-8"
    )

    # Empty logs tree so `status` reports "no logs yet" until a test
    # seeds one explicitly.
    (workspace / "logs").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(profile_base))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "hermes")
    monkeypatch.setenv(cron_verb.LAUNCHD_DIR_ENV, str(launchd_dir))
    return profile_dir, workspace, launchd_dir


def _seed_installed_plist(
    launchd_dir: Path,
    prefix: str,
    name: str,
    body: str,
) -> Path:
    """Drop `body` at `<launchd_dir>/<prefix>.<name>.plist` and return the path."""
    path = launchd_dir / f"{prefix}.{name}.plist"
    path.write_text(body, encoding="utf-8")
    return path


def _minimal_valid_plist_body(label: str) -> str:
    """Return a plistlib-parsable body with a matching Label.

    Uses `plistlib.dumps` so the fixture is byte-identical to what
    Apple's toolchain would emit. Body content is intentionally
    minimal — only Label is asserted by `_job_is_live`.
    """
    return plistlib.dumps({"Label": label}, fmt=plistlib.FMT_XML).decode("utf-8")


# ---------------------------------------------------------------------------
# Help + registration smoke tests (LANDMINE: `mineru --help` must never fail)
# ---------------------------------------------------------------------------


def test_root_help_renders_with_cron_installed(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`mineru --help` renders with the cron sub-app installed."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "cron" in result.stdout


def test_cron_help_lists_the_six_read_verbs(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`mineru cron --help` enumerates the six read-only verbs."""
    result = runner.invoke(app, ["cron", "--help"])
    assert result.exit_code == 0, result.output
    for verb in ("list", "status", "logs", "edit", "plist", "diff"):
        assert verb in result.stdout, f"missing {verb!r} in cron --help"
    # Explicit reminder text keeps operators from expecting install/run.
    assert "install" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Import-time side-effect guard (no ~/Library/LaunchAgents reads at import).
# ---------------------------------------------------------------------------


def test_importing_cron_verb_does_not_read_launchd_dir(monkeypatch, tmp_path):
    """Import-time side-effect guard.

    Re-import `mineru_cli.verbs.cron` with a monkeypatched
    `pathlib.Path.iterdir` and `plistlib.load` that raise on call.
    If the module reads the launchd dir at import time, one of these
    fires. This is the "help must never fail" LANDMINE from §7.
    """
    # Track any accidental call into plistlib.load at import time.
    calls: list = []

    def _forbid_plistlib_load(*args, **kwargs):
        calls.append(("plistlib.load", args, kwargs))
        raise AssertionError("plistlib.load called at import time")

    # Force reload path: pop the module, re-import, count calls.
    import importlib
    import plistlib as _plistlib

    monkeypatch.setattr(_plistlib, "load", _forbid_plistlib_load, raising=True)
    monkeypatch.delitem(sys.modules, "mineru_cli.verbs.cron", raising=False)
    module = importlib.import_module("mineru_cli.verbs.cron")

    # No call should have happened.
    assert calls == []
    assert module.cron_app is not None


# ---------------------------------------------------------------------------
# `mineru cron list`
# ---------------------------------------------------------------------------


def test_list_table_default(hermetic_profile: Tuple[Path, Path, Path]) -> None:
    """Default table renders all three fixture jobs; no live plists yet."""
    result = runner.invoke(app, ["cron", "list"])
    assert result.exit_code == 0, result.output
    assert "morning-brief" in result.stdout
    assert "cleanup-retention" in result.stdout
    assert "prompts-alignment" in result.stdout
    # Live column should be "no" for every job (no plists installed).
    assert "live" in result.stdout


def test_list_json_payload(hermetic_profile: Tuple[Path, Path, Path]) -> None:
    """`--json` emits a list of dicts with the discriminating fields."""
    result = runner.invoke(app, ["cron", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    names = {row["name"] for row in payload}
    assert names == {"morning-brief", "cleanup-retention", "prompts-alignment"}

    for row in payload:
        assert "kind" in row
        assert "schedule" in row and isinstance(row["schedule"], list)
        assert "schedule_humanized" in row
        assert "enabled" in row
        assert "live" in row
        assert "enabled_mismatch" in row

    # No installed plists yet -> live is False for all, and since all
    # jobs are enabled: True in YAML, enabled_mismatch is True.
    for row in payload:
        assert row["live"] is False
        assert row["enabled_mismatch"] is True


def test_list_reconciliation_flips_when_plist_installed(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Seed a plist with a matching Label -> `live` flips True, mismatch False."""
    _, _, launchd_dir = hermetic_profile
    body = _minimal_valid_plist_body("com.hermes.morning-brief")
    _seed_installed_plist(launchd_dir, "com.hermes", "morning-brief", body)

    result = runner.invoke(app, ["cron", "list", "--json"])
    assert result.exit_code == 0
    rows = {r["name"]: r for r in json.loads(result.stdout)}
    assert rows["morning-brief"]["live"] is True
    assert rows["morning-brief"]["enabled_mismatch"] is False
    # Other jobs stay live=False, mismatch=True.
    assert rows["cleanup-retention"]["live"] is False
    assert rows["cleanup-retention"]["enabled_mismatch"] is True


def test_list_verbose_carries_instruction_and_program_args(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    result = runner.invoke(app, ["cron", "list", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "recurring/morning-brief.md" in result.stdout
    assert "cleanup-retention.sh" in result.stdout


# ---------------------------------------------------------------------------
# `mineru cron status`
# ---------------------------------------------------------------------------


def _seed_log(workspace: Path, job: str, filename: str, content: str = "hello\n") -> Path:
    log_dir = workspace / "logs" / job
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def test_status_missing_job_exits_2(hermetic_profile: Tuple[Path, Path, Path]) -> None:
    result = runner.invoke(app, ["cron", "status", "not-a-real-job"])
    assert result.exit_code == 2
    assert "not-a-real-job" in result.stderr


def test_status_no_logs_reports_none_and_no_check_requested(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """cleanup-retention has no expected_output_glob → status says so."""
    result = runner.invoke(app, ["cron", "status", "cleanup-retention", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "cleanup-retention"
    assert payload["latest_log_path"] is None
    assert payload["expected_output_check"]["requested"] is False
    assert payload["expected_output_check"]["fresh"] is True
    assert payload["diff_summary"] == "no installed plist"


def test_status_fresh_expected_output(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """A briefs_morning/morning-*.md written just now → fresh=True."""
    _, workspace, _ = hermetic_profile
    (workspace / "briefs_morning").mkdir(parents=True, exist_ok=True)
    fresh_file = workspace / "briefs_morning" / "morning-test.md"
    fresh_file.write_text("hi", encoding="utf-8")

    result = runner.invoke(app, ["cron", "status", "morning-brief", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    check = payload["expected_output_check"]
    assert check["requested"] is True
    assert check["fresh"] is True
    assert check["matched_path"] == str(fresh_file)


def test_status_stale_expected_output(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Backdate the matching file → fresh=False."""
    _, workspace, _ = hermetic_profile
    (workspace / "briefs_morning").mkdir(parents=True, exist_ok=True)
    stale_file = workspace / "briefs_morning" / "morning-old.md"
    stale_file.write_text("hi", encoding="utf-8")
    # 4 hours old.
    past = stale_file.stat().st_mtime - (4 * 60 * 60)
    os.utime(stale_file, (past, past))

    result = runner.invoke(app, ["cron", "status", "morning-brief", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    check = payload["expected_output_check"]
    assert check["requested"] is True
    assert check["fresh"] is False
    assert check["matched_path"] is None
    assert len(check["matches"]) == 1


def test_status_reports_latest_log_ts(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    _, workspace, _ = hermetic_profile
    log_path = _seed_log(workspace, "morning-brief", "2026-08-01_07-00-05.log")
    result = runner.invoke(app, ["cron", "status", "morning-brief", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["latest_log_path"] == str(log_path)
    assert payload["latest_log_mtime"] is not None


def test_status_diff_summary_reports_differences(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Seed a divergent plist → diff_summary reports differing lines."""
    _, _, launchd_dir = hermetic_profile
    _seed_installed_plist(
        launchd_dir,
        "com.hermes",
        "morning-brief",
        "<plist>divergent body</plist>\n",
    )
    result = runner.invoke(app, ["cron", "status", "morning-brief", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "differs" in payload["diff_summary"]


# ---------------------------------------------------------------------------
# `mineru cron logs`
# ---------------------------------------------------------------------------


def test_logs_missing_dir_exits_1(hermetic_profile: Tuple[Path, Path, Path]) -> None:
    result = runner.invoke(app, ["cron", "logs", "morning-brief"])
    assert result.exit_code == 1
    assert "no logs" in result.stderr.lower()


def test_logs_tails_newest_file(hermetic_profile: Tuple[Path, Path, Path]) -> None:
    _, workspace, _ = hermetic_profile
    _seed_log(workspace, "morning-brief", "2026-07-31.log", "old content\n")
    newest = _seed_log(
        workspace, "morning-brief", "2026-08-01.log", "line-a\nline-b\nline-c\n"
    )
    result = runner.invoke(app, ["cron", "logs", "morning-brief", "-n", "2"])
    assert result.exit_code == 0, result.output
    assert "line-c" in result.stdout
    assert "line-b" in result.stdout
    # "old content" should not appear — we tailed the newer file only.
    assert "old content" not in result.stdout
    # Header shows the newest file path.
    assert str(newest) in result.stdout


def test_logs_rejects_zero_or_negative_lines(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    _, workspace, _ = hermetic_profile
    _seed_log(workspace, "morning-brief", "2026-08-01.log", "x\n")
    result = runner.invoke(app, ["cron", "logs", "morning-brief", "-n", "0"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# `mineru cron edit`
# ---------------------------------------------------------------------------


def test_edit_refuses_script_job(hermetic_profile: Tuple[Path, Path, Path]) -> None:
    result = runner.invoke(app, ["cron", "edit", "cleanup-retention"])
    assert result.exit_code == 2
    assert "no instruction" in result.stderr.lower() or "kind" in result.stderr.lower()


def test_edit_happy_path_launches_editor(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`EDITOR=true` (resolved via PATH) returns 0 without opening a UI.

    We use the bare command name `true` and let `shutil.which` resolve
    it — the sandbox this test runs under exposes `/usr/bin/true` on
    PATH but not `/bin/true`. Either path is fine for real operators.
    """
    import shutil as _shutil
    true_path = _shutil.which("true")
    assert true_path is not None, "true(1) must be on PATH for this test"
    monkeypatch.setenv("EDITOR", true_path)
    result = runner.invoke(app, ["cron", "edit", "morning-brief"])
    assert result.exit_code == 0, result.output


def test_edit_refuses_live_recurring_path(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LANDMINE: refuse to edit anything under `$MINERU_HOME/recurring/`.

    We simulate this by rewriting the LIVE_RECURRING_DIR guard to point
    at the workspace's recurring/ tree — then the workspace-scoped
    instruction file matches the "gated" pattern and the verb refuses.
    """
    _, workspace, _ = hermetic_profile
    monkeypatch.setattr(
        cron_verb,
        "LIVE_RECURRING_DIR",
        workspace / "recurring",
        raising=True,
    )
    result = runner.invoke(app, ["cron", "edit", "morning-brief"])
    assert result.exit_code == 2
    assert "gated" in result.stderr.lower() or "phase" in result.stderr.lower()


def test_edit_missing_editor_reports_cleanly(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EDITOR", "definitely-not-a-real-editor-name-xyzzy")
    result = runner.invoke(app, ["cron", "edit", "morning-brief"])
    assert result.exit_code == 2
    assert "not found" in result.stderr.lower()


# ---------------------------------------------------------------------------
# `mineru cron plist`
# ---------------------------------------------------------------------------


def test_plist_stdout(hermetic_profile: Tuple[Path, Path, Path]) -> None:
    result = runner.invoke(app, ["cron", "plist", "morning-brief"])
    assert result.exit_code == 0, result.output
    assert "<?xml" in result.stdout
    assert "com.hermes.morning-brief" in result.stdout
    assert "StartCalendarInterval" in result.stdout


def test_plist_out_writes_file(
    hermetic_profile: Tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    out_path = tmp_path / "written.plist"
    result = runner.invoke(app, ["cron", "plist", "morning-brief", "--out", str(out_path)])
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "com.hermes.morning-brief" in text


def test_plist_out_inside_launchd_dir_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """LANDMINE guard: --out inside launchd dir IS an implicit install."""
    _, _, launchd_dir = hermetic_profile
    forbidden = launchd_dir / "com.hermes.morning-brief.plist"
    result = runner.invoke(
        app, ["cron", "plist", "morning-brief", "--out", str(forbidden)]
    )
    assert result.exit_code == 2
    assert "landmine" in result.stderr.lower() or "launchd" in result.stderr.lower()
    assert not forbidden.exists(), (
        "plist verb must NEVER write into the launchd dir"
    )


def test_plist_out_inside_launchd_subdir_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Even a subdirectory of the launchd dir is refused."""
    _, _, launchd_dir = hermetic_profile
    sub = launchd_dir / "sub"
    forbidden = sub / "foo.plist"
    result = runner.invoke(
        app, ["cron", "plist", "morning-brief", "--out", str(forbidden)]
    )
    assert result.exit_code == 2
    assert not forbidden.exists()


# ---------------------------------------------------------------------------
# `mineru cron diff`
# ---------------------------------------------------------------------------


def test_diff_not_installed_shows_full_rendered(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    result = runner.invoke(app, ["cron", "diff", "morning-brief"])
    assert result.exit_code == 0, result.output
    assert "not installed" in result.stdout
    assert "<?xml" in result.stdout


def test_diff_clean_when_rendered_matches_installed(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Seed the installed plist with the exact rendered body → clean."""
    _, _, launchd_dir = hermetic_profile
    rendered = runner.invoke(app, ["cron", "plist", "morning-brief"]).stdout
    _seed_installed_plist(launchd_dir, "com.hermes", "morning-brief", rendered)
    result = runner.invoke(app, ["cron", "diff", "morning-brief"])
    assert result.exit_code == 0, result.output
    assert "clean" in result.stdout


def test_diff_differs_shows_unified_diff(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    _, _, launchd_dir = hermetic_profile
    _seed_installed_plist(
        launchd_dir,
        "com.hermes",
        "morning-brief",
        "<plist>divergent body</plist>\n",
    )
    result = runner.invoke(app, ["cron", "diff", "morning-brief"])
    assert result.exit_code == 0, result.output
    # Unified-diff hallmark: at least one +/- delta line beyond the
    # `+++` / `---` headers.
    body_lines = [
        line for line in result.stdout.splitlines()
        if line.startswith(("+", "-"))
        and not line.startswith(("+++", "---"))
    ]
    assert body_lines, "expected +/- diff lines in output"
