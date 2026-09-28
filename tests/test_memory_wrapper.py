"""Tests for the msearch facade wrapper + memory verb wiring (F4).

Covers:
  - `resolve_msearch_bin` honors MINERU_MSEARCH_BIN, falls back to default,
    treats empty-string env as unset.
  - `run_msearch` builds the expected argv and propagates the engine exit
    code unchanged (0, non-zero, and unusual codes like 77).
  - Missing binary → exit 127 with an actionable stderr message that names
    the resolved path AND the override env var.
  - `subprocess.run` is called with NO stdout/stderr override (pass-through),
    proving the wrapper never captures / parses engine output.
  - Wrapper source contains no output-transform substrings (belt-and-braces
    static check that we're facade-only).
  - `mineru memory search / tags / query` route argv correctly through the
    wrapper, including root-level `--pretty` / `--json` propagation and
    trailing-extras pass-through.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import typer

from mineru_cli.app import app
from mineru_cli.wrappers import msearch as msearch_wrapper
from mineru_cli.wrappers.msearch import (
    DEFAULT_MSEARCH_BIN,
    MISSING_BIN_EXIT_CODE,
    MSEARCH_BIN_ENV,
    resolve_msearch_bin,
    run_msearch,
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


# ------------------------------------------------------------- resolver ----


def test_resolve_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MSEARCH_BIN_ENV, raising=False)
    assert resolve_msearch_bin() == DEFAULT_MSEARCH_BIN


def test_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MSEARCH_BIN_ENV, "/tmp/fake_msearch")
    assert resolve_msearch_bin() == "/tmp/fake_msearch"


def test_resolve_empty_env_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MSEARCH_BIN_ENV, "")
    assert resolve_msearch_bin() == DEFAULT_MSEARCH_BIN


# ------------------------------------------------------- run_msearch happy


def test_run_msearch_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MSEARCH_BIN_ENV, "/tmp/fake_msearch")
    # Make the "binary" look present.
    with patch.object(msearch_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_msearch(["keyword", "robin", "--pretty"])

    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "/tmp/fake_msearch",
        "keyword",
        "robin",
        "--pretty",
    ]


def test_run_msearch_propagates_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MSEARCH_BIN_ENV, "/tmp/fake_msearch")
    with patch.object(msearch_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=42)
        with patch.object(subprocess, "run", fake_run):
            rc = run_msearch(["keyword", "nonsense-term"])
    # Direct-return path: propagation is exact, no floor/ceiling munging.
    assert rc == 42


def test_run_msearch_propagates_unusual_exit_code_77(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The firewall convention uses 77/78; the wrapper must not rewrite them."""
    monkeypatch.setenv(MSEARCH_BIN_ENV, "/tmp/fake_msearch")
    with patch.object(msearch_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=77)
        with patch.object(subprocess, "run", fake_run):
            rc = run_msearch(["tags"])
    assert rc == 77


