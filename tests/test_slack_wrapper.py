"""Tests for the slack-read + slack-refresh-users facade wrappers (P2-07).

Full mirror of `tests/test_memory_wrapper.py`, adapted for the two
Slack shell-script wrappers. Both wrappers use the msearch single-binary
shape (env override + default + pass-through stdio + exit-code
propagation + missing-bin -> 127). Covers:

  - `resolve_slack_read_bin` / `resolve_slack_refresh_users_bin` honor
    their respective env vars, fall back to documented defaults, treat
    empty-string env as unset.
  - `build_slack_read_argv` / `build_slack_refresh_users_argv` build
    the expected argv with the resolved binary as argv[0]. The
    documented-basename invariant (`slack-read` /
    `slack-refresh-users`) is asserted so a regression that swapped
    the binary for a different name would fail immediately.
  - `run_slack_read` / `run_slack_refresh_users` build the expected
    argv and propagate the engine exit code unchanged (0, non-zero,
    and unusual codes like 77 / 78 to guard against remapping).
  - `subprocess.run` is called with NO stdout/stderr override
    (pass-through), so the shell scripts' `Error: Slack token not in
    Keychain` / `Cached N users to ...` messages reach the caller
    untouched.
  - Missing binary -> exit 127 with an actionable stderr message that
    names the resolved path AND the override env var.
  - Wrapper source contains no output-transform substrings (facade-only
    static guard); no Slack Web API calls made from Python (no
    `slack.com`, no `curl`, no `requests`, no `urllib`).
  - End-to-end: fake binaries on disk propagate exit codes through the
    real `subprocess.run` code path (belt-and-braces regression guard
    on the actual code path, not just mock-level assertions).
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

from mineru_cli.wrappers import slack_channels as sc_wrapper
from mineru_cli.wrappers import slack_read as sr_wrapper
from mineru_cli.wrappers import slack_refresh_users as sru_wrapper
from mineru_cli.wrappers import slack_search_public as ssp_wrapper
from mineru_cli.wrappers import slack_thread as st_wrapper
from mineru_cli.wrappers.slack_channels import (
    DEFAULT_SLACK_CHANNELS_BIN,
    EXPECTED_BIN_BASENAME as SLACK_CHANNELS_EXPECTED_BASENAME,
    MISSING_BIN_EXIT_CODE as SC_MISSING,
    OBSERVER_BASENAME_MISMATCH_EXIT_CODE as SC_BASENAME_REFUSED,
    SLACK_CHANNELS_BIN_ENV,
    build_slack_channels_argv,
    resolve_slack_channels_bin,
    run_slack_channels,
)
from mineru_cli.wrappers.slack_read import (
    DEFAULT_SLACK_READ_BIN,
    EXPECTED_BIN_BASENAME as SLACK_READ_EXPECTED_BASENAME,
    MISSING_BIN_EXIT_CODE as SR_MISSING,
    OBSERVER_BASENAME_MISMATCH_EXIT_CODE as SR_BASENAME_REFUSED,
    SLACK_READ_BIN_ENV,
    build_slack_read_argv,
    resolve_slack_read_bin,
    run_slack_read,
)
from mineru_cli.wrappers.slack_refresh_users import (
    DEFAULT_SLACK_REFRESH_USERS_BIN,
    EXPECTED_BIN_BASENAME as SLACK_REFRESH_USERS_EXPECTED_BASENAME,
    MISSING_BIN_EXIT_CODE as SRU_MISSING,
    SLACK_REFRESH_USERS_BIN_ENV,
    build_slack_refresh_users_argv,
    resolve_slack_refresh_users_bin,
    run_slack_refresh_users,
)
from mineru_cli.wrappers.slack_search_public import (
    DEFAULT_SLACK_SEARCH_PUBLIC_BIN,
    EXPECTED_BIN_BASENAME as SLACK_SEARCH_PUBLIC_EXPECTED_BASENAME,
    MISSING_BIN_EXIT_CODE as SSP_MISSING,
    OBSERVER_BASENAME_MISMATCH_EXIT_CODE as SSP_BASENAME_REFUSED,
    SLACK_SEARCH_PUBLIC_BIN_ENV,
    build_slack_search_public_argv,
    resolve_slack_search_public_bin,
    run_slack_search_public,
)
from mineru_cli.wrappers.slack_thread import (
    DEFAULT_SLACK_THREAD_BIN,
    EXPECTED_BIN_BASENAME as SLACK_THREAD_EXPECTED_BASENAME,
    MISSING_BIN_EXIT_CODE as ST_MISSING,
    OBSERVER_BASENAME_MISMATCH_EXIT_CODE as ST_BASENAME_REFUSED,
    SLACK_THREAD_BIN_ENV,
    build_slack_thread_argv,
    resolve_slack_thread_bin,
    run_slack_thread,
)


# ------------------------------------------------------------------ helpers


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


# ============================================================================
# slack-read wrapper
# ============================================================================


# ------------------------------------------------------------- resolver ----


def test_slack_read_resolve_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SLACK_READ_BIN_ENV, raising=False)
    assert resolve_slack_read_bin() == DEFAULT_SLACK_READ_BIN


def test_slack_read_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SLACK_READ_BIN_ENV, "/tmp/fake/slack-read")
    assert resolve_slack_read_bin() == "/tmp/fake/slack-read"


def test_slack_read_resolve_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_READ_BIN_ENV, "")
    assert resolve_slack_read_bin() == DEFAULT_SLACK_READ_BIN


def test_slack_read_default_points_at_workspace_bin() -> None:
    """Sanity: the documented default is the workspace's slack-read path."""
    assert DEFAULT_SLACK_READ_BIN == str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin" / "slack-read")
    assert DEFAULT_SLACK_READ_BIN.endswith("/slack-read")


