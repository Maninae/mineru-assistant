"""Tests for the imsg-firewall facade wrapper (P2-06).

Full mirror of `tests/test_gmail_wrapper.py`, adapted for the iMessage
firewall path. Covers:

  - `resolve_imsg_firewall_bin` honors MINERU_IMSG_FIREWALL_BIN, falls
    back to default, treats empty-string env as unset.
  - INVARIANT: `build_imsg_firewall_argv`'s argv[0] resolves to an
    `imsg-firewall` path (NEVER bare `imsg`, NEVER `imsg-named`). This
    is the code-review-level guard on firewall preservation for reads.
  - `run_imsg_firewall` builds the expected argv and propagates the
    firewall's exit codes 0 / 77 (all blocked) / 78 (firewall error)
    unchanged.
  - Missing binary -> exit 127 with an actionable stderr message that
    names the resolved path AND the override env var.
  - `subprocess.run` is called with NO stdout/stderr override
    (pass-through), so the firewall's `redacted N of M units` stderr
    notices reach the user untouched.
  - Wrapper source contains no bypass literals - `imsg-named` (the
    FORBIDDEN-for-reads path per TOOLS.md) never appears as a live
    subprocess argv; the raw `imsg` binary path never appears as a
    live subprocess argv.
  - End-to-end: a fake imsg-firewall on disk exiting 77 causes the
    wrapper to return 77 through the real subprocess call.
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

from mineru_cli.wrappers import imsg_firewall as if_wrapper
from mineru_cli.wrappers.imsg_firewall import (
    DEFAULT_IMSG_FIREWALL_BIN,
    EXPECTED_BIN_BASENAME,
    FIREWALL_BASENAME_MISMATCH_EXIT_CODE,
    IMSG_FIREWALL_BIN_ENV,
    MISSING_BIN_EXIT_CODE,
    build_imsg_firewall_argv,
    resolve_imsg_firewall_bin,
    run_imsg_firewall,
)


# Layout constant used by the subprocess-based end-to-end test below.
REPO_ROOT = Path(__file__).resolve().parent.parent


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


# ------------------------------------------------------------- resolver ----


def test_resolve_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMSG_FIREWALL_BIN_ENV, raising=False)
    assert resolve_imsg_firewall_bin() == DEFAULT_IMSG_FIREWALL_BIN


def test_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "/tmp/fake/imsg-firewall")
    assert resolve_imsg_firewall_bin() == "/tmp/fake/imsg-firewall"


def test_resolve_empty_env_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "")
    assert resolve_imsg_firewall_bin() == DEFAULT_IMSG_FIREWALL_BIN


def test_default_points_at_workspace_bin() -> None:
    """Sanity: the documented default is the workspace's imsg-firewall path.

    Belt-and-braces guard on the wrapper's constant. If someone ever
    swaps the default to `/opt/homebrew/bin/imsg` or the FORBIDDEN
    `imsg-named` path this test fires.
    """
    assert DEFAULT_IMSG_FIREWALL_BIN == str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin" / "imsg-firewall")
    assert DEFAULT_IMSG_FIREWALL_BIN.endswith("/imsg-firewall")
    assert not DEFAULT_IMSG_FIREWALL_BIN.endswith("/imsg")
    assert not DEFAULT_IMSG_FIREWALL_BIN.endswith("/imsg-named")


# ------------------------- FIREWALL-PRESERVATION INVARIANT ---------------
# The single most important test in P2-06: argv[0] must resolve to an
# imsg-firewall path, NEVER bare `imsg` or the forbidden `imsg-named`.


def test_argv0_resolves_to_imsg_firewall_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default binary path's basename is `imsg-firewall`, not `imsg` or `imsg-named`."""
    monkeypatch.delenv(IMSG_FIREWALL_BIN_ENV, raising=False)
    argv = build_imsg_firewall_argv(["chats", "--limit", "5"])
    assert os.path.basename(argv[0]) == EXPECTED_BIN_BASENAME
    assert os.path.basename(argv[0]) == "imsg-firewall"
    # Explicitly not the raw imsg binary.
    assert argv[0] != "/opt/homebrew/bin/imsg"
    assert os.path.basename(argv[0]) != "imsg"
    # Explicitly not the FORBIDDEN-for-reads imsg-named path.
    assert argv[0] != str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin" / "imsg-named")
    assert os.path.basename(argv[0]) != "imsg-named"


