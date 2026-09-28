"""Tests for the Phase-2 Finance verbs (P2-09).

Covers every verb the P2-09 task requires:

  auth         : login (WRITE, INTERACTIVE), logout (WRITE), status (READ)
  accounts     : list, get, holdings, history, refresh (WRITE), refresh-status,
                 types, create (WRITE), update (WRITE), delete (WRITE, DESTRUCTIVE)
  tx           : list, get, summary, create (WRITE), update (WRITE),
                 delete (WRITE, DESTRUCTIVE), splits
  budgets      : list, set (WRITE)
  cashflow     : summary, details
  categories   : list, groups, create (WRITE), delete (WRITE, DESTRUCTIVE)
  tags         : list, create (WRITE), set (WRITE, REPLACES)
  recurring    : (bare verb -> monarch recurring list)
  institutions : list, subscription

Test discipline (P2 hard safety rule -- read this twice):

  - Every WRITE verb in this file is MOCKED: subprocess.run is never
    invoked against the live monarch CLI, so no auth login prompt fires,
    no transactions are created / updated / deleted, no accounts are
    created / updated / deleted, no budgets are set, no categories or
    tags are created / deleted. Every write test patches
    `mineru_cli.verbs.finance.run_monarch` with a recorder, asserts the
    argv the wrapper WOULD send, and verifies exit-code plumbing.
  - `tx delete`, `accounts delete`, `categories delete`, and `auth
    login/logout` are DESTRUCTIVE / stateful and NEVER executed live
    during dev.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
  - A belt-and-braces static grep ensures the verb file only reaches
    the underlying monarch CLI via the wrapper (never `import
    subprocess` or direct-shell strings) and never carries hardcoded
    Monarch credentials.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import finance as finance_verb
from mineru_cli.wrappers import monarch as monarch_wrapper
from mineru_cli.wrappers.monarch import (
    DEFAULT_MONARCH_BIN,
    EXPECTED_BIN_BASENAME,
    MISSING_BIN_EXIT_CODE,
    MONARCH_BIN_ENV,
    build_monarch_argv,
    resolve_monarch_bin,
    run_monarch,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _CompletedStub:
    """Mimics subprocess.CompletedProcess but only exposes returncode."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _make_run_recorder(returncode: int = 0):
    """Return (fake_run, calls_list). fake_run records every subprocess.run call."""
    calls: List[dict] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": list(cmd), "args": args, "kwargs": kwargs})
        return _CompletedStub(returncode)

    return fake_run, calls


def _record_run_monarch(recorded: List[List[str]], returncode: int = 0):
    def fake(args):
        recorded.append(list(args))
        return returncode

    return fake


def _invoke(args: List[str], returncode: int = 0):
    """Run the CLI with a patched wrapper. Returns (CliResult, recorded_argv_list)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.finance.run_monarch",
        _record_run_monarch(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ===========================================================================
# WRAPPER: resolver + argv invariant
# ===========================================================================


def test_resolve_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MONARCH_BIN_ENV, raising=False)
    assert resolve_monarch_bin() == DEFAULT_MONARCH_BIN


def test_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MONARCH_BIN_ENV, "/tmp/fake_monarch")
    assert resolve_monarch_bin() == "/tmp/fake_monarch"


def test_resolve_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MONARCH_BIN_ENV, "")
    assert resolve_monarch_bin() == DEFAULT_MONARCH_BIN


def test_default_points_at_workspace_bin_dir() -> None:
    """Sanity: the documented default is the workspace's monarch shim path."""
    assert DEFAULT_MONARCH_BIN == str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin" / "monarch")
    assert DEFAULT_MONARCH_BIN.endswith("/monarch")


def test_argv0_resolves_to_monarch_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MONARCH_BIN_ENV, raising=False)
    argv = build_monarch_argv(["accounts", "list"])
    assert os.path.basename(argv[0]) == EXPECTED_BIN_BASENAME
    assert os.path.basename(argv[0]) == "monarch"


def test_argv0_at_subprocess_call_site_is_monarch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: at the actual subprocess.run call-site argv[0] basename is `monarch`."""
    monkeypatch.setenv(MONARCH_BIN_ENV, "/tmp/fixtures/monarch")
    with patch.object(monarch_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_monarch(["accounts", "list"])
    assert len(calls) == 1
    assert os.path.basename(calls[0]["cmd"][0]) == "monarch"


# ===========================================================================
# WRAPPER: happy path + exit-code propagation
# ===========================================================================


def test_run_monarch_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MONARCH_BIN_ENV, "/tmp/fake_monarch")
    with patch.object(monarch_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_monarch(["transactions", "list", "--limit", "5"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "/tmp/fake_monarch",
        "transactions",
        "list",
        "--limit",
        "5",
    ]


def test_run_monarch_propagates_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MONARCH_BIN_ENV, "/tmp/fake_monarch")
    with patch.object(monarch_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=1)
        with patch.object(subprocess, "run", fake_run):
            rc = run_monarch(["accounts", "list"])
    assert rc == 1


def test_run_monarch_propagates_unusual_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper must not rewrite unusual codes."""
    monkeypatch.setenv(MONARCH_BIN_ENV, "/tmp/fake_monarch")
    with patch.object(monarch_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=42)
        with patch.object(subprocess, "run", fake_run):
            rc = run_monarch(["accounts", "list"])
    assert rc == 42


