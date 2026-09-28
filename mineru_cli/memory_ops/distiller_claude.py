"""Default distiller for `memory consolidate`: shells out to headless Claude Code.

`memory consolidate` stages a day's raw session fragments into a bundle
and then calls a `distiller: Callable[[str], str]` to turn that bundle
into the final consolidated `<YYYY-MM-DD>.md`. This module supplies
the default distiller wired into the CLI verb: it invokes the local
`claude` CLI in print / non-interactive mode with a static distillation
prompt appended to the system prompt, feeds the raw bundle in via
stdin, and returns the process's stdout.

Design rules the seam holds to:

  - Generic engine. The binary is bare `claude`, not any operator-
    specific wrapper. A live workspace may use a wrapper like `claude-fda`; the
    engine repo must not depend on that wrapper existing on PATH.
  - Argv carries flags only. The raw bundle text (which contains the
    user's private daily session notes) is delivered exclusively on
    stdin, never as an argv value. Keeps personal text out of `ps` /
    process listings and dodges argv-length limits on large bundles.
  - Prompt lives in a `.md` file (`prompts/consolidate_distiller.md`)
    loaded at runtime, not a Python string literal. Independent
    versioning and A/B tuning of the prompt.
  - No hardcoded model id. Claude Code's model default applies unless
    the caller passes an explicit override; model ids churn and the
    engine must not pin one.
  - Fail loud, never write garbage. Missing binary, non-zero exit, and
    subprocess timeout each raise `DistillerError` with an actionable
    message; the caller (`consolidate_daily_fragments`) surfaces the
    failure instead of writing a partial `<YYYY-MM-DD>.md`.

The distiller stays PLUGGABLE at the `consolidate_daily_fragments`
seam: this module is only the DEFAULT used when the caller does not
inject one. Alternative distillers (a local Ollama, a hosted API, a
test double) plug in unchanged.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

# Default subprocess timeout. Generous enough for a large daily bundle
# on a slow model + first-token latency; short enough that a hung
# `claude` process is caught in a reasonable time.
DEFAULT_TIMEOUT_SECONDS: float = 300.0

# Bare binary name. We resolve it via PATH (not a hardcoded absolute
# path) so any Claude Code install layout works: Homebrew, npm-global,
# a user-local `~/.local/bin/claude` symlink, or a custom install.
CLAUDE_BINARY_NAME: str = "claude"

# The distillation prompt sits next to this module as a plain `.md`
# file. It is the CANONICAL distillation-instruction prompt for the
# whole framework: both this CLI-side default distiller AND the
# recurring nightly consolidation job read this exact file, so the two
# never drift. The recurring template (`engine/recurring/consolidate-
# daily-memories.md.template` Step 3) references it via
# `PROMPT_PATH.read_text()`, so this attribute is a stable public
# name, not a private one — do not rename or leading-underscore it
# without updating that template.
PROMPT_PATH = Path(__file__).parent / "prompts" / "consolidate_distiller.md"


class DistillerError(RuntimeError):
    """Raised when the default distiller cannot produce output.

    Covers all three failure modes the caller must not silently swallow:

      - `claude` binary missing from PATH.
      - `claude` exited non-zero (captured stderr is in the message).
      - `claude` exceeded the configured timeout.

    `consolidate_daily_fragments` lets this propagate so no partial or
    empty `<YYYY-MM-DD>.md` gets written on failure.
    """


def _load_distiller_prompt() -> str:
    """Read the on-disk distillation prompt.

    Kept as its own function so tests can monkeypatch it and so any
    future switch to an environment override lands in one place.
    """
    return PROMPT_PATH.read_text(encoding="utf-8")


def _build_claude_argv(model: Optional[str]) -> List[str]:
    """Build the argv for the headless `claude` invocation.

    Flags only, no content. `--print` (-p) puts the CLI in
    non-interactive mode; `--output-format text` requests plain-text
    output (no JSON envelope) so the process stdout IS the
    consolidated markdown.

    SECURITY: The raw bundle fed to this distiller contains the
    operator's private daily fragments, which may in turn contain
    content that originated from external sources (emails, chat
    messages, web pages the operator visited). Any such content is a
    prompt-injection vector: an attacker who lands text in a fragment
    can try to steer the child `claude` into running tools. This is a
    pure text-in / text-out transform — it needs ZERO tools — so we
    lock the child down accordingly:

      - `--tools ""` grants NO built-in tools (Bash / Write /
        WebFetch / everything). An empty allowlist means the model
        literally cannot call any built-in tool.
      - `--permission-prompts none` denies any action that would
        otherwise trigger a permission prompt. Combined with the
        empty tool set, this is a belt-and-braces gate.
      - `--strict-mcp-config` (without a `--mcp-config`) prevents
        the child from picking up any MCP server config from the
        environment, so no MCP tools materialize either.
      - `--disable-slash-commands` disables skill invocation
        (skills can ship tool-using instructions).
      - We deliberately do NOT pass `--permission-mode
        bypassPermissions`. Silencing the permission prompt is
        exactly the wrong control for adversarial input.

    The distillation instructions ride on `--append-system-prompt`;
    the raw bundle rides on stdin. Both are added by `run_distiller`.
    """
    argv: List[str] = [
        CLAUDE_BINARY_NAME,
        "--print",
        "--output-format",
        "text",
        # SECURITY LOCK-DOWN — see docstring above. Order kept stable
        # for the argv-shape tests to assert against.
        "--tools",
        "",
        "--permission-prompts",
        "none",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]
    if model:
        argv.extend(["--model", model])
    return argv


def default_claude_cli_distiller(
    raw_bundle: str,
    *,
    model: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Distill `raw_bundle` by shelling out to `claude -p`.

    Args:
        raw_bundle: the full concatenated `<daily_bundle>` XML string
            produced by `consolidate.build_raw_bundle`. Delivered to
            the child on stdin. Never placed on argv.
        model: optional Claude Code model id to override the CLI's
            default. Left `None` for the shipped default so a churned
            model id never breaks the engine.
        timeout: seconds to wait for `claude` before killing it and
            raising `DistillerError`.

    Returns:
        The child's stdout as a UTF-8 string. Trailing whitespace is
        preserved so the caller's `write_text` reproduces exactly
        what the model emitted; the caller decides whether to
        normalize.

    Raises:
        DistillerError: `claude` was not on PATH, exited non-zero, or
            was killed for exceeding `timeout`. Message names the
            failure mode and, where available, the captured stderr.
    """
    binary_path = shutil.which(CLAUDE_BINARY_NAME)
    if binary_path is None:
        raise DistillerError(
            f"`{CLAUDE_BINARY_NAME}` binary not found on PATH. Install Claude "
            "Code (https://docs.claude.com/en/docs/claude-code) and ensure "
            f"`{CLAUDE_BINARY_NAME}` is on PATH, or inject a custom "
            "`distiller: Callable[[str], str]` at the "
            "`consolidate_daily_fragments` seam."
        )

    argv = _build_claude_argv(model)
    system_prompt = _load_distiller_prompt()
    # Both the system prompt (static distillation instructions) and the
    # raw bundle (dynamic private data) go through stdin/argv-safe
    # channels only. The bundle is on stdin; the prompt rides
    # `--append-system-prompt`, which the wrapper of the same name in
    # Claude Code accepts as a plain argv value.
    argv.extend(["--append-system-prompt", system_prompt])

    try:
        completed = subprocess.run(
            argv,
            input=raw_bundle,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DistillerError(
            f"`{CLAUDE_BINARY_NAME}` distiller timed out after "
            f"{timeout:.0f}s. Increase the timeout or investigate why the "
            "model is hanging on this bundle."
        ) from exc
    except FileNotFoundError as exc:
        # Race window: `shutil.which` said the binary was there, but it
        # vanished before `subprocess.run` could exec it.
        raise DistillerError(
            f"`{CLAUDE_BINARY_NAME}` binary disappeared between resolution "
            f"and exec ({exc})."
        ) from exc

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip()
        raise DistillerError(
            f"`{CLAUDE_BINARY_NAME}` distiller exited with code "
            f"{completed.returncode}. stderr: {stderr_tail!r}"
        )

    return completed.stdout
