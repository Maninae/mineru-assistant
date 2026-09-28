"""Tests for `mineru cron install` / `uninstall` (P4-05).

⚠️⚠️ HARD SAFETY POSTURE — READ TWICE ⚠️⚠️

  NO test in this file EVER writes to `~/Library/LaunchAgents`. Every
  test seeds `MINERU_LAUNCHD_DIR` to a `tmp_path` sub-directory. A
  paranoia snapshot compares the live LaunchAgents dir pre/post every
  test and fails on any drift.

  NO test in this file EVER invokes `launchctl`. The verbs under test
  do not call it either — a grep in this file for `launchctl` should
  match only the assertions on the dry-run PRINTED commands.

  NO test in this file EVER writes into `$MINERU_HOME/`. Every
  profile fixture writes a workspace under `tmp_path`.

  NO test in this file EVER sets `MINERU_CRON_ALLOW_LIVE=1`. The gate
  is verified only via its refusal message and the `--dry-run` bypass;
  the live path is exercised strictly through `MINERU_LAUNCHD_DIR=tmp_path`
  with the `--live-flip` flag AND `MINERU_CRON_ALLOW_LIVE=1` seeded via
  `monkeypatch.setenv` — but the target dir is a tmp path, NEVER
  `~/Library/LaunchAgents`. The gate refusal for the live LaunchAgents
  dir is tested by resolving the tmp dir to be equal to the live dir
  (never happens organically) — for realism we assert the refusal by
  pointing the launchd dir env directly at `~/Library/LaunchAgents`
  (still no write happens because the guard fires).

Coverage:

  (a) dry-run install of morning-brief prints the rendered plist body AND
      the `launchctl bootstrap gui/<uid> <plist>` command; writes nothing.
  (b) dry-run install of pet-summary (multi-instance) prints the
      StartCalendarInterval as an `<array>` of `<dict>` entries.
  (c) dry-run install of daily-consolidation renders the plist for a
      `custom_prompt=true` + `pre_steps` job — the pre_steps annotations
      surface either in the trigger-script path (P4-02 default) or in the
      alternate rendering path.
  (d) `MINERU_LAUNCHD_DIR=tmp_path` install (with `--live-flip` +
      `MINERU_CRON_ALLOW_LIVE=1`) writes the plist there; with
      `--backup-existing` and a pre-existing plist, the prior copy is
      preserved at `<workspace>/archive/launchd-backup-<today>/<name>.plist`.
  (e) Attempting to install without `--dry-run` and without
      `MINERU_CRON_ALLOW_LIVE=1` (with or without `--live-flip`) exits
      non-zero with the loud gate message.
  (f) Attempting to install `telegram-daemon` or `daemon-watchdog` is
      refused under every flag combination.

Plus:

  * `--all` in dry-run mode iterates every enabled job.
  * `--all` without the gate exits non-zero.
  * `uninstall --dry-run` prints the bootout command + the trash target;
    never trashes when the target is absent.
  * Uninstall of a blocklisted job is refused.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Tuple

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
# HARD-SAFETY snapshot guard — autouse fixture
# ---------------------------------------------------------------------------


def _snapshot_tree(root: Path) -> frozenset:
    """Return a frozenset of (str_path, size, mtime_ns) for each file under root."""
    if not root.exists():
        return frozenset()
    out = set()
    for p in root.rglob("*"):
        try:
            if p.is_file():
                st = p.stat()
                out.add((str(p), st.st_size, st.st_mtime_ns))
        except OSError:
            continue
    return frozenset(out)


LIVE_MEMORY_DIR = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "memory"
LIVE_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LIVE_RECURRING_DIR = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "recurring"


@pytest.fixture(autouse=True)
def hard_safety_no_live_writes():
    """Fail the test if it writes to live memory / LaunchAgents / recurring dirs.

    The whole point of P4-05's tests: NO test may accidentally install a
    plist into `~/Library/LaunchAgents` or scribble into the live
    workspace. The snapshot is defensive belt-and-braces on top of the
    per-test MINERU_LAUNCHD_DIR seeding.
    """
    before_memory = _snapshot_tree(LIVE_MEMORY_DIR)
    before_launchagents = _snapshot_tree(LIVE_LAUNCHAGENTS_DIR)
    before_recurring = _snapshot_tree(LIVE_RECURRING_DIR)
    yield
    after_memory = _snapshot_tree(LIVE_MEMORY_DIR)
    after_launchagents = _snapshot_tree(LIVE_LAUNCHAGENTS_DIR)
    after_recurring = _snapshot_tree(LIVE_RECURRING_DIR)
    if before_memory != after_memory:
        pytest.fail(
            "HARD SAFETY VIOLATION: files under "
            f"{LIVE_MEMORY_DIR} changed during test. "
            f"Added={after_memory - before_memory} "
            f"Removed={before_memory - after_memory}"
        )
    if before_launchagents != after_launchagents:
        pytest.fail(
            "HARD SAFETY VIOLATION: files under "
            f"{LIVE_LAUNCHAGENTS_DIR} changed during test. "
            f"Added={after_launchagents - before_launchagents} "
            f"Removed={before_launchagents - after_launchagents}"
        )
    if before_recurring != after_recurring:
        pytest.fail(
            "HARD SAFETY VIOLATION: files under "
            f"{LIVE_RECURRING_DIR} changed during test. "
            f"Added={after_recurring - before_recurring} "
            f"Removed={before_recurring - after_recurring}"
        )


# ---------------------------------------------------------------------------
# Hermetic profile + cron.yaml fixture — exercises the four discriminating
# job shapes: standard LLM, script, multi-instance, custom_prompt + pre_steps.
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

  # Single-instance LLM. Baseline for the dry-run smoke test.
  - name: morning-brief
    kind: llm
    schedule: "0 7 * * *"
    model: claude-opus-4-6
    instruction: recurring/morning-brief.md
    expected_output_glob: briefs_morning/morning-*.md
    idempotency_marker: briefs_morning/morning-{today}.md
    timeout_seconds: 3600

  # Multi-instance schedule (M/W/F) — plist StartCalendarInterval is an
  # <array> of <dict>. Verifies test (b).
  - name: pet-summary
    kind: llm
    schedule:
      - "31 23 * * 1"
      - "31 23 * * 3"
      - "31 23 * * 5"
    model: claude-sonnet-4-6
    instruction: recurring/pet-summary.md

  # custom_prompt + pre_steps — matches the live daily-consolidation
  # trigger shape. Verifies test (c).
  - name: daily-consolidation
    kind: llm
    schedule: "0 1 * * *"
    model: claude-sonnet-4-6
    instruction: recurring/consolidate-daily-memories.md
    idempotency_marker: memory/daily/{yesterday}.md
    expected_output_glob: memory/daily/{yesterday}.md
    custom_prompt: true
    custom_prompt_suffix: "\\n\\nIMPORTANT: The target date is {yesterday}."
    pre_steps:
      - cmd:
          - /tmp/consolidate/prewarm-ollama.sh
        allow_fail: true
        comment: "Ollama pre-warm (allow_fail so a cold model doesn't sink the job)."
      - cmd:
          - /tmp/consolidate/export-apple-notes.sh
          - "--yesterday"
          - "{yesterday}"
        allow_fail: false
        comment: "Apple Notes export (strict — no journal, no consolidation)."
    timeout_seconds: 7200

  # Script job — exercises the non-LLM branch through render_plist +
  # install. Verifies scripts install cleanly too.
  - name: cleanup-retention
    kind: script
    schedule: "7 4 2 * *"
    program_args:
      - /bin/bash
      - /tmp/testws/scripts/cleanup-retention.sh
    env:
      PATH: /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
"""


