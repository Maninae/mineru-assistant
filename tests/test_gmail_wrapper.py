"""Tests for the gog-firewall facade wrapper + gmail verb wiring (F5).

Covers:
  - `resolve_gog_firewall_bin` honors MINERU_GOG_FIREWALL_BIN, falls back
    to default, treats empty-string env as unset.
  - INVARIANT: `build_gog_firewall_argv`'s argv[0] resolves to a
    `gog-firewall` path (never bare `gog`). This is the code-review-level
    guard on firewall preservation.
  - `run_gog_firewall` builds the expected argv and propagates the
    firewall's exit codes 0 / 77 (all blocked) / 78 (firewall error)
    unchanged.
  - Missing binary → exit 127 with an actionable stderr message that
    names the resolved path AND the override env var.
  - `subprocess.run` is called with NO stdout/stderr override
    (pass-through), so the firewall's `redacted N of M units` stderr
    notices reach the user untouched.
  - Wrapper + verb source contain no `--raw`, no `--unsafe-strip-invisible`,
    no `/opt/homebrew/bin/gog` — the grep-verifiable half of the
    firewall-preservation invariant.
  - `mineru gmail search` routes argv correctly through the wrapper,
    including root-level `--pretty` / `--json` propagation and
    trailing-extras pass-through.
  - The verb docstring documents the firewall-preservation contract and
    `mineru gmail --help` surfaces the guarantee.
  - End-to-end: a fake gog-firewall on disk exiting 77 causes the CLI to
    exit 77 (firewall's "all blocked" convention preserved through the
    real subprocess call).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import typer

from mineru_cli.app import app
from mineru_cli.verbs import gmail as gmail_verb
from mineru_cli.wrappers import gog_firewall as gf_wrapper
from mineru_cli.wrappers.gog_firewall import (
    DEFAULT_GOG_FIREWALL_BIN,
    EXPECTED_BIN_BASENAME,
    FIREWALL_BASENAME_MISMATCH_EXIT_CODE,
    GOG_FIREWALL_BIN_ENV,
    MISSING_BIN_EXIT_CODE,
    build_gog_firewall_argv,
    resolve_gog_firewall_bin,
    run_gog_firewall,
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
    monkeypatch.delenv(GOG_FIREWALL_BIN_ENV, raising=False)
    assert resolve_gog_firewall_bin() == DEFAULT_GOG_FIREWALL_BIN


def test_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "/tmp/fake/gog-firewall")
    assert resolve_gog_firewall_bin() == "/tmp/fake/gog-firewall"


def test_resolve_empty_env_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "")
    assert resolve_gog_firewall_bin() == DEFAULT_GOG_FIREWALL_BIN


# ------------------------- FIREWALL-PRESERVATION INVARIANT ---------------
# The single most important test in F5: argv[0] must resolve to a
# gog-firewall path, NEVER bare `gog`. A regression that swapped the
# wrapper for raw gog would fail here.


def test_argv0_resolves_to_gog_firewall_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default binary path's basename is `gog-firewall`, not `gog`."""
    monkeypatch.delenv(GOG_FIREWALL_BIN_ENV, raising=False)
    argv = build_gog_firewall_argv(["gmail", "search", "newer_than:1d"])
    # argv[0] basename is exactly `gog-firewall`, not `gog` or `gog-anything-else`.
    assert os.path.basename(argv[0]) == EXPECTED_BIN_BASENAME
    assert os.path.basename(argv[0]) == "gog-firewall"
    # And explicitly not the raw gog binary.
    assert argv[0] != "/opt/homebrew/bin/gog"
    assert os.path.basename(argv[0]) != "gog"