def test_run_monarch_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run is called with NO stdout/stderr override.

    monarch's `auth login` prints MFA / trusted-device prompts on
    stderr and reads the password from stdin; pass-through is
    non-negotiable so that interactive branch works in a real shell.
    """
    monkeypatch.setenv(MONARCH_BIN_ENV, "/tmp/fake_monarch")
    with patch.object(monarch_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_monarch(["accounts", "list"])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


# ===========================================================================
# WRAPPER: missing-binary path
# ===========================================================================


def test_run_monarch_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(MONARCH_BIN_ENV, "/nonexistent/absolute/path/monarch")
    with pytest.raises(typer.Exit) as excinfo:
        run_monarch(["accounts", "list"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE

    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/monarch" in err
    assert MONARCH_BIN_ENV in err
    assert "not found" in err.lower()


def test_run_monarch_race_missing_at_exec_time_maps_to_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Binary check succeeded but exec raised FileNotFoundError."""
    monkeypatch.setenv(MONARCH_BIN_ENV, "/tmp/racing_monarch")
    with patch.object(monarch_wrapper, "_binary_available", return_value=True):

        def raising_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory")

        with patch.object(subprocess, "run", raising_run):
            with pytest.raises(typer.Exit) as excinfo:
                run_monarch(["accounts", "list"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert "/tmp/racing_monarch" in err


def test_run_monarch_env_points_at_directory_exits_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A directory-typed env override MUST NOT leak a PermissionError traceback.

    Regression guard: `_binary_available` used to accept anything that
    existed (including directories), so `subprocess.run` would raise
    `PermissionError` past the FileNotFoundError-only catch. The fix
    requires a regular executable file AND a broader OSError catch.
    """
    monkeypatch.setenv(MONARCH_BIN_ENV, str(tmp_path))
    with pytest.raises(typer.Exit) as excinfo:
        run_monarch(["accounts", "list"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert str(tmp_path) in err


def test_run_monarch_env_points_at_non_executable_file_exits_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A mode-644 file (no exec bit) MUST NOT leak a PermissionError traceback."""
    script = tmp_path / "monarch"
    script.write_text("#!/bin/sh\nexit 0\n")
    # Deliberately do NOT set the exec bit; default mode is 644.
    monkeypatch.setenv(MONARCH_BIN_ENV, str(script))
    with pytest.raises(typer.Exit) as excinfo:
        run_monarch(["accounts", "list"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert str(script) in err


# ===========================================================================
# End-to-end: fake monarch on disk, real subprocess.run
# ===========================================================================


@pytest.fixture
def fake_monarch_exit_0(tmp_path: Path) -> Path:
    """Fake monarch that emits a plausible JSON stub and exits 0."""
    script = tmp_path / "monarch"
    script.write_text(
        "#!/bin/sh\n"
        'printf \'[]\\n\'\n'
        "exit 0\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script


@pytest.fixture
def fake_monarch_exit_1(tmp_path: Path) -> Path:
    """Fake monarch that emits a plausible auth error and exits 1."""
    script = tmp_path / "monarch"
    script.write_text(
        "#!/bin/sh\n"
        'printf "Not authenticated. Run `monarch auth login`.\\n" >&2\n'
        "exit 1\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script


def test_run_monarch_end_to_end_fake_binary_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    fake_monarch_exit_0: Path,
) -> None:
    monkeypatch.setenv(MONARCH_BIN_ENV, str(fake_monarch_exit_0))
    rc = run_monarch(["accounts", "list"])
    assert rc == 0


def test_run_monarch_end_to_end_fake_binary_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    fake_monarch_exit_1: Path,
) -> None:
    monkeypatch.setenv(MONARCH_BIN_ENV, str(fake_monarch_exit_1))
    rc = run_monarch(["accounts", "list"])
    assert rc == 1


# ===========================================================================
# ROOT --json / --pretty NON-FORWARDING: end-to-end guard against the
# reviewer-caught regression (mineru --json finance <verb> was crashing
# monarch with exit-2 `No such option: --json`).
#
# These tests use the real fake monarch on disk (which records its argv to
# a sentinel file and exits 0), so if a future maintainer wires the root
# flags back into the argv the test will catch it TWO ways: (1) the exit
# won't be zero (a real monarch would reject the flag), and (2) the
# recorded argv sentinel will show `--json` / `--pretty` were forwarded.
# ===========================================================================


@pytest.fixture
def fake_monarch_argv_recorder(tmp_path: Path) -> tuple[Path, Path]:
    """Fake monarch that dumps its argv to a sentinel file and exits 0.

    Returns the (binary_path, sentinel_path) pair. The sentinel is one
    argument per line so tests can read it as `sentinel.read_text().split()`.
    """
    sentinel = tmp_path / "argv.txt"
    script = tmp_path / "monarch"
    script.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do echo "$a" >> {sentinel}; done\n'
        "exit 0\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script, sentinel


def test_root_json_end_to_end_does_not_reach_monarch(
    monkeypatch: pytest.MonkeyPatch,
    fake_monarch_argv_recorder: tuple[Path, Path],
) -> None:
    """`mineru --json finance auth status` exits 0 and does NOT pass --json to monarch.

    Regression guard: before the fix this returned exit 2 with
    `No such option: --json` because monarch rejected the injected flag.
    """
    binary, sentinel = fake_monarch_argv_recorder
    monkeypatch.setenv(MONARCH_BIN_ENV, str(binary))
    result = runner.invoke(app, ["--json", "finance", "auth", "status"])
    assert result.exit_code == 0
    recorded = sentinel.read_text().splitlines() if sentinel.exists() else []
    assert recorded == ["auth", "status"], (
        f"Expected clean argv, got {recorded!r}"
    )


def test_root_pretty_end_to_end_does_not_reach_monarch(
    monkeypatch: pytest.MonkeyPatch,
    fake_monarch_argv_recorder: tuple[Path, Path],
) -> None:
    """`mineru --pretty finance auth status` exits 0 and does NOT pass --pretty to monarch.

    Regression guard: before the fix this returned exit 2 with
    `No such option: --pretty` because monarch has no --pretty flag anywhere.
    """
    binary, sentinel = fake_monarch_argv_recorder
    monkeypatch.setenv(MONARCH_BIN_ENV, str(binary))
    result = runner.invoke(app, ["--pretty", "finance", "auth", "status"])
    assert result.exit_code == 0
    recorded = sentinel.read_text().splitlines() if sentinel.exists() else []
    assert recorded == ["auth", "status"], (
        f"Expected clean argv, got {recorded!r}"
    )


def test_root_json_end_to_end_tx_list_does_not_reach_monarch(
    monkeypatch: pytest.MonkeyPatch,
    fake_monarch_argv_recorder: tuple[Path, Path],
) -> None:
    """`mineru --json finance tx list --limit 1` exits 0 and forwards ONLY the trailing extras.

    Confirms the fix also holds for the highest-traffic verb (`tx list`)
    and that a legitimate trailing extra (`--limit 1`) still flows
    through cleanly.
    """
    binary, sentinel = fake_monarch_argv_recorder
    monkeypatch.setenv(MONARCH_BIN_ENV, str(binary))
    result = runner.invoke(
        app, ["--json", "finance", "tx", "list", "--limit", "1"]
    )
    assert result.exit_code == 0
    recorded = sentinel.read_text().splitlines() if sentinel.exists() else []
    assert recorded == ["transactions", "list", "--limit", "1"], (
        f"Expected --json swallowed and --limit forwarded, got {recorded!r}"
    )


def test_root_json_end_to_end_recurring_does_not_reach_monarch(
    monkeypatch: pytest.MonkeyPatch,
    fake_monarch_argv_recorder: tuple[Path, Path],
) -> None:
    """`mineru --json finance recurring` exits 0 with clean `recurring list` argv."""
    binary, sentinel = fake_monarch_argv_recorder
    monkeypatch.setenv(MONARCH_BIN_ENV, str(binary))
    result = runner.invoke(app, ["--json", "finance", "recurring"])
    assert result.exit_code == 0
    recorded = sentinel.read_text().splitlines() if sentinel.exists() else []
    assert recorded == ["recurring", "list"], (
        f"Expected clean argv, got {recorded!r}"
    )


def test_root_json_write_verb_does_not_reach_monarch(
    monkeypatch: pytest.MonkeyPatch,
    fake_monarch_argv_recorder: tuple[Path, Path],
) -> None:
    """`mineru --json finance accounts refresh` exits 0 with clean argv.

    Write-verb variant of the same regression: `accounts refresh` (and
    every other WRITE subverb) does not accept `--format` at all, so
    even the "translate --json to --format json" strategy would have
    broken this path. The fix (swallow root flags entirely) keeps the
    argv clean for writes too.
    """
    binary, sentinel = fake_monarch_argv_recorder
    monkeypatch.setenv(MONARCH_BIN_ENV, str(binary))
    result = runner.invoke(app, ["--json", "finance", "accounts", "refresh"])
    assert result.exit_code == 0
    recorded = sentinel.read_text().splitlines() if sentinel.exists() else []
    assert recorded == ["accounts", "refresh"], (
        f"Expected clean argv, got {recorded!r}"
    )


# ===========================================================================
# AUTH sub-app
# ===========================================================================


# --- auth login (WRITE, INTERACTIVE -- MOCKED ONLY) ---------------------


def test_auth_login_routes_to_monarch_auth_login() -> None:
    """`finance auth login` -> `monarch auth login`."""
    result, recorded = _invoke(["finance", "auth", "login"])
    assert result.exit_code == 0
    assert recorded == [["auth", "login"]]


def test_auth_login_extras_pass_through() -> None:
    result, recorded = _invoke(["finance", "auth", "login", "--json"])
    assert result.exit_code == 0
    assert recorded == [["auth", "login", "--json"]]


def test_auth_login_propagates_exit_code() -> None:
    result, _ = _invoke(["finance", "auth", "login"], returncode=1)
    assert result.exit_code == 1


def test_auth_login_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "auth", "login", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "write" in combined or "interactive" in combined


# --- auth logout (WRITE) ---


def test_auth_logout_routes_to_monarch_auth_logout() -> None:
    result, recorded = _invoke(["finance", "auth", "logout"])
    assert result.exit_code == 0
    assert recorded == [["auth", "logout"]]


def test_auth_logout_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "auth", "logout", "--help"])
    assert result.exit_code == 0


# --- auth status (READ) ---


def test_auth_status_routes_to_monarch_auth_status() -> None:
    result, recorded = _invoke(["finance", "auth", "status"])
    assert result.exit_code == 0
    assert recorded == [["auth", "status"]]


def test_auth_status_root_json_swallowed_not_forwarded() -> None:
    """Root-level `--json` MUST NOT be forwarded into monarch argv.

    Regression: monarch rejects `--json` on every subverb (exit 2 with
    `No such option: --json`), and its write subverbs (like `auth
    login/logout`) additionally reject `--format`, so translating
    isn't safe either. The mineru root flag is swallowed at the CLI
    edge; monarch's own default output is already JSON for verbs
    that support `--format`.
    """
    result, recorded = _invoke(["--json", "finance", "auth", "status"])
    assert result.exit_code == 0
    assert recorded == [["auth", "status"]]


def test_auth_status_root_pretty_swallowed_not_forwarded() -> None:
    """Root-level `--pretty` MUST NOT be forwarded into monarch argv.

    Same reason as the --json guard: monarch has no `--pretty` flag,
    so forwarding would crash the underlying CLI. Anyone who wants
    monarch's table renderer can pass `--format table` as a trailing
    extra which still flows through opaquely.
    """
    result, recorded = _invoke(["--pretty", "finance", "auth", "status"])
    assert result.exit_code == 0
    assert recorded == [["auth", "status"]]


def test_auth_status_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "auth", "status", "--help"])
    assert result.exit_code == 0


