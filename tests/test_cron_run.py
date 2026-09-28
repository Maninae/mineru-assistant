"""Tests for `mineru cron run` (P4-04) — in-band job runner.

⚠️⚠️ HARD SAFETY POSTURE — READ TWICE ⚠️⚠️

  NO test in this file EVER writes to `~/Library/LaunchAgents`. The runner
  doesn't touch launchd at all; a paranoia guard snapshots the dir pre/post
  every test and asserts equality.

  NO test in this file EVER writes into `$MINERU_HOME/memory/`
  or any other subdir of the live workspace. Every runner invocation sets
  `WORKSPACE=tmp_path` so the runner's writes land under `tmp_path/logs/`
  only.

  NO test in this file EVER invokes real `claude-fda` or the real
  `deliver-output.py`. `CC_BIN` and `DELIVER_BIN` are both pointed at
  bash stubs under `tmp_path` that capture their argv to a file and
  exit cleanly (or with a scripted rc).

  NO test in this file EVER runs `launchctl`.

Coverage:

  * `--dry-run` on an LLM job with no idempotency + no expected-output
    (memory-description-shape) prints the prompt + argv and NEITHER
    invokes the CC stub nor the DELIVER stub.
  * HARD SAFETY snapshot guard: before + after every test,
    `$MINERU_HOME/memory/` and `~/Library/LaunchAgents/` inode listings
    are compared — any drift fails the whole file.
  * `--force` overrides an idempotency guard that would otherwise skip.
  * The idempotency skip fires with a byte-for-byte message when the
    marker is today's mtime.
  * HARD-SAFETY refusal: `WORKSPACE=$MINERU_HOME` + default
    CC_BIN + no `MINERU_CRON_ALLOW_LIVE` = refusal with exit 2.
  * Standard prompt: CC argv matches `cc-job-lib.sh`'s exact line
    (`--permission-mode bypassPermissions --model X --verbose --print
    "Read <instr> and execute the job."`).
  * Custom prompt: instruction body + resolved `{yesterday}` suffix is
    concatenated into the `--print` payload.
  * Pre-steps: run BEFORE the CC invocation, in declared order;
    `allow_fail=true` continues after failure, `allow_fail=false`
    short-circuits with rc=64 + alert.
  * Expected-output check: a missing glob hit after CC=0 triggers
    rc=64 + a matching Telegram alert.
  * CC exits nonzero: an alert fires but the underlying rc is not
    masked (rc == the CC exit code).
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
)
from mineru_cli.verbs import cron as cron_verb


# The current typer/click here merges stderr into stdout by default; we
# assert on the merged output instead of relying on a split. Refusal
# messages appear regardless of which stream they landed on.
runner = CliRunner()


# ---------------------------------------------------------------------------
# HARD-SAFETY snapshot guard — autouse fixture
# ---------------------------------------------------------------------------
#
# The `snapshot` returns a frozenset of (path, mtime_ns) tuples for
# every regular file at the target root. Symlinks are resolved before
# stat so a swap-out is detected. A test that accidentally writes into
# these roots will diverge from the pre-snapshot and fail.
#
# We autouse this so an implementation regression that starts writing
# to live paths (e.g. a bad default) fails EVERY test in this file, not
# just the tests that explicitly assert on the paths.


def _snapshot_tree(root: Path) -> frozenset:
    """Return a frozenset of (str_path, size, mtime_ns) for each file under root.

    Missing root returns an empty frozenset — the paranoia guard only
    fires on drift, not on absence.
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
            # Best-effort: a race where a file vanished between rglob
            # and stat is fine; we're checking for writes, not reads.
            continue
    return frozenset(out)


LIVE_MEMORY_DIR = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "memory"
LIVE_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


@pytest.fixture(autouse=True)
def hard_safety_no_live_writes():
    """Fail the test if it writes to live memory / LaunchAgents dirs.

    This is the whole point of P4-04's toy-test: NO test may accidentally
    fire a real recurring job. The snapshot is defensive belt-and-braces
    on top of the per-test WORKSPACE seeding.
    """
    before_memory = _snapshot_tree(LIVE_MEMORY_DIR)
    before_launchagents = _snapshot_tree(LIVE_LAUNCHAGENTS_DIR)
    yield
    after_memory = _snapshot_tree(LIVE_MEMORY_DIR)
    after_launchagents = _snapshot_tree(LIVE_LAUNCHAGENTS_DIR)
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