def _write_profile_yaml(base: Path, name: str, workspace: Path) -> Path:
    """Materialize a schema-valid profile.yaml + workspace tree under `base`."""
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "briefs").mkdir(parents=True, exist_ok=True)
    (workspace / "recurring").mkdir(parents=True, exist_ok=True)
    # Seed the referenced instruction files so a downstream renderer /
    # verifier can grep them if it wants; render_plist doesn't require
    # them, but future increments might.
    for rel in (
        "recurring/morning-brief.md",
        "recurring/pet-summary.md",
        "recurring/consolidate-daily-memories.md",
    ):
        (workspace / rel).write_text(
            f"# {rel}\ninstruction body\n", encoding="utf-8"
        )
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

    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(profile_base))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "hermes")
    monkeypatch.setenv(cron_verb.LAUNCHD_DIR_ENV, str(launchd_dir))
    # Belt-and-braces: make sure MINERU_CRON_ALLOW_LIVE is never
    # inherited from the host into a subtest that intended to verify the
    # gate. Tests that opt in seed it explicitly.
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)
    return profile_dir, workspace, launchd_dir


def _plist_path_for(launchd_dir: Path, prefix: str, name: str) -> Path:
    """Return the path a plist would be installed at under `launchd_dir`."""
    return launchd_dir / f"{prefix}.{name}.plist"