# ===========================================================================
# ACCOUNTS sub-app
# ===========================================================================


# --- list (READ) ---


def test_accounts_list_routes_to_monarch_accounts_list() -> None:
    result, recorded = _invoke(["finance", "accounts", "list"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "list"]]


def test_accounts_list_extras_pass_through_format() -> None:
    result, recorded = _invoke(
        ["finance", "accounts", "list", "--format", "table"]
    )
    assert result.exit_code == 0
    assert recorded == [["accounts", "list", "--format", "table"]]


def test_accounts_list_root_pretty_swallowed_not_forwarded() -> None:
    """Root-level `--pretty` MUST NOT be forwarded into monarch argv.

    Monarch has no `--pretty` flag anywhere; forwarding it makes the
    engine exit 2. mineru swallows the root flag at the CLI edge.
    """
    result, recorded = _invoke(["--pretty", "finance", "accounts", "list"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "list"]]


def test_accounts_list_root_json_swallowed_not_forwarded() -> None:
    """Root-level `--json` MUST NOT be forwarded into monarch argv.

    Monarch uses `--format json|table` for output shape, not `--json`.
    Its default for `accounts list` is already `json`, so the common
    intent is honored without any injection.
    """
    result, recorded = _invoke(["--json", "finance", "accounts", "list"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "list"]]