# ---------------------- argv-shape invariant (basename == slack-read) -------
# The observer-only surface leans on the wrapper never silently pointing at
# a differently-named binary that might have write capabilities. Argv[0]
# basename must be exactly `slack-read`.


def test_slack_read_argv0_resolves_to_slack_read_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SLACK_READ_BIN_ENV, raising=False)
    argv = build_slack_read_argv(["C02RAQRC10T", "20"])
    assert Path(argv[0]).name == SLACK_READ_EXPECTED_BASENAME
    assert Path(argv[0]).name == "slack-read"


def test_slack_read_argv0_never_bare_slack_across_run_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: at the actual subprocess.run call-site, argv[0] basename is `slack-read`."""
    monkeypatch.setenv(SLACK_READ_BIN_ENV, "/tmp/fixtures/slack-read")
    with patch.object(sr_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_read(["C02RAQRC10T"])
    assert len(calls) == 1
    assert Path(calls[0]["cmd"][0]).name == "slack-read"


# ------------------------------------------------ run_slack_read happy ----


def test_run_slack_read_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_READ_BIN_ENV, "/tmp/fake/slack-read")
    with patch.object(sr_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_read(["C02RAQRC10T", "20"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "/tmp/fake/slack-read",
        "C02RAQRC10T",
        "20",
    ]


def test_run_slack_read_propagates_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero from the engine (missing Keychain, missing cache) surfaces intact."""
    monkeypatch.setenv(SLACK_READ_BIN_ENV, "/tmp/fake/slack-read")
    with patch.object(sr_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=1)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_read(["C02RAQRC10T"])
    assert rc == 1


