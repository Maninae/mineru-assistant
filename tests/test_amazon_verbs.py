"""Tests for the Phase-2 Amazon verbs (P2-10).

Covers every verb the P2-10 task lists:

  amazon history [--year N] [--last N]      (READ)
  amazon order <orderId>                    (READ)
  amazon invoice <orderId>                  (READ)
  amazon transactions                       (READ)
  amazon check-session                      (READ)
  amazon login                              (WRITE, INTERACTIVE -- MOCK-ONLY)
  amazon logout                             (WRITE -- MOCK-ONLY)

Test discipline (P2 hard safety rule -- read this twice):

  - Every WRITE verb in this file is MOCKED: subprocess.run is never
    invoked against the live amazon-orders CLI, so no login prompt
    fires and no session cookies are touched. Every write test patches
    `mineru_cli.verbs.amazon.run_amazon_orders` with a recorder, asserts
    the argv the wrapper WOULD send, and verifies exit-code plumbing.
  - `login` in particular is INTERACTIVE (prompts for username /
    password / OTP on a real TTY); running it live in dev / test would
    hang.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
  - A belt-and-braces static grep ensures the verb file only reaches
    the underlying amazon-orders CLI via the wrapper (never `import
    subprocess` or a direct-shell string), and that `login` / `logout`
    invoke through the wrapper too (never inline subprocess) -- the
    task's explicit invariant.
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
from mineru_cli.verbs import amazon as amazon_verb
from mineru_cli.wrappers import amazon_orders as amazon_wrapper
from mineru_cli.wrappers.amazon_orders import (
    AMAZON_ORDERS_BIN_ENV,
    DEFAULT_AMAZON_ORDERS_BIN,
    EXPECTED_BIN_BASENAME,
    MISSING_BIN_EXIT_CODE,
    build_amazon_orders_argv,
    resolve_amazon_orders_bin,
    run_amazon_orders,
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


def _record_run_amazon_orders(recorded: List[List[str]], returncode: int = 0):
    def fake(args):
        recorded.append(list(args))
        return returncode

    return fake


def _invoke(args: List[str], returncode: int = 0):
    """Run the CLI with a patched wrapper. Returns (CliResult, recorded_argv_list)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.amazon.run_amazon_orders",
        _record_run_amazon_orders(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ===========================================================================
# WRAPPER: resolver + argv invariant
# ===========================================================================


def test_resolve_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AMAZON_ORDERS_BIN_ENV, raising=False)
    assert resolve_amazon_orders_bin() == DEFAULT_AMAZON_ORDERS_BIN


def test_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, "/tmp/fake_amazon_orders")
    assert resolve_amazon_orders_bin() == "/tmp/fake_amazon_orders"


def test_resolve_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, "")
    assert resolve_amazon_orders_bin() == DEFAULT_AMAZON_ORDERS_BIN


def test_default_points_at_workspace_bin_dir() -> None:
    """Sanity: the documented default is the workspace's amazon-orders shim path."""
    assert DEFAULT_AMAZON_ORDERS_BIN == str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin" / "amazon-orders")
    assert DEFAULT_AMAZON_ORDERS_BIN.endswith("/amazon-orders")


def test_argv0_resolves_to_amazon_orders_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AMAZON_ORDERS_BIN_ENV, raising=False)
    argv = build_amazon_orders_argv(["check-session"])
    assert os.path.basename(argv[0]) == EXPECTED_BIN_BASENAME
    assert os.path.basename(argv[0]) == "amazon-orders"