def test_accounts_list_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "accounts", "list", "--help"])
    assert result.exit_code == 0


def test_accounts_list_propagates_engine_exit_code() -> None:
    result, _ = _invoke(["finance", "accounts", "list"], returncode=7)
    assert result.exit_code == 7


# --- get (READ) ---


def test_accounts_get_forwards_account_id() -> None:
    result, recorded = _invoke(["finance", "accounts", "get", "ACCT_ABC"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "get", "ACCT_ABC"]]


def test_accounts_get_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["finance", "accounts", "get", "ACCT_ABC", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["accounts", "get", "ACCT_ABC", "--json"]]


def test_accounts_get_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "accounts", "get", "--help"])
    assert result.exit_code == 0


# --- holdings (READ) ---


def test_accounts_holdings_forwards_account_id() -> None:
    result, recorded = _invoke(
        ["finance", "accounts", "holdings", "ACCT_XYZ"]
    )
    assert result.exit_code == 0
    assert recorded == [["accounts", "holdings", "ACCT_XYZ"]]


def test_accounts_holdings_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "accounts", "holdings", "--help"])
    assert result.exit_code == 0


# --- history (READ) ---


def test_accounts_history_forwards_account_id_and_extras() -> None:
    result, recorded = _invoke(
        [
            "finance", "accounts", "history", "ACCT_XYZ",
            "--format", "table",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["accounts", "history", "ACCT_XYZ", "--format", "table"]
    ]


def test_accounts_history_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "accounts", "history", "--help"])
    assert result.exit_code == 0


# --- refresh (WRITE -- triggers real bank sync) ---


def test_accounts_refresh_routes_to_monarch_accounts_refresh() -> None:
    result, recorded = _invoke(["finance", "accounts", "refresh"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "refresh"]]


def test_accounts_refresh_wait_extra_passes_through() -> None:
    result, recorded = _invoke(["finance", "accounts", "refresh", "--wait"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "refresh", "--wait"]]


def test_accounts_refresh_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "accounts", "refresh", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "write" in combined


# --- refresh-status (READ) ---


def test_accounts_refresh_status_routes() -> None:
    result, recorded = _invoke(["finance", "accounts", "refresh-status"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "refresh-status"]]


def test_accounts_refresh_status_help_smoke() -> None:
    result = runner.invoke(
        app, ["finance", "accounts", "refresh-status", "--help"]
    )
    assert result.exit_code == 0


# --- types (READ) ---


def test_accounts_types_routes() -> None:
    result, recorded = _invoke(["finance", "accounts", "types"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "types"]]


def test_accounts_types_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "accounts", "types", "--help"])
    assert result.exit_code == 0


# --- create (WRITE) ---


def test_accounts_create_forwards_required_name_and_type() -> None:
    """`finance accounts create --name X --type checking` -> monarch create with both flags."""
    result, recorded = _invoke(
        [
            "finance", "accounts", "create",
            "--name", "Chase Checking",
            "--type", "checking",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "accounts", "create",
            "--name", "Chase Checking",
            "--type", "checking",
        ]
    ]


def test_accounts_create_extras_pass_through_subtype_and_balance() -> None:
    result, recorded = _invoke(
        [
            "finance", "accounts", "create",
            "--name", "Fidelity Brokerage",
            "--type", "brokerage",
            "--subtype", "individual",
            "--balance", "12345.67",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "accounts", "create",
            "--name", "Fidelity Brokerage",
            "--type", "brokerage",
            "--subtype", "individual",
            "--balance", "12345.67",
        ]
    ]


def test_accounts_create_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "accounts", "create", "--help"])
    assert result.exit_code == 0
    assert "--name" in result.stdout
    assert "--type" in result.stdout
    assert "write" in result.stdout.lower()


# --- update (WRITE, partial) ---


def test_accounts_update_forwards_account_id() -> None:
    result, recorded = _invoke(["finance", "accounts", "update", "ACCT_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "update", "ACCT_XYZ"]]


def test_accounts_update_extras_pass_through_field_flags() -> None:
    result, recorded = _invoke(
        [
            "finance", "accounts", "update", "ACCT_XYZ",
            "--name", "New Name",
            "--balance", "500.00",
            "--hidden",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "accounts", "update", "ACCT_XYZ",
            "--name", "New Name",
            "--balance", "500.00",
            "--hidden",
        ]
    ]


def test_accounts_update_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "accounts", "update", "--help"])
    assert result.exit_code == 0
    assert "write" in result.stdout.lower()