def test_run_slack_read_propagates_unusual_exit_code_77(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The firewall convention uses 77; the wrapper must not rewrite unusual codes."""
    monkeypatch.setenv(SLACK_READ_BIN_ENV, "/tmp/fake/slack-read")
    with patch.object(sr_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=77)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_read(["C02RAQRC10T"])
    assert rc == 77


def test_run_slack_read_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run is called with NO stdout/stderr override.

    slack-read prints `Error: Slack token not in Keychain` /
    `Error: User cache not found` on stderr; those must reach the caller
    untouched so the operator knows exactly which recovery to run.
    """
    monkeypatch.setenv(SLACK_READ_BIN_ENV, "/tmp/fake/slack-read")
    with patch.object(sr_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_read(["C02RAQRC10T"])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


# -------------------------------------------------- missing-binary path ----


def test_run_slack_read_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(SLACK_READ_BIN_ENV, "/nonexistent/absolute/path/slack-read")

    with pytest.raises(typer.Exit) as excinfo:
        run_slack_read(["C02RAQRC10T"])
    assert excinfo.value.exit_code == SR_MISSING

    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/slack-read" in err
    assert SLACK_READ_BIN_ENV in err
    assert "not found" in err.lower()


def test_run_slack_read_directory_at_resolved_path_returns_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A directory at the resolved path must NOT crash with an OSError traceback."""
    dir_path = tmp_path / "somewhere" / "slack-read"
    dir_path.mkdir(parents=True)  # a directory, not a file
    monkeypatch.setenv(SLACK_READ_BIN_ENV, str(dir_path))

    with pytest.raises(typer.Exit) as excinfo:
        run_slack_read(["C02RAQRC10T"])
    assert excinfo.value.exit_code == SR_MISSING
    err = capsys.readouterr().err
    assert str(dir_path) in err


def test_run_slack_read_non_executable_file_at_resolved_path_returns_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A non-executable file must NOT crash with a PermissionError traceback."""
    non_exec = tmp_path / "slack-read"
    non_exec.write_text("#!/bin/sh\nexit 0\n")  # no chmod +x
    monkeypatch.setenv(SLACK_READ_BIN_ENV, str(non_exec))

    with pytest.raises(typer.Exit) as excinfo:
        run_slack_read(["C02RAQRC10T"])
    assert excinfo.value.exit_code == SR_MISSING
    err = capsys.readouterr().err
    assert str(non_exec) in err


# -------- OBSERVER-INVARIANT runtime guard (finding #1 regression) -----------


@pytest.mark.parametrize(
    "bad_binary",
    [
        "/opt/homebrew/bin/slack",
        "slack",
        "/tmp/bin/slack-cli",
        "/tmp/bin/slack-write",
    ],
)
def test_run_slack_read_refuses_non_slack_read_env_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    bad_binary: str,
) -> None:
    """Setting MINERU_SLACK_READ_BIN=<not-slack-read> must NOT shell out.

    The connected Slack workspace is a strict read-only observer surface;
    the wrapper refuses to shell out to any binary whose basename is
    not `slack-read` so a write-capable shim can never be silently
    substituted.
    """
    monkeypatch.setenv(SLACK_READ_BIN_ENV, bad_binary)

    def must_not_run(*a, **kw):
        raise AssertionError(
            "subprocess.run was called despite basename guard rejecting env"
        )

    with patch.object(subprocess, "run", must_not_run):
        with pytest.raises(typer.Exit) as excinfo:
            run_slack_read(["C02RAQRC10T"])

    assert excinfo.value.exit_code == SR_BASENAME_REFUSED
    err = capsys.readouterr().err
    assert SLACK_READ_BIN_ENV in err
    assert "slack-read" in err
    assert bad_binary in err


def test_run_slack_read_race_missing_at_exec_time_maps_to_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Binary check succeeded but exec raised FileNotFoundError."""
    monkeypatch.setenv(SLACK_READ_BIN_ENV, "/tmp/racing/slack-read")
    with patch.object(sr_wrapper, "_binary_available", return_value=True):

        def raising_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory")

        with patch.object(subprocess, "run", raising_run):
            with pytest.raises(typer.Exit) as excinfo:
                run_slack_read(["C02RAQRC10T"])
    assert excinfo.value.exit_code == SR_MISSING
    err = capsys.readouterr().err
    assert "/tmp/racing/slack-read" in err


# --------------------------------------- static "facade only" guard --------


SLACK_READ_WRAPPER_SRC = Path(sr_wrapper.__file__).read_text()


def test_slack_read_wrapper_source_does_no_output_parsing() -> None:
    """The wrapper file must not contain JSON parsing or output-mutation calls."""
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
        assert forbidden not in SLACK_READ_WRAPPER_SRC, (
            f"wrappers/slack_read.py must be facade-only; found {forbidden!r}"
        )


def test_slack_read_wrapper_source_makes_no_slack_web_api_calls() -> None:
    """The wrapper must not smuggle direct Slack Web API calls into Python.

    Facade-first: if a future verb needs an endpoint slack-read doesn't
    cover, extend the shell script (or add a sibling script + wrapper),
    do NOT curl / requests / urllib from Python. A grep-style guard here
    fails fast if someone tries.

    We look for QUOTED shapes (`"https://..."`, `'requests.'`) or
    Python code shapes (`import requests`, `from urllib`), not bare
    substrings, so a docstring mentioning "the shell script uses curl"
    does NOT trip the guard. Only actual code does.
    """
    for forbidden in (
        # Quoted URL literals (what an argv or requests.get(url) would look like)
        '"https://slack.com',
        "'https://slack.com",
        '"https://',
        "'https://",
        # Actual Python HTTP-client imports/calls
        "import requests",
        "from requests",
        "requests.get",
        "requests.post",
        "import urllib.request",
        "from urllib.request",
        "urlopen(",
        # A Python-level curl invocation would look like ["curl", ...] in argv
        '"curl",',
        "'curl',",
    ):
        assert forbidden not in SLACK_READ_WRAPPER_SRC, (
            f"wrappers/slack_read.py must not make direct API calls; found {forbidden!r}"
        )


# ---------------- true end-to-end: fake slack-read on disk, exit 1 ---------


@pytest.fixture
def fake_slack_read_exit_1(tmp_path: Path) -> Path:
    """Write a tiny executable script that mimics 'missing cache' failure (exit 1).

    Real subprocess call, no mocks. Verifies the shell script's exit
    code propagates through the actual `subprocess.run` code path.
    """
    script = tmp_path / "slack-read"
    script.write_text(
        "#!/bin/sh\n"
        "# Fake slack-read: mimic the 'User cache not found' branch, exit 1.\n"
        'printf "Error: User cache not found at $MINERU_HOME/cache/slack-users.json\\n" >&2\n'
        'printf "Run: slack-refresh-users\\n" >&2\n'
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_run_slack_read_end_to_end_fake_binary_propagates_1(
    monkeypatch: pytest.MonkeyPatch,
    fake_slack_read_exit_1: Path,
) -> None:
    monkeypatch.setenv(SLACK_READ_BIN_ENV, str(fake_slack_read_exit_1))
    rc = run_slack_read(["C02RAQRC10T"])
    assert rc == 1


@pytest.fixture
def fake_slack_read_exit_0(tmp_path: Path) -> Path:
    """Fake slack-read that emits a plausible JSON blob on stdout and exits 0."""
    script = tmp_path / "slack-read"
    script.write_text(
        "#!/bin/sh\n"
        'printf "{\\"ts\\":\\"1.0\\",\\"user\\":\\"the operator\\",\\"text\\":\\"gm\\"}\\n"\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_run_slack_read_end_to_end_stdout_flows_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_slack_read_exit_0: Path,
    capfd: pytest.CaptureFixture,
) -> None:
    """The fake's stdout reaches the caller's fd untouched (proves pass-through)."""
    monkeypatch.setenv(SLACK_READ_BIN_ENV, str(fake_slack_read_exit_0))
    capfd.readouterr()  # drain
    rc = run_slack_read(["C02RAQRC10T"])
    captured = capfd.readouterr()
    assert rc == 0
    assert '"user":"the operator"' in captured.out


# ============================================================================
# slack-refresh-users wrapper
# ============================================================================


# ------------------------------------------------------------- resolver ----


def test_slack_refresh_users_resolve_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SLACK_REFRESH_USERS_BIN_ENV, raising=False)
    assert (
        resolve_slack_refresh_users_bin() == DEFAULT_SLACK_REFRESH_USERS_BIN
    )


def test_slack_refresh_users_resolve_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_REFRESH_USERS_BIN_ENV, "/tmp/fake_slack_refresh_users"
    )
    assert resolve_slack_refresh_users_bin() == "/tmp/fake_slack_refresh_users"


def test_slack_refresh_users_resolve_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_REFRESH_USERS_BIN_ENV, "")
    assert (
        resolve_slack_refresh_users_bin() == DEFAULT_SLACK_REFRESH_USERS_BIN
    )


def test_slack_refresh_users_default_points_at_workspace_bin() -> None:
    """Sanity: documented default is the workspace's slack-refresh-users."""
    assert (
        DEFAULT_SLACK_REFRESH_USERS_BIN
        == str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin" / "slack-refresh-users")
    )
    assert DEFAULT_SLACK_REFRESH_USERS_BIN.endswith("/slack-refresh-users")


# ---------------------- argv-shape invariant ------------------------------


def test_slack_refresh_users_argv0_resolves_to_expected_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SLACK_REFRESH_USERS_BIN_ENV, raising=False)
    argv = build_slack_refresh_users_argv([])
    assert Path(argv[0]).name == SLACK_REFRESH_USERS_EXPECTED_BASENAME
    assert Path(argv[0]).name == "slack-refresh-users"