def test_argv0_resolves_to_gog_firewall_when_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env override must still keep argv[0] name-shaped like the firewall.

    We enforce that operators point MINERU_GOG_FIREWALL_BIN at a
    `gog-firewall`-named binary. A misconfigured value pointing at raw
    `gog` would be caught here in tests before shipping.
    """
    monkeypatch.setenv(
        GOG_FIREWALL_BIN_ENV, "/tmp/mineru_test_fixtures/gog-firewall"
    )
    argv = build_gog_firewall_argv(["gmail", "search", "x"])
    assert os.path.basename(argv[0]) == "gog-firewall"


def test_argv0_never_bare_gog_across_run_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_gog_firewall`'s subprocess argv[0] is a gog-firewall path.

    Belt-and-braces: even at the actual subprocess.run call-site (not
    just the argv-building helper), argv[0] is `gog-firewall`.
    """
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "/tmp/fixtures/gog-firewall")
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_gog_firewall(["gmail", "search", "x"])
    assert len(calls) == 1
    assert os.path.basename(calls[0]["cmd"][0]) == "gog-firewall"
    # And never the raw gog binary.
    assert calls[0]["cmd"][0] != "/opt/homebrew/bin/gog"
    assert os.path.basename(calls[0]["cmd"][0]) != "gog"


# ---------------------------------------------- run_gog_firewall happy ----


def test_run_gog_firewall_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "/tmp/fake/gog-firewall")
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_gog_firewall(["gmail", "search", "newer_than:1d", "--json"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "/tmp/fake/gog-firewall",
        "gmail",
        "search",
        "newer_than:1d",
        "--json",
    ]


# ---------------------- firewall exit code contract (0 / 77 / 78) --------


def test_run_gog_firewall_propagates_exit_0_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "/tmp/fake/gog-firewall")
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_gog_firewall(["gmail", "search", "x"])
    assert rc == 0


def test_run_gog_firewall_propagates_exit_77_all_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firewall's 'all units blocked' code must reach the caller intact."""
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "/tmp/fake/gog-firewall")
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=77)
        with patch.object(subprocess, "run", fake_run):
            rc = run_gog_firewall(["gmail", "search", "x"])
    assert rc == 77


def test_run_gog_firewall_propagates_exit_78_firewall_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firewall's own error code must reach the caller intact."""
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "/tmp/fake/gog-firewall")
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=78)
        with patch.object(subprocess, "run", fake_run):
            rc = run_gog_firewall(["gmail", "search", "x"])
    assert rc == 78


def test_run_gog_firewall_propagates_arbitrary_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "/tmp/fake/gog-firewall")
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=42)
        with patch.object(subprocess, "run", fake_run):
            rc = run_gog_firewall(["gmail", "search", "x"])
    assert rc == 42


# --------------------- stdio pass-through (redaction notices) ------------