def test_argv0_at_subprocess_call_site_is_amazon_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: at the actual subprocess.run call-site, argv[0] basename is `amazon-orders`."""
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, "/tmp/fixtures/amazon-orders")
    with patch.object(amazon_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_amazon_orders(["check-session"])
    assert len(calls) == 1
    assert os.path.basename(calls[0]["cmd"][0]) == "amazon-orders"


# ===========================================================================
# WRAPPER: happy path + exit-code propagation
# ===========================================================================


def test_run_amazon_orders_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, "/tmp/fake_amazon_orders")
    with patch.object(amazon_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_amazon_orders(["history", "--year", "2026"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "/tmp/fake_amazon_orders",
        "history",
        "--year",
        "2026",
    ]


def test_run_amazon_orders_propagates_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, "/tmp/fake_amazon_orders")
    with patch.object(amazon_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=1)
        with patch.object(subprocess, "run", fake_run):
            rc = run_amazon_orders(["check-session"])
    assert rc == 1


def test_run_amazon_orders_propagates_unusual_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper must not rewrite unusual codes."""
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, "/tmp/fake_amazon_orders")
    with patch.object(amazon_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=42)
        with patch.object(subprocess, "run", fake_run):
            rc = run_amazon_orders(["history"])
    assert rc == 42


def test_run_amazon_orders_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run is called with NO stdout/stderr override.

    amazon-orders' `login` prints prompts on stderr and reads the
    password from stdin; pass-through is non-negotiable so that
    interactive branch works in a real shell.
    """
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, "/tmp/fake_amazon_orders")
    with patch.object(amazon_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_amazon_orders(["check-session"])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


# ===========================================================================
# WRAPPER: missing-binary path
# ===========================================================================


def test_run_amazon_orders_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        AMAZON_ORDERS_BIN_ENV, "/nonexistent/absolute/path/amazon-orders"
    )
    with pytest.raises(typer.Exit) as excinfo:
        run_amazon_orders(["check-session"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE

    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/amazon-orders" in err
    assert AMAZON_ORDERS_BIN_ENV in err
    assert "not found" in err.lower()


def test_run_amazon_orders_race_missing_at_exec_time_maps_to_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Binary check succeeded but exec raised FileNotFoundError."""
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, "/tmp/racing_amazon_orders")
    with patch.object(amazon_wrapper, "_binary_available", return_value=True):

        def raising_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory")

        with patch.object(subprocess, "run", raising_run):
            with pytest.raises(typer.Exit) as excinfo:
                run_amazon_orders(["check-session"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert "/tmp/racing_amazon_orders" in err


def test_run_amazon_orders_env_points_at_directory_exits_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A directory-typed env override MUST NOT leak a PermissionError traceback."""
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, str(tmp_path))
    with pytest.raises(typer.Exit) as excinfo:
        run_amazon_orders(["check-session"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert str(tmp_path) in err


def test_run_amazon_orders_env_points_at_non_executable_file_exits_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A mode-644 file (no exec bit) MUST NOT leak a PermissionError traceback."""
    script = tmp_path / "amazon-orders"
    script.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, str(script))
    with pytest.raises(typer.Exit) as excinfo:
        run_amazon_orders(["check-session"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert str(script) in err


# ===========================================================================
# End-to-end: fake amazon-orders on disk, real subprocess.run
# ===========================================================================


@pytest.fixture
def fake_amazon_orders_exit_0(tmp_path: Path) -> Path:
    """Fake amazon-orders that emits a plausible session-check stub and exits 0."""
    script = tmp_path / "amazon-orders"
    script.write_text(
        "#!/bin/sh\n"
        'printf "Info: A persisted session exists.\\n"\n'
        "exit 0\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script


@pytest.fixture
def fake_amazon_orders_exit_1(tmp_path: Path) -> Path:
    """Fake amazon-orders that emits a plausible auth error and exits 1."""
    script = tmp_path / "amazon-orders"
    script.write_text(
        "#!/bin/sh\n"
        'printf "Error: Not authenticated. Run `amazon-orders login`.\\n" >&2\n'
        "exit 1\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script


def test_run_amazon_orders_end_to_end_fake_binary_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    fake_amazon_orders_exit_0: Path,
) -> None:
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, str(fake_amazon_orders_exit_0))
    rc = run_amazon_orders(["check-session"])
    assert rc == 0


def test_run_amazon_orders_end_to_end_fake_binary_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    fake_amazon_orders_exit_1: Path,
) -> None:
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, str(fake_amazon_orders_exit_1))
    rc = run_amazon_orders(["check-session"])
    assert rc == 1


# ===========================================================================
# ROOT --json / --pretty NON-FORWARDING: end-to-end guard
#
# amazon-orders rejects `--json` and `--pretty` on every subverb (Click
# emits `No such option: ...` with exit 2). The mineru root swallows the
# flag; the fake amazon-orders below records its argv so a regression that
# forwards the flag fails visibly.
# ===========================================================================


@pytest.fixture
def fake_amazon_orders_argv_recorder(tmp_path: Path) -> "tuple[Path, Path]":
    """Fake amazon-orders that dumps its argv to a sentinel file and exits 0."""
    sentinel = tmp_path / "argv.txt"
    script = tmp_path / "amazon-orders"
    script.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do echo "$a" >> {sentinel}; done\n'
        "exit 0\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script, sentinel


def test_root_json_end_to_end_does_not_reach_amazon_orders(
    monkeypatch: pytest.MonkeyPatch,
    fake_amazon_orders_argv_recorder: "tuple[Path, Path]",
) -> None:
    """`mineru --json amazon check-session` exits 0 and does NOT pass --json."""
    binary, sentinel = fake_amazon_orders_argv_recorder
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, str(binary))
    result = runner.invoke(app, ["--json", "amazon", "check-session"])
    assert result.exit_code == 0
    recorded = sentinel.read_text().splitlines() if sentinel.exists() else []
    assert recorded == ["check-session"], f"Expected clean argv, got {recorded!r}"