def test_slack_refresh_users_argv0_across_run_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_REFRESH_USERS_BIN_ENV, "/tmp/fixtures/slack-refresh-users"
    )
    with patch.object(sru_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_refresh_users([])
    assert len(calls) == 1
    assert Path(calls[0]["cmd"][0]).name == "slack-refresh-users"


# --------------------------------------- run_slack_refresh_users happy ----


def test_run_slack_refresh_users_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_REFRESH_USERS_BIN_ENV, "/tmp/fake_slack_refresh_users"
    )
    with patch.object(sru_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_refresh_users([])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["cmd"] == ["/tmp/fake_slack_refresh_users"]


def test_run_slack_refresh_users_forwards_extras_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even though slack-refresh-users currently takes no args, extras pass through."""
    monkeypatch.setenv(
        SLACK_REFRESH_USERS_BIN_ENV, "/tmp/fake_slack_refresh_users"
    )
    with patch.object(sru_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_refresh_users(["--future-flag", "value"])
    assert calls[0]["cmd"] == [
        "/tmp/fake_slack_refresh_users",
        "--future-flag",
        "value",
    ]


def test_run_slack_refresh_users_propagates_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_REFRESH_USERS_BIN_ENV, "/tmp/fake_slack_refresh_users"
    )
    with patch.object(sru_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=1)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_refresh_users([])
    assert rc == 1


def test_run_slack_refresh_users_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_REFRESH_USERS_BIN_ENV, "/tmp/fake_slack_refresh_users"
    )
    with patch.object(sru_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_refresh_users([])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


# -------------------------------------------------- missing-binary path ----


def test_run_slack_refresh_users_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        SLACK_REFRESH_USERS_BIN_ENV,
        "/nonexistent/absolute/path/slack-refresh-users",
    )

    with pytest.raises(typer.Exit) as excinfo:
        run_slack_refresh_users([])
    assert excinfo.value.exit_code == SRU_MISSING

    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/slack-refresh-users" in err
    assert SLACK_REFRESH_USERS_BIN_ENV in err
    assert "not found" in err.lower()


def test_run_slack_refresh_users_race_missing_at_exec_time_maps_to_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        SLACK_REFRESH_USERS_BIN_ENV, "/tmp/racing_slack_refresh_users"
    )
    with patch.object(sru_wrapper, "_binary_available", return_value=True):

        def raising_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory")

        with patch.object(subprocess, "run", raising_run):
            with pytest.raises(typer.Exit) as excinfo:
                run_slack_refresh_users([])
    assert excinfo.value.exit_code == SRU_MISSING
    err = capsys.readouterr().err
    assert "/tmp/racing_slack_refresh_users" in err


# --------------------------------------- static "facade only" guard --------


SRU_WRAPPER_SRC = Path(sru_wrapper.__file__).read_text()


def test_slack_refresh_users_wrapper_source_does_no_output_parsing() -> None:
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
        assert forbidden not in SRU_WRAPPER_SRC, (
            f"wrappers/slack_refresh_users.py must be facade-only; found {forbidden!r}"
        )


def test_slack_refresh_users_wrapper_source_makes_no_slack_web_api_calls() -> None:
    """Same rule as slack_read.py: no direct Slack Web API calls from Python.

    Uses the same quoted-shape / code-shape guards so docstring prose
    can freely describe the underlying shell-script behavior without
    tripping the test.
    """
    for forbidden in (
        '"https://slack.com',
        "'https://slack.com",
        '"https://',
        "'https://",
        "import requests",
        "from requests",
        "requests.get",
        "requests.post",
        "import urllib.request",
        "from urllib.request",
        "urlopen(",
        '"curl",',
        "'curl',",
    ):
        assert forbidden not in SRU_WRAPPER_SRC, (
            f"wrappers/slack_refresh_users.py must not make direct API calls; found {forbidden!r}"
        )


# ---------------- true end-to-end: fake slack-refresh-users on disk --------


@pytest.fixture
def fake_slack_refresh_users_exit_0(tmp_path: Path) -> Path:
    """Fake slack-refresh-users that emits the real script's happy-path lines."""
    script = tmp_path / "slack-refresh-users"
    script.write_text(
        "#!/bin/sh\n"
        'printf "Fetching Slack users...\\n"\n'
        'printf "Cached 42 users to /tmp/fake-users.json\\n"\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_run_slack_refresh_users_end_to_end_stdout_flows_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_slack_refresh_users_exit_0: Path,
    capfd: pytest.CaptureFixture,
) -> None:
    """The fake's stdout reaches the caller's fd untouched."""
    monkeypatch.setenv(
        SLACK_REFRESH_USERS_BIN_ENV, str(fake_slack_refresh_users_exit_0)
    )
    capfd.readouterr()
    rc = run_slack_refresh_users([])
    captured = capfd.readouterr()
    assert rc == 0
    assert "Cached 42 users" in captured.out


# ============================================================================
# slack-thread wrapper
# ============================================================================
#
# Same shape as slack_read: default/env resolver, argv-basename invariant,
# happy-path run, exit-code propagation (including unusual 77), pass-through
# stdio, missing-binary paths, observer-basename runtime guard, facade-only
# static guards (no output parsing, no direct HTTP), and an end-to-end fake
# binary that exercises the real subprocess.run code path.


def test_slack_thread_resolve_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SLACK_THREAD_BIN_ENV, raising=False)
    assert resolve_slack_thread_bin() == DEFAULT_SLACK_THREAD_BIN


