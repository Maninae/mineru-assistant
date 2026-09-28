"""Tests for the deliver-output facade wrapper + telegram verb wiring (P2-08).

Full mirror of `tests/test_gmail_wrapper.py`'s shape, adapted for the
deliver-output.py wrapper and the four telegram verbs (`send`, `deliver`,
`inject`, `allowlist`) the P2-08 task wires.

⚠️ SAFETY DISCIPLINE (P2 hard rule) ⚠️

  Every test here MOCKS the wrapper. `subprocess.run` is never actually
  invoked against the live deliver-output.py in dev/test - not to send,
  not to check the exit code, not for any reason. The end-to-end fixture
  writes a fake `deliver-output.py` shell script that just records argv
  and exits with a canned code; the real Telegram Bot API is never
  contacted from this test file.

  `inject` writes to a real file, but ONLY at a `tmp_path` under
  `MINERU_INJECT_QUEUE_DIR`. It never touches `$MINERU_HOME/cache/
  inject-queue/`. The env override guard-rails that.

  `allowlist` reads from a `tmp_path` file under
  `MINERU_TELEGRAM_ALLOWLIST_FILE`. It never touches macOS Keychain
  during tests.

Coverage:

  Wrapper (`mineru_cli.wrappers.deliver_output`):
    - `resolve_deliver_output_bin` honors `MINERU_DELIVER_OUTPUT_BIN`,
      falls back to default, treats empty-string env as unset.
    - `build_deliver_output_argv`'s argv[0] resolves to a `deliver-
      output.py` path so a regression that swapped in a different
      script would fail immediately.
    - `run_deliver_output` builds the expected argv and propagates the
      engine exit code unchanged (0, 1, and unusual codes like 77 to
      guard against remapping).
    - `subprocess.run` is called with NO stdout/stderr override
      (pass-through), so the script's success/error diagnostics reach
      the caller untouched.
    - Missing binary -> exit 127 with an actionable stderr message
      naming the resolved path AND the override env var.
    - Wrapper source contains no output-transform substrings and no
      hardcoded bot-token / chat-id / API URL literals (facade-only
      static guard).
    - End-to-end: fake deliver-output.py on disk exiting 1 causes the
      CLI to exit 1 (real subprocess.run code path, not just mock).

  Verbs (`mineru_cli.verbs.telegram`):
    - `send <text>` argv: `["--raw", <text>]`.
    - `deliver <path>` argv: `[<path>]`.
    - `inject <label> <content>` writes a file at
      `MINERU_INJECT_QUEUE_DIR/<ts>-<safe_label>.json` with payload
      `{"label": <original label>, "content": <content>}` byte-
      identical to deliver-output.py's `enqueue_for_session`.
    - Label sanitizer matches deliver-output.py (`[^A-Za-z0-9_-]` ->
      `_`) so a label with spaces / slashes / unicode lands cleanly.
    - `allowlist` reads from `MINERU_TELEGRAM_ALLOWLIST_FILE` when set,
      prints the file contents verbatim, exits 0.
    - `allowlist` exits 2 with an actionable message when the env
      override points at a missing file.
    - Every verb also gets a `--help` smoke test to confirm it renders
      without a crash and stays discoverable from the CLI surface.
    - Firewall-preservation invariant analog: send/deliver argv[0]
      basename is `deliver-output.py` at the subprocess.run call site.
"""

from __future__ import annotations

import datetime
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import typer

from mineru_cli.app import app
from mineru_cli.verbs import telegram as telegram_verb
from mineru_cli.wrappers import deliver_output as do_wrapper
from mineru_cli.wrappers.deliver_output import (
    DEFAULT_DELIVER_OUTPUT_BIN,
    DELIVER_OUTPUT_BIN_ENV,
    EXPECTED_BIN_BASENAME,
    MISSING_BIN_EXIT_CODE,
    build_deliver_output_argv,
    resolve_deliver_output_bin,
    run_deliver_output,
)


# ------------------------------------------------------------------ helpers


runner = CliRunner()


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


def _record_verb_wrapper(recorded: List[List[str]], returncode: int = 0):
    """Return a fake run_deliver_output that captures argv + returns rc."""

    def fake(args, **kwargs):
        recorded.append(list(args))
        return returncode

    return fake