def test_argv0_resolves_to_imsg_firewall_when_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env override still keeps argv[0] name-shaped like the firewall."""
    monkeypatch.setenv(
        IMSG_FIREWALL_BIN_ENV, "/tmp/mineru_test_fixtures/imsg-firewall"
    )
    argv = build_imsg_firewall_argv(["chats"])
    assert os.path.basename(argv[0]) == "imsg-firewall"


def test_argv0_never_bare_imsg_across_run_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_imsg_firewall`'s subprocess argv[0] is an imsg-firewall path.

    Belt-and-braces: even at the actual subprocess.run call-site (not
    just the argv-building helper), argv[0] is `imsg-firewall` -
    NEVER `imsg`, NEVER `imsg-named`.
    """
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "/tmp/fixtures/imsg-firewall")
    with patch.object(if_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_imsg_firewall(["chats"])
    assert len(calls) == 1
    assert os.path.basename(calls[0]["cmd"][0]) == "imsg-firewall"
    # And never the raw imsg binary.
    assert calls[0]["cmd"][0] != "/opt/homebrew/bin/imsg"
    assert os.path.basename(calls[0]["cmd"][0]) != "imsg"
    # And never the FORBIDDEN imsg-named path.
    assert calls[0]["cmd"][0] != str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin" / "imsg-named")
    assert os.path.basename(calls[0]["cmd"][0]) != "imsg-named"


# ---------------------------------------------- run_imsg_firewall happy ----


def test_run_imsg_firewall_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "/tmp/fake/imsg-firewall")
    with patch.object(if_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_imsg_firewall(["chats", "--limit", "5", "--json"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "/tmp/fake/imsg-firewall",
        "chats",
        "--limit",
        "5",
        "--json",
    ]


# ---------------------- firewall exit code contract (0 / 77 / 78) --------


def test_run_imsg_firewall_propagates_exit_0_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "/tmp/fake/imsg-firewall")
    with patch.object(if_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_imsg_firewall(["chats"])
    assert rc == 0


def test_run_imsg_firewall_propagates_exit_77_all_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firewall's 'all units blocked' code must reach the caller intact."""
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "/tmp/fake/imsg-firewall")
    with patch.object(if_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=77)
        with patch.object(subprocess, "run", fake_run):
            rc = run_imsg_firewall(["chats"])
    assert rc == 77


def test_run_imsg_firewall_propagates_exit_78_firewall_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firewall's own error code must reach the caller intact."""
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "/tmp/fake/imsg-firewall")
    with patch.object(if_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=78)
        with patch.object(subprocess, "run", fake_run):
            rc = run_imsg_firewall(["chats"])
    assert rc == 78


def test_run_imsg_firewall_propagates_arbitrary_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "/tmp/fake/imsg-firewall")
    with patch.object(if_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=42)
        with patch.object(subprocess, "run", fake_run):
            rc = run_imsg_firewall(["chats"])
    assert rc == 42


# --------------------- stdio pass-through (redaction notices) ------------


