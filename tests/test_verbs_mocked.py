"""End-to-end tests for `mineru memory search` and `mineru gmail search` (F7).

Purpose (F7 done-criteria):
  - Prove the wired verbs shell out to the RIGHT binary path (firewall-
    preservation regression guard: argv[0] for gmail is a `gog-firewall`
    binary, never bare `gog`).
  - Prove firewall exit codes 0 / 77 / 78 propagate unchanged from the
    fake gog-firewall to `mineru gmail`'s exit code.
  - Prove stderr pass-through: the fake engine's stderr reaches the
    caller untouched (no capture / no reformatting), so `redacted N of M
    units` notices survive the wrapper.

Why fake bash scripts (not mocks):
  These tests deliberately drive the real code path — `subprocess.run`
  invokes an actual on-disk fake binary named `msearch` / `gog-firewall`.
  That's the strongest signal we can give without hitting the live
  engines: a regression that swapped the wrapper for bare `gog`, added
  `stderr=PIPE`, or remapped exit codes would fail here.

Structure of the fakes:
  - `_write_fake_binary(dir, name, exit_code)` drops a tiny POSIX sh
    script that writes each argv element on its own line to the file at
    `$MINERU_TEST_ARGV_FILE` (argv[0] recorded as `argv0=…`, the rest as
    `arg=…`), echoes a stdout / stderr marker so pass-through can be
    verified, and exits with the given code.
  - The script name matters. We name the gmail fake `gog-firewall`
    literally, so `os.path.basename(argv[0]) == 'gog-firewall'` is a
    real filesystem fact, not a Python assertion in a mock.

Invocation strategy:
  We spawn the installed `.venv/bin/mineru` binary via `subprocess.run`.
  That path exercises the exact code the operator types: shell → mineru CLI →
  Typer callback → wrapper → subprocess.run → fake engine → back. No
  Python-level mock overrides that could mask a real regression.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import List, Tuple

import pytest


# --- Repo / venv layout ----------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_MINERU = REPO_ROOT / ".venv" / "bin" / "mineru"

# Synthetic seed profile shipped under tests/fixtures/. The engine repo
# ships NO live `current` active-profile symlink (that is per-user data,
# excluded from the public tree), so a bare `mineru` subprocess cannot
# resolve an active profile on its own. `clean_env` strips the `MINERU_*`
# vars that conftest sets for in-process CliRunner tests, so the spawn
# helper re-injects them here to point the child at the shipped seed —
# the subprocess analogue of the conftest autouse fixture.
_SEED_PROFILE_BASE = REPO_ROOT / "tests" / "fixtures" / "seed_profile_base"
_SEED_PROFILE_NAME = "mineru"


def _require_venv_mineru() -> Path:
    """Return the installed CLI path or skip the whole file cleanly.

    The F7 done-criteria say `pytest -q` runs green with NO skips. The
    seed venv in the worktree ships with `mineru` installed, so this
    skip only fires on a genuinely broken checkout — a signal, not
    noise.
    """
    if not VENV_MINERU.exists():
        pytest.skip(
            f"venv mineru missing at {VENV_MINERU}; run `pip install -e .` "
            "inside .venv/ first."
        )
    return VENV_MINERU


# --- Fake-binary factory ---------------------------------------------------


def _write_fake_binary(
    dest_dir: Path, name: str, exit_code: int = 0,
    stdout_line: str = "fake-stdout",
    stderr_line: str = "fake-stderr",
) -> Path:
    """Drop an executable POSIX sh script into `dest_dir/name`.

    The script:
      1. Writes argv[0] (`$0`) + every positional arg to
         `$MINERU_TEST_ARGV_FILE`, one per line, prefixed to make parsing
         trivial. Prefixing avoids ambiguity when an argument itself
         contains an `=` (e.g. `key=value`).
      2. Echoes `stdout_line` on stdout and `stderr_line` on stderr, so
         the caller can prove stdio pass-through works.
      3. Exits with the requested code, so the caller can prove firewall
         exit codes (0 / 77 / 78) propagate untouched through the wrapper
         and the Typer `raise typer.Exit(code=rc)` path.

    Placed at `dest_dir/name` so `os.path.basename(argv[0])` in the
    recorded log is literally `name` — the firewall-preservation
    invariant becomes a plain fs check, not a mock assertion.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    script = dest_dir / name
    # POSIX sh, not bash — keeps the fake portable across CI images. The
    # `printf` trick preserves literal newlines and never adds escapes.
    script.write_text(
        "#!/bin/sh\n"
        "# Fake engine binary for mineru CLI foundation tests (F7).\n"
        "# Records argv (including argv[0]) to $MINERU_TEST_ARGV_FILE and\n"
        "# emits deterministic stdout/stderr markers so pass-through and\n"
        "# argv routing can both be asserted end-to-end.\n"
        ": > \"$MINERU_TEST_ARGV_FILE\"\n"
        'printf "argv0=%s\\n" "$0" >> "$MINERU_TEST_ARGV_FILE"\n'
        'for a in "$@"; do\n'
        '  printf "arg=%s\\n" "$a" >> "$MINERU_TEST_ARGV_FILE"\n'
        'done\n'
        f'printf "{stdout_line}\\n"\n'
        f'printf "{stderr_line}\\n" >&2\n'
        f'exit {exit_code}\n'
    )
    mode = script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    script.chmod(mode)
    return script