# --- delete (WRITE, DESTRUCTIVE) ---


def test_accounts_delete_forwards_account_id() -> None:
    result, recorded = _invoke(["finance", "accounts", "delete", "ACCT_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["accounts", "delete", "ACCT_XYZ"]]


def test_accounts_delete_yes_extra_passes_through() -> None:
    result, recorded = _invoke(
        ["finance", "accounts", "delete", "ACCT_XYZ", "--yes"]
    )
    assert result.exit_code == 0
    assert recorded == [["accounts", "delete", "ACCT_XYZ", "--yes"]]


def test_accounts_delete_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "accounts", "delete", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "destructive" in combined or "write" in combined


def test_accounts_delete_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        ["finance", "accounts", "delete", "ACCT_XYZ"], returncode=9
    )
    assert result.exit_code == 9


# ===========================================================================
# TX (transactions) sub-app -- note the mineru surface uses `tx`, monarch's
# underlying group is `transactions`.
# ===========================================================================


# --- list (READ) ---


def test_tx_list_routes_to_monarch_transactions_list() -> None:
    result, recorded = _invoke(["finance", "tx", "list"])
    assert result.exit_code == 0
    assert recorded == [["transactions", "list"]]


def test_tx_list_extras_pass_through_filters() -> None:
    result, recorded = _invoke(
        [
            "finance", "tx", "list",
            "--limit", "50",
            "--start", "2026-01-01",
            "--end", "2026-01-31",
            "--search", "starbucks",
            "--accounts", "a,b",
            "--categories", "c,d",
            "--format", "json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "transactions", "list",
            "--limit", "50",
            "--start", "2026-01-01",
            "--end", "2026-01-31",
            "--search", "starbucks",
            "--accounts", "a,b",
            "--categories", "c,d",
            "--format", "json",
        ]
    ]


def test_tx_list_root_json_swallowed_not_forwarded() -> None:
    """Root-level `--json` MUST NOT be forwarded into monarch argv.

    Same reason as auth-status: monarch rejects `--json` (uses
    `--format json|table` instead). Since `transactions list` defaults
    to `--format json` anyway, dropping the flag is a no-op for the
    common case.
    """
    result, recorded = _invoke(["--json", "finance", "tx", "list"])
    assert result.exit_code == 0
    assert recorded == [["transactions", "list"]]


def test_tx_list_root_pretty_swallowed_not_forwarded() -> None:
    """Root-level `--pretty` MUST NOT be forwarded into monarch argv."""
    result, recorded = _invoke(["--pretty", "finance", "tx", "list"])
    assert result.exit_code == 0
    assert recorded == [["transactions", "list"]]


def test_tx_list_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tx", "list", "--help"])
    assert result.exit_code == 0


def test_tx_list_propagates_engine_exit_code() -> None:
    result, _ = _invoke(["finance", "tx", "list"], returncode=3)
    assert result.exit_code == 3


# --- get (READ) ---


def test_tx_get_forwards_transaction_id() -> None:
    result, recorded = _invoke(["finance", "tx", "get", "TXN_ABC"])
    assert result.exit_code == 0
    assert recorded == [["transactions", "get", "TXN_ABC"]]


def test_tx_get_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tx", "get", "--help"])
    assert result.exit_code == 0


# --- summary (READ) ---


def test_tx_summary_extras_pass_through_date_range() -> None:
    result, recorded = _invoke(
        [
            "finance", "tx", "summary",
            "--start", "2026-01-01",
            "--end", "2026-01-31",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "transactions", "summary",
            "--start", "2026-01-01",
            "--end", "2026-01-31",
        ]
    ]


def test_tx_summary_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tx", "summary", "--help"])
    assert result.exit_code == 0


# --- create (WRITE) ---


def test_tx_create_forwards_required_flags() -> None:
    result, recorded = _invoke(
        [
            "finance", "tx", "create",
            "--date", "2026-07-27",
            "--account", "ACCT_XYZ",
            "--amount", "-12.34",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "transactions", "create",
            "--date", "2026-07-27",
            "--account", "ACCT_XYZ",
            "--amount", "-12.34",
        ]
    ]


def test_tx_create_extras_pass_through_optional_flags() -> None:
    result, recorded = _invoke(
        [
            "finance", "tx", "create",
            "--date", "2026-07-27",
            "--account", "ACCT_XYZ",
            "--amount", "-45.67",
            "--merchant", "Trader Joe's",
            "--category", "CAT_GROC",
            "--notes", "groceries for week",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "transactions", "create",
            "--date", "2026-07-27",
            "--account", "ACCT_XYZ",
            "--amount", "-45.67",
            "--merchant", "Trader Joe's",
            "--category", "CAT_GROC",
            "--notes", "groceries for week",
        ]
    ]


def test_tx_create_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tx", "create", "--help"])
    assert result.exit_code == 0
    assert "--date" in result.stdout
    assert "--account" in result.stdout
    assert "--amount" in result.stdout
    assert "write" in result.stdout.lower()


def test_tx_create_preserves_amount_verbatim() -> None:
    """The amount token is passed through unchanged (no float round-trip)."""
    result, recorded = _invoke(
        [
            "finance", "tx", "create",
            "--date", "2026-07-27",
            "--account", "ACCT_XYZ",
            "--amount", "100",
        ]
    )
    assert result.exit_code == 0
    # Bug guard: `100` used to become `100.0` via `str(float(x))`.
    assert recorded == [
        [
            "transactions", "create",
            "--date", "2026-07-27",
            "--account", "ACCT_XYZ",
            "--amount", "100",
        ]
    ]


@pytest.mark.parametrize("bad_amount", ["nan", "inf", "-inf", "1e20", "not-a-number"])
def test_tx_create_rejects_non_decimal_amounts(bad_amount: str) -> None:
    """NaN / Infinity / non-numeric amounts fail loud at the CLI boundary.

    Without this guard a `--amount nan` invocation would send the literal
    string `nan` on to a live monarch write.
    """
    result, recorded = _invoke(
        [
            "finance", "tx", "create",
            "--date", "2026-07-27",
            "--account", "ACCT_XYZ",
            "--amount", bad_amount,
        ]
    )
    assert result.exit_code != 0
    assert recorded == []


# --- update (WRITE, partial) ---


def test_tx_update_forwards_transaction_id() -> None:
    result, recorded = _invoke(["finance", "tx", "update", "TXN_ABC"])
    assert result.exit_code == 0
    assert recorded == [["transactions", "update", "TXN_ABC"]]


def test_tx_update_extras_pass_through_field_flags() -> None:
    result, recorded = _invoke(
        [
            "finance", "tx", "update", "TXN_ABC",
            "--category", "CAT_NEW",
            "--merchant", "New Merchant",
            "--notes", "updated notes",
            "--hide",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "transactions", "update", "TXN_ABC",
            "--category", "CAT_NEW",
            "--merchant", "New Merchant",
            "--notes", "updated notes",
            "--hide",
        ]
    ]


def test_tx_update_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tx", "update", "--help"])
    assert result.exit_code == 0
    assert "write" in result.stdout.lower()


# --- delete (WRITE, DESTRUCTIVE) ---


def test_tx_delete_forwards_transaction_id() -> None:
    result, recorded = _invoke(["finance", "tx", "delete", "TXN_ABC"])
    assert result.exit_code == 0
    assert recorded == [["transactions", "delete", "TXN_ABC"]]


def test_tx_delete_yes_extra_passes_through() -> None:
    result, recorded = _invoke(
        ["finance", "tx", "delete", "TXN_ABC", "--yes"]
    )
    assert result.exit_code == 0
    assert recorded == [["transactions", "delete", "TXN_ABC", "--yes"]]


def test_tx_delete_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tx", "delete", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "destructive" in combined or "write" in combined


# --- splits (READ) ---


def test_tx_splits_forwards_transaction_id() -> None:
    result, recorded = _invoke(["finance", "tx", "splits", "TXN_ABC"])
    assert result.exit_code == 0
    assert recorded == [["transactions", "splits", "TXN_ABC"]]


def test_tx_splits_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tx", "splits", "--help"])
    assert result.exit_code == 0


# ===========================================================================
# BUDGETS sub-app
# ===========================================================================


# --- list (READ) ---


def test_budgets_list_routes() -> None:
    result, recorded = _invoke(["finance", "budgets", "list"])
    assert result.exit_code == 0
    assert recorded == [["budgets", "list"]]


def test_budgets_list_extras_pass_through_date_range() -> None:
    result, recorded = _invoke(
        [
            "finance", "budgets", "list",
            "--start", "2026-01-01",
            "--end", "2026-01-31",
            "--format", "json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "budgets", "list",
            "--start", "2026-01-01",
            "--end", "2026-01-31",
            "--format", "json",
        ]
    ]


def test_budgets_list_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "budgets", "list", "--help"])
    assert result.exit_code == 0