# ---------------------------------------------------------------------------
# Fixture: a hermetic profile whose cron.yaml exercises every code path.
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

  # No idempotency, no expected_output_glob — matches the memory-description
  # shape. The dry-run smoke test targets this job because it exercises the
  # minimal LLM path (no guards).
  - name: memory-description
    kind: llm
    schedule: "19 2 * * *"
    model: claude-sonnet-4-6
    instruction: recurring/memory-description-maintenance.md

  # Idempotency + expected_output_glob — matches morning-brief.
  - name: morning-brief
    kind: llm
    schedule: "0 7 * * *"
    model: claude-opus-4-6
    instruction: recurring/morning-brief.md
    expected_output_glob: briefs_morning/morning-*.md
    idempotency_marker: briefs_morning/morning-{today}.md
    timeout_seconds: 3600

  # custom_prompt + suffix + placeholder — matches daily-consolidation.
  - name: daily-consolidation
    kind: llm
    schedule: "0 1 * * *"
    model: claude-sonnet-4-6
    instruction: recurring/consolidate-daily-memories.md
    idempotency_marker: memory/daily/{yesterday}.md
    expected_output_glob: memory/daily/{yesterday}.md
    custom_prompt: true
    custom_prompt_suffix: "\\n\\nIMPORTANT: The target date is {yesterday}. Use this date everywhere."
    timeout_seconds: 7200

  # LLM job with two pre-steps: one allow_fail=true, one allow_fail=false.
  # Used to verify pre-step ordering + allow_fail semantics.
  - name: pre-step-job
    kind: llm
    schedule: "0 5 * * *"
    model: claude-sonnet-4-6
    instruction: recurring/pre-step-job.md
    pre_steps:
      - cmd:
          - /tmp/does-not-matter/pre1.sh
        allow_fail: true
        comment: "First pre-step (allow_fail)."
      - cmd:
          - /tmp/does-not-matter/pre2.sh
        allow_fail: false
        comment: "Second pre-step (strict)."

  # Script job — exercises the non-LLM branch.
  - name: cleanup-retention
    kind: script
    schedule: "7 4 2 * *"
    program_args:
      - /bin/echo
      - hello-from-script-job
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


def _write_recurring_files(workspace: Path) -> None:
    """Materialize the recurring/*.md files the fixture's LLM jobs reference."""
    recurring = workspace / "recurring"
    recurring.mkdir(parents=True, exist_ok=True)
    (recurring / "memory-description-maintenance.md").write_text(
        "# memory-description\nmaintenance body\n", encoding="utf-8"
    )
    (recurring / "morning-brief.md").write_text(
        "# morning-brief\nbrief body\n", encoding="utf-8"
    )
    (recurring / "consolidate-daily-memories.md").write_text(
        "CONSOLIDATE BODY LINE 1\nCONSOLIDATE BODY LINE 2\n",
        encoding="utf-8",
    )
    (recurring / "pre-step-job.md").write_text(
        "# pre-step-job\npre-step body\n", encoding="utf-8"
    )


def _make_stub(
    path: Path,
    *,
    capture_file: Path,
    exit_code: int = 0,
) -> None:
    """Write an executable bash stub at `path` that captures its argv.

    The stub appends one line per invocation to `capture_file`. Each
    line is a shell-quoted representation of argv (`$0 $1 $2 ...`) so
    the test can split and inspect it. The stub NEVER shells out to any
    real binary; it just echoes and returns `exit_code`.
    """
    path.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\0' \"$0\" \"$@\" >> {shlex.quote(str(capture_file))}\n"
        f"printf '\\n<<<END-OF-INVOCATION>>>\\n' >> {shlex.quote(str(capture_file))}\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _read_invocations(capture_file: Path) -> List[List[str]]:
    """Parse a capture file into a list-of-argv."""
    if not capture_file.exists():
        return []
    raw = capture_file.read_bytes()
    if not raw:
        return []
    out: List[List[str]] = []
    # Each invocation is <argv joined by NUL>\n<<<END-OF-INVOCATION>>>\n
    for chunk in raw.split(b"\n<<<END-OF-INVOCATION>>>\n"):
        if not chunk:
            continue
        argv = [part.decode("utf-8") for part in chunk.split(b"\x00") if part]
        if argv:
            out.append(argv)
    return out