def test_run_gog_firewall_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run is called with NO stdout/stderr override.

    That's how the firewall's `redacted N of M units` stderr notices flow
    straight to the user without any filtering or re-formatting. If
    someone ever adds `capture_output=True` or an explicit stderr=PIPE
    this test catches it immediately.
    """
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "/tmp/fake/gog-firewall")
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_gog_firewall(["gmail", "search", "x"])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


# -------------------------------------------------- missing-binary path ----


def test_run_gog_firewall_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        GOG_FIREWALL_BIN_ENV, "/nonexistent/absolute/path/gog-firewall"
    )
    with pytest.raises(typer.Exit) as excinfo:
        run_gog_firewall(["gmail", "search", "x"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE

    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/gog-firewall" in err
    assert GOG_FIREWALL_BIN_ENV in err
    assert "not found" in err.lower()


def test_run_gog_firewall_race_missing_at_exec_time_maps_to_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Binary check succeeded but exec raised FileNotFoundError."""
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, "/tmp/racing/gog-firewall")
    with patch.object(gf_wrapper, "_binary_available", return_value=True):

        def raising_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory")

        with patch.object(subprocess, "run", raising_run):
            with pytest.raises(typer.Exit) as excinfo:
                run_gog_firewall(["gmail", "search", "x"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert "/tmp/racing/gog-firewall" in err


def test_run_gog_firewall_directory_at_resolved_path_returns_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A directory at the resolved path must NOT crash with an OSError traceback.

    Regression guard on `_binary_available` + the OSError catch: a
    stale symlink pointing at a directory, or a truly-configured
    directory path, would previously satisfy `os.path.exists` and then
    `subprocess.run` would raise PermissionError / IsADirectoryError
    (both OSError subclasses NOT caught by `except FileNotFoundError`),
    leaking a Python traceback. Now the wrapper reports 127 cleanly.
    """
    dir_path = tmp_path / "somewhere" / "gog-firewall"
    dir_path.mkdir(parents=True)  # a directory, not a file
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, str(dir_path))

    with pytest.raises(typer.Exit) as excinfo:
        run_gog_firewall(["gmail", "search", "x"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE

    err = capsys.readouterr().err
    assert str(dir_path) in err
    assert GOG_FIREWALL_BIN_ENV in err


def test_run_gog_firewall_non_executable_file_at_resolved_path_returns_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A non-executable file must NOT crash with a PermissionError traceback."""
    non_exec = tmp_path / "gog-firewall"
    non_exec.write_text("#!/bin/sh\nexit 0\n")  # no chmod +x
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, str(non_exec))

    with pytest.raises(typer.Exit) as excinfo:
        run_gog_firewall(["gmail", "search", "x"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE

    err = capsys.readouterr().err
    assert str(non_exec) in err


# -------- BASENAME-PRESERVATION runtime guard (finding #1 regression) --------


@pytest.mark.parametrize(
    "bad_binary",
    [
        "/opt/homebrew/bin/gog",
        "gog",
        "/tmp/some/path/gog",
        "/tmp/other/gog-shim",
    ],
)
def test_run_gog_firewall_refuses_bare_gog_env_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    bad_binary: str,
) -> None:
    """Setting MINERU_GOG_FIREWALL_BIN=<not-gog-firewall> must NOT shell out.

    Direct regression guard for the runtime firewall-preservation check.
    A misconfigured env override that names bare `gog` or any other
    non-`gog-firewall` shim is refused with a firewall-error exit code
    (78) and an actionable stderr, BEFORE subprocess.run is called.
    """
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, bad_binary)

    # subprocess.run must NEVER be invoked in this path — a raised
    # AssertionError inside would surface if it were.
    def must_not_run(*a, **kw):
        raise AssertionError(
            "subprocess.run was called despite basename guard rejecting env"
        )

    with patch.object(subprocess, "run", must_not_run):
        with pytest.raises(typer.Exit) as excinfo:
            run_gog_firewall(["gmail", "search", "x"])

    assert excinfo.value.exit_code == FIREWALL_BASENAME_MISMATCH_EXIT_CODE
    err = capsys.readouterr().err
    assert GOG_FIREWALL_BIN_ENV in err
    assert "gog-firewall" in err
    # Message must name the actual misconfigured path so the operator can fix it.
    assert bad_binary in err


def test_run_gog_firewall_accepts_gog_firewall_named_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path whose basename IS `gog-firewall` clears the runtime guard.

    Positive counterpart to the parametrized rejection tests: the guard
    must not be overzealous; a correctly-named override still runs.
    """
    monkeypatch.setenv(
        GOG_FIREWALL_BIN_ENV, "/tmp/mineru_fixtures/gog-firewall"
    )
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_gog_firewall(["gmail", "search", "x"])
    assert rc == 0
    assert len(calls) == 1
    assert os.path.basename(calls[0]["cmd"][0]) == "gog-firewall"


def test_missing_binary_error_is_noun_agnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The wrapper backs 10 Workspace nouns; the error must not hardcode 'gmail'.

    Regression guard: previously the missing-binary stderr said
    `mineru gmail: gog-firewall engine not found ...` which misled every
    non-gmail operator (calendar, drive, docs, sheets, contacts, tasks,
    people, groups) into thinking they'd hit a gmail-specific error.
    """
    monkeypatch.setenv(
        GOG_FIREWALL_BIN_ENV, "/nonexistent/absolute/path/gog-firewall"
    )
    # Simulate a calendar caller; the wrapper doesn't and shouldn't care.
    with pytest.raises(typer.Exit):
        run_gog_firewall(["calendar", "list"])
    err = capsys.readouterr().err
    assert "mineru: gog-firewall" in err
    assert "mineru gmail:" not in err, (
        f"error still gmail-prefixed for a non-gmail caller: {err!r}"
    )


# ------------------------------------- static "no bypass flags" guard -----


WRAPPER_SRC = Path(gf_wrapper.__file__).read_text()
VERB_SRC = Path(gmail_verb.__file__).read_text()


def test_wrapper_source_has_no_bypass_flags() -> None:
    """The wrapper file must not inject firewall-bypassing flags anywhere.

    `--raw` and `--unsafe-strip-invisible` are out of scope for the
    foundation, and `/opt/homebrew/bin/gog` (the raw engine) must never
    be referenced as a code path — only mentioned in docstrings for
    context.
    """
    # These flags must not appear as string literals anywhere in the
    # wrapper (the source's docstring uses them as prose, so we look for
    # the flag-shape substring — but any real subprocess arg would be
    # written as a quoted literal like "--raw".)
    assert '"--raw"' not in WRAPPER_SRC
    assert "'--raw'" not in WRAPPER_SRC
    assert '"--unsafe-strip-invisible"' not in WRAPPER_SRC
    assert "'--unsafe-strip-invisible'" not in WRAPPER_SRC
    # No live code that invokes /opt/homebrew/bin/gog. It's OK to mention
    # the path in a docstring for context; the enforcement is that it
    # never appears as a subprocess argv[0].
    assert '"/opt/homebrew/bin/gog"' not in WRAPPER_SRC
    assert "'/opt/homebrew/bin/gog'" not in WRAPPER_SRC


def test_verb_source_has_no_bypass_flags() -> None:
    """The gmail verb file must not inject firewall-bypassing flags."""
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_wrapper_source_does_no_output_parsing() -> None:
    """The wrapper file must not parse or reshape engine output.

    Coarse grep: if anyone tries to `json.loads` firewall stdout, capture
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
            f"wrappers/gog_firewall.py must be facade-only; found {forbidden!r}"
        )


def test_codebase_read_verbs_never_import_raw_gog() -> None:
    """Grep the whole `mineru_cli/` tree for direct references to raw gog.

    The foundation invariant: no read verb calls `/opt/homebrew/bin/gog`
    or invokes a `gog` binary directly. gog-firewall is the only
    subprocess argv[0] for Gmail reads.

    Explicit allowlist (Phase 1.5): `profile/onboarding.py` legitimately
    shells out to `gog auth add <email>` during the one-time Google
    walkthrough. That is an OAuth bootstrap flow, NOT a data-read path
    (no email content, no calendar events), and gog owns the token
    store — the firewall has nothing to enforce on it.

    Extra invariant on the allowlisted file: the raw-gog callsite must
    only reach the `auth` verb (never `gmail`, `calendar`, `drive`, etc.
    — any of those would bypass the firewall on real content). Enforced
    by requiring every argv built from `GOG_BIN` in that file to have
    `"auth"` as its immediately-following element.
    """
    ALLOWED_RAW_GOG_CALLERS = frozenset(
        {"mineru_cli/profile/onboarding.py"}
    )
    package_root = Path(gmail_verb.__file__).resolve().parent.parent
    offenders = []
    for py_file in package_root.rglob("*.py"):
        text = py_file.read_text()
        # Match string literals; docstrings/prose can mention the path safely.
        if '"/opt/homebrew/bin/gog"' in text or "'/opt/homebrew/bin/gog'" in text:
            rel = py_file.resolve().relative_to(package_root.parent).as_posix()
            if rel in ALLOWED_RAW_GOG_CALLERS:
                continue
            offenders.append(str(py_file))
    assert offenders == [], (
        f"Found raw-gog references in read-verb code: {offenders}"
    )

    # Allowlisted-file check: every argv built with GOG_BIN in
    # onboarding.py must place `"auth"` as the next element. A regex
    # over the source keeps the check simple (no import + reflection):
    # find each `[GOG_BIN, ...]` list literal and read the second slot.
    onboarding_src = (
        package_root / "profile" / "onboarding.py"
    ).read_text()
    argv_pattern = re.compile(
        r"\[\s*GOG_BIN\s*,\s*(?P<next_arg>[\"'][^\"']*[\"'])"
    )
    matches = argv_pattern.findall(onboarding_src)
    assert matches, (
        "expected at least one `[GOG_BIN, ...]` argv in "
        "profile/onboarding.py; if the shape changed, re-derive the "
        "check to match the new pattern."
    )
    non_auth = [m for m in matches if m.strip("\"'") != "auth"]
    assert non_auth == [], (
        "raw-gog callsite in profile/onboarding.py may only reach the "
        f"`auth` verb; found argv[1] in {non_auth}."
    )


# ------------------------------------------ verb wiring via CliRunner ----


runner = CliRunner()


def _stub_run_gog_firewall(recorded: list, returncode: int = 0):
    """Patch mineru_cli.verbs.gmail.run_gog_firewall to record calls."""

    def fake(args, **kwargs):
        recorded.append(list(args))
        return returncode

    return fake


def test_gmail_search_forwards_query_and_extras() -> None:
    recorded: list = []
    with patch(
        "mineru_cli.verbs.gmail.run_gog_firewall",
        _stub_run_gog_firewall(recorded, 0),
    ):
        result = runner.invoke(
            app,
            ["gmail", "search", "newer_than:1d", "--max", "5", "--json"],
        )
    assert result.exit_code == 0
    assert recorded == [
        ["gmail", "search", "newer_than:1d", "--max", "5", "--json"]
    ]


def test_gmail_search_root_pretty_propagates() -> None:
    """Root-level `--pretty` (before the noun) must be folded into extras."""
    recorded: list = []
    with patch(
        "mineru_cli.verbs.gmail.run_gog_firewall",
        _stub_run_gog_firewall(recorded, 0),
    ):
        result = runner.invoke(
            app,
            ["--pretty", "gmail", "search", "newer_than:1d"],
        )
    assert result.exit_code == 0
    assert recorded == [["gmail", "search", "newer_than:1d", "--pretty"]]


def test_gmail_search_root_json_propagates() -> None:
    recorded: list = []
    with patch(
        "mineru_cli.verbs.gmail.run_gog_firewall",
        _stub_run_gog_firewall(recorded, 0),
    ):
        result = runner.invoke(
            app,
            ["--json", "gmail", "search", "newer_than:1d"],
        )
    assert result.exit_code == 0
    assert recorded == [["gmail", "search", "newer_than:1d", "--json"]]


def test_gmail_search_flag_not_duplicated_when_present_twice() -> None:
    """Root --json + trailing --json should still produce a single --json."""
    recorded: list = []
    with patch(
        "mineru_cli.verbs.gmail.run_gog_firewall",
        _stub_run_gog_firewall(recorded, 0),
    ):
        result = runner.invoke(
            app,
            ["--json", "gmail", "search", "newer_than:1d", "--json"],
        )
    assert result.exit_code == 0
    assert recorded == [["gmail", "search", "newer_than:1d", "--json"]]


def test_gmail_search_propagates_exit_77_all_blocked() -> None:
    """Firewall's 'all blocked' code surfaces as CLI exit 77."""
    recorded: list = []
    with patch(
        "mineru_cli.verbs.gmail.run_gog_firewall",
        _stub_run_gog_firewall(recorded, 77),
    ):
        result = runner.invoke(app, ["gmail", "search", "x"])
    assert result.exit_code == 77


def test_gmail_search_propagates_exit_78_firewall_error() -> None:
    """Firewall's own error code surfaces as CLI exit 78."""
    recorded: list = []
    with patch(
        "mineru_cli.verbs.gmail.run_gog_firewall",
        _stub_run_gog_firewall(recorded, 78),
    ):
        result = runner.invoke(app, ["gmail", "search", "x"])
    assert result.exit_code == 78


def test_gmail_search_missing_binary_via_env_returns_127(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: env-pointed-at-nowhere + real wrapper produces exit 127."""
    monkeypatch.setenv(
        GOG_FIREWALL_BIN_ENV, "/definitely/not/here/gog-firewall"
    )
    result = runner.invoke(app, ["gmail", "search", "x"])
    assert result.exit_code == MISSING_BIN_EXIT_CODE
    assert "/definitely/not/here/gog-firewall" in result.stderr
    assert GOG_FIREWALL_BIN_ENV in result.stderr


# ------------------------- help surface documents the invariant ----------


def test_gmail_help_mentions_firewall_preservation() -> None:
    """`mineru gmail --help` should surface the firewall guarantee."""
    result = runner.invoke(app, ["gmail", "--help"])
    assert result.exit_code == 0
    # The verb-level help mentions gog-firewall by name and preservation intent.
    combined = result.stdout.lower()
    assert "gog-firewall" in combined or "firewall" in combined


def test_gmail_search_help_mentions_firewall_preservation() -> None:
    """`mineru gmail search --help` surfaces the contract to end users."""
    result = runner.invoke(app, ["gmail", "search", "--help"])
    assert result.exit_code == 0
    assert "firewall" in result.stdout.lower()


# ---------------- true end-to-end: fake gog-firewall on disk, exit 77 -----


@pytest.fixture
def fake_gog_firewall_exit_77(tmp_path: Path) -> Path:
    """Write a tiny executable script that exits 77 (all-blocked convention).

    Real subprocess call — no mocks. Verifies the firewall's 77 exit code
    propagates through the actual `subprocess.run` code path, not just
    through the recorder shim.
    """
    script = tmp_path / "gog-firewall"
    script.write_text(
        "#!/bin/sh\n"
        "# Fake firewall: emit a plausible 'redacted' notice to stderr, exit 77.\n"
        'printf "redacted 3 of 3 units (fake)\\n" >&2\n'
        "exit 77\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_gmail_search_end_to_end_fake_binary_propagates_77(
    monkeypatch: pytest.MonkeyPatch,
    fake_gog_firewall_exit_77: Path,
) -> None:
    """The full CLI → wrapper → subprocess → fake binary chain propagates 77.

    This is the strongest confirmation we can give without hitting the
    real firewall: a real fs script named `gog-firewall` returns 77,
    and the CLI's exit code is 77.
    """
    monkeypatch.setenv(GOG_FIREWALL_BIN_ENV, str(fake_gog_firewall_exit_77))
    # CliRunner captures output by default; we just need the exit code.
    result = runner.invoke(app, ["gmail", "search", "x"])
    assert result.exit_code == 77


@pytest.fixture
def fake_gog_firewall_exit_0(tmp_path: Path) -> Path:
    """Fake firewall that emits a redaction notice on stderr and exits 0."""
    script = tmp_path / "gog-firewall"
    script.write_text(
        "#!/bin/sh\n"
        'printf "{\\"threads\\": []}\\n"\n'
        'printf "redacted 1 of 4 units (fake)\\n" >&2\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_gmail_search_end_to_end_stderr_redaction_notice_flows_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_gog_firewall_exit_0: Path,
) -> None:
    """`mineru gmail search`'s STDERR carries the firewall's redaction notice.

    Uses a real subprocess spawn of the installed `mineru` binary so the
    child truly writes to its own stderr fd — the same channel the user
    sees in a terminal. A CliRunner-only check would not prove this,
    because Typer's runner replaces sys.stdout/sys.stderr but the
    subprocess writes to the real fd; a regression that filtered stderr
    inside the wrapper (e.g. `stderr=PIPE` and discard) would still
    make the CliRunner-only test pass while breaking real users.
    """
    venv_mineru = REPO_ROOT / ".venv" / "bin" / "mineru"
    if not venv_mineru.exists():
        pytest.skip(f"venv mineru missing at {venv_mineru}")

    env = os.environ.copy()
    # Drop any user-set MINERU_* overrides that could bleed through.
    for key in list(env):
        if key.startswith("MINERU_"):
            env.pop(key, None)
    # Re-point the child at the shipped synthetic seed profile: the engine
    # repo ships no `current` active-profile symlink, so a bare `mineru`
    # subprocess would otherwise fail profile resolution before ever
    # reaching the firewall wrapper. Mirrors the conftest autouse fixture.
    seed_base = REPO_ROOT / "tests" / "fixtures" / "seed_profile_base"
    env["MINERU_PROFILE"] = "mineru"
    env["MINERU_PROFILE_ROOT"] = str(seed_base)
    env[GOG_FIREWALL_BIN_ENV] = str(fake_gog_firewall_exit_0)

    result = subprocess.run(
        [str(venv_mineru), "gmail", "search", "x"],
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
    # The fake firewall's stdout also flows through — verifies we did NOT
    # replace the real subprocess with a mock in the CLI layer.
    assert "threads" in result.stdout, (
        f"stdout regression: expected JSON payload; got {result.stdout!r}"
    )
    # THIS is the load-bearing assertion: the firewall's redaction notice
    # reached the real CLI stderr, not a captured / filtered surface.
    assert "redacted 1 of 4 units" in result.stderr, (
        f"redaction notice missing from CLI stderr: {result.stderr!r}"
    )