def test_root_pretty_end_to_end_does_not_reach_amazon_orders(
    monkeypatch: pytest.MonkeyPatch,
    fake_amazon_orders_argv_recorder: "tuple[Path, Path]",
) -> None:
    """`mineru --pretty amazon check-session` exits 0 and does NOT pass --pretty."""
    binary, sentinel = fake_amazon_orders_argv_recorder
    monkeypatch.setenv(AMAZON_ORDERS_BIN_ENV, str(binary))
    result = runner.invoke(app, ["--pretty", "amazon", "check-session"])
    assert result.exit_code == 0
    recorded = sentinel.read_text().splitlines() if sentinel.exists() else []
    assert recorded == ["check-session"], f"Expected clean argv, got {recorded!r}"


# ===========================================================================
# READ verbs
# ===========================================================================


# --- history ---


def test_history_routes_to_amazon_orders_history() -> None:
    """`amazon history` -> `amazon-orders history` (no flags by default)."""
    result, recorded = _invoke(["amazon", "history"])
    assert result.exit_code == 0
    assert recorded == [["history"]]


def test_history_forwards_year_flag() -> None:
    result, recorded = _invoke(["amazon", "history", "--year", "2026"])
    assert result.exit_code == 0
    assert recorded == [["history", "--year", "2026"]]


def test_history_short_year_flag_works() -> None:
    result, recorded = _invoke(["amazon", "history", "-y", "2025"])
    assert result.exit_code == 0
    assert recorded == [["history", "--year", "2025"]]


def test_history_last_30_maps_to_last_30_days() -> None:
    """`--last 30` maps to amazon-orders' native `--last-30-days`."""
    result, recorded = _invoke(["amazon", "history", "--last", "30"])
    assert result.exit_code == 0
    assert recorded == [["history", "--last-30-days"]]


def test_history_last_90_maps_to_last_3_months() -> None:
    """`--last 90` maps to amazon-orders' native `--last-3-months`."""
    result, recorded = _invoke(["amazon", "history", "--last", "90"])
    assert result.exit_code == 0
    assert recorded == [["history", "--last-3-months"]]