@pytest.fixture
def hermetic_run_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Dict[str, Path]:
    """Set up a hermetic workspace + profile + stubs for the runner.

    Returns a dict with:
      * `workspace`     — WORKSPACE root
      * `profile_dir`   — the profile the runner resolves
      * `cc_bin`        — path to the CC stub script
      * `cc_capture`    — file the CC stub appends to
      * `deliver_bin`   — path to the DELIVER stub script
      * `deliver_capture` — file the DELIVER stub appends to
      * `launchd_dir`   — tmp launchd dir (never touched by run, but the
                          read verbs share the same env, and the fixture
                          keeps the world consistent)
    """
    profile_base = tmp_path / "profiles"
    workspace = tmp_path / "workspace"
    stubs_dir = tmp_path / "stubs"
    stubs_dir.mkdir(parents=True, exist_ok=True)
    launchd_dir = tmp_path / "launchagents"
    launchd_dir.mkdir(parents=True, exist_ok=True)

    profile_dir = _write_profile_yaml(profile_base, "hermes", workspace)
    _write_recurring_files(workspace)

    cc_capture = tmp_path / "cc-invocations.txt"
    deliver_capture = tmp_path / "deliver-invocations.txt"
    cc_bin = stubs_dir / "claude-fda-stub.sh"
    deliver_bin = stubs_dir / "deliver-stub.sh"
    _make_stub(cc_bin, capture_file=cc_capture, exit_code=0)
    _make_stub(deliver_bin, capture_file=deliver_capture, exit_code=0)

    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(profile_base))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "hermes")
    monkeypatch.setenv(cron_verb.WORKSPACE_ENV, str(workspace))
    monkeypatch.setenv(cron_verb.CC_BIN_ENV, str(cc_bin))
    # DELIVER_BIN is a shell string (matches cc-job-lib.sh) so shlex.split
    # is exercised end-to-end.
    monkeypatch.setenv(cron_verb.DELIVER_BIN_ENV, str(deliver_bin))
    monkeypatch.setenv(cron_verb.LAUNCHD_DIR_ENV, str(launchd_dir))
    # Not set: MINERU_CRON_ALLOW_LIVE — the guard only fires when
    # WORKSPACE resolves to LIVE_WORKSPACE_PATH, which never happens
    # under a tmp workspace.

    return {
        "workspace": workspace,
        "profile_dir": profile_dir,
        "cc_bin": cc_bin,
        "cc_capture": cc_capture,
        "deliver_bin": deliver_bin,
        "deliver_capture": deliver_capture,
        "launchd_dir": launchd_dir,
    }


# ---------------------------------------------------------------------------
# `--dry-run` sanity + argv capture
# ---------------------------------------------------------------------------