def test_slack_thread_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SLACK_THREAD_BIN_ENV, "/tmp/fake/slack-thread")
    assert resolve_slack_thread_bin() == "/tmp/fake/slack-thread"


def test_slack_thread_resolve_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_THREAD_BIN_ENV, "")
    assert resolve_slack_thread_bin() == DEFAULT_SLACK_THREAD_BIN


def test_slack_thread_default_points_at_workspace_bin() -> None:
    assert DEFAULT_SLACK_THREAD_BIN.endswith("/slack-thread")


def test_slack_thread_argv0_resolves_to_slack_thread_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SLACK_THREAD_BIN_ENV, raising=False)
    argv = build_slack_thread_argv(["C02RAQRC10T", "1723050000.123456"])
    assert Path(argv[0]).name == SLACK_THREAD_EXPECTED_BASENAME
    assert Path(argv[0]).name == "slack-thread"


def test_run_slack_thread_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_THREAD_BIN_ENV, "/tmp/fake/slack-thread")
    with patch.object(st_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_thread(["C02RAQRC10T", "1723050000.123456"])
    assert rc == 0
    assert calls[0]["cmd"] == [
        "/tmp/fake/slack-thread",
        "C02RAQRC10T",
        "1723050000.123456",
    ]


def test_run_slack_thread_propagates_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_THREAD_BIN_ENV, "/tmp/fake/slack-thread")
    with patch.object(st_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=1)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_thread(["C02RAQRC10T", "1723050000.123456"])
    assert rc == 1


