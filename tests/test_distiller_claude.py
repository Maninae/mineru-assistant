"""Tests for the default `memory consolidate` distiller.

Covers `mineru_cli.memory_ops.distiller_claude.default_claude_cli_distiller`,
the headless-Claude-CLI backend that `consolidate_daily_fragments` uses
when no custom distiller is injected.

None of these tests invoke real `claude`. The subprocess call is
monkeypatched so the assertions cover:

  - argv shape: `-p`, `--output-format text`, the security lock-down
    flags (empty `--tools`, `--permission-prompts none`,
    `--strict-mcp-config`, `--disable-slash-commands`), optional
    `--model`, and the on-disk distillation prompt passed via
    `--append-system-prompt` (short, static, argv-safe).
  - SECURITY contract: `--permission-mode bypassPermissions` must be
    ABSENT (a prior version passed it, which is the wrong control
    for adversarial input — the raw bundle can contain
    externally-sourced text and is a prompt-injection vector). The
    child must be locked to a tool-less, text-only transform.
  - stdin carries the raw bundle (private daily notes) and argv does
    NOT — this is the "keep personal text out of `ps` / process
    listings" invariant.
  - Prompt file: the distillation instructions are loaded from
    `prompts/consolidate_distiller.md`, not hardcoded in Python.
  - Failure modes fail LOUD: missing binary, non-zero exit, timeout
    each raise `DistillerError` with an actionable message. Nothing
    silently returns empty output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from mineru_cli.memory_ops import distiller_claude
from mineru_cli.memory_ops.distiller_claude import (
    CLAUDE_BINARY_NAME,
    DEFAULT_TIMEOUT_SECONDS,
    DistillerError,
    default_claude_cli_distiller,
)


# ---------------------------------------------------------------- fixtures


class _FakeCompleted:
    """Stand-in for `subprocess.CompletedProcess` — only the fields the
    distiller reads (returncode/stdout/stderr) matter."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def claude_on_path(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pretend `claude` resolves to a fixed absolute path on PATH."""
    resolved = "/fake/prefix/bin/claude"
    monkeypatch.setattr(
        "mineru_cli.memory_ops.distiller_claude.shutil.which",
        lambda name: resolved if name == CLAUDE_BINARY_NAME else None,
    )
    return resolved


