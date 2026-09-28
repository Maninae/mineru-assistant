"""HARD SAFETY invariants for the Phase-4 SAFE build of `mineru cron` (P4-07).

This file is the "you shall not pass" backstop. Every assertion here pins a
safety-critical invariant that MUST hold regardless of how the underlying
implementation evolves. If any of these regress, the P4-07 review — and the
cutover it precedes — must halt.

Coverage:

  # HARD SAFETY invariants (§7 of the cutover doc):

    1. Importing `mineru_cli.verbs.cron` does NOT read or write
       `~/Library/LaunchAgents`. Asserted via a stat snapshot before /
       after a fresh import.
    2. `mineru cron install <name>` without `--dry-run` and without
       `MINERU_CRON_ALLOW_LIVE=1` exits non-zero AND writes nothing
       (no plist under the resolved launchd dir, no drift in the
       workspace).
    3. `mineru cron run <name>` refuses to execute against
       `WORKSPACE=$MINERU_HOME` with the DEFAULT `CC_BIN` unless
       `MINERU_CRON_ALLOW_LIVE=1` is set. `--dry-run` bypasses. Overridden
       `CC_BIN` bypasses.
    4. The `telegram-daemon` / `daemon-watchdog` blocklists cannot be
       bypassed by ANY flag combination on install OR uninstall,
       regardless of --dry-run / --live-flip / --backup-existing /
       MINERU_CRON_ALLOW_LIVE=1 / --all. Belt-and-braces: even a
       synthetic invocation that reaches `_install_one` (name IS in
       cron.yaml) is refused; the guard is called BEFORE the Phase-4
       live gate.

  # Semantic regressions the cutover cannot afford to lose:

    5. Multi-instance schedule regressions (LANDMINE §7): render_plist
       for pet-summary emits exactly 3 StartCalendarInterval `<dict>`
       entries with Weekdays 1/3/5 at 23:31; prompts-alignment emits
       exactly 2 with Weekdays 2/5 at 22:00. XML byte-inspected.
    6. Custom-prompt + pre_steps regression (LANDMINE §7): a `cron run
       daily-consolidation --dry-run` invocation emits the concatenated
       YESTERDAY-pinned prompt AND runs each `pre_step` in order (via
       stubbed `subprocess.run`), with `allow_fail=true` on the journal
       export step honored (a failing step logs a warning + fires alert
       stub, does NOT abort). The strict `allow_fail=false` step aborts
       with rc=64 + an alert.

  # LANDMINE (§7): `mineru --help` must NEVER fail.

    7. After importing every verb subpackage (including the freshly
       loaded cron sub-app), `runner.invoke(app, ["--help"])` returns
       exit 0 and includes 'cron' in the output. Belt-and-braces smoke
       runs BEFORE and AFTER installing the new shim tests, so a broken
       `app.py` fails loudly.

⚠️⚠️ HARD SAFETY POSTURE — READ TWICE ⚠️⚠️

  NO test in this file EVER writes to `~/Library/LaunchAgents`. An
  autouse snapshot fixture compares the live dir pre / post every test
  and fails on any drift (belt-and-braces on top of the per-test
  `MINERU_LAUNCHD_DIR` seeding).

  NO test in this file EVER writes into `$MINERU_HOME/`. Every
  profile fixture writes a workspace under `tmp_path`.

  NO test in this file EVER invokes `launchctl` or a real `claude-fda`
  binary. Every subprocess entry point is either --dry-run (nothing
  spawns) or stubbed via `monkeypatch.setattr(cron_verb.subprocess,
  "run", ...)`.
"""

from __future__ import annotations

import importlib
import os
import plistlib
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

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
# Live-tree snapshot guard (autouse)
# ---------------------------------------------------------------------------
#
# Same shape as the guards in test_cron_install.py / test_cron_run.py — but
# widened to cover ALL three live surfaces (memory, LaunchAgents, recurring)
# so no test in this file can regress any of them silently.


LIVE_MEMORY_DIR = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "memory"
LIVE_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LIVE_RECURRING_DIR = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "recurring"
LIVE_WORKSPACE = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))


def _snapshot_tree(root: Path) -> frozenset:
    """Return a frozenset of (path, size, mtime_ns) for each file at `root`.

    Missing root -> empty frozenset. We're checking for drift, not for
    presence.
    """
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


@pytest.fixture(autouse=True)
def hard_safety_no_live_writes() -> Any:
    """Fail the test on any drift under memory / LaunchAgents / recurring."""
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
# Hermetic profile + cron.yaml fixture — exercises the shapes each safety
# invariant needs to reach. Includes the two BLOCKLIST names in cron.yaml
# so we can drive `_install_one`'s blocklist path (defense in depth) even
# with `_get_job_or_exit` returning a resolved job.
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

  # Baseline single-instance LLM job for the install refusal test.
  - name: morning-brief
    kind: llm
    schedule: "0 7 * * *"
    model: claude-opus-4-6
    instruction: recurring/morning-brief.md
    expected_output_glob: briefs_morning/morning-*.md
    idempotency_marker: briefs_morning/morning-{today}.md
    timeout_seconds: 3600

  # Multi-instance M/W/F (Weekday 1/3/5) at 23:31 — pins the pet-summary
  # regression asserted below.
  - name: pet-summary
    kind: llm
    schedule:
      - "31 23 * * 1"
      - "31 23 * * 3"
      - "31 23 * * 5"
    model: claude-sonnet-4-6
    instruction: recurring/pet-summary.md

  # Multi-instance Tue/Fri (Weekday 2/5) at 22:00 — pins the
  # prompts-alignment regression asserted below.
  - name: prompts-alignment
    kind: llm
    schedule:
      - "0 22 * * 2"
      - "0 22 * * 5"
    model: claude-opus-4-6
    instruction: recurring/prompts-alignment.md

  # custom_prompt + pre_steps job for the daily-consolidation regression.
  # Placeholder pre-step paths get rewritten by the test to point at tmp
  # stubs; the stubs are only consulted for the placeholder-substitution
  # + ordering test, never invoked under --dry-run.
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
          - /tmp/safety-fixture/prewarm-ollama.sh
        allow_fail: true
        comment: "Ollama pre-warm (allow_fail so a cold model doesn't sink the job)."
      - cmd:
          - /tmp/safety-fixture/export-apple-notes.sh
          - "--yesterday"
          - "{yesterday}"
        allow_fail: false
        comment: "Apple Notes export (strict — no journal, no consolidation)."
    timeout_seconds: 7200

  # Blocklisted names planted in cron.yaml so the blocklist path in
  # `_install_one` is REACHABLE even after `_get_job_or_exit` resolves
  # the name. Defense-in-depth: the blocklist must fire before any live
  # gate would be consulted. In real life these jobs never live in
  # cron.yaml (they belong to Landline / are paused), but adding them
  # here is the way to prove the guard cannot be bypassed.
  - name: telegram-daemon
    kind: llm
    schedule: "0 0 * * *"
    model: claude-opus-4-6
    instruction: recurring/telegram-daemon.md

  - name: daemon-watchdog
    kind: llm
    schedule: "0 0 * * *"
    model: claude-opus-4-6
    instruction: recurring/daemon-watchdog.md

