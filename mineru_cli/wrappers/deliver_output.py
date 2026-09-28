"""Facade wrapper around the live deliver-output.py Telegram delivery script.

Phase 2 task P2-08 wire-up. `mineru telegram send` and `mineru telegram
deliver` shell out through this module. Mirrors the msearch wrapper
shape exactly: single binary (the deliver-output.py script under
`$MINERU_HOME/scripts/`), env-overridable path, stdio pass-through, exit-
code propagation, missing-bin -> 127.

⚠️ SAFETY (bold, non-negotiable) ⚠️

  **During dev/test NO real Telegram delivery ever fires.** Every test
  in `tests/test_telegram_verbs.py` patches the wrapper functions with a
  recorder and asserts the argv that WOULD be sent to the underlying
  script. The wrapper's `run_deliver_output` is NEVER exercised live in
  the CI/dev loop.

  If a maintainer runs the wired verb by hand (e.g. `mineru telegram
  send "test"` from a real shell), that IS a real send to the operator's
  Telegram chat via the live bot token loaded by deliver-output.py
  itself. The wrapper does no gating — the human at the terminal is the
  final safety layer. Confirm with the operator before invoking outbound.

  The underlying deliver-output.py enforces its own allowlist
  (Keychain-backed `telegram-allowed-chat-ids`, fail-closed on empty)
  so a misconfigured chat id can't smuggle a delivery to the wrong
  destination. That gate lives in the live tool, not here; the wrapper
  is a pure pass-through.

Contract (deliberately narrow, mirrors msearch.py):

  - Read binary path from `MINERU_DELIVER_OUTPUT_BIN` env, default
    `$MINERU_HOME/scripts/deliver-output.py`. Tests override
    the env to point at a fake.
  - Build the argv as `[binary, *args]` where `args` is exactly what
    deliver-output.py expects after its own program name. The live
    script's positional shape is `<file_path>` OR `--raw <text>` -
    both flow through opaquely.
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr
    override. The engine's stdout (the "Delivered N chars via
    Telegram" success line) and stderr (the "ERROR: Missing
    TELEGRAM_BOT_TOKEN" / "Telegram API error: {...}" / "chat_id X is
    not in the Telegram allowlist" diagnostics) flow straight through
    to the caller's fds. This preserves the operator's ability to
    diagnose delivery failures without wrapper-level filtering.
  - Return the engine's exit code unchanged. deliver-output.py uses
    `sys.exit(1)` on every error branch (missing token / missing
    allowlist / chat not allowed / empty content / file not found /
    HTTP failure) and returns 0 on success; those propagate unchanged.
  - If the binary is missing, print an actionable error to stderr
    naming the RESOLVED path and raise `typer.Exit(127)` (POSIX
    "command not found"). Never fall back silently to a different
    binary.

  The script has a `#!/usr/bin/env python3` shebang and is chmod +x,
  so subprocess.run can execute it directly without a leading
  interpreter argument.

Non-goals (strict):

  - No parsing of deliver-output.py stdout/stderr.
  - No reshaping / re-emitting engine output.
  - No filtering of the argv tail (extras pass through opaque).
  - No allowlist enforcement, no chunking, no HTML conversion - the
    live script owns all of that.
  - No token handling. deliver-output.py reads the bot token from
    macOS Keychain via `security`; the wrapper never sees it. This
    preserves SECURITY.md's "Never Store Plaintext Secrets on Disk"
    contract - the CLI process never has the token in memory.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_overlay

# Documented default matches TOOLS.md and the P2-08 task description
# (`$MINERU_HOME/scripts/deliver-output.py`). The script is executable and
# has a python3 shebang, so subprocess.run can invoke it directly.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_DELIVER_OUTPUT_BIN = str(_MINERU_HOME / "scripts" / "deliver-output.py")

# Env var name that overrides the default (documented; tests use it).
DELIVER_OUTPUT_BIN_ENV = "MINERU_DELIVER_OUTPUT_BIN"

# POSIX "command not found" - the semantically-right exit for a missing
# engine binary. Distinct from typical app errors (usually 1 or 2), so
# callers can special-case it in shell pipelines.
MISSING_BIN_EXIT_CODE = 127

# Basename we expect argv[0] to resolve to. Used by tests as the
# invariant guard: any wrapper regression that swapped in a differently
# named script would fail this check.
EXPECTED_BIN_BASENAME = "deliver-output.py"


def resolve_deliver_output_bin() -> str:
    """Return the deliver-output.py path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(DELIVER_OUTPUT_BIN_ENV)
    if override:
        return override
    return DEFAULT_DELIVER_OUTPUT_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids `PermissionError` / `IsADirectoryError` traceback leaks
      when the resolved path is a directory or a non-executable file.
    - Bare name (e.g. `deliver-output.py`): defer to PATH via
      `shutil.which`, which already requires executability.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_deliver_output_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Kept as a separate helper so the safety-invariant test can assert on
    argv[0] without needing to mock subprocess. The returned list is
    exactly `[resolved_binary, *args]` - no filtering, no rewriting.
    """
    return [resolve_deliver_output_bin(), *args]


def _keychain_env_for_ctx(ctx: Optional[Any]) -> Optional[dict]:
    """Build the env dict that pins deliver-output.py to the active profile's Keychain.

    `scripts/deliver-output.py`'s `keychain_get(service, account=...)`
    now honors `MINERU_KEYCHAIN_ACCOUNT` (default `"mineru"`), so
    exporting the active profile's `keychain_account` into the child env
    routes the Telegram bot-token / chat-id / allowlist lookups into the
    right per-profile namespace on a multi-tenant Mac. Also stacks the
    other two shared profile-env keys (MINERU_HOME /
    MINERU_INJECT_QUEUE_DIR) via `profile_env_overlay` so a downstream
    `push_send.py` sub-invocation reads THIS profile's VAPID / APP_DIR
    (step-5 audit, Finding 10). Returns `None` when no profile is on the
    context (unit tests that don't hydrate); the caller then omits `env=`
    and the child inherits the ambient env.
    """
    if ctx is None:
        return None
    obj = getattr(ctx, "obj", None) or {}
    profile = obj.get("profile_obj")
    if profile is None:
        return None
    account = getattr(profile, "keychain_account", None)
    if not account:
        return None
    overlay = profile_env_overlay(profile)
    return {**os.environ, **overlay, "MINERU_KEYCHAIN_ACCOUNT": account}


def run_deliver_output(
    args: Sequence[str], ctx: Optional[Any] = None
) -> int:
    """Shell out to `deliver-output.py <args...>` with stdio pass-through.

    ⚠️ OUTBOUND to Telegram in production. See the module docstring's
    SAFETY block. Every dev/test caller MUST patch this function; no
    live invocation of the underlying script should ever happen in a
    test or dev-loop run.

    Args:
        args: argv tail passed to deliver-output.py after the program
              name. Example: `["--raw", "hello world"]` for a raw text
              send, or `["briefs_morning/morning-2026-07-27.md"]` for a
              file delivery.

    Returns:
        The engine's exit code (0 on delivered success; 1 on any
        error branch). Callers convert to `typer.Exit(code=rc)` to
        propagate through the CLI.

    Raises:
        typer.Exit(127): the deliver-output.py script at the resolved
            path does not exist. Stderr already carries an actionable
            message naming the path and the override env var.
    """
    binary = resolve_deliver_output_bin()

    if not _binary_available(binary):
        typer.echo(
            f"mineru telegram: deliver-output.py engine not found at {binary!r} "
            f"(set {DELIVER_OUTPUT_BIN_ENV} to override, "
            f"default {DEFAULT_DELIVER_OUTPUT_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    cmd: List[str] = [binary, *args]
    env = _keychain_env_for_ctx(ctx)
    run_kwargs: dict = {"check": False}
    if env is not None:
        run_kwargs["env"] = env
    try:
        completed = subprocess.run(cmd, **run_kwargs)
    except OSError:
        # Race window OR a permission / directory anomaly the availability
        # check couldn't foresee. Cover the whole OSError family
        # (FileNotFoundError, PermissionError, IsADirectoryError) with one
        # friendly message instead of leaking a raw Python traceback.
        typer.echo(
            f"mineru telegram: deliver-output.py engine vanished or is not "
            f"executable at {binary!r} (set {DELIVER_OUTPUT_BIN_ENV} to "
            f"override, default {DEFAULT_DELIVER_OUTPUT_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
