"""Facade wrapper around the live slack-thread engine at $MINERU_HOME/bin/slack-thread.

Sibling of slack_read / slack_refresh_users. Wraps a single bash script
that hits Slack's `conversations.replies` endpoint (read-only). Follows
the exact same shape as `wrappers/slack_read.py`:

  - Env override -> documented default.
  - Argv is `[binary, channel, ts, *extras]` opaquely; wrapper does not
    parse output or reshape argv.
  - `subprocess.run(cmd, check=False)` with NO stdout/stderr override so
    the caller's fds see the shell script's output byte-identically.
  - Exit code propagates unchanged; missing binary -> `typer.Exit(127)`.
  - The wrapper never sees the Slack token: the shell script pulls it
    from macOS Keychain via `security find-generic-password`.

READ-ONLY invariant. The `slack-thread` script only calls
`conversations.replies`, a pure read endpoint. There is no companion
write script and this wrapper's basename guard rejects any env override
whose basename is not `slack-thread`, so a write-capable shim cannot be
silently substituted (defense-in-depth mirror of the slack_read guard).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_overlay


# Default matches the workspace bin/ layout (mirror of slack_read).
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_SLACK_THREAD_BIN = str(_MINERU_HOME / "bin" / "slack-thread")

# Env var name that overrides the default; documented and used by tests.
SLACK_THREAD_BIN_ENV = "MINERU_SLACK_THREAD_BIN"

# POSIX "command not found" — matches the other wrappers.
MISSING_BIN_EXIT_CODE = 127

# 78 = wrapper-refused; distinct from POSIX 127 so pipelines can tell
# "engine missing" from "observer-mode invariant violated" apart.
OBSERVER_BASENAME_MISMATCH_EXIT_CODE = 78

# Basename argv[0] must resolve to. Used both at runtime and by tests.
EXPECTED_BIN_BASENAME = "slack-thread"


def resolve_slack_thread_bin() -> str:
    """Return the slack-thread binary path: env override or documented default.

    Empty-string env var is treated as unset (matches shell `[ -z "$VAR" ]`
    semantics used across the wrappers).
    """
    override = os.environ.get(SLACK_THREAD_BIN_ENV)
    if override:
        return override
    return DEFAULT_SLACK_THREAD_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    Absolute path / path with a separator: require an executable regular
    file (rejects directories and non-executables so we get a friendly
    127 instead of a downstream `PermissionError` traceback). Bare name:
    defer to `shutil.which` (already requires executability).
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_slack_thread_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Split out from `run_slack_thread` so tests can assert on argv[0]
    without needing to mock subprocess. Return is exactly
    `[resolved_binary, *args]` — no filtering, no rewriting.
    """
    return [resolve_slack_thread_bin(), *args]


def _slack_env_for_ctx(ctx: Optional[Any]) -> Optional[dict]:
    """Env overlay that pins slack-thread to the active profile's Keychain.

    Mirrors slack_read's helper exactly. Exports SLACK_KEYCHAIN_ACCOUNT
    from the active profile so `security find-generic-password -a
    "$SLACK_KEYCHAIN_ACCOUNT"` targets the right per-profile namespace,
    plus the three shared MINERU_* keys via `profile_env_overlay`.
    Returns None when the ctx has no hydrated profile (unit tests that
    construct a ctx by hand), leaving the child to inherit the ambient
    env unchanged.
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
    return {**os.environ, **overlay, "SLACK_KEYCHAIN_ACCOUNT": account}


def run_slack_thread(
    args: Sequence[str], ctx: Optional[Any] = None
) -> int:
    """Shell out to `slack-thread <args...>` with stdio pass-through.

    Args:
        args: argv tail after the program name. slack-thread expects
              `<channel_id> <parent_ts> [limit]`; the wrapper does not
              inspect this list, so future extensions land without a
              wrapper change.
        ctx:  optional Typer context; when hydrated with an active
              profile, its `keychain_account` and MINERU_* env keys are
              exported for the child so multi-profile Keychain lookups
              route correctly.

    Returns:
        The engine's exit code (0 on success, 1 on the two known error
        branches — missing Keychain token or missing user cache).

    Raises:
        typer.Exit(127): binary is missing / not executable at the
            resolved path. Stderr already carries a message naming the
            path and the override env var.
        typer.Exit(78): the resolved binary's basename is not
            `slack-thread` — refuse to shell out so a write-capable
            shim cannot substitute silently.
    """
    binary = resolve_slack_thread_bin()

    basename = os.path.basename(binary)
    if basename != EXPECTED_BIN_BASENAME:
        typer.echo(
            f"mineru slack: {SLACK_THREAD_BIN_ENV} must point at a "
            f"{EXPECTED_BIN_BASENAME!r}-named binary; got {binary!r} "
            f"(basename {basename!r}). Refusing to shell out: the Slack "
            f"integration is a strict read-only observer surface; "
            f"pointing at a write-capable shim would breach that policy. "
            f"Default: {DEFAULT_SLACK_THREAD_BIN!r}.",
            err=True,
        )
        raise typer.Exit(code=OBSERVER_BASENAME_MISMATCH_EXIT_CODE)

    if not _binary_available(binary):
        typer.echo(
            f"mineru slack: slack-thread engine not found at {binary!r} "
            f"(set {SLACK_THREAD_BIN_ENV} to override, "
            f"default {DEFAULT_SLACK_THREAD_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    cmd: List[str] = [binary, *args]
    env = _slack_env_for_ctx(ctx)
    run_kwargs: dict = {"check": False}
    if env is not None:
        run_kwargs["env"] = env
    try:
        completed = subprocess.run(cmd, **run_kwargs)
    except OSError:
        # Race window OR a permission / directory anomaly the
        # availability check couldn't foresee. Same friendly-error
        # shape as slack_read: name the resolved path, name the env
        # override, exit 127.
        typer.echo(
            f"mineru slack: slack-thread engine vanished or is not "
            f"executable at {binary!r} (set {SLACK_THREAD_BIN_ENV} to "
            f"override, default {DEFAULT_SLACK_THREAD_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