"""


def _write_profile_yaml(base: Path, name: str, workspace: Path) -> Path:
    """Materialize a schema-valid profile.yaml + workspace tree under `base`."""
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "briefs").mkdir(parents=True, exist_ok=True)
    (workspace / "recurring").mkdir(parents=True, exist_ok=True)
    for rel in (
        "recurring/morning-brief.md",
        "recurring/pet-summary.md",
        "recurring/prompts-alignment.md",
        "recurring/consolidate-daily-memories.md",
        "recurring/telegram-daemon.md",
        "recurring/daemon-watchdog.md",
    ):
        (workspace / rel).write_text(
            f"# {rel}\nCONSOLIDATE BODY LINE 1\nCONSOLIDATE BODY LINE 2\n",
            encoding="utf-8",
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
    """Materialize a profile + cron.yaml + tmp launchd dir + workspace.

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
    # Never inherit ALLOW_LIVE from the host — a passing safety test must
    # PROVE the gate fires, not silently ride an opt-in flag.
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)
    return profile_dir, workspace, launchd_dir


# ===========================================================================
# INVARIANT 1 — Import-time side-effect guard
# ===========================================================================
#
# Importing `mineru_cli.verbs.cron` must NOT read or write
# `~/Library/LaunchAgents`. Every path resolution is lazy; the module
# builds only Typer registrations at import time. If a future refactor
# adds an eager `os.listdir` / `plistlib.load` at import scope, this
# test fires.


def test_import_does_not_touch_live_launchagents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh-import `mineru_cli.verbs.cron`; snapshot the live dir pre/post.

    Belt-and-braces: also monkeypatch `plistlib.load` and
    `pathlib.Path.iterdir` (limited to the live LaunchAgents dir) to
    RAISE on call. If a stray import-time read happens, one of these
    trips loudly.
    """
    before = _snapshot_tree(LIVE_LAUNCHAGENTS_DIR)

    # Track any accidental call at import time.
    calls: List[Tuple[str, tuple, dict]] = []

    def _forbid_plistlib_load(*args, **kwargs):
        calls.append(("plistlib.load", args, kwargs))
        raise AssertionError("plistlib.load called at import time")

    real_iterdir = Path.iterdir

    def _guarded_iterdir(self, *args, **kwargs):
        try:
            resolved = self.expanduser().resolve()
        except OSError:
            return real_iterdir(self, *args, **kwargs)
        try:
            live_resolved = LIVE_LAUNCHAGENTS_DIR.expanduser().resolve()
        except OSError:
            return real_iterdir(self, *args, **kwargs)
        if resolved == live_resolved:
            calls.append(("Path.iterdir(LaunchAgents)", args, kwargs))
            raise AssertionError(
                "Path.iterdir called against ~/Library/LaunchAgents at import"
            )
        return real_iterdir(self, *args, **kwargs)

    import plistlib as _plistlib

    monkeypatch.setattr(_plistlib, "load", _forbid_plistlib_load, raising=True)
    monkeypatch.setattr(Path, "iterdir", _guarded_iterdir, raising=True)

    monkeypatch.delitem(sys.modules, "mineru_cli.verbs.cron", raising=False)
    module = importlib.import_module("mineru_cli.verbs.cron")

    # No forbidden calls happened during import.
    assert calls == []
    # Module has the expected surface.
    assert module.cron_app is not None
    assert module.BLOCKLIST_JOB_NAMES == (
        "telegram-daemon",
        "daemon-watchdog",
    )

    # Live dir snapshot is unchanged.
    after = _snapshot_tree(LIVE_LAUNCHAGENTS_DIR)
    assert before == after, (
        "importing mineru_cli.verbs.cron mutated ~/Library/LaunchAgents: "
        f"added={after - before} removed={before - after}"
    )


# ===========================================================================
# INVARIANT 2 — `mineru cron install <name>` without --dry-run + without
# MINERU_CRON_ALLOW_LIVE=1 exits non-zero AND writes nothing anywhere
# ===========================================================================


def _launchd_dir_snapshot(launchd_dir: Path) -> frozenset:
    """Return the set of file names under the tmp launchd dir."""
    if not launchd_dir.exists():
        return frozenset()
    return frozenset(str(p.relative_to(launchd_dir)) for p in launchd_dir.rglob("*") if p.is_file())


def test_install_without_dry_run_and_without_allow_live_exits_nonzero_writes_nothing(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """The default `mineru cron install <name>` invocation must refuse.

    Verifies:
      * exit code != 0
      * refusal message names the gate (--live-flip AND ALLOW_LIVE=1)
      * the resolved launchd dir is unchanged
      * nothing landed under the workspace archive/backup path either
    """
    _, workspace, launchd_dir = hermetic_profile
    launchd_before = _launchd_dir_snapshot(launchd_dir)
    ws_archive_before = _snapshot_tree(workspace / "archive")

    result = runner.invoke(app, ["cron", "install", "morning-brief"])
    assert result.exit_code != 0, (
        "install without --dry-run + without ALLOW_LIVE=1 must refuse; "
        f"got exit 0 with stdout={result.stdout!r}"
    )
    combined = result.stdout + (result.stderr or "")
    assert "LIVE INSTALL DISABLED" in combined
    assert "--live-flip" in combined
    assert cron_verb.ALLOW_LIVE_ENV in combined

    # Nothing written to the resolved launchd dir.
    launchd_after = _launchd_dir_snapshot(launchd_dir)
    assert launchd_before == launchd_after, (
        "install refusal wrote into the resolved launchd dir: "
        f"added={launchd_after - launchd_before}"
    )
    # And nothing landed in the workspace archive (no backup either).
    ws_archive_after = _snapshot_tree(workspace / "archive")
    assert ws_archive_before == ws_archive_after


def test_install_with_flip_but_no_env_exits_nonzero_writes_nothing(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`--live-flip` alone (without ALLOW_LIVE=1) must still refuse."""
    _, _, launchd_dir = hermetic_profile
    launchd_before = _launchd_dir_snapshot(launchd_dir)

    result = runner.invoke(app, ["cron", "install", "morning-brief", "--live-flip"])
    assert result.exit_code != 0, result.output
    combined = result.stdout + (result.stderr or "")
    assert cron_verb.ALLOW_LIVE_ENV in combined
    launchd_after = _launchd_dir_snapshot(launchd_dir)
    assert launchd_before == launchd_after