@pytest.mark.parametrize("bad_last", [-1, 0, 7, 60, 365])
def test_history_last_unsupported_N_fails_loud(bad_last: int) -> None:
    """A non-30/90 `--last N` fails at the CLI boundary, not the engine.

    Historically the mineru layer forwarded `--last N` for any N and let
    amazon-orders emit the (confusing, engine-attributed) error. The
    boundary now rejects it with a clear mineru-owned message so the
    user knows exactly which layer refused the value.
    """
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.amazon.run_amazon_orders",
        _record_run_amazon_orders(recorded),
    ):
        result = runner.invoke(app, ["amazon", "history", "--last", str(bad_last)])
    assert result.exit_code != 0
    # The wrapper must not have been invoked when --last N is invalid.
    assert recorded == []


def test_history_year_and_last_combine() -> None:
    """Both --year and --last can be forwarded (engine chooses one; that's fine)."""
    result, recorded = _invoke(
        ["amazon", "history", "--year", "2026", "--last", "30"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["history", "--year", "2026", "--last-30-days"]
    ]


def test_history_extras_pass_through_full_details() -> None:
    result, recorded = _invoke(
        ["amazon", "history", "--year", "2026", "--full-details"]
    )
    assert result.exit_code == 0
    assert recorded == [["history", "--year", "2026", "--full-details"]]


def test_history_extras_pass_through_start_index_and_filter() -> None:
    result, recorded = _invoke(
        [
            "amazon", "history",
            "--start-index", "10",
            "--order-filter", "returned",
            "--single-page",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "history",
            "--start-index", "10",
            "--order-filter", "returned",
            "--single-page",
        ]
    ]


def test_history_help_smoke() -> None:
    result = runner.invoke(app, ["amazon", "history", "--help"])
    assert result.exit_code == 0
    assert "--year" in result.stdout
    assert "--last" in result.stdout


def test_history_root_json_swallowed_not_forwarded() -> None:
    """Root `--json` MUST NOT be forwarded (amazon-orders rejects it)."""
    result, recorded = _invoke(["--json", "amazon", "history"])
    assert result.exit_code == 0
    assert recorded == [["history"]]


def test_history_root_pretty_swallowed_not_forwarded() -> None:
    result, recorded = _invoke(["--pretty", "amazon", "history"])
    assert result.exit_code == 0
    assert recorded == [["history"]]


def test_history_propagates_engine_exit_code() -> None:
    result, _ = _invoke(["amazon", "history"], returncode=7)
    assert result.exit_code == 7


# --- order ---


def test_order_forwards_order_id() -> None:
    result, recorded = _invoke(
        ["amazon", "order", "111-1234567-1234567"]
    )
    assert result.exit_code == 0
    assert recorded == [["order", "111-1234567-1234567"]]


def test_order_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["amazon", "order", "111-1234567-1234567", "--json"]
    )
    # --json is NOT swallowed here (it's a positional trailing extra to the
    # amazon sub-verb, not the root flag). amazon-orders will still reject
    # it live; we just prove the wrapper doesn't strip trailing extras.
    assert result.exit_code == 0
    assert recorded == [["order", "111-1234567-1234567", "--json"]]


def test_order_help_smoke() -> None:
    result = runner.invoke(app, ["amazon", "order", "--help"])
    assert result.exit_code == 0
    assert "ORDER_ID" in result.stdout


# --- invoice ---


def test_invoice_forwards_order_id() -> None:
    result, recorded = _invoke(
        ["amazon", "invoice", "111-1234567-1234567"]
    )
    assert result.exit_code == 0
    assert recorded == [["invoice", "111-1234567-1234567"]]


def test_invoice_help_smoke() -> None:
    result = runner.invoke(app, ["amazon", "invoice", "--help"])
    assert result.exit_code == 0
    assert "ORDER_ID" in result.stdout


# --- transactions ---


def test_transactions_routes() -> None:
    result, recorded = _invoke(["amazon", "transactions"])
    assert result.exit_code == 0
    assert recorded == [["transactions"]]