def test_run_slack_thread_propagates_unusual_77(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firewall convention exit 77 must not be rewritten by the wrapper."""
    monkeypatch.setenv(SLACK_THREAD_BIN_ENV, "/tmp/fake/slack-thread")
    with patch.object(st_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=77)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_thread(["C02RAQRC10T", "1723050000.123456"])
    assert rc == 77


def test_run_slack_thread_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run is called with NO stdout/stderr override."""
    monkeypatch.setenv(SLACK_THREAD_BIN_ENV, "/tmp/fake/slack-thread")
    with patch.object(st_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_thread(["C02RAQRC10T", "1723050000.123456"])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


def test_run_slack_thread_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        SLACK_THREAD_BIN_ENV, "/nonexistent/absolute/path/slack-thread"
    )
    with pytest.raises(typer.Exit) as excinfo:
        run_slack_thread(["C02RAQRC10T", "1723050000.123456"])
    assert excinfo.value.exit_code == ST_MISSING
    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/slack-thread" in err
    assert SLACK_THREAD_BIN_ENV in err
    assert "not found" in err.lower()


@pytest.mark.parametrize(
    "bad_binary",
    [
        "/opt/homebrew/bin/slack",
        "slack",
        "/tmp/bin/slack-cli",
        "/tmp/bin/slack-write",
    ],
)
def test_run_slack_thread_refuses_non_slack_thread_env_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    bad_binary: str,
) -> None:
    """The observer basename guard rejects a write-capable env override."""
    monkeypatch.setenv(SLACK_THREAD_BIN_ENV, bad_binary)

    def must_not_run(*a, **kw):
        raise AssertionError(
            "subprocess.run was called despite basename guard rejecting env"
        )

    with patch.object(subprocess, "run", must_not_run):
        with pytest.raises(typer.Exit) as excinfo:
            run_slack_thread(["C02RAQRC10T", "1723050000.123456"])
    assert excinfo.value.exit_code == ST_BASENAME_REFUSED
    err = capsys.readouterr().err
    assert SLACK_THREAD_BIN_ENV in err
    assert "slack-thread" in err
    assert bad_binary in err


ST_WRAPPER_SRC = Path(st_wrapper.__file__).read_text()


def test_slack_thread_wrapper_source_does_no_output_parsing() -> None:
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
        assert forbidden not in ST_WRAPPER_SRC, (
            f"wrappers/slack_thread.py must be facade-only; found {forbidden!r}"
        )


def test_slack_thread_wrapper_source_makes_no_slack_web_api_calls() -> None:
    for forbidden in (
        '"https://slack.com',
        "'https://slack.com",
        '"https://',
        "'https://",
        "import requests",
        "from requests",
        "requests.get",
        "requests.post",
        "import urllib.request",
        "from urllib.request",
        "urlopen(",
        '"curl",',
        "'curl',",
    ):
        assert forbidden not in ST_WRAPPER_SRC, (
            f"wrappers/slack_thread.py must not make direct API calls; found {forbidden!r}"
        )


@pytest.fixture
def fake_slack_thread_exit_0(tmp_path: Path) -> Path:
    """Fake slack-thread that emits a plausible reshaped JSON payload."""
    script = tmp_path / "slack-thread"
    script.write_text(
        "#!/bin/sh\n"
        'printf "{\\"ok\\":true,\\"channel\\":\\"$1\\",\\"thread_ts\\":\\"$2\\",\\"messages\\":[]}\\n"\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_run_slack_thread_end_to_end_stdout_flows_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_slack_thread_exit_0: Path,
    capfd: pytest.CaptureFixture,
) -> None:
    """Real subprocess call to a fake binary: stdout survives unchanged."""
    monkeypatch.setenv(SLACK_THREAD_BIN_ENV, str(fake_slack_thread_exit_0))
    capfd.readouterr()
    rc = run_slack_thread(["C02RAQRC10T", "1723050000.123456"])
    captured = capfd.readouterr()
    assert rc == 0
    assert '"ok":true' in captured.out
    assert '"channel":"C02RAQRC10T"' in captured.out
    assert '"thread_ts":"1723050000.123456"' in captured.out


# ============================================================================
# slack-channels wrapper
# ============================================================================


def test_slack_channels_resolve_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SLACK_CHANNELS_BIN_ENV, raising=False)
    assert resolve_slack_channels_bin() == DEFAULT_SLACK_CHANNELS_BIN


def test_slack_channels_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SLACK_CHANNELS_BIN_ENV, "/tmp/fake/slack-channels")
    assert resolve_slack_channels_bin() == "/tmp/fake/slack-channels"


def test_slack_channels_resolve_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_CHANNELS_BIN_ENV, "")
    assert resolve_slack_channels_bin() == DEFAULT_SLACK_CHANNELS_BIN


def test_slack_channels_default_points_at_workspace_bin() -> None:
    assert DEFAULT_SLACK_CHANNELS_BIN.endswith("/slack-channels")