def test_install_all_without_gate_exits_nonzero_writes_nothing(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`--all` without any gate refuses AND leaves the launchd dir untouched."""
    _, _, launchd_dir = hermetic_profile
    launchd_before = _launchd_dir_snapshot(launchd_dir)

    result = runner.invoke(app, ["cron", "install", "--all"])
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "LIVE BATCH INSTALL DISABLED" in combined
    launchd_after = _launchd_dir_snapshot(launchd_dir)
    assert launchd_before == launchd_after


# ===========================================================================
# INVARIANT 3 — `mineru cron run <name>` refuses to execute against
# WORKSPACE=$MINERU_HOME with the default CC_BIN unless
# MINERU_CRON_ALLOW_LIVE=1 is set.
# ===========================================================================


def test_run_refuses_against_live_workspace_without_allow_live(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HARD-SAFETY refusal fires on the real geometry.

    We simulate "live" by pointing the guard's live-workspace constant
    at the tmp workspace itself, then unset CC_BIN so the default is
    consulted. ALLOW_LIVE is NOT set (fixture unsets it).
    """
    _, workspace, _ = hermetic_profile
    monkeypatch.setenv(cron_verb.WORKSPACE_ENV, str(workspace))
    # Rewire the live-workspace constant so the guard fires against
    # our tmp workspace (which is what WORKSPACE resolves to).
    monkeypatch.setattr(
        cron_verb, "LIVE_WORKSPACE_PATH", workspace, raising=True
    )
    # Default CC_BIN — no override. This is the "operator didn't wire a
    # stub" case that the guard is designed to catch.
    monkeypatch.delenv(cron_verb.CC_BIN_ENV, raising=False)
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)

    # Belt-and-braces: stub subprocess.run so even if the guard MISFIRES,
    # nothing shells out to a real CC binary. A misfiring guard would
    # increment the call count; a working guard leaves it at zero.
    calls: List[list] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(app, ["cron", "run", "morning-brief"])
    assert result.exit_code == 2, (result.stdout, result.stderr)
    combined = result.stdout + (result.stderr or "")
    assert "HARD SAFETY" in combined
    # No subprocess call happened — the refusal fires BEFORE any real work.
    assert calls == [], (
        f"expected no subprocess.run call on refusal path; got {calls}"
    )


def test_run_refuses_pointing_at_actual_live_workspace_path(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal fires when `WORKSPACE` is the REAL live path.

    We don't rewire `LIVE_WORKSPACE_PATH` here — we let the module's
    default (`$MINERU_HOME`) stand and point WORKSPACE at it
    directly. The guard resolves both and compares; a match with the
    default CC_BIN + no ALLOW_LIVE = refuse.
    """
    monkeypatch.setenv(cron_verb.WORKSPACE_ENV, str(cron_verb.LIVE_WORKSPACE_PATH))
    monkeypatch.delenv(cron_verb.CC_BIN_ENV, raising=False)
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)

    calls: List[list] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(app, ["cron", "run", "morning-brief"])
    assert result.exit_code == 2, (result.stdout, result.stderr)
    combined = result.stdout + (result.stderr or "")
    assert "HARD SAFETY" in combined
    assert str(cron_verb.LIVE_WORKSPACE_PATH) in combined
    assert calls == []


def test_run_dry_run_bypasses_live_workspace_refusal(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--dry-run` is the escape hatch: no CC invocation to gate.

    Even with WORKSPACE=live geometry + default CC_BIN, --dry-run
    proceeds cleanly.
    """
    _, workspace, _ = hermetic_profile
    monkeypatch.setenv(cron_verb.WORKSPACE_ENV, str(workspace))
    monkeypatch.setattr(
        cron_verb, "LIVE_WORKSPACE_PATH", workspace, raising=True
    )
    monkeypatch.delenv(cron_verb.CC_BIN_ENV, raising=False)
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)

    result = runner.invoke(
        app, ["cron", "run", "morning-brief", "--dry-run"]
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)


# ===========================================================================
# INVARIANT 4 — Blocklist bypass proof
# ===========================================================================
#
# For each of the three blocklisted names, exhaustively enumerate flag
# combos on `install` and `uninstall` and verify EVERY invocation exits
# non-zero. The blocklist must be uncircumventable even with the strongest
# operator opt-in (--live-flip + MINERU_CRON_ALLOW_LIVE=1 + --dry-run).
#
# The fixture cron.yaml PLANTS the three blocklist names, so they resolve
# through `_get_job_or_exit` and reach the blocklist guard in
# `_install_one` / `_uninstall_one`. In real life these jobs never live
# in cron.yaml — this test proves the guard STILL fires when they do.


_BLOCKLIST_NAMES = ("telegram-daemon", "daemon-watchdog")


# Every non-trivial combination of flags an operator could conjure.
# We include --dry-run + --live-flip because a stray combination must
# not be a bypass either. The MINERU_CRON_ALLOW_LIVE env is toggled
# separately as a parameter so pytest ids describe both axes.
_INSTALL_FLAG_COMBOS = [
    pytest.param([], id="bare"),
    pytest.param(["--dry-run"], id="dry-run"),
    pytest.param(["--live-flip"], id="live-flip"),
    pytest.param(["--dry-run", "--live-flip"], id="dry-run+live-flip"),
    pytest.param(["--backup-existing"], id="backup-existing"),
    pytest.param(
        ["--live-flip", "--backup-existing"], id="live-flip+backup-existing"
    ),
    pytest.param(
        ["--dry-run", "--backup-existing"], id="dry-run+backup-existing"
    ),
    pytest.param(
        ["--dry-run", "--live-flip", "--backup-existing"],
        id="dry-run+live-flip+backup-existing",
    ),
]


@pytest.mark.parametrize("name", _BLOCKLIST_NAMES)
@pytest.mark.parametrize("flags", _INSTALL_FLAG_COMBOS)
@pytest.mark.parametrize("allow_live", [False, True], ids=["no-env", "with-env"])
def test_install_blocklist_cannot_be_bypassed(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    flags: List[str],
    allow_live: bool,
) -> None:
    """Every flag combo must refuse install of a blocklisted name.

    The blocklist check runs FIRST in `_install_one` — before both the
    live-flip gate and the plist render — so even the strongest opt-in
    (--live-flip + ALLOW_LIVE=1) must land on a refusal message.
    """
    _, _, launchd_dir = hermetic_profile
    launchd_before = _launchd_dir_snapshot(launchd_dir)
    if allow_live:
        monkeypatch.setenv(cron_verb.ALLOW_LIVE_ENV, "1")

    # Stub subprocess.run so a misfiring guard cannot invoke launchctl or
    # trash. A working guard leaves the call count at zero.
    calls: List[list] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(app, ["cron", "install", name, *flags])
    assert result.exit_code != 0, (
        f"install of blocklisted {name!r} with flags={flags} "
        f"allow_live={allow_live} must refuse; got exit 0 output={result.output!r}"
    )
    # Nothing landed under the launchd dir.
    launchd_after = _launchd_dir_snapshot(launchd_dir)
    assert launchd_before == launchd_after, (
        f"install of {name!r} wrote into launchd dir: "
        f"added={launchd_after - launchd_before}"
    )
    # No subprocess call happened.
    assert calls == [], (
        f"expected no subprocess.run call on refusal path; got {calls}"
    )