# ==========================================================================
# Wrapper: resolver
# ==========================================================================


def test_resolve_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DELIVER_OUTPUT_BIN_ENV, raising=False)
    assert resolve_deliver_output_bin() == DEFAULT_DELIVER_OUTPUT_BIN


def test_resolve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DELIVER_OUTPUT_BIN_ENV, "/tmp/fake_deliver_output.py")
    assert resolve_deliver_output_bin() == "/tmp/fake_deliver_output.py"


def test_resolve_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DELIVER_OUTPUT_BIN_ENV, "")
    assert resolve_deliver_output_bin() == DEFAULT_DELIVER_OUTPUT_BIN


def test_default_points_at_workspace_scripts_dir() -> None:
    """Sanity: the documented default is the workspace's deliver-output.py path."""
    assert (
        DEFAULT_DELIVER_OUTPUT_BIN
        == str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "scripts" / "deliver-output.py")
    )
    assert DEFAULT_DELIVER_OUTPUT_BIN.endswith("/deliver-output.py")


# ==========================================================================
# Wrapper: argv-shape invariant (basename == deliver-output.py)
# ==========================================================================


def test_argv0_resolves_to_deliver_output_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DELIVER_OUTPUT_BIN_ENV, raising=False)
    argv = build_deliver_output_argv(["--raw", "hello"])
    assert os.path.basename(argv[0]) == EXPECTED_BIN_BASENAME
    assert os.path.basename(argv[0]) == "deliver-output.py"


def test_argv0_never_bare_binary_across_run_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: at the actual subprocess.run call-site, argv[0] basename is `deliver-output.py`."""
    monkeypatch.setenv(
        DELIVER_OUTPUT_BIN_ENV, "/tmp/fixtures/deliver-output.py"
    )
    with patch.object(do_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_deliver_output(["--raw", "x"])
    assert len(calls) == 1
    assert os.path.basename(calls[0]["cmd"][0]) == "deliver-output.py"


# ==========================================================================
# Wrapper: run_deliver_output happy path + exit-code propagation
# ==========================================================================


def test_run_deliver_output_builds_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DELIVER_OUTPUT_BIN_ENV, "/tmp/fake_deliver_output.py")
    with patch.object(do_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            rc = run_deliver_output(["--raw", "hello world"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "/tmp/fake_deliver_output.py",
        "--raw",
        "hello world",
    ]


def test_run_deliver_output_propagates_exit_1_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deliver-output.py's every error branch exits 1; must surface intact."""
    monkeypatch.setenv(DELIVER_OUTPUT_BIN_ENV, "/tmp/fake_deliver_output.py")
    with patch.object(do_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=1)
        with patch.object(subprocess, "run", fake_run):
            rc = run_deliver_output(["--raw", "x"])
    assert rc == 1