# --- set (WRITE) ---


def test_budgets_set_forwards_category_id_and_amount() -> None:
    """The amount token is forwarded verbatim (no float round-trip)."""
    result, recorded = _invoke(
        ["finance", "budgets", "set", "CAT_GROC", "500"]
    )
    assert result.exit_code == 0
    assert recorded == [["budgets", "set", "CAT_GROC", "500"]]


def test_budgets_set_extras_pass_through_date_and_future() -> None:
    result, recorded = _invoke(
        [
            "finance", "budgets", "set", "CAT_GROC", "600",
            "--date", "2026-08-01",
            "--future",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "budgets", "set", "CAT_GROC", "600",
            "--date", "2026-08-01",
            "--future",
        ]
    ]


def test_budgets_set_amount_zero_clears() -> None:
    """amount=0 is the documented "clear the budget" sentinel; must pass through."""
    result, recorded = _invoke(["finance", "budgets", "set", "CAT_GROC", "0"])
    assert result.exit_code == 0
    assert recorded == [["budgets", "set", "CAT_GROC", "0"]]


def test_budgets_set_preserves_decimal_precision() -> None:
    """Decimal tokens are forwarded verbatim (500.00 stays 500.00, not 500.0)."""
    result, recorded = _invoke(
        ["finance", "budgets", "set", "CAT_GROC", "500.00"]
    )
    assert result.exit_code == 0
    assert recorded == [["budgets", "set", "CAT_GROC", "500.00"]]


@pytest.mark.parametrize("bad_amount", ["nan", "inf", "-inf", "1e20", "not-a-number"])
def test_budgets_set_rejects_non_decimal_amounts(bad_amount: str) -> None:
    """NaN / Infinity / non-numeric amounts fail loud at the CLI boundary.

    Monarch's own AMOUNT parser would coerce these strings via float() and
    silently write a `nan` / `inf` literal into the live account. Reject
    at the mineru boundary before the value reaches the wrapper.
    """
    result, recorded = _invoke(
        ["finance", "budgets", "set", "CAT_GROC", bad_amount]
    )
    assert result.exit_code != 0
    # The wrapper must not have been invoked when the amount fails validation.
    assert recorded == []


def test_budgets_set_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "budgets", "set", "--help"])
    assert result.exit_code == 0
    assert "write" in result.stdout.lower()


# ===========================================================================
# CASHFLOW sub-app (READ-ONLY)
# ===========================================================================