@pytest.mark.parametrize("name", _BLOCKLIST_NAMES)
@pytest.mark.parametrize("flags", _INSTALL_FLAG_COMBOS)
@pytest.mark.parametrize("allow_live", [False, True], ids=["no-env", "with-env"])
def test_uninstall_blocklist_cannot_be_bypassed(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    flags: List[str],
    allow_live: bool,
) -> None:
    """Every flag combo must refuse uninstall of a blocklisted name.

    Same shape as install: `_uninstall_one` calls
    `_refuse_blocklisted_job` first. Even with `--live-flip` +
    ALLOW_LIVE=1 + a legitimate cron.yaml entry, the guard fires.
    """
    _, _, launchd_dir = hermetic_profile
    launchd_before = _launchd_dir_snapshot(launchd_dir)
    if allow_live:
        monkeypatch.setenv(cron_verb.ALLOW_LIVE_ENV, "1")

    # Drop --backup-existing which is install-only. Uninstall doesn't
    # accept it and typer would error out for a different reason.
    uninstall_flags = [f for f in flags if f != "--backup-existing"]

    calls: List[list] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(app, ["cron", "uninstall", name, *uninstall_flags])
    assert result.exit_code != 0, (
        f"uninstall of blocklisted {name!r} with flags={uninstall_flags} "
        f"allow_live={allow_live} must refuse; got exit 0 output={result.output!r}"
    )
    launchd_after = _launchd_dir_snapshot(launchd_dir)
    assert launchd_before == launchd_after
    assert calls == []


def test_blocklist_tuple_shape_is_pinned() -> None:
    """The blocklist tuple must contain exactly the two Landline daemon names.

    A future accidental deletion of either of them (e.g., a well-meaning
    refactor that "extracts" the tuple into config and drops a value)
    would silently disable the guard. This test pins the exact members.
    """
    assert cron_verb.BLOCKLIST_JOB_NAMES == (
        "telegram-daemon",
        "daemon-watchdog",
    )


def test_refuse_blocklisted_job_helper_fires_directly() -> None:
    """The internal helper exits 2 on each blocklisted name.

    Defense in depth: a caller that skips `_get_job_or_exit` and jumps
    straight to `_refuse_blocklisted_job` still gets refused. This test
    guarantees the helper stays wired up and doesn't degrade to a no-op.
    """
    for name in _BLOCKLIST_NAMES:
        with pytest.raises(Exception) as exc_info:
            cron_verb._refuse_blocklisted_job(name)
        # typer.Exit inherits from click.exceptions.Exit — both carry
        # `exit_code`; default is 1 if the attr is absent.
        assert getattr(exc_info.value, "exit_code", 1) == 2


# ===========================================================================
# INVARIANT 5 — Multi-instance schedule regressions
# ===========================================================================
#
# The plist template must support `List[Dict]` StartCalendarInterval for
# multi-instance schedules (LANDMINE §7). We byte-inspect the rendered
# XML to make sure a future refactor cannot silently drop an entry or
# emit the wrong weekday.


def _render_by_name(hermetic_profile_tuple: Tuple[Path, Path, Path], name: str) -> str:
    """Load the profile + cron.yaml under the fixture and render `name`."""
    from mineru_cli.cron import get_job, load_cron_config, render_plist
    from mineru_cli.profile import load_active_profile

    profile = load_active_profile("hermes")
    config = load_cron_config(profile)
    job = get_job(config, name)
    assert job is not None, f"{name!r} not present in fixture cron.yaml"
    return render_plist(job, profile)


def _extract_start_calendar_dicts(rendered: str) -> List[Dict[str, int]]:
    """Parse the rendered plist XML and return the StartCalendarInterval entries.

    Uses `plistlib.loads` on the fully rendered XML so we validate the
    tree the way launchd would parse it. Returns a list of dicts even
    for the single-instance case (wrapping the lone dict in a list) so
    the two shapes are testable uniformly.
    """
    parsed = plistlib.loads(rendered.encode("utf-8"))
    entries = parsed.get("StartCalendarInterval")
    if entries is None:
        return []
    if isinstance(entries, dict):
        return [entries]
    assert isinstance(entries, list), f"unexpected type {type(entries)}"
    return entries


