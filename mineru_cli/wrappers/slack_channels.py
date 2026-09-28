"""Facade wrapper around the live slack-channels engine at $MINERU_HOME/bin/slack-channels.

Sibling of slack_read / slack_thread / slack_refresh_users. Wraps a
single bash script that hits Slack's `conversations.list` endpoint
(read-only). Follows the same wrapper shape:

  - Env override -> documented default.
  - Argv is `[binary, *extras]` opaquely; the bash script owns its own
    flag parsing (`--include-private`, `--limit N`).
  - `subprocess.run(cmd, check=False)` with NO stdout/stderr override.
  - Exit code propagates unchanged; missing binary -> `typer.Exit(127)`.
  - The wrapper never sees the Slack token: the shell script pulls it
    from macOS Keychain via `security find-generic-password`.

READ-ONLY invariant. `slack-channels` only calls the `conversations.list`
read endpoint, and the basename guard rejects any env override whose
basename is not `slack-channels`, defending against a silent swap for a
write-capable shim.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_overlay


_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_SLACK_CHANNELS_BIN = str(_MINERU_HOME / "bin" / "slack-channels")

SLACK_CHANNELS_BIN_ENV = "MINERU_SLACK_CHANNELS_BIN"

MISSING_BIN_EXIT_CODE = 127
OBSERVER_BASENAME_MISMATCH_EXIT_CODE = 78
EXPECTED_BIN_BASENAME = "slack-channels"


def resolve_slack_channels_bin() -> str:
    """Return the slack-channels binary path (env override or default)."""
    override = os.environ.get(SLACK_CHANNELS_BIN_ENV)
    if override:
        return override
    return DEFAULT_SLACK_CHANNELS_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` is an executable regular file (or a resolvable bare name)."""
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_slack_channels_argv(args: Sequence[str]) -> List[str]:
    """Return exactly `[resolved_binary, *args]` — no filtering, no rewriting."""
    return [resolve_slack_channels_bin(), *args]


def _slack_env_for_ctx(ctx: Optional[Any]) -> Optional[dict]:
    """Env overlay that pins slack-channels to the active profile's Keychain.

    Mirror of the slack_read/slack_thread helper. See those for the
    full rationale (Finding 7 belt-and-braces on Keychain scoping).
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


def run_slack_channels(
    args: Sequence[str], ctx: Optional[Any] = None
) -> int:
    """Shell out to `slack-channels <args...>` with stdio pass-through.

    Args:
        args: argv tail; the bash script accepts `[--include-private]
              [--limit N]`. The wrapper forwards opaquely so any future
              flag lands without a wrapper change.
        ctx:  optional Typer context; when hydrated, exports the active
              profile's Keychain account into the child env.

    Returns:
        The engine's exit code (0 success, 1 missing Keychain token, 2
        unknown flag). The wrapper does not rewrite unusual codes.

    Raises:
        typer.Exit(127): binary missing / not executable.
        typer.Exit(78): env override points at the wrong basename.
    """
    binary = resolve_slack_channels_bin()

    basename = os.path.basename(binary)
    if basename != EXPECTED_BIN_BASENAME:
        typer.echo(
            f"mineru slack: {SLACK_CHANNELS_BIN_ENV} must point at a "
            f"{EXPECTED_BIN_BASENAME!r}-named binary; got {binary!r} "
            f"(basename {basename!r}). Refusing to shell out: the Slack "
            f"integration is a strict read-only observer surface; "
            f"pointing at a write-capable shim would breach that policy. "
            f"Default: {DEFAULT_SLACK_CHANNELS_BIN!r}.",
            err=True,
        )
        raise typer.Exit(code=OBSERVER_BASENAME_MISMATCH_EXIT_CODE)

    if not _binary_available(binary):
        typer.echo(
            f"mineru slack: slack-channels engine not found at {binary!r} "
            f"(set {SLACK_CHANNELS_BIN_ENV} to override, "
            f"default {DEFAULT_SLACK_CHANNELS_BIN!r}).",
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
        typer.echo(
            f"mineru slack: slack-channels engine vanished or is not "
            f"executable at {binary!r} (set {SLACK_CHANNELS_BIN_ENV} to "
            f"override, default {DEFAULT_SLACK_CHANNELS_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