# ---------------------------------------------------------------------------
# (a) Dry-run install of morning-brief prints plist + launchctl commands
# ---------------------------------------------------------------------------


def test_install_dry_run_prints_plist_and_bootstrap_command(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Dry-run install of morning-brief:

      * Prints the rendered plist body (XML + Label).
      * Prints the `launchctl bootstrap gui/<uid> <plist>` command.
      * Writes nothing to the launchd dir.
    """
    _, _, launchd_dir = hermetic_profile
    target = _plist_path_for(launchd_dir, "com.hermes", "morning-brief")

    result = runner.invoke(app, ["cron", "install", "morning-brief", "--dry-run"])
    assert result.exit_code == 0, result.output
    # Rendered plist body signatures.
    assert "<?xml" in result.stdout
    assert "com.hermes.morning-brief" in result.stdout
    assert "StartCalendarInterval" in result.stdout
    # Bootstrap command with the correct domain target + plist path.
    assert "launchctl" in result.stdout
    assert "bootstrap" in result.stdout
    assert f"gui/{os.getuid()}" in result.stdout
    assert str(target) in result.stdout
    # Nothing written.
    assert not target.exists(), (
        f"dry-run install must NEVER write into the launchd dir; "
        f"found {target}"
    )
    # Launchd dir contains no *.plist files at all.
    assert list(launchd_dir.glob("*.plist")) == [], (
        "dry-run install left a plist in the launchd dir"
    )


# ---------------------------------------------------------------------------
# (b) Dry-run install of pet-summary — multi-instance StartCalendarInterval
# ---------------------------------------------------------------------------


def test_install_dry_run_pet_summary_multi_instance_array(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Dry-run install of the M/W/F pet-summary job.

    StartCalendarInterval must render as an `<array>` of `<dict>` entries
    (P4-02 multi-instance path). We assert that there's an `<array>`
    element between the `StartCalendarInterval` `<key>` and the closing
    `</array>`, and that the array carries three `<dict>` entries (one
    per Weekday=1, 3, 5).
    """
    result = runner.invoke(app, ["cron", "install", "pet-summary", "--dry-run"])
    assert result.exit_code == 0, result.output
    body = result.stdout

    # StartCalendarInterval is followed by an <array>, with three <dict>
    # entries (Weekday 1, 3, 5). We use a lenient regex + count so a
    # future indentation tweak in the renderer doesn't break the test.
    m = re.search(
        r"<key>StartCalendarInterval</key>\s*<array>(.*?)</array>",
        body,
        flags=re.DOTALL,
    )
    assert m is not None, (
        "expected StartCalendarInterval to render as an <array>; got:\n" + body
    )
    dict_count = m.group(1).count("<dict>")
    assert dict_count == 3, (
        f"expected 3 <dict> entries in the pet-summary array; got {dict_count}. "
        f"Body was:\n{body}"
    )
    # Also assert each Weekday shows up in the rendered body.
    for weekday in ("1", "3", "5"):
        assert f"<key>Weekday</key>\n            <integer>{weekday}</integer>" in body, (
            f"missing Weekday={weekday} in pet-summary plist"
        )


# ---------------------------------------------------------------------------
# (c) Dry-run install of daily-consolidation — custom_prompt + pre_steps
# ---------------------------------------------------------------------------


def test_install_dry_run_daily_consolidation_custom_prompt(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Dry-run install of daily-consolidation renders a valid plist.

    The custom_prompt + pre_steps annotations don't change the plist
    ProgramArguments (P4-02 default is the trigger-script; the pre_steps
    live inside the trigger script itself). What we verify here is that
    the plist for this shape renders cleanly:

      * Label = `com.hermes.daily-consolidation`.
      * TimeOut = 7200 (per-job override).
      * StartCalendarInterval carries Hour=1 + Minute=0.
      * ProgramArguments points at the trigger-script (P4-02 default
        rendering path). This preserves live parity: pre_steps live
        inside the trigger script.

    The alternate rendering path (where the plist directly invokes
    `mineru cron run <name>`) is NOT wired in P4-02; when it lands,
    both this test and P4-02 will be updated in lockstep.
    """
    result = runner.invoke(
        app, ["cron", "install", "daily-consolidation", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    body = result.stdout

    assert "com.hermes.daily-consolidation" in body
    # TimeOut = 7200 (per-job override from the fixture).
    assert re.search(r"<key>TimeOut</key>\s*<integer>7200</integer>", body), (
        "expected TimeOut=7200 for daily-consolidation"
    )
    # StartCalendarInterval carries Hour=1 (matches "0 1 * * *").
    assert re.search(r"<key>Hour</key>\s*<integer>1</integer>", body), (
        "expected Hour=1 for daily-consolidation"
    )
    # ProgramArguments points at the trigger-script (P4-02 default).
    assert "trigger-daily-consolidation-claude-code.sh" in body, (
        "expected ProgramArguments to point at the daily-consolidation "
        "trigger script (P4-02 trigger-script rendering path)"
    )


# ---------------------------------------------------------------------------
# (d) MINERU_LAUNCHD_DIR=tmp_path install writes the plist + backup path
# ---------------------------------------------------------------------------


def test_install_live_flip_to_tmp_dir_writes_plist(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MINERU_LAUNCHD_DIR=tmp_path` + `--live-flip` + `ALLOW_LIVE=1`.

    * Writes the plist to `<tmp_path>/com.hermes.morning-brief.plist`.
    * Bootstrap subprocess is stubbed to succeed cleanly.
    * Nothing lands in `~/Library/LaunchAgents` (autouse guard verifies).
    """
    _, _, launchd_dir = hermetic_profile
    target = _plist_path_for(launchd_dir, "com.hermes", "morning-brief")

    # Stub subprocess.run inside the cron verb module so the "live" path
    # doesn't shell out to real launchctl. We record the calls to prove
    # the bootstrap argv was formed correctly.
    calls = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setenv(cron_verb.ALLOW_LIVE_ENV, "1")

    result = runner.invoke(
        app, ["cron", "install", "morning-brief", "--live-flip"]
    )
    assert result.exit_code == 0, result.output
    # Plist was written to the tmp launchd dir.
    assert target.exists(), (
        f"live install did not write plist to {target}"
    )
    body = target.read_text(encoding="utf-8")
    assert "com.hermes.morning-brief" in body
    # launchctl was invoked with bootstrap + the correct plist path.
    bootstrap_calls = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
    assert bootstrap_calls, (
        f"expected a launchctl bootstrap call; got {calls}"
    )
    assert str(target) in bootstrap_calls[0]
    assert f"gui/{os.getuid()}" in bootstrap_calls[0]


def test_install_backup_existing_preserves_prior_copy(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--backup-existing` copies the pre-existing plist to the workspace.

    Seed a fixture plist at the target path, run install with
    `--backup-existing` + `--live-flip` (+ tmp launchd dir + ALLOW_LIVE),
    and verify:

      * A backup copy landed at
        `<workspace>/archive/launchd-backup-<today>/morning-brief.plist`
        with the ORIGINAL fixture body.
      * The target path is now the FRESH rendered plist.
    """
    import datetime as _datetime

    _, workspace, launchd_dir = hermetic_profile
    target = _plist_path_for(launchd_dir, "com.hermes", "morning-brief")
    original_body = "<?xml version=\"1.0\"?><plist>ORIGINAL FIXTURE</plist>\n"
    target.write_text(original_body, encoding="utf-8")

    # Stub launchctl subprocess.
    monkeypatch.setattr(
        cron_verb.subprocess,
        "run",
        lambda *a, **k: type("_R", (), {"returncode": 0})(),
    )
    monkeypatch.setenv(cron_verb.ALLOW_LIVE_ENV, "1")

    result = runner.invoke(
        app,
        ["cron", "install", "morning-brief", "--live-flip", "--backup-existing"],
    )
    assert result.exit_code == 0, result.output

    today = _datetime.date.today().isoformat()
    backup_path = workspace / "archive" / f"launchd-backup-{today}" / "morning-brief.plist"
    assert backup_path.exists(), (
        f"backup did not land at {backup_path}"
    )
    assert backup_path.read_text(encoding="utf-8") == original_body, (
        "backup body does not match the pre-existing plist"
    )
    # Target now carries the fresh rendered body.
    fresh_body = target.read_text(encoding="utf-8")
    assert "com.hermes.morning-brief" in fresh_body
    assert "StartCalendarInterval" in fresh_body
    assert "ORIGINAL FIXTURE" not in fresh_body


# ---------------------------------------------------------------------------
# (e) No --dry-run + no MINERU_CRON_ALLOW_LIVE => loud gate refusal
# ---------------------------------------------------------------------------


def test_install_without_dry_run_and_without_flip_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """The default `mineru cron install <name>` refuses in the SAFE build.

    Refusal message must name the phrase 'LIVE INSTALL DISABLED' so
    an operator immediately sees the gate.
    """
    _, _, launchd_dir = hermetic_profile
    result = runner.invoke(app, ["cron", "install", "morning-brief"])
    assert result.exit_code != 0, result.output
    combined = result.stdout + (result.stderr or "")
    assert "LIVE INSTALL DISABLED" in combined
    assert "--live-flip" in combined
    assert cron_verb.ALLOW_LIVE_ENV in combined
    # Nothing written.
    target = _plist_path_for(launchd_dir, "com.hermes", "morning-brief")
    assert not target.exists()


def test_install_with_flip_but_no_env_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`--live-flip` alone (without ALLOW_LIVE=1) is refused."""
    _, _, launchd_dir = hermetic_profile
    result = runner.invoke(app, ["cron", "install", "morning-brief", "--live-flip"])
    assert result.exit_code != 0, result.output
    combined = result.stdout + (result.stderr or "")
    assert cron_verb.ALLOW_LIVE_ENV in combined
    target = _plist_path_for(launchd_dir, "com.hermes", "morning-brief")
    assert not target.exists()


def test_install_dry_run_never_refused_even_without_flip(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`--dry-run` always bypasses the gate, regardless of env or flag."""
    result = runner.invoke(app, ["cron", "install", "morning-brief", "--dry-run"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# (f) telegram-daemon / daemon-watchdog are refused under any flag combo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param(["--dry-run"], id="dry-run"),
        pytest.param(["--live-flip"], id="live-flip-only"),
        pytest.param([], id="bare"),
    ],
)
def test_install_telegram_daemon_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
    flags,
) -> None:
    """`telegram-daemon` is on the blocklist — refused under every flag combo.

    The job is not in cron.yaml (it belongs to Landline), so we hit the
    "no job named" path FIRST. But we also want the blocklist to fire on
    a hypothetical cron.yaml entry, so we assert the blocklist message
    when the name IS resolvable. For a name not in cron.yaml, the "no
    job named" refusal is sufficient — the blocklist is defense in depth.
    """
    result = runner.invoke(app, ["cron", "install", "telegram-daemon", *flags])
    assert result.exit_code != 0, result.output
    combined = result.stdout + (result.stderr or "")
    # Either the "no job named" path or the blocklist path is acceptable —
    # both preserve the invariant. Explicit assertion: the name IS in
    # our blocklist tuple.
    assert "telegram-daemon" in cron_verb.BLOCKLIST_JOB_NAMES
    # A refusal message somewhere in the output.
    assert "telegram-daemon" in combined or "no job" in combined.lower()


def test_install_daemon_watchdog_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`daemon-watchdog` is on the blocklist — refused."""
    result = runner.invoke(
        app, ["cron", "install", "daemon-watchdog", "--dry-run"]
    )
    assert result.exit_code != 0, result.output


def test_blocklist_contains_expected_names() -> None:
    """The blocklist must include the two Landline daemon names."""
    assert "telegram-daemon" in cron_verb.BLOCKLIST_JOB_NAMES
    assert "daemon-watchdog" in cron_verb.BLOCKLIST_JOB_NAMES


# Blocklist enforced via the internal `_refuse_blocklisted_job` helper —
# even a synthetic invocation that bypasses cron.yaml resolution must trip
# the guard. This is the "defense in depth" claim.


def test_refuse_blocklisted_job_helper_fires_on_blocklisted_names() -> None:
    """The helper exits non-zero when the name is on the blocklist."""
    for name in ("telegram-daemon", "daemon-watchdog"):
        with pytest.raises(Exception) as exc_info:
            cron_verb._refuse_blocklisted_job(name)
        # typer.Exit inherits from click.exceptions.Exit, both carry the code.
        assert getattr(exc_info.value, "exit_code", 1) == 2


def test_refuse_blocklisted_job_helper_ignores_ordinary_names() -> None:
    """Ordinary job names pass through cleanly."""
    for name in ("morning-brief", "pet-summary", "daily-consolidation"):
        # No raise = OK.
        cron_verb._refuse_blocklisted_job(name)


# ---------------------------------------------------------------------------
# --all flag
# ---------------------------------------------------------------------------


def test_install_all_dry_run_prints_every_enabled_job(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`--all --dry-run` iterates every enabled job in cron.yaml."""
    result = runner.invoke(app, ["cron", "install", "--all", "--dry-run"])
    assert result.exit_code == 0, result.output
    for name in ("morning-brief", "pet-summary", "daily-consolidation", "cleanup-retention"):
        assert name in result.stdout, f"missing {name!r} in --all dry-run output"


def test_install_all_without_gate_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`--all` without --dry-run / --live-flip refuses with the batch message."""
    result = runner.invoke(app, ["cron", "install", "--all"])
    assert result.exit_code != 0, result.output
    combined = result.stdout + (result.stderr or "")
    assert "LIVE BATCH INSTALL DISABLED" in combined


def test_install_bare_without_name_or_all_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`mineru cron install` with no argument and no --all errors cleanly."""
    result = runner.invoke(app, ["cron", "install"])
    assert result.exit_code != 0, result.output
    combined = result.stdout + (result.stderr or "")
    assert "job name" in combined.lower() or "--all" in combined


def test_install_all_plus_name_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Passing both a name AND --all is ambiguous → refused."""
    result = runner.invoke(app, ["cron", "install", "morning-brief", "--all", "--dry-run"])
    assert result.exit_code != 0, result.output


# ---------------------------------------------------------------------------
# Live LaunchAgents dir guard (defense in depth)
# ---------------------------------------------------------------------------


def test_install_refuses_live_launchagents_dir_even_with_gate(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with `--live-flip` + `ALLOW_LIVE=1`, the live dir is refused.

    Phase-4 SAFE build keeps `~/Library/LaunchAgents` off-limits. the operator's
    cutover runbook flips this in a later gated step.

    Belt-and-braces: no subprocess is stubbed here — if the guard
    misfires, the autouse snapshot fixture catches any real write.
    """
    monkeypatch.setenv(cron_verb.LAUNCHD_DIR_ENV, str(LIVE_LAUNCHAGENTS_DIR))
    monkeypatch.setenv(cron_verb.ALLOW_LIVE_ENV, "1")

    result = runner.invoke(
        app, ["cron", "install", "morning-brief", "--live-flip"]
    )
    assert result.exit_code != 0, result.output
    combined = result.stdout + (result.stderr or "")
    assert "LANDMINE" in combined or "LaunchAgents" in combined


# ---------------------------------------------------------------------------
# uninstall — mirror behavior
# ---------------------------------------------------------------------------


def test_uninstall_dry_run_prints_bootout_and_trash_target(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Dry-run uninstall:

      * Prints the bootout command with `gui/<uid>/<label>`.
      * Prints the plist path it WOULD trash (when it exists).
      * Never invokes `launchctl` or `trash`.
    """
    _, _, launchd_dir = hermetic_profile
    target = _plist_path_for(launchd_dir, "com.hermes", "morning-brief")
    # Seed a plist so the dry-run shows the "would trash" line.
    target.write_text("<plist>seed</plist>", encoding="utf-8")

    result = runner.invoke(
        app, ["cron", "uninstall", "morning-brief", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    body = result.stdout
    assert "launchctl" in body and "bootout" in body
    assert f"gui/{os.getuid()}/com.hermes.morning-brief" in body
    assert str(target) in body
    # The seed plist is still there — dry-run never trashes.
    assert target.exists(), "dry-run uninstall must not trash the plist"


def test_uninstall_dry_run_no_plist_present(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Dry-run uninstall against a missing plist prints a "nothing to trash" note."""
    result = runner.invoke(
        app, ["cron", "uninstall", "morning-brief", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "does not exist" in result.stdout or "nothing to trash" in result.stdout


def test_uninstall_without_gate_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`mineru cron uninstall <name>` without --dry-run refuses in SAFE build."""
    result = runner.invoke(app, ["cron", "uninstall", "morning-brief"])
    assert result.exit_code != 0, result.output
    combined = result.stdout + (result.stderr or "")
    assert "LIVE INSTALL DISABLED" in combined


def test_uninstall_all_dry_run_iterates_every_job(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`uninstall --all --dry-run` iterates every enabled job."""
    result = runner.invoke(app, ["cron", "uninstall", "--all", "--dry-run"])
    assert result.exit_code == 0, result.output
    for name in ("morning-brief", "pet-summary", "daily-consolidation", "cleanup-retention"):
        assert name in result.stdout


def test_uninstall_bare_without_name_or_all_is_refused(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Bare `mineru cron uninstall` errors cleanly."""
    result = runner.invoke(app, ["cron", "uninstall"])
    assert result.exit_code != 0, result.output


# ---------------------------------------------------------------------------
# help renders (LANDMINE: `mineru --help` must never fail)
# ---------------------------------------------------------------------------


def test_root_help_still_renders_with_install_uninstall(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`mineru --help` renders with install/uninstall added.

    LANDMINE from cutover §7: a broken `app.py` silently drops every
    legacy call to fallback. Any importer-time breakage in the new
    verbs would fail this test loudly.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output


def test_cron_help_lists_install_and_uninstall(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`mineru cron --help` enumerates install + uninstall."""
    result = runner.invoke(app, ["cron", "--help"])
    assert result.exit_code == 0, result.output
    assert "install" in result.stdout
    assert "uninstall" in result.stdout


def test_install_help_mentions_gate(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """The install verb's help must name the gate for operator legibility."""
    result = runner.invoke(app, ["cron", "install", "--help"])
    assert result.exit_code == 0, result.output
    assert "--live-flip" in result.stdout
    assert cron_verb.ALLOW_LIVE_ENV in result.stdout
    assert "--dry-run" in result.stdout
    assert "--backup-existing" in result.stdout
    assert "--all" in result.stdout