def test_pet_summary_multi_schedule_renders_three_weekday_dicts(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """pet-summary emits exactly 3 dicts with Weekdays 1/3/5 at 23:31."""
    rendered = _render_by_name(hermetic_profile, "pet-summary")
    entries = _extract_start_calendar_dicts(rendered)
    assert len(entries) == 3, (
        f"expected 3 StartCalendarInterval entries; got {len(entries)}. "
        f"Entries={entries}"
    )
    weekdays = sorted(e["Weekday"] for e in entries)
    assert weekdays == [1, 3, 5], f"unexpected weekdays: {weekdays}"
    for entry in entries:
        assert entry.get("Hour") == 23, entry
        assert entry.get("Minute") == 31, entry

    # Byte-inspect the raw XML too: it must render as an <array> of
    # <dict>, not a single <dict>. Regression against a future refactor
    # that flattens the schedule silently.
    m = re.search(
        r"<key>StartCalendarInterval</key>\s*<array>(.*?)</array>",
        rendered,
        flags=re.DOTALL,
    )
    assert m is not None, (
        "expected StartCalendarInterval rendered as <array>; got:\n" + rendered
    )
    dict_count = m.group(1).count("<dict>")
    assert dict_count == 3, (
        f"expected 3 <dict> entries in pet-summary; got {dict_count}"
    )


def test_prompts_alignment_multi_schedule_renders_two_weekday_dicts(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """prompts-alignment emits exactly 2 dicts with Weekdays 2/5 at 22:00."""
    rendered = _render_by_name(hermetic_profile, "prompts-alignment")
    entries = _extract_start_calendar_dicts(rendered)
    assert len(entries) == 2, (
        f"expected 2 StartCalendarInterval entries; got {len(entries)}. "
        f"Entries={entries}"
    )
    weekdays = sorted(e["Weekday"] for e in entries)
    assert weekdays == [2, 5], f"unexpected weekdays: {weekdays}"
    for entry in entries:
        assert entry.get("Hour") == 22, entry
        assert entry.get("Minute") == 0, entry

    # Byte-inspect: <array> of two <dict>.
    m = re.search(
        r"<key>StartCalendarInterval</key>\s*<array>(.*?)</array>",
        rendered,
        flags=re.DOTALL,
    )
    assert m is not None, (
        "expected StartCalendarInterval rendered as <array>; got:\n" + rendered
    )
    dict_count = m.group(1).count("<dict>")
    assert dict_count == 2, (
        f"expected 2 <dict> entries in prompts-alignment; got {dict_count}"
    )


# ===========================================================================
# INVARIANT 6 — Custom-prompt + pre_steps regression
# ===========================================================================
#
# `cron run daily-consolidation --dry-run` must emit the concatenated
# YESTERDAY-pinned prompt AND print each pre_step in declared order.
# We also test:
#   * Live-mode: `subprocess.run` is stubbed; pre_steps fire in order;
#     allow_fail=true on step 1 continues after a nonzero rc; allow_fail=false
#     on step 2 aborts with rc=64.
#   * Live-mode: alert on strict pre-step failure carries the pre-step number.


def test_daily_consolidation_dry_run_emits_prompt_and_pre_steps_in_order(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """--dry-run prints prompt body + resolved suffix AND both pre-steps in order.

    Pin `--date 2026-08-01` so `{yesterday}` resolves to 2026-07-31
    deterministically.
    """
    _, workspace, _ = hermetic_profile
    # Set WORKSPACE to the tmp workspace so the guard doesn't trip.
    # (--dry-run bypasses it anyway, but keeps the geometry consistent.)
    import os as _os
    _os.environ[cron_verb.WORKSPACE_ENV] = str(workspace)
    try:
        result = runner.invoke(
            app,
            [
                "cron",
                "run",
                "daily-consolidation",
                "--dry-run",
                "--date",
                "2026-08-01",
                "--force",
            ],
        )
    finally:
        _os.environ.pop(cron_verb.WORKSPACE_ENV, None)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    body = result.stdout

    # Instruction body appears verbatim in the concatenated prompt.
    assert "CONSOLIDATE BODY LINE 1" in body
    assert "CONSOLIDATE BODY LINE 2" in body
    # Suffix with placeholder resolved: yesterday of 2026-08-01 = 2026-07-31.
    assert "IMPORTANT: The target date is 2026-07-31." in body
    # Body precedes suffix — the suffix is APPENDED, not injected.
    body_idx = body.index("CONSOLIDATE BODY LINE 2")
    suffix_idx = body.index("IMPORTANT: The target date is 2026-07-31.")
    assert body_idx < suffix_idx, (
        "prompt suffix must be appended after the instruction body"
    )

    # Both pre-steps surface in declared order.
    pre1_line_idx = body.find("prewarm-ollama.sh")
    pre2_line_idx = body.find("export-apple-notes.sh")
    assert pre1_line_idx >= 0, "missing pre-step 1 (prewarm) header"
    assert pre2_line_idx >= 0, "missing pre-step 2 (apple-notes) header"
    assert pre1_line_idx < pre2_line_idx, (
        f"pre-steps out of order: pre1@{pre1_line_idx} pre2@{pre2_line_idx}"
    )
    # Placeholder resolution in the pre-step argv itself.
    assert "2026-07-31" in body


def test_daily_consolidation_live_mode_runs_pre_steps_in_order_via_stubbed_subprocess(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live mode (no --dry-run) calls subprocess.run for each pre-step, in order.

    We stub subprocess.run to record argv + return rc=0. Ordering is
    verified: pre1 (prewarm), pre2 (apple-notes), then CC.
    """
    _, workspace, _ = hermetic_profile
    # Point WORKSPACE at tmp and set a fake CC_BIN so the guard passes.
    monkeypatch.setenv(cron_verb.WORKSPACE_ENV, str(workspace))
    monkeypatch.setenv(cron_verb.CC_BIN_ENV, "/tmp/fake-cc-bin")
    monkeypatch.setenv(cron_verb.DELIVER_BIN_ENV, "/tmp/fake-deliver")
    # Pre-materialize the expected-output file so freshness passes.
    (workspace / "memory" / "daily").mkdir(parents=True, exist_ok=True)
    (workspace / "memory" / "daily" / "2026-07-31.md").write_text(
        "seeded consolidated body\n", encoding="utf-8"
    )

    calls: List[List[str]] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(
        app,
        [
            "cron",
            "run",
            "daily-consolidation",
            "--date",
            "2026-08-01",
            "--force",
        ],
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)

    # Order: pre1 (prewarm), pre2 (apple-notes), CC — subprocess.run
    # gets each in turn (no other subprocess calls happen on the happy
    # path because deliver isn't invoked when everything succeeds).
    assert len(calls) >= 3, f"expected >=3 subprocess.run calls; got {calls}"
    # First call is the prewarm-ollama pre-step.
    assert any("prewarm-ollama.sh" in a for a in calls[0]), calls[0]
    # Second call is the apple-notes export pre-step with placeholder
    # substituted.
    assert any("export-apple-notes.sh" in a for a in calls[1]), calls[1]
    assert "2026-07-31" in calls[1], (
        f"expected {{yesterday}} = 2026-07-31 substituted in pre-step 2 argv; "
        f"got {calls[1]}"
    )
    # Third call is the CC binary invocation.
    assert calls[2][0] == "/tmp/fake-cc-bin", (
        f"expected CC binary to be the third subprocess.run call; got {calls[2]}"
    )


def test_daily_consolidation_pre_step_allow_fail_true_continues(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonzero rc from the allow_fail=true pre-step fires an alert but continues.

    Pre-step 1 (`prewarm-ollama.sh`) is `allow_fail: true`. If it exits
    nonzero, the runner logs a warning, dispatches a failure alert
    (`|| true` semantics), and PROCEEDS to pre-step 2 and CC.
    """
    _, workspace, _ = hermetic_profile
    monkeypatch.setenv(cron_verb.WORKSPACE_ENV, str(workspace))
    monkeypatch.setenv(cron_verb.CC_BIN_ENV, "/tmp/fake-cc-bin")
    monkeypatch.setenv(cron_verb.DELIVER_BIN_ENV, "/tmp/fake-deliver")
    (workspace / "memory" / "daily").mkdir(parents=True, exist_ok=True)
    (workspace / "memory" / "daily" / "2026-07-31.md").write_text(
        "seeded\n", encoding="utf-8"
    )

    calls: List[List[str]] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            # pre-step 1 (prewarm) fails; everything else succeeds.
            # Distinguish by first argv element.
            def __init__(self):
                if argv and "prewarm-ollama.sh" in argv[0]:
                    self.returncode = 5
                else:
                    self.returncode = 0

        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(
        app,
        [
            "cron",
            "run",
            "daily-consolidation",
            "--date",
            "2026-08-01",
            "--force",
        ],
    )
    # Exit is clean — allow_fail=true kept the job going.
    assert result.exit_code == 0, (result.stdout, result.stderr)

    # We expect: pre1 (prewarm, fails), an alert dispatch (deliver
    # subprocess), pre2 (apple-notes, ok), CC. Ordering-wise, there's an
    # alert between pre1 and pre2 -- but the alert is a separate
    # subprocess.run call with the deliver bin. All 5 calls are present.
    # We validate:
    #   * calls[0] is prewarm.
    #   * some later call is the deliver alert with "allow_fail" text.
    #   * later still, pre2 (apple-notes) fires.
    #   * finally, CC fires.
    assert any("prewarm-ollama.sh" in a for a in calls[0]), calls[0]
    apple_indices = [
        i for i, argv in enumerate(calls) if any("export-apple-notes.sh" in a for a in argv)
    ]
    cc_indices = [
        i for i, argv in enumerate(calls) if argv and argv[0] == "/tmp/fake-cc-bin"
    ]
    deliver_indices = [
        i for i, argv in enumerate(calls) if argv and "/tmp/fake-deliver" in argv[0]
    ]
    assert apple_indices, "apple-notes pre-step never fired despite allow_fail"
    assert cc_indices, "CC never fired despite allow_fail on pre-step 1"
    assert deliver_indices, "no deliver alert fired for the allow_fail failure"
    # Order: deliver alert lands BEFORE apple-notes (the runner alerts,
    # then continues to the next pre-step).
    assert deliver_indices[0] < apple_indices[0], (
        "alert must dispatch before the runner continues to the next pre-step; "
        f"deliver at {deliver_indices}, apple-notes at {apple_indices}"
    )
    # And apple-notes fires before CC.
    assert apple_indices[0] < cc_indices[0], (
        "pre-steps must complete before CC fires"
    )
    # The alert message includes "allow_fail" and the exit code 5.
    deliver_argv = calls[deliver_indices[0]]
    joined = " ".join(deliver_argv)
    assert "allow_fail" in joined, (
        f"alert should mention allow_fail; got: {joined}"
    )
    assert "5" in joined, f"alert should include exit code 5; got: {joined}"


def test_daily_consolidation_pre_step_allow_fail_false_aborts_with_rc64(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonzero rc from the allow_fail=false pre-step aborts with rc=64 + alert.

    Pre-step 2 (`export-apple-notes.sh`) is `allow_fail: false`. A
    nonzero rc must (a) fire an alert with the pre-step number, and
    (b) short-circuit the runner with rc=64.
    """
    _, workspace, _ = hermetic_profile
    monkeypatch.setenv(cron_verb.WORKSPACE_ENV, str(workspace))
    monkeypatch.setenv(cron_verb.CC_BIN_ENV, "/tmp/fake-cc-bin")
    monkeypatch.setenv(cron_verb.DELIVER_BIN_ENV, "/tmp/fake-deliver")

    calls: List[List[str]] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            def __init__(self):
                # pre-step 2 fails.
                if argv and "export-apple-notes.sh" in argv[0]:
                    self.returncode = 7
                else:
                    self.returncode = 0

        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(
        app,
        [
            "cron",
            "run",
            "daily-consolidation",
            "--date",
            "2026-08-01",
            "--force",
        ],
    )
    assert result.exit_code == 64, (result.stdout, result.stderr)
    # pre1 + pre2 fired; CC did NOT.
    cc_calls = [c for c in calls if c and c[0] == "/tmp/fake-cc-bin"]
    assert cc_calls == [], "CC must not run after strict pre-step failure"
    # Alert dispatched with the pre-step number.
    deliver_calls = [c for c in calls if c and "/tmp/fake-deliver" in c[0]]
    assert deliver_calls, "expected a failure alert"
    joined = " ".join(deliver_calls[-1])
    assert "pre-step 2" in joined, (
        f"alert should mention pre-step 2; got: {joined}"
    )
    assert "7" in joined, f"alert should include exit code 7; got: {joined}"


# ===========================================================================
# LANDMINE — `mineru --help` must never fail
# ===========================================================================
#
# Run BEFORE and AFTER importing every verb subpackage. A broken `app.py`
# regression breaks every shim on the legacy fallback path; catching it
# here is the last line of defense.


_VERB_SUBPACKAGE_NAMES = (
    "amazon",
    "brevity",
    "browser",
    "calendar",
    "contacts",
    "cron",
    "custom",
    # 2026-09-16 audit §2B: `people` verb module renamed to `directory`.
    "directory",
    "docs",
    "drive",
    "finance",
    "gmail",
    "groups",
    "imessage",
    "memory",
    "profile",
    "secrets",
    "sheets",
    "slack",
    "tasks",
    "telegram",
)


def test_mineru_help_renders_before_reimports(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """Baseline: `mineru --help` renders cleanly before any reimport."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "cron" in result.stdout


def test_mineru_help_renders_after_reimporting_every_verb_subpackage(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LANDMINE (§7): `mineru --help` must NEVER fail after imports.

    The whole shim safety net depends on this: a broken `app.py`
    silently degrades every legacy call to the fallback binary. If a
    future refactor introduces an eager side effect in any verb
    subpackage, this test catches it.

    Note: after reimport, we ALSO reimport `mineru_cli.app` so the
    Typer group picks up the fresh sub-app references — otherwise the
    root `app` object still holds the pre-reimport instances and the
    tree renders fine but from stale objects.

    HYGIENE: uses `monkeypatch.delitem` on `sys.modules` so pytest
    restores the original module objects at teardown. A bare
    `sys.modules.pop` would leave OTHER test files' cached references
    stale — they'd see fresh class objects while their `is` assertions
    still point at the originals. This is exactly the interaction that
    broke `test_custom_verbs.py::test_after_add_new_verb_appears_in_root_help`
    during the P4-07 shakedown; the fix is to make every reimport
    scoped, not global.
    """
    # Drop every verb subpackage via monkeypatch — this snapshots the
    # OLD module object and restores it on teardown, so downstream tests
    # see the pre-P4-07 world.
    imported: List[str] = []
    for verb in _VERB_SUBPACKAGE_NAMES:
        module_name = f"mineru_cli.verbs.{verb}"
        monkeypatch.delitem(sys.modules, module_name, raising=False)
        module = importlib.import_module(module_name)
        assert module is not None, f"failed to import {module_name}"
        imported.append(module_name)
    assert len(imported) == 21, (
        f"expected 21 verb subpackages; got {len(imported)}: {imported}"
    )

    # Reimport the app module so it picks up the fresh sub-apps —
    # ALSO via monkeypatch so the ORIGINAL `mineru_cli.app` (which every
    # other test file imported at collection time and cached its `app`
    # global from) is restored at teardown.
    monkeypatch.delitem(sys.modules, "mineru_cli.app", raising=False)
    app_module = importlib.import_module("mineru_cli.app")
    result = runner.invoke(app_module.app, ["--help"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "cron" in result.stdout


def test_mineru_cron_help_renders(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`mineru cron --help` renders cleanly and mentions the read + write verbs."""
    result = runner.invoke(app, ["cron", "--help"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    for verb in ("list", "status", "logs", "edit", "plist", "diff", "run", "install", "uninstall"):
        assert verb in result.stdout, f"missing {verb!r} in cron --help"


def test_mineru_cron_list_json_renders_without_touching_real_launchd(
    hermetic_profile: Tuple[Path, Path, Path],
) -> None:
    """`mineru cron list --json` renders (autouse guard proves no live drift)."""
    import json

    result = runner.invoke(app, ["cron", "list", "--json"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    # All fixture job names are present.
    names = {row["name"] for row in payload}
    for expected in ("morning-brief", "pet-summary", "prompts-alignment", "daily-consolidation"):
        assert expected in names, (
            f"missing {expected!r} in `mineru cron list --json`; names={names}"
        )


# ===========================================================================
# DEFENSE-IN-DEPTH INVARIANTS (LOW-severity fixes, 2026-08-01)
# ===========================================================================
#
# Each block below pins one of three separate hardening steps. They are
# small individually but each closes a real bypass:
#
#   * INVARIANT DID-1 — `cron run <name> --dry-run` creates NO file
#     under the workspace `logs/<name>/`. Previously the dryrun log
#     landed at `<workspace>/logs/<name>/dryrun-<ts>.log`, which is
#     real workspace state; the fix routes it to `tempfile.mkdtemp()`.
#
#   * INVARIANT DID-2 — `cron install <name>` refuses to write through
#     a symlink at either the target plist path OR the backup path.
#     A pre-existing symlink at `<launchd_dir>/<label>.plist` would
#     otherwise clobber whatever the symlink targets (write_text /
#     shutil.copy2 both follow symlinks by default).
#
#   * INVARIANT DID-3 — `cron install <name> --live-flip
#     --backup-existing` refuses when the ACTIVE PROFILE's workspace
#     resolves to the LIVE workspace at `LIVE_WORKSPACE_PATH`, unless
#     `MINERU_CRON_ALLOW_LIVE=1` is set. Mirrors the same guard the
#     `run` verb already uses.


# ---------------------------------------------------------------------------
# INVARIANT DID-1 — dry-run creates NO file under workspace `logs/<name>/`
# ---------------------------------------------------------------------------


def test_dry_run_writes_no_file_under_workspace_logs(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cron run <name> --dry-run` must NOT create <workspace>/logs/<name>/.

    Regression pin for the LOW-severity finding: previously the dryrun
    log opened at `<workspace>/logs/<name>/dryrun-<ts>.log`, which is
    real workspace state — a preview against the live workspace would
    have silently seeded `logs/` there. The fix routes the transcript
    to a `tempfile.mkdtemp()` off-tree dir; the workspace must remain
    untouched.
    """
    _, workspace, _ = hermetic_profile
    monkeypatch.setenv(cron_verb.WORKSPACE_ENV, str(workspace))
    # No CC / DELIVER — --dry-run never invokes them, but we still
    # unset ALLOW_LIVE so this test proves the transcript-off-tree
    # behavior on the safe path.
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)

    result = runner.invoke(
        app, ["cron", "run", "morning-brief", "--dry-run"]
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)

    # `<workspace>/logs/morning-brief/` must not exist at all.
    log_dir = workspace / "logs" / "morning-brief"
    assert not log_dir.exists(), (
        f"dry-run created workspace state at {log_dir}; contents="
        f"{list(log_dir.iterdir()) if log_dir.exists() else None}"
    )
    # And no `<workspace>/logs/` at all was seeded by the preview.
    workspace_logs = workspace / "logs"
    if workspace_logs.exists():
        contents = list(workspace_logs.iterdir())
        assert contents == [], (
            f"dry-run seeded workspace logs dir; contents={contents}"
        )

    # Transcript announcement + a real off-tree file.
    assert "[dry-run] transcript at " in result.stdout
    transcript_line = next(
        line for line in result.stdout.splitlines()
        if line.startswith("[dry-run] transcript at ")
    )
    transcript_path = Path(transcript_line[len("[dry-run] transcript at "):])
    assert transcript_path.exists(), transcript_path
    # And the transcript path is NOT under the workspace.
    try:
        transcript_path.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise AssertionError(
            f"dry-run transcript landed under the workspace at {transcript_path}"
        )


def test_dry_run_against_simulated_live_workspace_writes_no_file_under_logs(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dry-run no-write-under-logs invariant holds even when the
    resolved WORKSPACE == the simulated LIVE workspace.

    This is the scenario the finding calls out: `mineru cron run <name>
    --dry-run` against `WORKSPACE=$MINERU_HOME` used to write
    a dryrun-*.log under the LIVE `<WS>/logs/<name>/`. We simulate that
    by pointing `LIVE_WORKSPACE_PATH` at the tmp workspace and hitting
    `--dry-run` — the guard bypasses (dry-run is safe) but the
    transcript must land off-tree, not in `<workspace>/logs/`.
    """
    _, workspace, _ = hermetic_profile
    monkeypatch.setenv(cron_verb.WORKSPACE_ENV, str(workspace))
    monkeypatch.setattr(
        cron_verb, "LIVE_WORKSPACE_PATH", workspace, raising=True
    )
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)

    result = runner.invoke(
        app, ["cron", "run", "morning-brief", "--dry-run"]
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)

    log_dir = workspace / "logs" / "morning-brief"
    assert not log_dir.exists(), (
        f"dry-run seeded live workspace at {log_dir}"
    )


# ---------------------------------------------------------------------------
# INVARIANT DID-2 — install refuses to write through a symlinked target
# ---------------------------------------------------------------------------


def test_install_refuses_symlinked_target_path(
    hermetic_profile: Tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing symlink at `<launchd_dir>/<label>.plist` must NOT be
    written through.

    Regression pin: `target_path.write_text(...)` follows symlinks by
    default; a symlink at the leaf would clobber whatever the symlink
    points at. The install refuses with exit 2 + a LANDMINE message and
    leaves the symlink target untouched.
    """
    _, workspace, launchd_dir = hermetic_profile
    label = "com.hermes.morning-brief"
    target_path = launchd_dir / f"{label}.plist"

    # Sentinel file the symlink targets; a naive write would clobber it.
    sentinel = tmp_path / "sentinel_that_must_not_be_touched.txt"
    sentinel.write_text("PRISTINE\n", encoding="utf-8")
    target_path.symlink_to(sentinel)

    # Full opt-in on both gates so we reach the leaf-write path.
    monkeypatch.setenv(cron_verb.ALLOW_LIVE_ENV, "1")
    # Stub launchctl so a misfiring guard doesn't shell out.
    calls: List[list] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(
        app, ["cron", "install", "morning-brief", "--live-flip"]
    )
    assert result.exit_code != 0, (
        f"install through symlinked target must refuse; got exit 0 "
        f"stdout={result.stdout!r}"
    )
    combined = result.stdout + (result.stderr or "")
    assert "LANDMINE" in combined
    assert "symlink" in combined.lower()
    # Sentinel is untouched.
    assert sentinel.read_text(encoding="utf-8") == "PRISTINE\n", (
        "install wrote through the symlink and clobbered the sentinel"
    )
    # No launchctl bootstrap happened (the refusal is BEFORE bootstrap).
    bootstrap_calls = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
    assert bootstrap_calls == [], (
        f"expected no bootstrap on refusal path; got {bootstrap_calls}"
    )


def test_install_refuses_symlinked_backup_path(
    hermetic_profile: Tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing symlink at the backup destination must NOT be written through.

    Same shape as the target-symlink guard, but for `_perform_backup`.
    `shutil.copy2` follows symlinks at the destination, so a symlink
    at `<workspace>/archive/launchd-backup-<today>/<name>.plist` would
    clobber the symlink target. The install refuses with exit 2 + a
    LANDMINE message and leaves the symlink target untouched.
    """
    import datetime as _dt
    _, workspace, launchd_dir = hermetic_profile
    label = "com.hermes.morning-brief"
    target_path = launchd_dir / f"{label}.plist"
    # Seed a pre-existing "currently installed" plist so backup runs.
    target_path.write_text(
        "<?xml version=\"1.0\"?><plist>SEED</plist>\n", encoding="utf-8"
    )

    # Sentinel the backup symlink points at.
    sentinel = tmp_path / "backup_sentinel_that_must_not_be_touched.txt"
    sentinel.write_text("PRISTINE_BACKUP\n", encoding="utf-8")

    # Plant a symlink at the backup destination.
    today = _dt.date.today().isoformat()
    backup_dir = workspace / "archive" / f"launchd-backup-{today}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dest = backup_dir / "morning-brief.plist"
    backup_dest.symlink_to(sentinel)

    monkeypatch.setenv(cron_verb.ALLOW_LIVE_ENV, "1")
    calls: List[list] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(
        app,
        [
            "cron", "install", "morning-brief",
            "--live-flip", "--backup-existing",
        ],
    )
    assert result.exit_code != 0, (
        f"install with symlinked backup destination must refuse; got exit 0"
    )
    combined = result.stdout + (result.stderr or "")
    assert "LANDMINE" in combined
    assert "symlink" in combined.lower()
    # Sentinel untouched.
    assert sentinel.read_text(encoding="utf-8") == "PRISTINE_BACKUP\n", (
        "install copied through the backup symlink and clobbered the sentinel"
    )


# ---------------------------------------------------------------------------
# INVARIANT DID-3 — install refuses live-workspace backup without ALLOW_LIVE
# ---------------------------------------------------------------------------


def test_install_refuses_live_workspace_profile_without_allow_live(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cron install --live-flip --backup-existing` with the active profile
    pointing at the LIVE workspace must refuse without ALLOW_LIVE=1.

    Regression pin for the LOW-severity finding: the previous behavior
    would happily write backups into
    `<LIVE_WORKSPACE>/archive/launchd-backup-<today>/` any time the
    active profile's workspace_absolute matched the live workspace,
    even though the operator only opted in to `--live-flip` for a
    mirror `MINERU_LAUNCHD_DIR`. The fix mirrors the `run` verb's
    HARD-SAFETY refusal.

    Defense-in-depth posture: the outer `_refuse_live_install_without_optin`
    gate ALREADY refuses without ALLOW_LIVE=1 (via the --live-flip
    requirement). This test additionally proves the workspace-level
    guard is present and fires — we neutralize the outer gate so the
    inner one is the ONLY refusal path exercised. That way a future
    refactor of the outer gate doesn't silently open a bypass to the
    live workspace.
    """
    _, workspace, _ = hermetic_profile
    # Simulate "profile.workspace_absolute == LIVE_WORKSPACE_PATH" by
    # rewiring the constant to the tmp workspace (the fixture profile
    # already points at that same tmp workspace).
    monkeypatch.setattr(
        cron_verb, "LIVE_WORKSPACE_PATH", workspace, raising=True
    )
    # ALLOW_LIVE explicitly unset — must trip the inner guard.
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)
    # Neutralize the outer live-install gate so the inner workspace
    # guard is the ONLY refusal path this test exercises.
    monkeypatch.setattr(
        cron_verb,
        "_refuse_live_install_without_optin",
        lambda *a, **k: None,
        raising=True,
    )

    calls: List[list] = []

    def _fake_subprocess_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(
        app,
        [
            "cron", "install", "morning-brief",
            "--live-flip", "--backup-existing",
        ],
    )
    assert result.exit_code != 0, (
        "install with LIVE workspace profile + no ALLOW_LIVE must refuse; "
        f"got exit 0 stdout={result.stdout!r}"
    )
    combined = result.stdout + (result.stderr or "")
    assert "HARD SAFETY" in combined, (
        f"expected inner-guard 'HARD SAFETY' message; got: {combined!r}"
    )
    assert cron_verb.ALLOW_LIVE_ENV in combined
    # No backup created.
    import datetime as _dt
    today = _dt.date.today().isoformat()
    backup_dir = workspace / "archive" / f"launchd-backup-{today}"
    assert not backup_dir.exists(), (
        f"install refusal still created backup dir at {backup_dir}"
    )
    # No launchctl call happened.
    bootstrap_calls = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
    assert bootstrap_calls == []


def test_install_live_workspace_profile_allowed_with_allow_live(
    hermetic_profile: Tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `MINERU_CRON_ALLOW_LIVE=1`, the live-workspace install proceeds.

    Complements the refusal test: proves the guard is a gate, not a
    permanent blocker. The operator can still explicitly opt in to
    installing against the live workspace, matching the `run` verb's
    ALLOW_LIVE escape.
    """
    _, workspace, launchd_dir = hermetic_profile
    monkeypatch.setattr(
        cron_verb, "LIVE_WORKSPACE_PATH", workspace, raising=True
    )
    monkeypatch.setenv(cron_verb.ALLOW_LIVE_ENV, "1")

    def _fake_subprocess_run(argv, *args, **kwargs):
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(cron_verb.subprocess, "run", _fake_subprocess_run)

    result = runner.invoke(
        app,
        ["cron", "install", "morning-brief", "--live-flip"],
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    target = launchd_dir / "com.hermes.morning-brief.plist"
    assert target.exists(), f"expected {target} to be written"
