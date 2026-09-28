"""Tests for the Phase-2 Brevity verb (P2-10).

Covers the single verb the P2-10 task lists:

  brevity <url-or-file> [--extended]        (READ-ONLY)

brevity is registered as a BARE COMMAND on the root `mineru` app
(not a group), so the natural human-typed order `mineru brevity
<url> --extended` works. See `verbs/brevity.py` docstring's Shape
choice note for why.

Test discipline (P2 hard safety rule):

  - brevity is entirely READ-ONLY -- it fetches external content but
    never posts / sends / writes -- so mocking is a wrapper-shape
    hygiene aid, not a safety requirement here.
  - Every path also gets a `--help` smoke test to confirm the verb
    renders without a crash and stays discoverable.
  - A belt-and-braces static grep ensures the verb file only reaches
    the underlying brevity CLI via the wrapper (never `import
    subprocess` or a direct-shell string).
  - A separate static grep ensures NEITHER `artifact-detect` nor
    `artifact-remove` appear as verbs (spec §7 marks them DEAD).
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
from mineru_cli.verbs import brevity as brevity_verb
from mineru_cli.wrappers import brevity as brevity_wrapper
from mineru_cli.wrappers.brevity import (
    BREVITY_BIN_ENV,
    DEFAULT_BREVITY_BIN,
    EXPECTED_BIN_BASENAME,
    MISSING_BIN_EXIT_CODE,
    build_brevity_argv,
    resolve_brevity_bin,
    run_brevity,
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


def _record_run_brevity(recorded: List[List[str]], returncode: int = 0):
    def fake(args):
        recorded.append(list(args))
        return returncode

    return fake


def _invoke(args: List[str], returncode: int = 0):
    """Run the CLI with a patched wrapper. Returns (CliResult, recorded_argv_list).

    We patch the wrapper at the registration-closure symbol path. Because
    `register_brevity(app)` defines the callback inside a factory function,
    the callback's module-level reference to `run_brevity` still resolves
    via `mineru_cli.verbs.brevity` -- so patching there is the right seam.
    """
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.brevity.run_brevity",
        _record_run_brevity(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ===========================================================================
# WRAPPER: resolver + argv invariant
# ===========================================================================


def test_resolve_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BREVITY_BIN_ENV, raising=False)
    assert resolve_brevity_bin() == DEFAULT_BREVITY_BIN


def test_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BREVITY_BIN_ENV, "/tmp/fake_brevity")
    assert resolve_brevity_bin() == "/tmp/fake_brevity"


def test_resolve_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BREVITY_BIN_ENV, "")
    assert resolve_brevity_bin() == DEFAULT_BREVITY_BIN


def test_default_points_at_workspace_bin_dir() -> None:
    """Sanity: the documented default is the workspace's brevity shim path."""
    assert DEFAULT_BREVITY_BIN == str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin" / "brevity")
    assert DEFAULT_BREVITY_BIN.endswith("/brevity")


def test_argv0_resolves_to_brevity_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(BREVITY_BIN_ENV, raising=False)
    argv = build_brevity_argv(["https://example.com/article"])
    assert os.path.basename(argv[0]) == EXPECTED_BIN_BASENAME
    assert os.path.basename(argv[0]) == "brevity"


def test_argv0_at_subprocess_call_site_is_brevity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: at the actual subprocess.run call-site, argv[0] basename is `brevity`."""
    monkeypatch.setenv(BREVITY_BIN_ENV, "/tmp/fixtures/brevity")
    with patch.object(brevity_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_brevity(["https://example.com/article"])
    assert len(calls) == 1
    assert os.path.basename(calls[0]["cmd"][0]) == "brevity"


# ===========================================================================
# WRAPPER: happy path + exit-code propagation
# ===========================================================================


def test_run_brevity_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BREVITY_BIN_ENV, "/tmp/fake_brevity")
    with patch.object(brevity_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_brevity(["https://example.com/article", "--extended"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "/tmp/fake_brevity",
        "https://example.com/article",
        "--extended",
    ]


def test_run_brevity_propagates_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BREVITY_BIN_ENV, "/tmp/fake_brevity")
    with patch.object(brevity_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=1)
        with patch.object(subprocess, "run", fake_run):
            rc = run_brevity(["https://example.com/paywalled"])
    assert rc == 1


def test_run_brevity_propagates_unusual_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper must not rewrite unusual codes."""
    monkeypatch.setenv(BREVITY_BIN_ENV, "/tmp/fake_brevity")
    with patch.object(brevity_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=42)
        with patch.object(subprocess, "run", fake_run):
            rc = run_brevity(["./doc.pdf"])
    assert rc == 42