def test_dry_run_prints_prompt_and_does_not_invoke_stubs(
    hermetic_run_env: Dict[str, Path],
) -> None:
    """`mineru cron run memory-description --dry-run`:

    - prints the CC argv + prompt to stdout,
    - writes NO file under the workspace's `logs/<name>/` (the dryrun
      transcript is routed to a `tempfile.mkdtemp()` off-tree so a
      preview against the live workspace touches nothing),
    - prints the transcript path so the operator can find it,
    - NEITHER invokes the CC stub NOR the DELIVER stub.
    """
    env = hermetic_run_env
    result = runner.invoke(app, ["cron", "run", "memory-description", "--dry-run"])
    assert result.exit_code == 0, (result.stdout, result.stderr)

    # Standard prompt is present in stdout — this is the exact string
    # cc-job-lib.sh sends.
    assert (
        "Read recurring/memory-description-maintenance.md and execute the job."
        in result.stdout
    )
    # cc-argv line surfaces so the operator can copy-paste it.
    assert "[cc-argv]" in result.stdout
    # No skip notice — no idempotency marker on this job.
    assert "idempotent_guard" not in result.stdout

    # Neither stub was invoked.
    assert _read_invocations(env["cc_capture"]) == []
    assert _read_invocations(env["deliver_capture"]) == []

    # Dryrun transcript path is announced.
    assert "[dry-run] transcript at" in result.stdout
    # Workspace `logs/memory-description/` was NOT created by the
    # preview. Contract: --dry-run must not seed live-workspace state.
    log_dir = env["workspace"] / "logs" / "memory-description"
    assert not log_dir.exists(), (
        f"dry-run must NOT create {log_dir}; found "
        f"{[p.name for p in log_dir.iterdir()] if log_dir.exists() else None}"
    )

    # The transcript itself sits under a tempdir; read it and confirm
    # the header/argv lines got written there.
    transcript_line = next(
        line for line in result.stdout.splitlines()
        if line.startswith("[dry-run] transcript at ")
    )
    transcript_path = Path(transcript_line[len("[dry-run] transcript at "):])
    assert transcript_path.exists(), (
        f"expected tempfile transcript at {transcript_path}"
    )
    # Not under the workspace.
    try:
        transcript_path.relative_to(env["workspace"])
    except ValueError:
        pass
    else:
        raise AssertionError(
            f"dry-run transcript landed under the workspace at {transcript_path}"
        )
    body = transcript_path.read_text(encoding="utf-8")
    assert "Starting memory-description" in body
    assert "cc-argv" in body