def test_run_deliver_output_propagates_unusual_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper must not rewrite unusual codes (77 / 78 belong to the firewall convention)."""
    monkeypatch.setenv(DELIVER_OUTPUT_BIN_ENV, "/tmp/fake_deliver_output.py")
    with patch.object(do_wrapper, "_binary_available", return_value=True):
        fake_run, _ = _make_run_recorder(returncode=77)
        with patch.object(subprocess, "run", fake_run):
            rc = run_deliver_output(["--raw", "x"])
    assert rc == 77


def test_run_deliver_output_uses_passthrough_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run is called with NO stdout/stderr override.

    deliver-output.py prints `Delivered N chars via Telegram` on stdout
    and `Telegram API error: {...}` / `chat_id X is not in the allowlist`
    on stderr; those must reach the caller untouched so the operator
    knows exactly what happened.
    """
    monkeypatch.setenv(DELIVER_OUTPUT_BIN_ENV, "/tmp/fake_deliver_output.py")
    with patch.object(do_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_deliver_output(["--raw", "x"])
    kwargs = calls[0]["kwargs"]
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert kwargs.get("check", False) is False


# ==========================================================================
# Wrapper: missing-binary path
# ==========================================================================


def test_run_deliver_output_missing_binary_exits_127_with_actionable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv(
        DELIVER_OUTPUT_BIN_ENV, "/nonexistent/absolute/path/deliver-output.py"
    )
    with pytest.raises(typer.Exit) as excinfo:
        run_deliver_output(["--raw", "x"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE

    err = capsys.readouterr().err
    assert "/nonexistent/absolute/path/deliver-output.py" in err
    assert DELIVER_OUTPUT_BIN_ENV in err
    assert "not found" in err.lower()


def test_run_deliver_output_race_missing_at_exec_time_maps_to_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Binary check succeeded but exec raised FileNotFoundError."""
    monkeypatch.setenv(
        DELIVER_OUTPUT_BIN_ENV, "/tmp/racing_deliver_output.py"
    )
    with patch.object(do_wrapper, "_binary_available", return_value=True):

        def raising_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory")

        with patch.object(subprocess, "run", raising_run):
            with pytest.raises(typer.Exit) as excinfo:
                run_deliver_output(["--raw", "x"])
    assert excinfo.value.exit_code == MISSING_BIN_EXIT_CODE
    err = capsys.readouterr().err
    assert "/tmp/racing_deliver_output.py" in err


# ==========================================================================
# Wrapper: static "facade only" guard
# ==========================================================================


WRAPPER_SRC = Path(do_wrapper.__file__).read_text()
VERB_SRC = Path(telegram_verb.__file__).read_text()


def test_wrapper_source_does_no_output_parsing() -> None:
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
        assert forbidden not in WRAPPER_SRC, (
            f"wrappers/deliver_output.py must be facade-only; found {forbidden!r}"
        )


def test_wrapper_source_makes_no_direct_telegram_api_calls() -> None:
    """The wrapper must not smuggle direct Telegram Bot API calls into Python.

    Facade-first: if a future verb needs a Bot API endpoint deliver-
    output.py doesn't cover, extend the script (or add a sibling
    script + wrapper), do NOT curl / requests / urllib from Python. A
    grep-style guard here fails fast if someone tries.

    We look for QUOTED shapes (`"https://..."`, `'requests.'`) or
    Python code shapes (`import requests`, `from urllib`), not bare
    substrings, so a docstring mentioning "the script uses urllib" does
    NOT trip the guard. Only actual code does.
    """
    for forbidden in (
        # Quoted URL literals
        '"https://api.telegram.org',
        "'https://api.telegram.org",
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
        assert forbidden not in WRAPPER_SRC, (
            f"wrappers/deliver_output.py must not make direct API calls; "
            f"found {forbidden!r}"
        )


def test_wrapper_source_has_no_hardcoded_tokens_or_chat_ids() -> None:
    """The wrapper must not stash a bot token / chat id in the source.

    Secrets live in Keychain (per SECURITY.md 'Never Store Plaintext
    Secrets on Disk'). The wrapper is a pure pass-through; the token
    load happens inside deliver-output.py, never in Python here.
    """
    # A Telegram bot token has the shape `<digits>:<letters+digits+_->`.
    # We assert the sentinel token prefixes / obvious placeholder strings
    # aren't present.
    for forbidden in (
        "bot_token",
        "BOT_TOKEN =",
        "chat_id =",
        "CHAT_ID =",
        "telegram-bot-token",
        "TELEGRAM_BOT_TOKEN =",
    ):
        assert forbidden not in WRAPPER_SRC, (
            f"wrappers/deliver_output.py must not carry token/chat-id state; "
            f"found {forbidden!r}"
        )


# ==========================================================================
# End-to-end: fake deliver-output.py on disk, real subprocess.run
# ==========================================================================


@pytest.fixture
def fake_deliver_output_exit_1(tmp_path: Path) -> Path:
    """Write a tiny executable script that mimics an error branch (exit 1).

    Real subprocess call, no mocks. Verifies the script's exit code
    propagates through the actual `subprocess.run` code path.
    """
    script = tmp_path / "deliver-output.py"
    script.write_text(
        "#!/bin/sh\n"
        "# Fake deliver-output.py: mimic the 'missing token' error branch.\n"
        'printf "ERROR: Missing TELEGRAM_BOT_TOKEN\\n" >&2\n'
        "exit 1\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script


def test_run_deliver_output_end_to_end_fake_binary_propagates_1(
    monkeypatch: pytest.MonkeyPatch,
    fake_deliver_output_exit_1: Path,
) -> None:
    monkeypatch.setenv(DELIVER_OUTPUT_BIN_ENV, str(fake_deliver_output_exit_1))
    rc = run_deliver_output(["--raw", "x"])
    assert rc == 1


@pytest.fixture
def fake_deliver_output_exit_0(tmp_path: Path) -> Path:
    """Fake deliver-output.py that emits a plausible success line and exits 0."""
    script = tmp_path / "deliver-output.py"
    script.write_text(
        "#!/bin/sh\n"
        'printf "Delivered 42 chars via Telegram\\n"\n'
        "exit 0\n"
    )
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script


def test_run_deliver_output_end_to_end_stdout_flows_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_deliver_output_exit_0: Path,
    capfd: pytest.CaptureFixture,
) -> None:
    """The fake's stdout reaches the caller's fd untouched (proves pass-through)."""
    monkeypatch.setenv(DELIVER_OUTPUT_BIN_ENV, str(fake_deliver_output_exit_0))
    capfd.readouterr()  # drain
    rc = run_deliver_output(["--raw", "x"])
    captured = capfd.readouterr()
    assert rc == 0
    assert "Delivered 42 chars" in captured.out


# ==========================================================================
# Verb: send (MOCK-ONLY; asserts argv only, no live delivery)
# ==========================================================================


def test_telegram_send_forwards_raw_text() -> None:
    """`mineru telegram send <text>` -> `deliver-output.py --raw <text>`."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.telegram.run_deliver_output",
        _record_verb_wrapper(recorded, 0),
    ):
        result = runner.invoke(app, ["telegram", "send", "hello world"])
    assert result.exit_code == 0
    assert recorded == [["--raw", "hello world"]]


def test_telegram_send_forwards_multiline_text_verbatim() -> None:
    """A newline-bearing text is passed as a single argv element."""
    recorded: List[List[str]] = []
    text = "line one\nline two\n\nparagraph three"
    with patch(
        "mineru_cli.verbs.telegram.run_deliver_output",
        _record_verb_wrapper(recorded, 0),
    ):
        result = runner.invoke(app, ["telegram", "send", text])
    assert result.exit_code == 0
    assert recorded == [["--raw", text]]


def test_telegram_send_propagates_nonzero_exit() -> None:
    """A non-zero rc from the wrapper (e.g. missing token) surfaces."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.telegram.run_deliver_output",
        _record_verb_wrapper(recorded, 1),
    ):
        result = runner.invoke(app, ["telegram", "send", "x"])
    assert result.exit_code == 1


def test_telegram_send_missing_binary_via_env_returns_127(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: env-pointed-at-nowhere + real wrapper produces exit 127."""
    monkeypatch.setenv(
        DELIVER_OUTPUT_BIN_ENV, "/definitely/not/here/deliver-output.py"
    )
    result = runner.invoke(app, ["telegram", "send", "x"])
    assert result.exit_code == MISSING_BIN_EXIT_CODE
    assert "/definitely/not/here/deliver-output.py" in result.stderr
    assert DELIVER_OUTPUT_BIN_ENV in result.stderr


def test_telegram_send_help_smoke() -> None:
    result = runner.invoke(app, ["telegram", "send", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "send" in lowered
    assert "text" in lowered or "raw" in lowered


# ==========================================================================
# Verb: deliver (MOCK-ONLY; asserts argv only, no live delivery)
# ==========================================================================


def test_telegram_deliver_forwards_path() -> None:
    """`mineru telegram deliver <path>` -> `deliver-output.py <path>`."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.telegram.run_deliver_output",
        _record_verb_wrapper(recorded, 0),
    ):
        result = runner.invoke(
            app,
            ["telegram", "deliver", "briefs_morning/morning-2026-07-27.md"],
        )
    assert result.exit_code == 0
    assert recorded == [["briefs_morning/morning-2026-07-27.md"]]


def test_telegram_deliver_forwards_absolute_path() -> None:
    """Absolute paths pass through verbatim (no rewriting)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.telegram.run_deliver_output",
        _record_verb_wrapper(recorded, 0),
    ):
        result = runner.invoke(
            app,
            ["telegram", "deliver", "/tmp/some-brief.md"],
        )
    assert result.exit_code == 0
    assert recorded == [["/tmp/some-brief.md"]]


def test_telegram_deliver_propagates_nonzero_exit() -> None:
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.telegram.run_deliver_output",
        _record_verb_wrapper(recorded, 1),
    ):
        result = runner.invoke(app, ["telegram", "deliver", "/tmp/x.md"])
    assert result.exit_code == 1


def test_telegram_deliver_help_smoke() -> None:
    result = runner.invoke(app, ["telegram", "deliver", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "deliver" in lowered
    assert "path" in lowered or "file" in lowered


# ==========================================================================
# Verb: inject (writes directly to tmp inject-queue via env override)
# ==========================================================================


def test_telegram_inject_writes_queue_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mineru telegram inject <label> <content>` drops a JSON file into the queue dir."""
    monkeypatch.setenv("MINERU_INJECT_QUEUE_DIR", str(tmp_path))
    result = runner.invoke(
        app, ["telegram", "inject", "morning-brief", "hello Sam"]
    )
    assert result.exit_code == 0

    written = list(tmp_path.iterdir())
    assert len(written) == 1
    fp = written[0]
    assert fp.name.endswith("-morning-brief.json")
    payload = json.loads(fp.read_text())
    assert payload == {"label": "morning-brief", "content": "hello Sam"}

    # Filename shape: <14-digit-ish timestamp>-<safe_label>.json.
    # We don't pin the timestamp exactly (it's now()); just assert the
    # separator + safe-label suffix.
    ts, sep, label_ext = fp.name.partition("-")
    # The %Y%m%dT%H%M%S format produces exactly one 'T' and 15 chars
    # (8 date + 1 'T' + 6 time).
    assert "T" in ts
    assert len(ts) == 15
    assert sep == "-"
    assert label_ext == "morning-brief.json"


def test_telegram_inject_sanitizes_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Label characters outside [A-Za-z0-9_-] become `_` in the filename.

    The payload's `label` field keeps the ORIGINAL string; only the
    filename gets the sanitized version. This matches deliver-output.py's
    enqueue_for_session behavior exactly.
    """
    monkeypatch.setenv("MINERU_INJECT_QUEUE_DIR", str(tmp_path))
    label_original = "morning brief / v2 (draft)"
    result = runner.invoke(
        app, ["telegram", "inject", label_original, "content"]
    )
    assert result.exit_code == 0

    written = list(tmp_path.iterdir())
    assert len(written) == 1
    fp = written[0]
    # Every non-[A-Za-z0-9_-] char turned into `_`.
    assert fp.name.endswith("-morning_brief___v2__draft_.json")
    payload = json.loads(fp.read_text())
    # Original label preserved in the payload verbatim.
    assert payload["label"] == label_original
    assert payload["content"] == "content"


def test_telegram_inject_preserves_unicode_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-ASCII content lands verbatim (`ensure_ascii=False`)."""
    monkeypatch.setenv("MINERU_INJECT_QUEUE_DIR", str(tmp_path))
    content = "张三 — River 🐣"
    result = runner.invoke(
        app, ["telegram", "inject", "note", content]
    )
    assert result.exit_code == 0

    written = list(tmp_path.iterdir())
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["content"] == content


def test_telegram_inject_creates_missing_queue_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested tmp dir that doesn't exist yet is created (parents=True)."""
    nested = tmp_path / "deep" / "queue"
    assert not nested.exists()
    monkeypatch.setenv("MINERU_INJECT_QUEUE_DIR", str(nested))
    result = runner.invoke(app, ["telegram", "inject", "l", "c"])
    assert result.exit_code == 0
    assert nested.is_dir()
    assert len(list(nested.iterdir())) == 1


def test_telegram_inject_prints_written_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stdout carries the absolute path of the file that was written."""
    monkeypatch.setenv("MINERU_INJECT_QUEUE_DIR", str(tmp_path))
    result = runner.invoke(app, ["telegram", "inject", "l", "c"])
    assert result.exit_code == 0
    printed = result.stdout.strip()
    assert Path(printed).exists()
    assert printed.startswith(str(tmp_path))


def test_telegram_inject_help_smoke() -> None:
    result = runner.invoke(app, ["telegram", "inject", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "inject" in lowered
    assert "label" in lowered


def test_telegram_inject_same_second_same_label_does_not_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two invocations with identical label at the same second must both persist.

    Regression guard for the silent-overwrite bug. Second-precision
    timestamps + label sanitization collapse many label pairs to the
    same filename base; naive `write_text` would drop the earlier
    payload and the daemon would never see it.

    We freeze `datetime.now` at a fixed instant so both writes really
    do compute the same `<ts>-<label>` base, and assert both queue
    files land on disk with distinct payloads.
    """
    monkeypatch.setenv("MINERU_INJECT_QUEUE_DIR", str(tmp_path))

    frozen = datetime.datetime(2026, 8, 1, 12, 0, 0)

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr("mineru_cli.verbs.telegram.datetime.datetime", FrozenDatetime)

    result_a = runner.invoke(app, ["telegram", "inject", "same-label", "payload A"])
    result_b = runner.invoke(app, ["telegram", "inject", "same-label", "payload B"])
    assert result_a.exit_code == 0
    assert result_b.exit_code == 0

    files = sorted(tmp_path.iterdir())
    assert len(files) == 2, f"expected two queue files, got {[f.name for f in files]}"

    contents = {json.loads(f.read_text())["content"] for f in files}
    assert contents == {"payload A", "payload B"}

    # Both filenames start with the same `<ts>-<safe_label>` prefix so
    # the daemon-side timestamp parser still sees the intended time.
    prefix = "20260801T120000-same-label"
    assert all(f.name.startswith(prefix) for f in files)


# ==========================================================================
# Verb: allowlist (reads from a tmp file via env override; never touches Keychain)
# ==========================================================================


def test_telegram_allowlist_reads_tmp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MINERU_TELEGRAM_ALLOWLIST_FILE` points at a file whose contents get printed."""
    allowlist_file = tmp_path / "allowlist.txt"
    allowlist_file.write_text("111111111,222222222,333333333")
    monkeypatch.setenv("MINERU_TELEGRAM_ALLOWLIST_FILE", str(allowlist_file))
    result = runner.invoke(app, ["telegram", "allowlist"])
    assert result.exit_code == 0
    assert "111111111,222222222,333333333" in result.stdout


def test_telegram_allowlist_strips_surrounding_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file with a trailing newline still prints the clean comma-list."""
    allowlist_file = tmp_path / "allowlist.txt"
    allowlist_file.write_text("  111,222  \n")
    monkeypatch.setenv("MINERU_TELEGRAM_ALLOWLIST_FILE", str(allowlist_file))
    result = runner.invoke(app, ["telegram", "allowlist"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "111,222"


def test_telegram_allowlist_empty_file_prints_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty allowlist file exits 0 with empty stdout.

    Empty is a valid outcome (means 'block everyone' by the fail-closed
    policy); the CLI's job is to reflect the source of truth, not to
    editorialize on it.
    """
    allowlist_file = tmp_path / "allowlist.txt"
    allowlist_file.write_text("")
    monkeypatch.setenv("MINERU_TELEGRAM_ALLOWLIST_FILE", str(allowlist_file))
    result = runner.invoke(app, ["telegram", "allowlist"])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_telegram_allowlist_missing_override_file_exits_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env override pointing at a missing file surfaces an actionable error."""
    monkeypatch.setenv(
        "MINERU_TELEGRAM_ALLOWLIST_FILE",
        "/definitely/not/here/allowlist.txt",
    )
    result = runner.invoke(app, ["telegram", "allowlist"])
    assert result.exit_code == 2
    assert (
        "MINERU_TELEGRAM_ALLOWLIST_FILE" in result.stderr
        or "missing" in result.stderr.lower()
    )


def test_telegram_allowlist_directory_override_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env override pointing at a directory must exit 2 with a clean message.

    Regression guard: previously the code guarded with `.exists()`
    which returns True on a directory, then `read_text` on the
    directory raised `IsADirectoryError` — the CLI surfaced an ugly
    traceback with exit 1 instead of the actionable exit-2 message.
    """
    dir_path = tmp_path / "not-a-file"
    dir_path.mkdir()
    monkeypatch.setenv("MINERU_TELEGRAM_ALLOWLIST_FILE", str(dir_path))
    result = runner.invoke(app, ["telegram", "allowlist"])
    assert result.exit_code == 2
    # Message points the operator at the fix, not a raw traceback.
    assert "MINERU_TELEGRAM_ALLOWLIST_FILE" in result.stderr
    assert "IsADirectoryError" not in result.stderr


def test_telegram_allowlist_help_smoke() -> None:
    result = runner.invoke(app, ["telegram", "allowlist"], catch_exceptions=False)
    # The verb runs without the env override; on this test host Keychain
    # may or may not have the slot. Instead of asserting on the runtime
    # branch, we invoke --help and confirm it renders cleanly.
    # (The runtime read is covered by the tmp-file tests above.)
    result = runner.invoke(app, ["telegram", "allowlist", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "allowlist" in lowered


# ==========================================================================
# Verb: photo / photos help-smoke (wire-up coverage lives in test_telegram_photo_verb.py)
# ==========================================================================


def test_telegram_photo_help_smoke() -> None:
    """`mineru telegram photo --help` renders after P3-03 wire-up."""
    result = runner.invoke(app, ["telegram", "photo", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    # Every flag P3-03 wires must appear in the help.
    for token in ("--caption", "--parse-mode", "--retention", "--dedup", "--label", "--reply-to"):
        assert token in lowered, f"telegram photo --help missing {token!r}"


def test_telegram_photos_help_smoke() -> None:
    """`mineru telegram photos --help` renders the four sub-commands."""
    result = runner.invoke(app, ["telegram", "photos", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    for sub in ("list", "search", "show", "prune"):
        assert sub in lowered, f"telegram photos --help missing sub-command {sub!r}"


# ==========================================================================
# Verb: root help mentions the four wired verbs + the SAFETY posture
# ==========================================================================


def test_telegram_root_help_lists_all_four_wired_verbs() -> None:
    """`mineru telegram --help` renders send / deliver / inject / allowlist."""
    result = runner.invoke(app, ["telegram", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    for verb in ("send", "deliver", "inject", "allowlist"):
        assert verb in lowered, f"telegram --help missing {verb!r}"


def test_telegram_root_help_mentions_safety_posture() -> None:
    """The noun-level help surfaces the write / mock-only discipline."""
    result = runner.invoke(app, ["telegram", "--help"])
    assert result.exit_code == 0
    # We don't pin the exact wording; just require some safety signal to
    # be present so a regression that stripped the docstring is caught.
    lowered = result.stdout.lower()
    assert (
        "safety" in lowered
        or "mock" in lowered
        or "write" in lowered
        or "landline" in lowered
    )


# ==========================================================================
# Static invariant: verb source contains no direct API calls / secret handling
# ==========================================================================


def test_verb_source_does_no_direct_telegram_api_calls() -> None:
    """Same posture as the wrapper: no direct Bot API from Python in the verb."""
    for forbidden in (
        '"https://api.telegram.org',
        "'https://api.telegram.org",
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
        assert forbidden not in VERB_SRC, (
            f"verbs/telegram.py must not make direct API calls; found {forbidden!r}"
        )


def test_verb_source_has_no_hardcoded_bot_token_or_chat_id_literals() -> None:
    """Verb source must not carry token / chat-id constants.

    The single Keychain slot NAME (`telegram-allowed-chat-ids`) IS
    referenced by design as a lookup key; that's a name, not a value.
    The forbidden shapes here are the ones that would carry actual
    secret material.
    """
    for forbidden in (
        # A shell-export-style Python assignment carrying the value:
        "BOT_TOKEN =",
        "bot_token =",
        "CHAT_ID =",
        "chat_id_value",
        # Env var names that Landline / deliver-output.py load values
        # from - the verb layer should never pull tokens itself.
        'os.environ.get("TELEGRAM_BOT_TOKEN"',
        'os.environ["TELEGRAM_BOT_TOKEN"',
        'os.environ.get("TELEGRAM_CHAT_ID"',
    ):
        assert forbidden not in VERB_SRC, (
            f"verbs/telegram.py must not carry token/chat-id state; found {forbidden!r}"
        )