def test_cashflow_summary_routes() -> None:
    result, recorded = _invoke(["finance", "cashflow", "summary"])
    assert result.exit_code == 0
    assert recorded == [["cashflow", "summary"]]


def test_cashflow_summary_extras_pass_through_date_range() -> None:
    result, recorded = _invoke(
        [
            "finance", "cashflow", "summary",
            "--start", "2026-01-01",
            "--end", "2026-06-30",
            "--format", "json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "cashflow", "summary",
            "--start", "2026-01-01",
            "--end", "2026-06-30",
            "--format", "json",
        ]
    ]


def test_cashflow_summary_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "cashflow", "summary", "--help"])
    assert result.exit_code == 0


def test_cashflow_details_routes() -> None:
    result, recorded = _invoke(["finance", "cashflow", "details"])
    assert result.exit_code == 0
    assert recorded == [["cashflow", "details"]]


def test_cashflow_details_extras_pass_through_date_range() -> None:
    result, recorded = _invoke(
        [
            "finance", "cashflow", "details",
            "--start", "2026-01-01",
            "--end", "2026-06-30",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "cashflow", "details",
            "--start", "2026-01-01",
            "--end", "2026-06-30",
        ]
    ]


def test_cashflow_details_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "cashflow", "details", "--help"])
    assert result.exit_code == 0


# ===========================================================================
# CATEGORIES sub-app
# ===========================================================================


# --- list (READ) ---


def test_categories_list_routes() -> None:
    result, recorded = _invoke(["finance", "categories", "list"])
    assert result.exit_code == 0
    assert recorded == [["categories", "list"]]


def test_categories_list_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "categories", "list", "--help"])
    assert result.exit_code == 0


# --- groups (READ) ---


def test_categories_groups_routes() -> None:
    result, recorded = _invoke(["finance", "categories", "groups"])
    assert result.exit_code == 0
    assert recorded == [["categories", "groups"]]


def test_categories_groups_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "categories", "groups", "--help"])
    assert result.exit_code == 0


# --- create (WRITE) ---


def test_categories_create_forwards_name() -> None:
    result, recorded = _invoke(
        ["finance", "categories", "create", "Baby Supplies"]
    )
    assert result.exit_code == 0
    assert recorded == [["categories", "create", "Baby Supplies"]]


def test_categories_create_extras_pass_through_group_and_icon() -> None:
    result, recorded = _invoke(
        [
            "finance", "categories", "create", "Baby Supplies",
            "--group", "GRP_HOME",
            "--icon", "baby",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "categories", "create", "Baby Supplies",
            "--group", "GRP_HOME",
            "--icon", "baby",
        ]
    ]


def test_categories_create_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "categories", "create", "--help"])
    assert result.exit_code == 0
    assert "write" in result.stdout.lower()


# --- delete (WRITE, DESTRUCTIVE) ---


def test_categories_delete_forwards_category_id() -> None:
    result, recorded = _invoke(
        ["finance", "categories", "delete", "CAT_OLD"]
    )
    assert result.exit_code == 0
    assert recorded == [["categories", "delete", "CAT_OLD"]]


def test_categories_delete_yes_extra_passes_through() -> None:
    result, recorded = _invoke(
        ["finance", "categories", "delete", "CAT_OLD", "--yes"]
    )
    assert result.exit_code == 0
    assert recorded == [["categories", "delete", "CAT_OLD", "--yes"]]


def test_categories_delete_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "categories", "delete", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "destructive" in combined or "write" in combined


# ===========================================================================
# TAGS sub-app
# ===========================================================================


# --- list (READ) ---


def test_tags_list_routes() -> None:
    result, recorded = _invoke(["finance", "tags", "list"])
    assert result.exit_code == 0
    assert recorded == [["tags", "list"]]


def test_tags_list_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tags", "list", "--help"])
    assert result.exit_code == 0


# --- create (WRITE) ---


def test_tags_create_forwards_name() -> None:
    result, recorded = _invoke(["finance", "tags", "create", "carrot-fund"])
    assert result.exit_code == 0
    assert recorded == [["tags", "create", "carrot-fund"]]


def test_tags_create_extras_pass_through_color() -> None:
    result, recorded = _invoke(
        [
            "finance", "tags", "create", "carrot-fund",
            "--color", "#ff9900",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["tags", "create", "carrot-fund", "--color", "#ff9900"]
    ]


def test_tags_create_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tags", "create", "--help"])
    assert result.exit_code == 0
    assert "write" in result.stdout.lower()


# --- set (WRITE, REPLACES) ---


def test_tags_set_forwards_transaction_id_and_tag_ids() -> None:
    result, recorded = _invoke(
        ["finance", "tags", "set", "TXN_ABC", "TAG_A,TAG_B"]
    )
    assert result.exit_code == 0
    assert recorded == [["tags", "set", "TXN_ABC", "TAG_A,TAG_B"]]


def test_tags_set_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "tags", "set", "--help"])
    assert result.exit_code == 0
    assert "write" in result.stdout.lower()


# ===========================================================================
# RECURRING (bare verb -> monarch recurring list)
# ===========================================================================


def test_recurring_routes_to_monarch_recurring_list() -> None:
    """`finance recurring` -> `monarch recurring list` (only subverb today)."""
    result, recorded = _invoke(["finance", "recurring"])
    assert result.exit_code == 0
    assert recorded == [["recurring", "list"]]


def test_recurring_with_list_muscle_memory_does_not_double_list() -> None:
    """`finance recurring list` MUST NOT send `recurring list list` to monarch.

    Muscle memory from the raw monarch CLI (`monarch recurring list`) is
    natural; before the fix Typer treated the trailing `list` as a
    positional extra and shipped an argv monarch would reject.
    """
    result, recorded = _invoke(["finance", "recurring", "list"])
    assert result.exit_code == 0
    assert recorded == [["recurring", "list"]]