def test_slack_channels_argv0_basename(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SLACK_CHANNELS_BIN_ENV, raising=False)
    argv = build_slack_channels_argv([])
    assert Path(argv[0]).name == SLACK_CHANNELS_EXPECTED_BASENAME
    assert Path(argv[0]).name == "slack-channels"


def test_run_slack_channels_no_args_forwards_empty_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_CHANNELS_BIN_ENV, "/tmp/fake/slack-channels")
    with patch.object(sc_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_channels([])
    assert rc == 0
    assert calls[0]["cmd"] == ["/tmp/fake/slack-channels"]


def test_run_slack_channels_include_private_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_CHANNELS_BIN_ENV, "/tmp/fake/slack-channels")
    with patch.object(sc_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_channels(["--include-private"])
    assert calls[0]["cmd"] == [
        "/tmp/fake/slack-channels",
        "--include-private",
    ]


def test_run_slack_channels_propagates_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_CHANNELS_BIN_ENV, "/tmp/fake/slack-channels")
    with patch.object(sc_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=1)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_channels([])
    assert rc == 1


def test_run_slack_channels_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_CHANNELS_BIN_ENV, "/tmp/fake/slack-channels")
    with patch.object(sc_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_channels([])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs


def test_run_slack_channels_missing_binary_exits_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        SLACK_CHANNELS_BIN_ENV, "/nonexistent/absolute/path/slack-channels"
    )
    with pytest.raises(typer.Exit) as excinfo:
        run_slack_channels([])
    assert excinfo.value.exit_code == SC_MISSING
    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/slack-channels" in err
    assert SLACK_CHANNELS_BIN_ENV in err


@pytest.mark.parametrize(
    "bad_binary",
    [
        "/opt/homebrew/bin/slack",
        "slack",
        "/tmp/bin/slack-write",
    ],
)
def test_run_slack_channels_refuses_wrong_basename_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    bad_binary: str,
) -> None:
    monkeypatch.setenv(SLACK_CHANNELS_BIN_ENV, bad_binary)

    def must_not_run(*a, **kw):
        raise AssertionError("basename guard failed")

    with patch.object(subprocess, "run", must_not_run):
        with pytest.raises(typer.Exit) as excinfo:
            run_slack_channels([])
    assert excinfo.value.exit_code == SC_BASENAME_REFUSED
    err = capsys.readouterr().err
    assert "slack-channels" in err
    assert bad_binary in err


SC_WRAPPER_SRC = Path(sc_wrapper.__file__).read_text()


def test_slack_channels_wrapper_source_does_no_output_parsing() -> None:
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
        assert forbidden not in SC_WRAPPER_SRC, (
            f"wrappers/slack_channels.py must be facade-only; found {forbidden!r}"
        )


def test_slack_channels_wrapper_source_makes_no_slack_web_api_calls() -> None:
    for forbidden in (
        '"https://slack.com',
        "'https://slack.com",
        '"https://',
        "'https://",
        "import requests",
        "from requests",
        "requests.get",
        "requests.post",
        "import urllib.request",
        "from urllib.request",
        "urlopen(",
        '"curl",',
        "'curl',",
    ):
        assert forbidden not in SC_WRAPPER_SRC, (
            f"wrappers/slack_channels.py must not make direct API calls; found {forbidden!r}"
        )


@pytest.fixture
def fake_slack_channels_exit_0(tmp_path: Path) -> Path:
    """Fake slack-channels that emits a plausible reshaped JSON payload."""
    script = tmp_path / "slack-channels"
    script.write_text(
        "#!/bin/sh\n"
        'printf "{\\"ok\\":true,\\"channels\\":[{\\"id\\":\\"C1\\",\\"name\\":\\"general\\"}]}\\n"\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_run_slack_channels_end_to_end_stdout_flows_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_slack_channels_exit_0: Path,
    capfd: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(SLACK_CHANNELS_BIN_ENV, str(fake_slack_channels_exit_0))
    capfd.readouterr()
    rc = run_slack_channels([])
    captured = capfd.readouterr()
    assert rc == 0
    assert '"name":"general"' in captured.out


# ============================================================================
# slack-search-public wrapper
# ============================================================================


def test_slack_search_public_resolve_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SLACK_SEARCH_PUBLIC_BIN_ENV, raising=False)
    assert resolve_slack_search_public_bin() == DEFAULT_SLACK_SEARCH_PUBLIC_BIN


def test_slack_search_public_resolve_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_SEARCH_PUBLIC_BIN_ENV, "/tmp/fake/slack-search-public"
    )
    assert resolve_slack_search_public_bin() == "/tmp/fake/slack-search-public"


def test_slack_search_public_resolve_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_SEARCH_PUBLIC_BIN_ENV, "")
    assert resolve_slack_search_public_bin() == DEFAULT_SLACK_SEARCH_PUBLIC_BIN