def test_dry_run_writes_no_files_outside_workspace_and_launchd(
    hermetic_run_env: Dict[str, Path],
    tmp_path: Path,
) -> None:
    """No writes anywhere under the workspace at all under --dry-run.

    Tightened after the dry-run transcript was moved off-tree into a
    `tempfile.mkdtemp()` dir: a preview must not create ANY file under
    `<workspace>/`, including `<workspace>/logs/<name>/`. That way a
    stray `mineru cron run <name> --dry-run` against the live workspace
    is a true no-op instead of silently seeding `logs/<name>/dryrun-*.log`.
    """
    env = hermetic_run_env

    def snapshot_all(root: Path) -> frozenset:
        out = set()
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            st = p.stat()
            out.add((str(rel), st.st_size))
        return frozenset(out)

    before = snapshot_all(env["workspace"])
    result = runner.invoke(app, ["cron", "run", "memory-description", "--dry-run"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    after = snapshot_all(env["workspace"])
    assert before == after, (
        f"--dry-run wrote files under the workspace: added={after - before}, "
        f"removed={before - after}"
    )
    # Launchd dir must remain empty (it started empty).
    assert list(env["launchd_dir"].iterdir()) == []


# ---------------------------------------------------------------------------
# Idempotency guard + --force override
# ---------------------------------------------------------------------------


def _seed_today_marker(workspace: Path, rel: str) -> Path:
    """Materialize a marker file whose mtime date == today (system TZ)."""
    path = workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("seeded\n", encoding="utf-8")
    # os.utime unnecessary — write_text sets mtime to now, which is today.
    return path


def test_idempotency_skip_when_marker_is_today(
    hermetic_run_env: Dict[str, Path],
) -> None:
    """A today-mtime marker triggers the byte-for-byte skip and rc=0."""
    env = hermetic_run_env
    # Use --date to pin today's string; the marker file was written now
    # so its mtime IS today.
    import datetime
    today = datetime.date.today().isoformat()
    marker = _seed_today_marker(env["workspace"], f"briefs_morning/morning-{today}.md")

    result = runner.invoke(
        app, ["cron", "run", "morning-brief", "--date", today]
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    # cc-job-lib.sh line 84 byte-for-byte.
    assert "[idempotent_guard] Already ran today:" in result.stdout
    assert str(marker) in result.stdout
    # No CC / DELIVER invocation because we short-circuited.
    assert _read_invocations(env["cc_capture"]) == []


def test_force_overrides_idempotency_guard(
    hermetic_run_env: Dict[str, Path],
) -> None:
    """`--force` runs the job even when today's marker is present.

    We pair --force with --dry-run so no real subprocess fires; the
    assertion is that the runner PROCEEDS to the prompt-building stage
    (transcript announced, prompt printed).
    """
    env = hermetic_run_env
    import datetime
    today = datetime.date.today().isoformat()
    _seed_today_marker(env["workspace"], f"briefs_morning/morning-{today}.md")

    result = runner.invoke(
        app,
        ["cron", "run", "morning-brief", "--force", "--dry-run", "--date", today],
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "idempotent_guard" not in result.stdout
    assert (
        "Read recurring/morning-brief.md and execute the job." in result.stdout
    )
    # Dry-run must not seed <workspace>/logs/morning-brief/ — the
    # transcript is routed to a tempfile.mkdtemp() off-tree.
    log_dir = env["workspace"] / "logs" / "morning-brief"
    assert not log_dir.exists(), (
        f"dry-run must NOT create {log_dir}"
    )
    # And the transcript announcement is present.
    assert "[dry-run] transcript at" in result.stdout


# ---------------------------------------------------------------------------
# HARD-SAFETY refusal
# ---------------------------------------------------------------------------


def test_hard_safety_refuses_live_workspace_with_default_cc_bin(
    hermetic_run_env: Dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HARD SAFETY: WORKSPACE=live + default CC_BIN + no allow-live -> refuse.

    We simulate "live" by pointing `LIVE_WORKSPACE_PATH` at the tmp
    workspace itself. Then we unset `CC_BIN` so the runner falls back
    to `DEFAULT_CC_BIN` (which does NOT exist under tmp, so a real
    invocation would fail loud — but the refusal fires first).
    """
    env = hermetic_run_env
    # Rewire the live-workspace constant so the guard fires against
    # our tmp workspace (which is what WORKSPACE resolves to).
    monkeypatch.setattr(
        cron_verb, "LIVE_WORKSPACE_PATH", env["workspace"], raising=True
    )
    # Unset CC_BIN so the guard sees the default.
    monkeypatch.delenv(cron_verb.CC_BIN_ENV, raising=False)
    # Ensure ALLOW_LIVE is NOT set.
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)

    result = runner.invoke(app, ["cron", "run", "memory-description"])
    assert result.exit_code == 2, (result.stdout, result.stderr)
    assert "HARD SAFETY" in result.stderr or "HARD SAFETY" in result.stdout
    # No CC stub invocation regardless.
    assert _read_invocations(env["cc_capture"]) == []


def test_hard_safety_allow_live_1_permits_run(
    hermetic_run_env: Dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MINERU_CRON_ALLOW_LIVE=1` opts out of the refusal.

    The CC stub is used (path from the fixture); the workspace is
    pointed at the "live" constant to satisfy the geometry. The runner
    proceeds and the stub is invoked exactly once.
    """
    env = hermetic_run_env
    monkeypatch.setattr(
        cron_verb, "LIVE_WORKSPACE_PATH", env["workspace"], raising=True
    )
    # CC_BIN is set (the stub); default check would exit here because
    # the override bypasses the refusal, but we still test the opt-out
    # by keeping the workspace-live geometry.
    monkeypatch.setenv(cron_verb.ALLOW_LIVE_ENV, "1")

    result = runner.invoke(app, ["cron", "run", "memory-description"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    invocations = _read_invocations(env["cc_capture"])
    assert len(invocations) == 1


def test_hard_safety_dry_run_bypasses_refusal(
    hermetic_run_env: Dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--dry-run` skips the refusal even on live geometry + default CC_BIN."""
    env = hermetic_run_env
    monkeypatch.setattr(
        cron_verb, "LIVE_WORKSPACE_PATH", env["workspace"], raising=True
    )
    monkeypatch.delenv(cron_verb.CC_BIN_ENV, raising=False)
    monkeypatch.delenv(cron_verb.ALLOW_LIVE_ENV, raising=False)

    result = runner.invoke(
        app, ["cron", "run", "memory-description", "--dry-run"]
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)


# ---------------------------------------------------------------------------
# Real CC argv shape (byte-for-byte parity with cc-job-lib.sh)
# ---------------------------------------------------------------------------


def test_standard_prompt_cc_argv_matches_cc_job_lib(
    hermetic_run_env: Dict[str, Path],
) -> None:
    """CC argv from the runner matches `cc-job-lib.sh::run_cc_job` exactly.

    The bash line is:
      "$CC_BIN" --permission-mode bypassPermissions --model "$model" \\
                --verbose --print "Read $instr and execute the job."
    """
    env = hermetic_run_env
    result = runner.invoke(app, ["cron", "run", "memory-description"])
    assert result.exit_code == 0, (result.stdout, result.stderr)

    invocations = _read_invocations(env["cc_capture"])
    assert len(invocations) == 1
    argv = invocations[0]
    # argv[0] is bash's $0 — the stub's own path.
    assert argv[0] == str(env["cc_bin"])
    # The remaining args (starting at [1]) mirror the cc-job-lib.sh argv.
    assert argv[1:] == [
        "--permission-mode",
        "bypassPermissions",
        "--model",
        "claude-sonnet-4-6",
        "--verbose",
        "--print",
        "Read recurring/memory-description-maintenance.md and execute the job.",
    ]


# ---------------------------------------------------------------------------
# Custom prompt + suffix + `{yesterday}` placeholder
# ---------------------------------------------------------------------------


def test_custom_prompt_concatenates_body_and_resolved_suffix(
    hermetic_run_env: Dict[str, Path],
) -> None:
    """daily-consolidation-style job: prompt = instruction body + resolved suffix.

    We pin `--date` so `{yesterday}` resolves deterministically. We also
    pre-materialize the expected-output file so the freshness check
    passes (the CC stub doesn't produce the real consolidated file).
    """
    env = hermetic_run_env
    # `--date 2026-08-01` -> {yesterday} = 2026-07-31.
    (env["workspace"] / "memory" / "daily").mkdir(parents=True, exist_ok=True)
    (env["workspace"] / "memory" / "daily" / "2026-07-31.md").write_text(
        "seeded consolidated body\n", encoding="utf-8"
    )
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

    invocations = _read_invocations(env["cc_capture"])
    assert len(invocations) == 1
    argv = invocations[0]
    prompt = argv[-1]  # the last arg is the --print payload
    # Instruction body must be present verbatim.
    assert "CONSOLIDATE BODY LINE 1" in prompt
    assert "CONSOLIDATE BODY LINE 2" in prompt
    # Suffix must be present with `{yesterday}` resolved (yesterday of
    # 2026-08-01 is 2026-07-31).
    assert "IMPORTANT: The target date is 2026-07-31." in prompt
    # And it must be APPENDED (body first, suffix after).
    body_idx = prompt.index("CONSOLIDATE BODY LINE 2")
    suffix_idx = prompt.index("IMPORTANT: The target date is 2026-07-31.")
    assert body_idx < suffix_idx


# ---------------------------------------------------------------------------
# Pre-steps: ordering, allow_fail semantics
# ---------------------------------------------------------------------------


@pytest.fixture
def pre_step_stubs(
    hermetic_run_env: Dict[str, Path], tmp_path: Path
) -> Dict[str, Path]:
    """Materialize the two pre-step stubs referenced by pre-step-job.

    Returns:
      * `pre1_bin`, `pre1_capture` — pre-step 1 stub (allow_fail=true)
      * `pre2_bin`, `pre2_capture` — pre-step 2 stub (allow_fail=false)
      * `order_log` — file all three stubs (pre1, pre2, cc) append to
                      so we can verify ordering.
    """
    # Rewrite the cron.yaml's pre-step paths to point at tmp stubs.
    env = hermetic_run_env
    order_log = tmp_path / "order.log"
    stubs_dir = tmp_path / "prestep-stubs"
    stubs_dir.mkdir(parents=True, exist_ok=True)
    pre1_bin = stubs_dir / "pre1.sh"
    pre2_bin = stubs_dir / "pre2.sh"

    def _order_stub(path: Path, tag: str, exit_code: int) -> None:
        path.write_text(
            "#!/bin/bash\n"
            f"echo {tag} >> {shlex.quote(str(order_log))}\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    _order_stub(pre1_bin, "pre1", exit_code=0)
    _order_stub(pre2_bin, "pre2", exit_code=0)

    # Also swap the CC stub for one that tags "cc" into the same log,
    # so we can inspect the whole ordering in one file.
    cc_bin = env["cc_bin"]
    cc_bin.write_text(
        "#!/bin/bash\n"
        f"echo cc >> {shlex.quote(str(order_log))}\n"
        f"printf '%s\\0' \"$0\" \"$@\" >> {shlex.quote(str(env['cc_capture']))}\n"
        f"printf '\\n<<<END-OF-INVOCATION>>>\\n' >> {shlex.quote(str(env['cc_capture']))}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    cc_bin.chmod(cc_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Rewrite the cron.yaml to point at the tmp pre-step stubs.
    cron_yaml = env["profile_dir"] / "cron.yaml"
    body = cron_yaml.read_text(encoding="utf-8")
    body = body.replace("/tmp/does-not-matter/pre1.sh", str(pre1_bin))
    body = body.replace("/tmp/does-not-matter/pre2.sh", str(pre2_bin))
    cron_yaml.write_text(body, encoding="utf-8")

    return {
        "pre1_bin": pre1_bin,
        "pre2_bin": pre2_bin,
        "order_log": order_log,
    }


def test_pre_steps_run_before_cc_in_declared_order(
    hermetic_run_env: Dict[str, Path],
    pre_step_stubs: Dict[str, Path],
) -> None:
    """Both pre-steps fire, in order, BEFORE the CC invocation."""
    env = hermetic_run_env
    result = runner.invoke(app, ["cron", "run", "pre-step-job"])
    assert result.exit_code == 0, (result.stdout, result.stderr)

    order = pre_step_stubs["order_log"].read_text(encoding="utf-8").splitlines()
    assert order == ["pre1", "pre2", "cc"], f"unexpected order: {order}"


def test_pre_step_allow_fail_true_continues(
    hermetic_run_env: Dict[str, Path],
    pre_step_stubs: Dict[str, Path],
) -> None:
    """A non-zero pre1 (allow_fail=true) fires an alert but continues to pre2 + CC."""
    # Rewrite pre1 to exit 5.
    pre1 = pre_step_stubs["pre1_bin"]
    body = pre1.read_text(encoding="utf-8").replace("exit 0", "exit 5")
    pre1.write_text(body, encoding="utf-8")

    env = hermetic_run_env
    result = runner.invoke(app, ["cron", "run", "pre-step-job"])
    assert result.exit_code == 0, (result.stdout, result.stderr)

    # All three still ran.
    order = pre_step_stubs["order_log"].read_text(encoding="utf-8").splitlines()
    assert order == ["pre1", "pre2", "cc"]

    # And DELIVER got the alert.
    deliveries = _read_invocations(env["deliver_capture"])
    assert deliveries, "expected an alert delivery for the allow_fail=true failure"
    # Alert message includes "allow_fail, continuing" and the exit code.
    joined = " ".join(deliveries[0])
    assert "allow_fail" in joined
    assert "5" in joined


def test_pre_step_allow_fail_false_short_circuits_with_rc64(
    hermetic_run_env: Dict[str, Path],
    pre_step_stubs: Dict[str, Path],
) -> None:
    """A non-zero pre2 (allow_fail=false) stops the run with rc=64 + alert."""
    pre2 = pre_step_stubs["pre2_bin"]
    body = pre2.read_text(encoding="utf-8").replace("exit 0", "exit 7")
    pre2.write_text(body, encoding="utf-8")

    env = hermetic_run_env
    result = runner.invoke(app, ["cron", "run", "pre-step-job"])
    assert result.exit_code == 64, (result.stdout, result.stderr)

    # pre1 + pre2 ran; CC did NOT.
    order = pre_step_stubs["order_log"].read_text(encoding="utf-8").splitlines()
    assert order == ["pre1", "pre2"], f"cc must not have run: {order}"

    deliveries = _read_invocations(env["deliver_capture"])
    assert deliveries, "expected a failure alert"
    joined = " ".join(deliveries[0])
    # The strict-failure message includes the pre-step number and the exit.
    assert "pre-step 2" in joined
    assert "7" in joined


# ---------------------------------------------------------------------------
# Expected-output check + failure alert
# ---------------------------------------------------------------------------


def test_expected_output_missing_sets_rc64_and_alerts(
    hermetic_run_env: Dict[str, Path],
) -> None:
    """CC=0 but no fresh output matching the glob -> rc=64 + alert."""
    env = hermetic_run_env
    # morning-brief has expected_output_glob=briefs_morning/morning-*.md.
    # No such file exists in tmp workspace -> freshness fails -> rc=64.
    result = runner.invoke(app, ["cron", "run", "morning-brief"])
    assert result.exit_code == 64, (result.stdout, result.stderr)

    deliveries = _read_invocations(env["deliver_capture"])
    assert deliveries, "expected a failure alert"
    joined = " ".join(deliveries[0])
    assert "no recent output matched" in joined
    assert "briefs_morning/morning-*.md" in joined


def test_cc_nonzero_exits_alerts_but_preserves_rc(
    hermetic_run_env: Dict[str, Path],
) -> None:
    """CC exits nonzero -> alert fires, but runner surfaces the underlying rc.

    We rewrite the CC stub to exit 42; the alert must not mask that.
    """
    env = hermetic_run_env
    _make_stub(env["cc_bin"], capture_file=env["cc_capture"], exit_code=42)

    result = runner.invoke(app, ["cron", "run", "memory-description"])
    assert result.exit_code == 42, (result.stdout, result.stderr)

    deliveries = _read_invocations(env["deliver_capture"])
    assert deliveries, "expected a failure alert"
    joined = " ".join(deliveries[0])
    assert "CC exited nonzero" in joined
    assert "42" in joined


# ---------------------------------------------------------------------------
# Script job path
# ---------------------------------------------------------------------------


def test_script_job_runs_program_args_without_cc(
    hermetic_run_env: Dict[str, Path],
) -> None:
    """A `kind: script` job invokes its argv directly, no CC involvement."""
    env = hermetic_run_env
    # cleanup-retention runs `/bin/echo hello-from-script-job`.
    result = runner.invoke(app, ["cron", "run", "cleanup-retention"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    # No CC invocation.
    assert _read_invocations(env["cc_capture"]) == []

    # The echo output landed in the log.
    log_dir = env["workspace"] / "logs" / "cleanup-retention"
    logs = sorted(log_dir.glob("*.log"))
    assert logs
    body = logs[0].read_text(encoding="utf-8")
    assert "hello-from-script-job" in body


def test_script_job_dry_run_prints_argv_without_invoking(
    hermetic_run_env: Dict[str, Path],
) -> None:
    """--dry-run on a script job prints the argv, doesn't invoke."""
    env = hermetic_run_env
    result = runner.invoke(
        app, ["cron", "run", "cleanup-retention", "--dry-run"]
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "SCRIPT argv=" in result.stdout
    assert "/bin/echo" in result.stdout
    # Nothing landed in the script's log dir (a real invocation would
    # have written both a `<ts>.log` AND captured /bin/echo's output).
    log_dir = env["workspace"] / "logs" / "cleanup-retention"
    real_logs = list(log_dir.glob("[0-9]*.log"))
    assert real_logs == [], "dry-run must not produce a real <ts>.log"


# ---------------------------------------------------------------------------
# `mineru --help` LANDMINE: root help must still render with `run` registered.
# ---------------------------------------------------------------------------


def test_root_help_still_renders_with_run_registered() -> None:
    """LANDMINE: `mineru --help` must never fail (shim health-probe)."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "cron" in result.stdout


def test_cron_run_help_renders() -> None:
    """`mineru cron run --help` documents --force / --dry-run / --date."""
    result = runner.invoke(app, ["cron", "run", "--help"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    for flag in ("--force", "--dry-run", "--date"):
        assert flag in result.stdout, f"missing {flag!r} in cron run --help"