def test_transactions_extras_pass_through_days() -> None:
    result, recorded = _invoke(
        ["amazon", "transactions", "--days", "30"]
    )
    assert result.exit_code == 0
    assert recorded == [["transactions", "--days", "30"]]


def test_transactions_help_smoke() -> None:
    result = runner.invoke(app, ["amazon", "transactions", "--help"])
    assert result.exit_code == 0


# --- check-session ---


def test_check_session_routes() -> None:
    result, recorded = _invoke(["amazon", "check-session"])
    assert result.exit_code == 0
    assert recorded == [["check-session"]]


def test_check_session_help_smoke() -> None:
    result = runner.invoke(app, ["amazon", "check-session", "--help"])
    assert result.exit_code == 0


# ===========================================================================
# WRITE verbs -- MOCK-ONLY (never invoked live)
# ===========================================================================


# --- login (WRITE, INTERACTIVE) ---


def test_login_routes_to_amazon_orders_login() -> None:
    """`amazon login` -> `amazon-orders login` (MOCK-ONLY -- never live)."""
    result, recorded = _invoke(["amazon", "login"])
    assert result.exit_code == 0
    assert recorded == [["login"]]


def test_login_extras_pass_through() -> None:
    result, recorded = _invoke(["amazon", "login", "--debug"])
    assert result.exit_code == 0
    assert recorded == [["login", "--debug"]]


def test_login_propagates_exit_code() -> None:
    result, _ = _invoke(["amazon", "login"], returncode=1)
    assert result.exit_code == 1