def test_recurring_with_list_muscle_memory_still_passes_extras() -> None:
    """`finance recurring list --format table` strips the leading `list`."""
    result, recorded = _invoke(
        ["finance", "recurring", "list", "--format", "table"]
    )
    assert result.exit_code == 0
    assert recorded == [["recurring", "list", "--format", "table"]]


def test_recurring_extras_pass_through_format() -> None:
    result, recorded = _invoke(
        ["finance", "recurring", "--format", "table"]
    )
    assert result.exit_code == 0
    assert recorded == [["recurring", "list", "--format", "table"]]


def test_recurring_root_json_swallowed_not_forwarded() -> None:
    """Root-level `--json` MUST NOT be forwarded into `recurring list` argv.

    Monarch's `recurring list` uses `--format json|table` and rejects
    `--json`. The bare `mineru finance recurring` still resolves to
    `monarch recurring list` with json as the engine default.
    """
    result, recorded = _invoke(["--json", "finance", "recurring"])
    assert result.exit_code == 0
    assert recorded == [["recurring", "list"]]


def test_recurring_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "recurring", "--help"])
    assert result.exit_code == 0


# ===========================================================================
# INSTITUTIONS sub-app (READ-ONLY)
# ===========================================================================


def test_institutions_list_routes() -> None:
    result, recorded = _invoke(["finance", "institutions", "list"])
    assert result.exit_code == 0
    assert recorded == [["institutions", "list"]]


def test_institutions_list_extras_pass_through_format() -> None:
    result, recorded = _invoke(
        ["finance", "institutions", "list", "--format", "json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["institutions", "list", "--format", "json"]
    ]


def test_institutions_list_help_smoke() -> None:
    result = runner.invoke(app, ["finance", "institutions", "list", "--help"])
    assert result.exit_code == 0


def test_institutions_subscription_routes() -> None:
    result, recorded = _invoke(["finance", "institutions", "subscription"])
    assert result.exit_code == 0
    assert recorded == [["institutions", "subscription"]]


def test_institutions_subscription_help_smoke() -> None:
    result = runner.invoke(
        app, ["finance", "institutions", "subscription", "--help"]
    )
    assert result.exit_code == 0


# ===========================================================================
# ROOT `mineru finance --help` regression guard
# ===========================================================================


def test_finance_root_help_lists_every_sub_group() -> None:
    """`mineru finance --help` surfaces every wired sub-group + `recurring` verb."""
    result = runner.invoke(app, ["finance", "--help"])
    assert result.exit_code == 0
    expected = (
        "auth",
        "accounts",
        "tx",
        "budgets",
        "cashflow",
        "categories",
        "tags",
        "recurring",
        "institutions",
    )
    for verb in expected:
        assert verb in result.stdout, (
            f"`mineru finance --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_finance_root_help_mentions_monarch_and_safety() -> None:
    """The noun-level help surfaces the Monarch backing + write/mock discipline."""
    result = runner.invoke(app, ["finance", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "monarch" in lowered
    # SAFETY signal: writes are mock-only in tests, real invocation is a
    # real mutation. Any of these tokens is sufficient.
    assert "write" in lowered or "safety" in lowered or "mock" in lowered


# ===========================================================================
# STATIC INVARIANT: verb / wrapper source hygiene
# ===========================================================================


VERB_SRC = Path(finance_verb.__file__).read_text()
WRAPPER_SRC = Path(monarch_wrapper.__file__).read_text()


def test_verb_source_only_reaches_monarch_via_wrapper() -> None:
    """A regression that reached for subprocess directly (bypassing the wrapper) fails here."""
    for forbidden in ("import subprocess", "from subprocess"):
        assert forbidden not in VERB_SRC, (
            f"finance verb must route via the wrapper, not direct subprocess; "
            f"found {forbidden!r}"
        )


def test_verb_source_has_no_hardcoded_monarch_credentials() -> None:
    """The verb file must not carry Monarch email / password / token literals.

    Auth is handled by `monarch auth login` interactively; the mineru
    verb layer never sees credentials. A regression that stashed a
    literal here would fail this check.
    """
    for forbidden in (
        "MONARCH_PASSWORD",
        "MONARCH_TOKEN =",
        "monarch_password",
        "monarch_token",
        # Bearer-token / session-token style env-var reads that should
        # only ever happen inside the monarch CLI itself, not here.
        'os.environ.get("MONARCH_TOKEN"',
        'os.environ["MONARCH_TOKEN"',
    ):
        assert forbidden not in VERB_SRC, (
            f"verbs/finance.py must not carry Monarch credential state; "
            f"found {forbidden!r}"
        )


def test_wrapper_source_does_no_output_parsing() -> None:
    """The wrapper file must be facade-only (no JSON parsing, no capture)."""
    for forbidden in (
        "json.loads",
        "json.dumps",
        "capture_output",
        "PIPE",
        "communicate(",
        ".stdout.decode",
        ".stderr.decode",
        "readlines(",
    ):
        assert forbidden not in WRAPPER_SRC, (
            f"wrappers/monarch.py must be facade-only; found {forbidden!r}"
        )


def test_wrapper_source_has_no_hardcoded_monarch_credentials() -> None:
    """Same posture on the wrapper: no credential literals or env reads for tokens."""
    for forbidden in (
        "MONARCH_PASSWORD",
        "MONARCH_TOKEN =",
        "monarch_password",
        "monarch_token",
        'os.environ.get("MONARCH_TOKEN"',
        'os.environ["MONARCH_TOKEN"',
    ):
        assert forbidden not in WRAPPER_SRC, (
            f"wrappers/monarch.py must not carry Monarch credential state; "
            f"found {forbidden!r}"
        )