def test_run_brevity_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run is called with NO stdout/stderr override.

    brevity emits the Smart Brevity summary on stdout and error prose
    on stderr; pass-through is non-negotiable so the caller can pipe
    the summary and read the diagnostics.
    """
    monkeypatch.setenv(BREVITY_BIN_ENV, "/tmp/fake_brevity")
    with patch.object(brevity_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_brevity(["https://example.com/article"])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


# ===========================================================================
# WRAPPER: missing-binary path
# ===========================================================================


def test_run_brevity_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(BREVITY_BIN_ENV, "/nonexistent/absolute/path/brevity")
    with pytest.raises(typer.Exit) as excinfo:
        run_brevity(["https://example.com/article"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE

    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/brevity" in err
    assert BREVITY_BIN_ENV in err
    assert "not found" in err.lower()


def test_run_brevity_race_missing_at_exec_time_maps_to_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Binary check succeeded but exec raised FileNotFoundError."""
    monkeypatch.setenv(BREVITY_BIN_ENV, "/tmp/racing_brevity")
    with patch.object(brevity_wrapper, "_binary_available", return_value=True):

        def raising_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory")

        with patch.object(subprocess, "run", raising_run):
            with pytest.raises(typer.Exit) as excinfo:
                run_brevity(["https://example.com/article"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert "/tmp/racing_brevity" in err


def test_run_brevity_env_points_at_directory_exits_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A directory-typed env override MUST NOT leak a PermissionError traceback."""
    monkeypatch.setenv(BREVITY_BIN_ENV, str(tmp_path))
    with pytest.raises(typer.Exit) as excinfo:
        run_brevity(["https://example.com/article"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert str(tmp_path) in err


def test_run_brevity_env_points_at_non_executable_file_exits_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A mode-644 file (no exec bit) MUST NOT leak a PermissionError traceback."""
    script = tmp_path / "brevity"
    script.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setenv(BREVITY_BIN_ENV, str(script))
    with pytest.raises(typer.Exit) as excinfo:
        run_brevity(["https://example.com/article"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert str(script) in err


# ===========================================================================
# End-to-end: fake brevity on disk, real subprocess.run
# ===========================================================================


@pytest.fixture
def fake_brevity_exit_0(tmp_path: Path) -> Path:
    """Fake brevity that emits a plausible summary stub and exits 0."""
    script = tmp_path / "brevity"
    script.write_text(
        "#!/bin/sh\n"
        'printf "## The big picture\\nSmart Brevity summary.\\n"\n'
        "exit 0\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script


@pytest.fixture
def fake_brevity_exit_1(tmp_path: Path) -> Path:
    """Fake brevity that emits an extraction error and exits 1."""
    script = tmp_path / "brevity"
    script.write_text(
        "#!/bin/sh\n"
        'printf "Error: Could not extract content from input.\\n" >&2\n'
        "exit 1\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script


def test_run_brevity_end_to_end_fake_binary_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    fake_brevity_exit_0: Path,
) -> None:
    monkeypatch.setenv(BREVITY_BIN_ENV, str(fake_brevity_exit_0))
    rc = run_brevity(["https://example.com/article"])
    assert rc == 0


def test_run_brevity_end_to_end_fake_binary_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    fake_brevity_exit_1: Path,
) -> None:
    monkeypatch.setenv(BREVITY_BIN_ENV, str(fake_brevity_exit_1))
    rc = run_brevity(["https://example.com/paywalled"])
    assert rc == 1


# ===========================================================================
# ROOT --json / --pretty NON-FORWARDING: end-to-end guard
#
# brevity rejects any unknown option (its `case` block on `--*` calls
# `error()` -> `exit 1`, "Unknown option: --json"). The mineru root
# swallows the flag; the fake brevity below records its argv so a
# regression that forwards the flag fails visibly.
# ===========================================================================


@pytest.fixture
def fake_brevity_argv_recorder(tmp_path: Path) -> "tuple[Path, Path]":
    """Fake brevity that dumps its argv to a sentinel file and exits 0."""
    sentinel = tmp_path / "argv.txt"
    script = tmp_path / "brevity"
    script.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do echo "$a" >> {sentinel}; done\n'
        "exit 0\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script, sentinel


def test_root_json_end_to_end_does_not_reach_brevity(
    monkeypatch: pytest.MonkeyPatch,
    fake_brevity_argv_recorder: "tuple[Path, Path]",
) -> None:
    """`mineru --json brevity <url>` exits 0 and does NOT pass --json."""
    binary, sentinel = fake_brevity_argv_recorder
    monkeypatch.setenv(BREVITY_BIN_ENV, str(binary))
    result = runner.invoke(
        app, ["--json", "brevity", "https://example.com/article"]
    )
    assert result.exit_code == 0
    recorded = sentinel.read_text().splitlines() if sentinel.exists() else []
    assert recorded == ["https://example.com/article"], (
        f"Expected clean argv, got {recorded!r}"
    )


def test_root_pretty_end_to_end_does_not_reach_brevity(
    monkeypatch: pytest.MonkeyPatch,
    fake_brevity_argv_recorder: "tuple[Path, Path]",
) -> None:
    """`mineru --pretty brevity <url>` exits 0 and does NOT pass --pretty."""
    binary, sentinel = fake_brevity_argv_recorder
    monkeypatch.setenv(BREVITY_BIN_ENV, str(binary))
    result = runner.invoke(
        app, ["--pretty", "brevity", "https://example.com/article"]
    )
    assert result.exit_code == 0
    recorded = sentinel.read_text().splitlines() if sentinel.exists() else []
    assert recorded == ["https://example.com/article"], (
        f"Expected clean argv, got {recorded!r}"
    )


# ===========================================================================
# BREVITY verb (bare command on root app)
# ===========================================================================


def test_brevity_routes_url_only() -> None:
    """`brevity <url>` -> `brevity <url>` (no --extended)."""
    result, recorded = _invoke(["brevity", "https://example.com/article"])
    assert result.exit_code == 0
    assert recorded == [["https://example.com/article"]]


def test_brevity_routes_extended_after_url() -> None:
    """Natural human-typed order: `brevity <url> --extended`."""
    result, recorded = _invoke(
        ["brevity", "https://example.com/article", "--extended"]
    )
    assert result.exit_code == 0
    assert recorded == [["https://example.com/article", "--extended"]]


def test_brevity_routes_extended_before_url() -> None:
    """Also works in reverse order: `brevity --extended <url>`."""
    result, recorded = _invoke(
        ["brevity", "--extended", "https://example.com/article"]
    )
    assert result.exit_code == 0
    assert recorded == [["https://example.com/article", "--extended"]]


def test_brevity_accepts_local_file_path() -> None:
    """The positional accepts a local file path just as well as a URL."""
    result, recorded = _invoke(["brevity", "./notes.pdf"])
    assert result.exit_code == 0
    assert recorded == [["./notes.pdf"]]


def test_brevity_accepts_local_file_with_extended() -> None:
    result, recorded = _invoke(["brevity", "./notes.pdf", "--extended"])
    assert result.exit_code == 0
    assert recorded == [["./notes.pdf", "--extended"]]


def test_brevity_missing_positional_shows_error() -> None:
    """`mineru brevity` (no URL) fails with Typer's missing-argument error."""
    # We deliberately do NOT patch the wrapper here -- Typer bails at
    # argument parsing time before the callback runs.
    result = runner.invoke(app, ["brevity"])
    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "missing" in combined or "url_or_file" in combined


def test_brevity_help_smoke() -> None:
    result = runner.invoke(app, ["brevity", "--help"])
    assert result.exit_code == 0
    assert "URL_OR_FILE" in result.stdout
    assert "--extended" in result.stdout


def test_brevity_short_help_flag() -> None:
    """The verb honors `-h` as well as `--help`."""
    result = runner.invoke(app, ["brevity", "-h"])
    assert result.exit_code == 0
    assert "URL_OR_FILE" in result.stdout


def test_brevity_root_json_swallowed_not_forwarded() -> None:
    """Root `--json` MUST NOT be forwarded (brevity has no such flag)."""
    result, recorded = _invoke(
        ["--json", "brevity", "https://example.com/article"]
    )
    assert result.exit_code == 0
    assert recorded == [["https://example.com/article"]]


def test_brevity_root_pretty_swallowed_not_forwarded() -> None:
    """Root `--pretty` MUST NOT be forwarded (brevity has no such flag)."""
    result, recorded = _invoke(
        ["--pretty", "brevity", "https://example.com/article"]
    )
    assert result.exit_code == 0
    assert recorded == [["https://example.com/article"]]


def test_brevity_propagates_engine_exit_code() -> None:
    """A non-zero exit from the wrapper propagates through the CLI."""
    result, _ = _invoke(
        ["brevity", "https://example.com/paywalled"], returncode=1
    )
    assert result.exit_code == 1


# ===========================================================================
# ROOT `mineru brevity --help` regression guard
# ===========================================================================


def test_root_help_lists_brevity_command() -> None:
    """`mineru --help` surfaces the bare `brevity` command.

    brevity is a bare command on the root app (not a group), so it
    shows up in the Commands section of `mineru --help` rather than
    as a noun sub-app. Verify it stays discoverable.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "brevity" in result.stdout, (
        f"root help missing bare `brevity` command. Output:\n{result.stdout}"
    )


# ===========================================================================
# STATIC INVARIANTS: verb / wrapper source hygiene
# ===========================================================================


VERB_SRC = Path(brevity_verb.__file__).read_text()
WRAPPER_SRC = Path(brevity_wrapper.__file__).read_text()


def test_verb_source_only_reaches_brevity_via_wrapper() -> None:
    """A regression that reached for subprocess directly (bypassing the wrapper) fails here."""
    for forbidden in ("import subprocess", "from subprocess"):
        assert forbidden not in VERB_SRC, (
            f"brevity verb must route via the wrapper, not direct subprocess; "
            f"found {forbidden!r}"
        )


def test_verb_source_calls_run_brevity() -> None:
    """The verb body must invoke `run_brevity` (the single wrapper entry)."""
    assert "run_brevity(" in VERB_SRC, (
        "brevity verb must invoke run_brevity(...) to reach the wrapper"
    )


def test_verb_and_wrapper_source_do_not_wrap_dead_artifact_tools() -> None:
    """Neither `artifact-detect` nor `artifact-remove` may appear anywhere.

    Spec §7 marks both scripts DEAD. The task explicitly says: do NOT
    create verbs for them.
    """
    for name in ("artifact-detect", "artifact-remove"):
        # No argv-list literal that would represent a subprocess argv.
        assert f'["{name}"' not in VERB_SRC, (
            f"verbs/brevity.py contains argv literal for dead tool {name!r}"
        )
        assert f'["{name}"' not in WRAPPER_SRC, (
            f"wrappers/brevity.py contains argv literal for dead tool {name!r}"
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
            f"wrappers/brevity.py must be facade-only; found {forbidden!r}"
        )