def test_slack_search_public_default_points_at_workspace_bin() -> None:
    assert DEFAULT_SLACK_SEARCH_PUBLIC_BIN.endswith("/slack-search-public")


def test_slack_search_public_argv0_basename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SLACK_SEARCH_PUBLIC_BIN_ENV, raising=False)
    argv = build_slack_search_public_argv(["sabbath"])
    assert Path(argv[0]).name == SLACK_SEARCH_PUBLIC_EXPECTED_BASENAME
    assert Path(argv[0]).name == "slack-search-public"


def test_run_slack_search_public_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_SEARCH_PUBLIC_BIN_ENV, "/tmp/fake/slack-search-public"
    )
    with patch.object(ssp_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_search_public(["worship set list"])
    assert rc == 0
    assert calls[0]["cmd"] == [
        "/tmp/fake/slack-search-public",
        "worship set list",
    ]


def test_run_slack_search_public_extras_forwarded_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_SEARCH_PUBLIC_BIN_ENV, "/tmp/fake/slack-search-public"
    )
    with patch.object(ssp_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_search_public(["prayer", "--count", "5"])
    assert calls[0]["cmd"] == [
        "/tmp/fake/slack-search-public",
        "prayer",
        "--count",
        "5",
    ]


def test_run_slack_search_public_propagates_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_SEARCH_PUBLIC_BIN_ENV, "/tmp/fake/slack-search-public"
    )
    with patch.object(ssp_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=1)
        with patch.object(subprocess, "run", fake_run):
            rc = run_slack_search_public(["sabbath"])
    assert rc == 1


def test_run_slack_search_public_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        SLACK_SEARCH_PUBLIC_BIN_ENV, "/tmp/fake/slack-search-public"
    )
    with patch.object(ssp_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_search_public(["sabbath"])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs


def test_run_slack_search_public_missing_binary_exits_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        SLACK_SEARCH_PUBLIC_BIN_ENV,
        "/nonexistent/absolute/path/slack-search-public",
    )
    with pytest.raises(typer.Exit) as excinfo:
        run_slack_search_public(["sabbath"])
    assert excinfo.value.exit_code == SSP_MISSING
    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/slack-search-public" in err
    assert SLACK_SEARCH_PUBLIC_BIN_ENV in err


@pytest.mark.parametrize(
    "bad_binary",
    [
        "/opt/homebrew/bin/slack",
        "slack",
        "/tmp/bin/slack-write",
    ],
)
def test_run_slack_search_public_refuses_wrong_basename_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    bad_binary: str,
) -> None:
    monkeypatch.setenv(SLACK_SEARCH_PUBLIC_BIN_ENV, bad_binary)

    def must_not_run(*a, **kw):
        raise AssertionError("basename guard failed")

    with patch.object(subprocess, "run", must_not_run):
        with pytest.raises(typer.Exit) as excinfo:
            run_slack_search_public(["sabbath"])
    assert excinfo.value.exit_code == SSP_BASENAME_REFUSED
    err = capsys.readouterr().err
    assert "slack-search-public" in err
    assert bad_binary in err


SSP_WRAPPER_SRC = Path(ssp_wrapper.__file__).read_text()


def test_slack_search_public_wrapper_source_does_no_output_parsing() -> None:
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
        assert forbidden not in SSP_WRAPPER_SRC, (
            f"wrappers/slack_search_public.py must be facade-only; found {forbidden!r}"
        )


def test_slack_search_public_wrapper_source_makes_no_slack_web_api_calls() -> None:
    for forbidden in (
        '"https://slack.com',
        "'https://slack.com",
        '"https://',
        "'https://",
        "import requests",
        "from requests",
        "requests.get",
        "requests.post",
        "import urllib.request",
        "from urllib.request",
        "urlopen(",
        '"curl",',
        "'curl',",
    ):
        assert forbidden not in SSP_WRAPPER_SRC, (
            f"wrappers/slack_search_public.py must not make direct API calls; found {forbidden!r}"
        )


@pytest.fixture
def fake_slack_search_public_exit_0(tmp_path: Path) -> Path:
    """Fake slack-search-public that echoes the query in a plausible payload."""
    script = tmp_path / "slack-search-public"
    script.write_text(
        "#!/bin/sh\n"
        'printf "{\\"ok\\":true,\\"query\\":\\"$1\\",\\"total\\":0,\\"matches\\":[]}\\n"\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_run_slack_search_public_end_to_end_stdout_flows_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_slack_search_public_exit_0: Path,
    capfd: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        SLACK_SEARCH_PUBLIC_BIN_ENV, str(fake_slack_search_public_exit_0)
    )
    capfd.readouterr()
    rc = run_slack_search_public(["sabbath"])
    captured = capfd.readouterr()
    assert rc == 0
    assert '"query":"sabbath"' in captured.out
