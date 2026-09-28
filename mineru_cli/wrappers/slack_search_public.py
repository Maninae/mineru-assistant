"""Facade wrapper around the live slack-search-public engine at $MINERU_HOME/bin/slack-search-public.

Sibling of slack_read / slack_thread / slack_channels /
slack_refresh_users. Wraps a single bash script that hits Slack's
`search.messages` endpoint (read-only, scoped to public channels).

NOTE: `search.messages` requires a USER token (xoxp-), not a bot token
(xoxb-). If the Keychain entry is a bot token, Slack responds with
`ok: false, error: "not_allowed_token_type"`, which propagates via
stdout as JSON. The wrapper does not diagnose this; the bash script
surfaces the raw error field so the operator can see the exact reason.

Follows the same wrapper shape as the other slack wrappers:
  - Env override -> documented default.
  - Argv opaquely `[binary, query, *extras]`.
  - Pass-through stdio, exit code propagation.
  - Basename guard on env override (READ-ONLY invariant).
  - Wrapper never sees the token (Keychain lookup lives in the shell).
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
DEFAULT_SLACK_SEARCH_PUBLIC_BIN = str(
    _MINERU_HOME / "bin" / "slack-search-public"
)

SLACK_SEARCH_PUBLIC_BIN_ENV = "MINERU_SLACK_SEARCH_PUBLIC_BIN"

MISSING_BIN_EXIT_CODE = 127
OBSERVER_BASENAME_MISMATCH_EXIT_CODE = 78
EXPECTED_BIN_BASENAME = "slack-search-public"


def resolve_slack_search_public_bin() -> str:
    """Return the slack-search-public binary path (env override or default)."""
    override = os.environ.get(SLACK_SEARCH_PUBLIC_BIN_ENV)
    if override:
        return override
    return DEFAULT_SLACK_SEARCH_PUBLIC_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` is an executable regular file (or a resolvable bare name)."""
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_slack_search_public_argv(args: Sequence[str]) -> List[str]:
    """Return exactly `[resolved_binary, *args]` — no filtering, no rewriting."""
    return [resolve_slack_search_public_bin(), *args]


def _slack_env_for_ctx(ctx: Optional[Any]) -> Optional[dict]:
    """Env overlay that pins slack-search-public to the active profile's Keychain."""
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


def run_slack_search_public(
    args: Sequence[str], ctx: Optional[Any] = None
) -> int:
    """Shell out to `slack-search-public <args...>` with stdio pass-through.

    Args:
        args: argv tail. Expected shape: `<query> [--count N] [--sort ...]`.
              Wrapper does not parse or reshape.
        ctx:  optional Typer context (see the other slack wrappers for
              the per-profile env plumbing).

    Returns:
        Engine exit code unchanged.

    Raises:
        typer.Exit(127): binary missing / not executable.
        typer.Exit(78): env override points at the wrong basename.
    """
    binary = resolve_slack_search_public_bin()

    basename = os.path.basename(binary)
    if basename != EXPECTED_BIN_BASENAME:
        typer.echo(
            f"mineru slack: {SLACK_SEARCH_PUBLIC_BIN_ENV} must point at a "
            f"{EXPECTED_BIN_BASENAME!r}-named binary; got {binary!r} "
            f"(basename {basename!r}). Refusing to shell out: the Slack "
            f"integration is a strict read-only observer surface; "
            f"pointing at a write-capable shim would breach that policy. "
            f"Default: {DEFAULT_SLACK_SEARCH_PUBLIC_BIN!r}.",
            err=True,
        )
        raise typer.Exit(code=OBSERVER_BASENAME_MISMATCH_EXIT_CODE)

    if not _binary_available(binary):
        typer.echo(
            f"mineru slack: slack-search-public engine not found at {binary!r} "
            f"(set {SLACK_SEARCH_PUBLIC_BIN_ENV} to override, "
            f"default {DEFAULT_SLACK_SEARCH_PUBLIC_BIN!r}).",
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
            f"mineru slack: slack-search-public engine vanished or is not "
            f"executable at {binary!r} (set {SLACK_SEARCH_PUBLIC_BIN_ENV} to "
            f"override, default {DEFAULT_SLACK_SEARCH_PUBLIC_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