def test_run_msearch_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run is called with NO stdout/stderr override.

    That's how engine output flows straight to the CLI's fds. If someone
    ever adds `capture_output=True` or an explicit stdout=... this test
    catches it immediately.
    """
    monkeypatch.setenv(MSEARCH_BIN_ENV, "/tmp/fake_msearch")
    with patch.object(msearch_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_msearch(["tags"])

    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


# -------------------------------------------------- missing-binary path ----


def test_run_msearch_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(MSEARCH_BIN_ENV, "/nonexistent/absolute/path/msearch")

    with pytest.raises(typer.Exit) as excinfo:
        run_msearch(["keyword", "foo"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE

    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/msearch" in err
    assert MSEARCH_BIN_ENV in err
    # Must NOT falsely claim success — actionable, not silent.
    assert "not found" in err.lower()


def test_run_msearch_race_missing_at_exec_time_maps_to_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Binary check succeeded but exec raised FileNotFoundError.

    Same actionable stderr + exit 127 rather than an opaque traceback.
    """
    monkeypatch.setenv(MSEARCH_BIN_ENV, "/tmp/racing_msearch")
    with patch.object(msearch_wrapper, "_binary_available", return_value=True):

        def raising_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory")

        with patch.object(subprocess, "run", raising_run):
            with pytest.raises(typer.Exit) as excinfo:
                run_msearch(["tags"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert "/tmp/racing_msearch" in err


# ------------------------------------- static "facade only" guard ---------


WRAPPER_SRC = Path(msearch_wrapper.__file__).read_text()


def test_wrapper_source_does_no_output_parsing() -> None:
    """The wrapper file must not contain JSON parsing or output-mutation calls.

    Coarse grep: if anyone tries to `json.loads` msearch stdout, capture
    output for reshaping, or reformat the engine bytes, this fires.
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
            f"wrappers/msearch.py must be facade-only; found {forbidden!r}"
        )


# ------------------------------------------ verb wiring via CliRunner ----


runner = CliRunner()


def _stub_run_msearch(recorded: list, returncode: int = 0):
    """Patch mineru_cli.verbs.memory.run_msearch to record calls."""

    def fake(args):
        recorded.append(list(args))
        # We must raise typer.Exit ourselves because the real wrapper does.
        # But recording the argv is what the verb wiring tests really care
        # about; the CLI runner surfaces the exit code from typer.Exit
        # raised by the verb using our return value.
        return returncode

    return fake


# The seed `profiles/mineru/profile.yaml` is loaded by the root callback in
# these tests, so `--workspace <profile.workspace_absolute>` is injected
# immediately after the verb and BEFORE any user-supplied extras. Compute
# the expected fragment once and reuse it. This isolation-forwarding is a
# Phase-1 security invariant — see
# tests/test_profile_namespacing.py::test_memory_search_uses_per_profile_workspace.
from mineru_cli.profile import load_active_profile as _load_active_profile

# Synthetic seed profile shipped under tests/fixtures/ (the engine repo does
# NOT ship a live `profiles/` tree). `load_active_profile("mineru",
# base_dir=...)` reads the generic `mineru` seed here.
_SEED_PROFILE_BASE = Path(__file__).resolve().parent / "fixtures" / "seed_profile_base"
_SEED_WORKSPACE_ABS = str(
    _load_active_profile("mineru", base_dir=_SEED_PROFILE_BASE).workspace_absolute
)
_WS_ARGS = ["--workspace", _SEED_WORKSPACE_ABS]


def test_memory_search_forwards_term_and_extras() -> None:
    recorded: list = []
    with patch("mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded, 0)):
        result = runner.invoke(
            app,
            ["memory", "search", "robin", "--pretty", "--top", "3"],
        )
    assert result.exit_code == 0
    assert recorded == [["keyword", "robin", *_WS_ARGS, "--pretty", "--top", "3"]]


def test_memory_search_root_pretty_propagates() -> None:
    """Root-level `--pretty` (before the noun) must be folded into extras."""
    recorded: list = []
    with patch("mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded, 0)):
        result = runner.invoke(app, ["--pretty", "memory", "search", "river"])
    assert result.exit_code == 0
    assert recorded == [["keyword", "river", *_WS_ARGS, "--pretty"]]


def test_memory_search_root_json_propagates() -> None:
    recorded: list = []
    with patch("mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded, 0)):
        result = runner.invoke(app, ["--json", "memory", "search", "river"])
    assert result.exit_code == 0
    assert recorded == [["keyword", "river", *_WS_ARGS, "--json"]]


def test_memory_search_flag_not_duplicated_when_present_twice() -> None:
    """Root --pretty + trailing --pretty should still produce a single --pretty."""
    recorded: list = []
    with patch("mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded, 0)):
        result = runner.invoke(
            app,
            ["--pretty", "memory", "search", "river", "--pretty"],
        )
    assert result.exit_code == 0
    assert recorded == [["keyword", "river", *_WS_ARGS, "--pretty"]]


def test_memory_tags_forwards_extras() -> None:
    recorded: list = []
    with patch("mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded, 0)):
        result = runner.invoke(app, ["memory", "tags", "--count"])
    assert result.exit_code == 0
    assert recorded == [["tags", *_WS_ARGS, "--count"]]


def test_memory_tags_root_pretty_propagates() -> None:
    recorded: list = []
    with patch("mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded, 0)):
        result = runner.invoke(app, ["--pretty", "memory", "tags", "--count"])
    assert result.exit_code == 0
    assert recorded == [["tags", *_WS_ARGS, "--count", "--pretty"]]


def test_memory_query_forwards_question_and_extras() -> None:
    recorded: list = []
    with patch("mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded, 0)):
        result = runner.invoke(
            app,
            ["memory", "query", "that warranty claim we filed last spring", "--top", "5"],
        )
    assert result.exit_code == 0
    assert recorded == [
        ["query", "that warranty claim we filed last spring", *_WS_ARGS, "--top", "5"]
    ]


def test_memory_search_propagates_nonzero_exit_from_engine() -> None:
    """A non-zero rc from the wrapper must surface as the CLI exit code."""
    recorded: list = []
    with patch("mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded, 42)):
        result = runner.invoke(app, ["memory", "search", "nothingmatches"])
    assert result.exit_code == 42


def test_memory_search_missing_binary_via_env_returns_127(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: env-pointed-at-nowhere + real wrapper produces exit 127."""
    monkeypatch.setenv(MSEARCH_BIN_ENV, "/definitely/not/here/msearch")
    result = runner.invoke(app, ["memory", "search", "foo"])
    assert result.exit_code == MISSING_BIN_EXIT_CODE
    # Stderr carries the actionable message with the resolved path.
    assert "/definitely/not/here/msearch" in result.stderr
    assert MSEARCH_BIN_ENV in result.stderr