@pytest.fixture
def capture_run(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Capture the call to `subprocess.run` and drive its return value.

    The dict returned exposes:
      - `captured`: kwargs of the last `subprocess.run` call.
      - `set_result(returncode, stdout, stderr)`: seed the fake exit.
      - `set_exception(exc)`: make the fake call raise instead.
    """
    state: Dict[str, Any] = {
        "captured": {},
        "result": _FakeCompleted(returncode=0, stdout="DEFAULT-OK\n"),
        "exception": None,
    }

    def fake_run(*args: Any, **kwargs: Any) -> _FakeCompleted:
        state["captured"]["args"] = args
        state["captured"]["kwargs"] = kwargs
        if state["exception"] is not None:
            raise state["exception"]
        return state["result"]

    monkeypatch.setattr(
        "mineru_cli.memory_ops.distiller_claude.subprocess.run",
        fake_run,
    )

    def set_result(returncode: int, stdout: str = "", stderr: str = "") -> None:
        state["result"] = _FakeCompleted(returncode=returncode, stdout=stdout, stderr=stderr)

    def set_exception(exc: BaseException) -> None:
        state["exception"] = exc

    state["set_result"] = set_result
    state["set_exception"] = set_exception
    return state


# ----------------------------------------------------------- happy path


def test_default_distiller_returns_child_stdout(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    capture_run["set_result"](returncode=0, stdout="CONSOLIDATED-OUTPUT\n")
    out = default_claude_cli_distiller("raw-bundle-content")
    assert out == "CONSOLIDATED-OUTPUT\n"


def test_default_distiller_argv_shape(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    """Argv carries flags only: `-p`, output format, the security
    lock-down flags, and the distillation instructions via
    `--append-system-prompt`.

    Neither the raw bundle nor a model id appear in argv on the default
    invocation."""
    raw = "private-daily-fragment-body-with-sensitive-details"
    default_claude_cli_distiller(raw)

    argv_positional = capture_run["captured"]["args"][0]
    assert argv_positional[0] == CLAUDE_BINARY_NAME
    assert "--print" in argv_positional
    assert "--output-format" in argv_positional
    fmt_idx = argv_positional.index("--output-format")
    assert argv_positional[fmt_idx + 1] == "text"

    # The distillation prompt is on argv via `--append-system-prompt`;
    # the prompt file's text must be what the flag receives.
    assert "--append-system-prompt" in argv_positional
    prompt_idx = argv_positional.index("--append-system-prompt")
    prompt_value = argv_positional[prompt_idx + 1]
    on_disk_prompt = distiller_claude.PROMPT_PATH.read_text(encoding="utf-8")
    assert prompt_value == on_disk_prompt

    # No `--model` on the default invocation (model id churn safety).
    assert "--model" not in argv_positional

    # And the private raw bundle text must NOT be anywhere on argv.
    for token in argv_positional:
        assert raw not in token, "raw bundle leaked into argv"


# ----------------------------------------------------------- security lock-down


def test_default_distiller_never_passes_bypass_permissions(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    """SECURITY REGRESSION GUARD: an earlier version of the distiller
    passed `--permission-mode bypassPermissions`. That is the exact
    wrong control for an input that may contain externally-sourced
    text (emails, chat, web content that made it into the operator's
    daily fragments) — a successful prompt-injection could then run
    tools with no gate. The fix locks the child to a tool-less
    transform (see `test_default_distiller_locks_down_tools`). This
    test guards the "bypass" flag never returns."""
    default_claude_cli_distiller("raw")
    argv_positional = capture_run["captured"]["args"][0]
    assert "--permission-mode" not in argv_positional, (
        "distiller must NOT pass --permission-mode; the whole point of "
        "the lock-down is that no permission bypass is in play."
    )
    assert "bypassPermissions" not in argv_positional
    assert "--dangerously-skip-permissions" not in argv_positional
    assert "--allow-dangerously-skip-permissions" not in argv_positional


def test_default_distiller_locks_down_tools(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    """SECURITY CONTRACT: the child `claude` must have NO tools.

    The distillation is a pure text-in / text-out transform. The
    input carries operator content that may in turn carry externally-
    sourced text (email bodies, chat messages, web excerpts pulled
    into the daily log). Any of that is a prompt-injection vector.
    The mitigation is structural, not textual: the child gets no
    tools, no permission-prompt handler that could grant one, no
    MCP servers, no slash-command surface. If a fifth line of defense
    ever gets added (e.g. `--restricted`), extend this test — do NOT
    weaken it.
    """
    default_claude_cli_distiller("raw")
    argv_positional = capture_run["captured"]["args"][0]

    # `--tools ""` — empty built-in tool allowlist.
    assert "--tools" in argv_positional
    tools_idx = argv_positional.index("--tools")
    assert argv_positional[tools_idx + 1] == "", (
        "`--tools` must be the empty string to disable ALL built-in tools."
    )

    # `--permission-prompts none` — anything that would prompt for
    # permission is denied automatically.
    assert "--permission-prompts" in argv_positional
    pp_idx = argv_positional.index("--permission-prompts")
    assert argv_positional[pp_idx + 1] == "none"

    # `--strict-mcp-config` — with no `--mcp-config` this means no
    # MCP servers are loaded, so no MCP tools materialize.
    assert "--strict-mcp-config" in argv_positional
    assert "--mcp-config" not in argv_positional, (
        "`--strict-mcp-config` is used WITHOUT `--mcp-config` on purpose "
        "(zero MCP servers). If you need an MCP server here, review the "
        "security implications first."
    )

    # `--disable-slash-commands` — no skill can inject a tool-using
    # instruction into the child.
    assert "--disable-slash-commands" in argv_positional


def test_default_distiller_passes_raw_bundle_via_stdin(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    raw = "STDIN-CARRIED-BUNDLE-CONTENT"
    default_claude_cli_distiller(raw)
    kwargs = capture_run["captured"]["kwargs"]
    assert kwargs["input"] == raw
    # And the standard subprocess plumbing: capture output, text mode,
    # don't check-raise (we handle the returncode ourselves).
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False


def test_default_distiller_forwards_optional_model_flag(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    """A caller-supplied model id lands on argv as `--model <id>`.
    Absent by default so the CLI's own model default applies."""
    default_claude_cli_distiller("raw", model="claude-sonnet-4-6")
    argv_positional = capture_run["captured"]["args"][0]
    assert "--model" in argv_positional
    model_idx = argv_positional.index("--model")
    assert argv_positional[model_idx + 1] == "claude-sonnet-4-6"


def test_default_distiller_default_timeout_forwarded(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    default_claude_cli_distiller("raw")
    assert capture_run["captured"]["kwargs"]["timeout"] == DEFAULT_TIMEOUT_SECONDS


def test_default_distiller_custom_timeout_forwarded(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    default_claude_cli_distiller("raw", timeout=17.5)
    assert capture_run["captured"]["kwargs"]["timeout"] == 17.5


# ----------------------------------------------------------- prompt file


def test_prompt_file_exists_and_is_nonempty() -> None:
    """The distillation prompt must live in a `.md` on disk (per the
    engine's `prompts-live-in-md-files` rule) and must be substantive
    enough to guide the model."""
    assert distiller_claude.PROMPT_PATH.exists(), (
        f"missing distiller prompt at {distiller_claude.PROMPT_PATH}"
    )
    body = distiller_claude.PROMPT_PATH.read_text(encoding="utf-8")
    # Load-bearing terms the prompt must carry (cardinal rule +
    # input/output shape) so a silent gutting of the file is caught.
    assert "distill" in body.lower()
    assert "daily_bundle" in body
    assert "fragment" in body
    assert len(body) > 500


def test_prompt_path_is_public_symbol() -> None:
    """PROMPT_PATH is the single source of truth referenced by BOTH the
    CLI-side distiller AND the recurring nightly consolidation template.
    Renaming or hiding it under a leading underscore would silently break
    the recurring job. Guard the public name here."""
    assert hasattr(distiller_claude, "PROMPT_PATH")
    assert distiller_claude.PROMPT_PATH == (
        Path(distiller_claude.__file__).parent
        / "prompts"
        / "consolidate_distiller.md"
    )


# ----------------------------------------------------------- failure modes


def test_default_distiller_missing_binary_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claude` not on PATH -> DistillerError names the binary and points
    the operator at the seam or an install."""
    monkeypatch.setattr(
        "mineru_cli.memory_ops.distiller_claude.shutil.which",
        lambda name: None,
    )
    with pytest.raises(DistillerError) as excinfo:
        default_claude_cli_distiller("raw")
    msg = str(excinfo.value)
    assert CLAUDE_BINARY_NAME in msg
    assert "PATH" in msg
    # Points the operator at either an install or the pluggable seam.
    assert "install" in msg.lower() or "distiller" in msg.lower()


def test_default_distiller_missing_binary_does_not_invoke_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing binary short-circuits BEFORE the subprocess call, so no
    process ever launches."""
    monkeypatch.setattr(
        "mineru_cli.memory_ops.distiller_claude.shutil.which",
        lambda name: None,
    )
    with patch(
        "mineru_cli.memory_ops.distiller_claude.subprocess.run"
    ) as mock_run:
        with pytest.raises(DistillerError):
            default_claude_cli_distiller("raw")
    mock_run.assert_not_called()


def test_default_distiller_non_zero_exit_raises_with_stderr(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    capture_run["set_result"](
        returncode=42,
        stdout="partial-output-that-must-not-be-returned",
        stderr="model refused / oauth expired\n",
    )
    with pytest.raises(DistillerError) as excinfo:
        default_claude_cli_distiller("raw")
    msg = str(excinfo.value)
    assert "42" in msg
    assert "model refused" in msg
    # Partial stdout must not silently be surfaced as success.
    assert "partial-output-that-must-not-be-returned" not in msg


def test_default_distiller_timeout_raises_actionable_error(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    capture_run["set_exception"](
        subprocess.TimeoutExpired(cmd=["claude", "-p"], timeout=300.0)
    )
    with pytest.raises(DistillerError) as excinfo:
        default_claude_cli_distiller("raw", timeout=300.0)
    msg = str(excinfo.value)
    assert "timed out" in msg.lower()
    assert "300" in msg


def test_default_distiller_race_between_which_and_exec(
    claude_on_path: str, capture_run: Dict[str, Any]
) -> None:
    """`shutil.which` said the binary was there; it vanished before
    exec. Report the race, don't leak a raw Python traceback."""
    capture_run["set_exception"](FileNotFoundError("claude"))
    with pytest.raises(DistillerError) as excinfo:
        default_claude_cli_distiller("raw")
    assert "disappeared" in str(excinfo.value).lower() or "not found" in str(
        excinfo.value
    ).lower()