def test_run_imsg_firewall_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run is called with NO stdout/stderr override.

    That's how the firewall's `redacted N of M units` stderr notices
    flow straight to the user without any filtering or re-formatting.
    """
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "/tmp/fake/imsg-firewall")
    with patch.object(if_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_imsg_firewall(["chats"])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


# -------------------------------------------------- missing-binary path ----


def test_run_imsg_firewall_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        IMSG_FIREWALL_BIN_ENV, "/nonexistent/absolute/path/imsg-firewall"
    )
    with pytest.raises(typer.Exit) as excinfo:
        run_imsg_firewall(["chats"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE

    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/imsg-firewall" in err
    assert IMSG_FIREWALL_BIN_ENV in err
    assert "not found" in err.lower()


def test_run_imsg_firewall_directory_at_resolved_path_returns_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A directory at the resolved path must NOT crash with an OSError traceback."""
    dir_path = tmp_path / "somewhere" / "imsg-firewall"
    dir_path.mkdir(parents=True)  # a directory, not a file
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, str(dir_path))

    with pytest.raises(typer.Exit) as excinfo:
        run_imsg_firewall(["chats"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert str(dir_path) in err


def test_run_imsg_firewall_non_executable_file_at_resolved_path_returns_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A non-executable file must NOT crash with a PermissionError traceback."""
    non_exec = tmp_path / "imsg-firewall"
    non_exec.write_text("#!/bin/sh\nexit 0\n")  # no chmod +x
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, str(non_exec))

    with pytest.raises(typer.Exit) as excinfo:
        run_imsg_firewall(["chats"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert str(non_exec) in err


# -------- BASENAME-PRESERVATION runtime guard (finding #1 regression) --------


@pytest.mark.parametrize(
    "bad_binary",
    [
        "/opt/homebrew/bin/imsg",
        "imsg",
        str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin" / "imsg-named"),
        "imsg-named",
        "/tmp/other/imsg-shim",
    ],
)
def test_run_imsg_firewall_refuses_non_imsg_firewall_env_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    bad_binary: str,
) -> None:
    """Setting MINERU_IMSG_FIREWALL_BIN=<not-imsg-firewall> must NOT shell out.

    Direct regression guard for the runtime firewall-preservation check.
    A misconfigured env override that names bare `imsg`, the forbidden
    `imsg-named` path, or any non-`imsg-firewall` shim is refused with
    the firewall-error exit code (78) and an actionable stderr, BEFORE
    subprocess.run is called.
    """
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, bad_binary)

    def must_not_run(*a, **kw):
        raise AssertionError(
            "subprocess.run was called despite basename guard rejecting env"
        )

    with patch.object(subprocess, "run", must_not_run):
        with pytest.raises(typer.Exit) as excinfo:
            run_imsg_firewall(["chats"])

    assert excinfo.value.exit_code == FIREWALL_BASENAME_MISMATCH_EXIT_CODE
    err = capsys.readouterr().err
    assert IMSG_FIREWALL_BIN_ENV in err
    assert "imsg-firewall" in err
    assert bad_binary in err


def test_run_imsg_firewall_race_missing_at_exec_time_maps_to_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Binary check succeeded but exec raised FileNotFoundError."""
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, "/tmp/racing/imsg-firewall")
    with patch.object(if_wrapper, "_binary_available", return_value=True):

        def raising_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory")

        with patch.object(subprocess, "run", raising_run):
            with pytest.raises(typer.Exit) as excinfo:
                run_imsg_firewall(["chats"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert "/tmp/racing/imsg-firewall" in err


# ------------------------------------- static "no bypass paths" guard -----


WRAPPER_SRC = Path(if_wrapper.__file__).read_text()


def test_wrapper_source_has_no_bypass_binary_literals() -> None:
    """The wrapper file must not use forbidden binary paths as string literals.

    The raw `imsg` at `/opt/homebrew/bin/imsg` and the FORBIDDEN
    `imsg-named` path at `$MINERU_HOME/bin/imsg-named` must
    never appear as subprocess argv literals here. Prose mentions in
    docstrings are OK; the check looks for the quoted-string form.
    """
    # Raw imsg binary path must never be a live subprocess argv[0].
    assert '"/opt/homebrew/bin/imsg"' not in WRAPPER_SRC
    assert "'/opt/homebrew/bin/imsg'" not in WRAPPER_SRC
    # FORBIDDEN imsg-named path must never be a live subprocess argv[0].
    # Match a quoted string literal (path or Path segment) ending in
    # imsg-named; docstring/backtick prose mentions do not have a straight
    # quote immediately after and so are correctly ignored.
    assert 'imsg-named"' not in WRAPPER_SRC
    assert "imsg-named'" not in WRAPPER_SRC


def test_wrapper_source_does_no_output_parsing() -> None:
    """The wrapper file must not parse or reshape engine output.

    Coarse grep: if anyone tries to `json.loads` firewall stdout,
    capture output for reshaping, or reformat the engine bytes, this
    fires.
    """
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
            f"wrappers/imsg_firewall.py must be facade-only; found {forbidden!r}"
        )


def test_codebase_read_verbs_never_import_raw_imsg_paths() -> None:
    """Grep the whole `mineru_cli/` tree for direct references to raw imsg or imsg-named.

    The invariant: no read verb calls `/opt/homebrew/bin/imsg` or
    `$MINERU_HOME/bin/imsg-named` as a subprocess argv[0].
    `imsg-firewall` is the only argv[0] for iMessage reads.

    Carve-out: `wrappers/imsg.py` legitimately uses
    `/opt/homebrew/bin/imsg` as its default binary (that's the whole
    point of the outbound wrapper). We skip that file - it is the
    WRITE-side wrapper and has its own tests. Everywhere else, the
    raw `imsg` path is a regression.
    """
    package_root = Path(if_wrapper.__file__).resolve().parent.parent
    write_wrapper = (package_root / "wrappers" / "imsg.py").resolve()
    offenders = []
    for py_file in package_root.rglob("*.py"):
        if py_file.resolve() == write_wrapper:
            continue
        text = py_file.read_text()
        # imsg-named is FORBIDDEN for reads; anywhere it appears as a
        # quoted string literal (path or Path segment) is a bug. Backtick /
        # docstring prose has no straight quote after imsg-named, so it is
        # correctly ignored.
        if 'imsg-named"' in text or "imsg-named'" in text:
            offenders.append((str(py_file), "imsg-named"))
        # Raw imsg is fine ONLY in the write-side wrapper (already
        # excluded); anywhere else is a bug.
        if (
            '"/opt/homebrew/bin/imsg"' in text
            or "'/opt/homebrew/bin/imsg'" in text
        ):
            offenders.append((str(py_file), "raw-imsg"))
    assert offenders == [], (
        f"Found bypass-path references in read-verb code: {offenders}"
    )


# ---------------- true end-to-end: fake imsg-firewall on disk, exit 77 ----


@pytest.fixture
def fake_imsg_firewall_exit_77(tmp_path: Path) -> Path:
    """Write a tiny executable script that exits 77 (all-blocked convention)."""
    script = tmp_path / "imsg-firewall"
    script.write_text(
        "#!/bin/sh\n"
        "# Fake firewall: emit a plausible 'redacted' notice to stderr, exit 77.\n"
        'printf "redacted 3 of 3 units (fake)\\n" >&2\n'
        "exit 77\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_run_imsg_firewall_end_to_end_fake_binary_propagates_77(
    monkeypatch: pytest.MonkeyPatch,
    fake_imsg_firewall_exit_77: Path,
) -> None:
    """The wrapper -> subprocess -> fake binary chain propagates 77.

    Real subprocess call, no mocks. Verifies the firewall's 77 exit
    code propagates through the actual `subprocess.run` code path.
    """
    monkeypatch.setenv(IMSG_FIREWALL_BIN_ENV, str(fake_imsg_firewall_exit_77))
    rc = run_imsg_firewall(["chats"])
    assert rc == 77


@pytest.fixture
def fake_imsg_firewall_exit_0(tmp_path: Path) -> Path:
    """Fake firewall that emits a redaction notice on stderr and exits 0."""
    script = tmp_path / "imsg-firewall"
    script.write_text(
        "#!/bin/sh\n"
        'printf "{\\"chats\\": []}\\n"\n'
        'printf "redacted 1 of 4 units (fake)\\n" >&2\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_run_imsg_firewall_end_to_end_stderr_redaction_notice_flows_through(
    fake_imsg_firewall_exit_0: Path,
) -> None:
    """`mineru imessage chats`'s STDERR carries the firewall's redaction notice.

    Spawns the installed `mineru` binary via real subprocess so the child
    truly writes to its own stderr fd — the same channel the user sees.
    A CliRunner-level check would not catch a regression where the CLI
    layer captured / filtered the child stderr, because Typer's runner
    replaces sys.stderr but the child writes to the real fd.
    """
    venv_mineru = REPO_ROOT / ".venv" / "bin" / "mineru"
    if not venv_mineru.exists():
        pytest.skip(f"venv mineru missing at {venv_mineru}")

    env = os.environ.copy()
    for key in list(env):
        if key.startswith("MINERU_"):
            env.pop(key, None)
    # Re-point the child at the shipped synthetic seed profile: the engine
    # repo ships no `current` active-profile symlink, so a bare `mineru`
    # subprocess would otherwise fail profile resolution before reaching
    # the firewall wrapper. Mirrors the conftest autouse fixture.
    seed_base = REPO_ROOT / "tests" / "fixtures" / "seed_profile_base"
    env["MINERU_PROFILE"] = "mineru"
    env["MINERU_PROFILE_ROOT"] = str(seed_base)
    env[IMSG_FIREWALL_BIN_ENV] = str(fake_imsg_firewall_exit_0)

    result = subprocess.run(
        [str(venv_mineru), "imessage", "chats"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=30,
    )

    assert result.returncode == 0, (
        f"unexpected exit rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "chats" in result.stdout, (
        f"stdout regression: expected JSON payload; got {result.stdout!r}"
    )
    assert "redacted 1 of 4 units" in result.stderr, (
        f"redaction notice missing from CLI stderr: {result.stderr!r}"
    )