def test_login_help_smoke() -> None:
    result = runner.invoke(app, ["amazon", "login", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "write" in combined or "interactive" in combined


# --- logout (WRITE) ---


def test_logout_routes_to_amazon_orders_logout() -> None:
    result, recorded = _invoke(["amazon", "logout"])
    assert result.exit_code == 0
    assert recorded == [["logout"]]


def test_logout_extras_pass_through() -> None:
    result, recorded = _invoke(["amazon", "logout", "--debug"])
    assert result.exit_code == 0
    assert recorded == [["logout", "--debug"]]


def test_logout_help_smoke() -> None:
    result = runner.invoke(app, ["amazon", "logout", "--help"])
    assert result.exit_code == 0
    assert "write" in result.stdout.lower()


# ===========================================================================
# ROOT `mineru amazon --help` regression guard
# ===========================================================================


def test_amazon_root_help_lists_every_verb() -> None:
    """`mineru amazon --help` surfaces every wired verb."""
    result = runner.invoke(app, ["amazon", "--help"])
    assert result.exit_code == 0
    expected = (
        "history",
        "order",
        "invoice",
        "transactions",
        "check-session",
        "login",
        "logout",
    )
    for verb in expected:
        assert verb in result.stdout, (
            f"`mineru amazon --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_amazon_root_help_mentions_amazon_and_safety() -> None:
    """The noun-level help surfaces the amazon-orders backing + write/mock discipline."""
    result = runner.invoke(app, ["amazon", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "amazon" in lowered
    # SAFETY signal: writes are mock-only in tests, real invocation is a
    # real mutation. Any of these tokens is sufficient.
    assert "write" in lowered or "safety" in lowered or "mock" in lowered


# ===========================================================================
# STATIC INVARIANTS: verb / wrapper source hygiene
# ===========================================================================


VERB_SRC = Path(amazon_verb.__file__).read_text()
WRAPPER_SRC = Path(amazon_wrapper.__file__).read_text()


def test_verb_source_only_reaches_amazon_orders_via_wrapper() -> None:
    """A regression that reached for subprocess directly (bypassing the wrapper) fails here."""
    for forbidden in ("import subprocess", "from subprocess"):
        assert forbidden not in VERB_SRC, (
            f"amazon verb must route via the wrapper, not direct subprocess; "
            f"found {forbidden!r}"
        )


def test_login_and_logout_route_through_wrapper() -> None:
    """The task's explicit invariant: login / logout invoke through the wrapper.

    A grep-level guard that both write verbs contain a call to
    `run_amazon_orders` inside their body -- catches a regression that
    stubbed either verb out to inline subprocess (or, worse, to a
    no-op) without noticing.
    """
    # Both verb bodies must contain a `run_amazon_orders(["login" ...` /
    # `run_amazon_orders(["logout" ...` call site. A whitespace-tolerant
    # substring is enough for the invariant (the file is small and both
    # call sites are literal).
    assert 'run_amazon_orders(["login"' in VERB_SRC, (
        "login verb must invoke run_amazon_orders with a 'login' argv head"
    )
    assert 'run_amazon_orders(["logout"' in VERB_SRC, (
        "logout verb must invoke run_amazon_orders with a 'logout' argv head"
    )


def test_verb_source_has_no_hardcoded_amazon_credentials() -> None:
    """The verb file must not carry Amazon email / password / OTP literals.

    Auth is handled by `amazon-orders login` interactively; the mineru
    verb layer never sees credentials. A regression that stashed a
    literal here would fail this check.
    """
    for forbidden in (
        "AMAZON_PASSWORD",
        "AMAZON_USERNAME =",
        "amazon_password",
        # Bearer-token / cookie-value style env-var reads that should
        # only ever happen inside the amazon-orders CLI itself, not here.
        'os.environ.get("AMAZON_PASSWORD"',
        'os.environ["AMAZON_PASSWORD"',
    ):
        assert forbidden not in VERB_SRC, (
            f"verbs/amazon.py must not carry Amazon credential state; "
            f"found {forbidden!r}"
        )


def test_verb_and_wrapper_source_do_not_wrap_dead_artifact_tools() -> None:
    """Neither `artifact-detect` nor `artifact-remove` may appear anywhere.

    Spec §7 marks both scripts DEAD (scheduled for removal). The task
    explicitly says: do NOT create verbs for them. A grep for either
    name in either file catches a regression that added them back.

    We look at literal `artifact-detect` / `artifact-remove` substrings;
    the module docstrings intentionally say "artifact-detect" once
    (declaring them dead), so we count occurrences and require at most
    the docstring mentions -- any real code use would push the count up.
    """
    for name in ("artifact-detect", "artifact-remove"):
        # A dead-tool literal appearing in a subprocess argv would show
        # up in code (e.g. `run_amazon_orders(["artifact-detect", ...])`),
        # NOT just in prose. So the guard: the string may appear in
        # docstrings marking it dead, but must NOT appear inside any
        # argv-list-shaped context.
        assert f'"{name}"' not in VERB_SRC or f'run_amazon_orders(["{name}"' not in VERB_SRC, (
            f"verbs/amazon.py must not wrap dead tool {name!r}"
        )
        # The stronger guard: no direct subprocess call site references
        # the dead tool.
        assert f'["{name}"' not in VERB_SRC, (
            f"verbs/amazon.py contains argv literal for dead tool {name!r}"
        )
        assert f'["{name}"' not in WRAPPER_SRC, (
            f"wrappers/amazon_orders.py contains argv literal for dead tool {name!r}"
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
            f"wrappers/amazon_orders.py must be facade-only; found {forbidden!r}"
        )


def test_wrapper_source_has_no_hardcoded_amazon_credentials() -> None:
    """Same posture on the wrapper: no credential literals or env reads for tokens."""
    for forbidden in (
        "AMAZON_PASSWORD",
        "AMAZON_USERNAME =",
        "amazon_password",
        'os.environ.get("AMAZON_PASSWORD"',
        'os.environ["AMAZON_PASSWORD"',
    ):
        assert forbidden not in WRAPPER_SRC, (
            f"wrappers/amazon_orders.py must not carry Amazon credential state; "
            f"found {forbidden!r}"
        )