def _parse_recorded_argv(argv_file: Path) -> Tuple[str, List[str]]:
    """Return (argv0, [arg, arg, ...]) from a fake's recording file.

    Prefixed-line format (`argv0=…\\nargv=…\\n…`) so an argument with an
    embedded `=` never confuses the parser: we split on the first `=`
    only and trust the prefix.
    """
    text = argv_file.read_text()
    argv0 = ""
    args: List[str] = []
    for line in text.splitlines():
        if line.startswith("argv0="):
            argv0 = line[len("argv0="):]
        elif line.startswith("arg="):
            args.append(line[len("arg="):])
    return argv0, args


# --- Test fixtures ---------------------------------------------------------


@pytest.fixture
def argv_recording_file(tmp_path: Path) -> Path:
    """One-per-test recording file for the fake engine to write into."""
    return tmp_path / "recorded_argv.txt"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any user-set MINERU_* overrides that could bleed into the child.

    Not strictly needed here (we set the two we care about), but cheap
    insurance against a dev-shell export shadowing our tmp bin paths.
    """
    for key in list(os.environ):
        if key.startswith("MINERU_"):
            monkeypatch.delenv(key, raising=False)


def _spawn(
    args: List[str],
    *,
    env_overrides: dict,
) -> subprocess.CompletedProcess:
    """Spawn the installed `mineru` binary with a merged env.

    Uses PATH from the current shell so tools like `sh` resolve, but
    layers on the test-specific overrides last (env, argv_file, fake
    bin paths). `text=True` gives us stdout/stderr as strings.
    """
    env = os.environ.copy()
    # Point the child at the shipped synthetic seed profile (the engine repo
    # ships no `current` symlink). Set BEFORE env_overrides so a test that
    # needs a different profile can still override these two keys.
    env.setdefault("MINERU_PROFILE", _SEED_PROFILE_NAME)
    env.setdefault("MINERU_PROFILE_ROOT", str(_SEED_PROFILE_BASE))
    env.update(env_overrides)
    return subprocess.run(
        [str(_require_venv_mineru()), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=30,
    )


# ------------------------------- MEMORY ------------------------------------


def test_memory_search_calls_fake_msearch_with_expected_argv(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """`mineru memory search foo --pretty` invokes the fake msearch with the right argv.

    Exercises the F4 wire-up end-to-end: the CLI passes `keyword foo`
    plus the propagated `--pretty` through to the msearch subprocess.
    """
    fake_bin = _write_fake_binary(tmp_path / "bin", "msearch", exit_code=0)

    result = _spawn(
        ["memory", "search", "foo", "--pretty"],
        env_overrides={
            "MINERU_MSEARCH_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 0, (
        f"unexpected exit rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    argv0, args = _parse_recorded_argv(argv_recording_file)
    # argv[0] IS the fake we handed the wrapper, byte-for-byte.
    assert argv0 == str(fake_bin), (
        f"argv[0] mismatch: got {argv0!r}, expected {str(fake_bin)!r}"
    )
    # argv tail is the msearch invocation the wrapper is supposed to build.
    # Phase-1 profile isolation: the verb layer forwards `--workspace
    # <profile.workspace_absolute>` BEFORE any user-supplied extras so
    # `mineru memory` runs against the active profile's tree, never the
    # engine's default `$MINERU_HOME`. See
    # tests/test_profile_namespacing.py :: test_memory_search_hits_active_profiles_tree.
    assert "--workspace" in args, (
        f"argv tail must forward --workspace for profile isolation: {args!r}"
    )
    ws_idx = args.index("--workspace")
    assert args[:ws_idx] == ["keyword", "foo"], (
        f"argv tail before --workspace: {args[:ws_idx]!r}"
    )
    assert args[ws_idx + 2:] == ["--pretty"], (
        f"argv tail after --workspace <root>: {args[ws_idx + 2:]!r}"
    )


def test_memory_search_stdout_passthrough(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """The fake's stdout reaches `mineru`'s stdout unchanged."""
    fake_bin = _write_fake_binary(
        tmp_path / "bin", "msearch", exit_code=0,
        stdout_line="MINERU_STDOUT_MARKER_MEMORY",
    )
    result = _spawn(
        ["memory", "search", "foo"],
        env_overrides={
            "MINERU_MSEARCH_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 0
    assert "MINERU_STDOUT_MARKER_MEMORY" in result.stdout


def test_memory_search_stderr_passthrough(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """The fake's stderr reaches `mineru`'s stderr unchanged.

    Mirror of the gmail redaction-notice test: the wrapper must never
    capture / filter engine stderr, or diagnostic noise (backtrace,
    warning, msearch-internal timing) would silently vanish.
    """
    fake_bin = _write_fake_binary(
        tmp_path / "bin", "msearch", exit_code=0,
        stderr_line="MINERU_STDERR_MARKER_MEMORY",
    )
    result = _spawn(
        ["memory", "search", "foo"],
        env_overrides={
            "MINERU_MSEARCH_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 0
    assert "MINERU_STDERR_MARKER_MEMORY" in result.stderr


def test_memory_search_propagates_nonzero_exit(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    fake_bin = _write_fake_binary(tmp_path / "bin", "msearch", exit_code=42)
    result = _spawn(
        ["memory", "search", "foo"],
        env_overrides={
            "MINERU_MSEARCH_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 42


# ------------------------------- GMAIL -------------------------------------
# F7 done-criteria: argv[0] must resolve to a gog-firewall binary, never
# bare `gog`. AND firewall exit codes 0 / 77 / 78 must propagate unchanged.


def test_gmail_search_calls_fake_gog_firewall_with_expected_argv(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """`mineru gmail search '<query>' --max 5 --json` argv contract.

    This is the F7 firewall-preservation regression guard. Two facts:
      1. The wrapper shells out to a binary whose BASENAME is
         literally `gog-firewall` (not `gog`).
      2. The argv tail is exactly `["gmail", "search", "<query>",
         "--max", "5", "--json"]` — extras pass-through intact, no
         --raw / --unsafe-strip-invisible injected by the wrapper.
    """
    fake_bin = _write_fake_binary(
        tmp_path / "bin", "gog-firewall", exit_code=0,
    )
    query = "newer_than:1d"

    result = _spawn(
        ["gmail", "search", query, "--max", "5", "--json"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 0, (
        f"unexpected exit rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    argv0, args = _parse_recorded_argv(argv_recording_file)
    # ------ Firewall-preservation invariant ------
    # argv[0] basename MUST be `gog-firewall`, NEVER bare `gog` or any
    # variant like `gog-fwl`.
    assert os.path.basename(argv0) == "gog-firewall", (
        f"argv[0] basename regressed: got {os.path.basename(argv0)!r} "
        f"({argv0!r}). Foundation invariant: gmail READ verbs shell out "
        "to `gog-firewall`, never bare `gog`."
    )
    assert os.path.basename(argv0) != "gog"
    # ------ Argv tail contract ------
    assert args == ["gmail", "search", query, "--max", "5", "--json"], (
        f"argv tail regressed: {args!r}"
    )
    # ------ No bypass flags injected ------
    for banned in ("--raw", "--unsafe-strip-invisible"):
        assert banned not in args, (
            f"wrapper injected firewall-bypass flag {banned!r}: {args!r}"
        )


def test_gmail_search_refuses_to_shell_out_when_env_points_at_bare_gog(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """A `MINERU_GOG_FIREWALL_BIN=/…/gog` env misconfiguration MUST be rejected.

    The firewall-preservation invariant is that gmail READ verbs shell
    out to `gog-firewall`, never bare `gog` (which bypasses the
    per-unit prompt-injection screen). An operator or script that
    points the override at a `gog`-named binary would break the
    invariant — the wrapper defends against this at runtime by
    checking `basename == "gog-firewall"` before shelling out and
    exiting with the firewall's own 78 (firewall error) code so a
    caller can distinguish it from POSIX 127.

    We name the fake `gog` and assert:
      - exit code is 78 (firewall-basename-mismatch).
      - stderr names the env var, the actual basename, AND the expected
        `gog-firewall` name, so the operator has a diagnosable message.
      - the fake was NEVER invoked (argv-recording file is absent or
        empty), proving the guard fires BEFORE the exec.
    """
    fake_bare_gog = _write_fake_binary(
        tmp_path / "bin_wrong", "gog", exit_code=0,
    )
    result = _spawn(
        ["gmail", "search", "x"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_bare_gog),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    # 78 = FIREWALL_BASENAME_MISMATCH_EXIT_CODE (see wrappers/gog_firewall.py).
    assert result.returncode == 78, (
        f"expected the wrapper to refuse with exit 78; "
        f"got rc={result.returncode}\nstderr:\n{result.stderr}"
    )
    # Actionable stderr: mentions the env var + expected name + actual
    # basename so the operator can fix it without reading source.
    assert "MINERU_GOG_FIREWALL_BIN" in result.stderr
    assert "gog-firewall" in result.stderr
    # The fake was never exec'd, so no argv was recorded.
    assert not argv_recording_file.exists() or not argv_recording_file.read_text().strip(), (
        "wrapper shelled out to the misnamed binary despite the basename guard"
    )


def test_gmail_search_propagates_exit_0(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """Delivered (0) surfaces as CLI exit 0."""
    fake_bin = _write_fake_binary(
        tmp_path / "bin", "gog-firewall", exit_code=0,
    )
    result = _spawn(
        ["gmail", "search", "x"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 0


def test_gmail_search_propagates_exit_77_all_blocked(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """Firewall's 'all units blocked' code (77) surfaces as CLI exit 77.

    This is the reason the wrapper never remaps engine exit codes —
    shell pipelines can special-case 77 to distinguish "everything was
    prompt-injection" from a normal error.
    """
    fake_bin = _write_fake_binary(
        tmp_path / "bin", "gog-firewall", exit_code=77,
    )
    result = _spawn(
        ["gmail", "search", "x"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 77


def test_gmail_search_propagates_exit_78_firewall_error(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """Firewall's own error code (78) surfaces as CLI exit 78 unchanged."""
    fake_bin = _write_fake_binary(
        tmp_path / "bin", "gog-firewall", exit_code=78,
    )
    result = _spawn(
        ["gmail", "search", "x"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 78


def test_gmail_search_stderr_passthrough_redaction_style(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """`redacted N of M units`-shaped stderr reaches the caller intact.

    We fake the firewall's real-world stderr shape so a regression that
    silently captures / rewrites the stderr channel (a real security
    concern — the operator relies on seeing which units were blocked)
    would fail here immediately.
    """
    fake_bin = _write_fake_binary(
        tmp_path / "bin", "gog-firewall", exit_code=0,
        stderr_line="redacted 3 of 5 units (fake)",
    )
    result = _spawn(
        ["gmail", "search", "x"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 0
    assert "redacted 3 of 5 units" in result.stderr


def test_gmail_search_stdout_passthrough(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    fake_bin = _write_fake_binary(
        tmp_path / "bin", "gog-firewall", exit_code=0,
        stdout_line="MINERU_STDOUT_MARKER_GMAIL",
    )
    result = _spawn(
        ["gmail", "search", "x"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 0
    assert "MINERU_STDOUT_MARKER_GMAIL" in result.stdout


def test_gmail_search_root_json_flag_propagates_end_to_end(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """Root-level `--json` is folded into the engine argv end-to-end."""
    fake_bin = _write_fake_binary(
        tmp_path / "bin", "gog-firewall", exit_code=0,
    )
    result = _spawn(
        ["--json", "gmail", "search", "x"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 0
    _, args = _parse_recorded_argv(argv_recording_file)
    assert "--json" in args, (
        f"root --json didn't propagate to engine argv: {args!r}"
    )


def test_gmail_search_missing_binary_returns_127(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """When MINERU_GOG_FIREWALL_BIN points nowhere, exit 127 with actionable stderr.

    The path's basename is `gog-firewall` so the runtime basename-preservation
    guard passes; the missing-binary check is then what fires 127.
    """
    missing_path = tmp_path / "not_here" / "gog-firewall"
    result = _spawn(
        ["gmail", "search", "x"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(missing_path),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 127
    assert str(missing_path) in result.stderr
    assert "MINERU_GOG_FIREWALL_BIN" in result.stderr


def test_gmail_search_env_pointed_at_bare_gog_refuses_to_shell_out(
    tmp_path: Path,
    argv_recording_file: Path,
    clean_env: None,
) -> None:
    """Setting MINERU_GOG_FIREWALL_BIN=/some/path/gog must be refused at runtime.

    Regression guard on the firewall-preservation runtime enforcement:
    the wrapper docstring's §0+§3.1 invariant used to be enforced only by
    unit tests. Now the wrapper refuses to shell out with exit 78 and an
    actionable message whenever the resolved basename isn't `gog-firewall`.
    """
    # Actually create a real executable that would exit 0 if invoked, so
    # the ONLY reason a run fails is the basename guard, not availability.
    bad_bin = tmp_path / "bin" / "gog"
    bad_bin.parent.mkdir(parents=True, exist_ok=True)
    bad_bin.write_text("#!/bin/sh\nexit 0\n")
    bad_bin.chmod(0o755)

    result = _spawn(
        ["gmail", "search", "x"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(bad_bin),
            "MINERU_TEST_ARGV_FILE": str(argv_recording_file),
        },
    )
    assert result.returncode == 78, (
        f"expected firewall-error 78, got {result.returncode}; "
        f"stderr:\n{result.stderr}"
    )
    assert "gog-firewall" in result.stderr
    assert "bypass" in result.stderr.lower()
    # And we NEVER shelled out to that binary — the fake binary would
    # have created the argv recording file; its absence is the proof.
    assert not argv_recording_file.exists(), (
        "bare `gog` shim was executed despite the runtime guard"
    )
