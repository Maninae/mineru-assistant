"""Facade wrapper around the live slack-refresh-users engine.

Companion to `wrappers/slack_read.py`. Both are single-binary facades
in the msearch shape; each wraps one script in `$MINERU_HOME/bin/` that
already exists.

`slack-refresh-users` refreshes the local `$MINERU_HOME/cache/slack-users.json`
cache that `slack-read` uses to translate raw Slack user-ids into
`real_name` (or falling back to `name`). The cache is stale-tolerant on
the read side (missing rows fall back to the raw id), but new members
won't render as names until this script has run since they joined.

Why a separate wrapper file (not folded into slack_read.py):

  Each wrapper module is one narrow responsibility (see
  `wrappers/__init__.py`), and the two binaries are physically
  separate scripts. Keeping the wrappers in separate files means the
  `resolve_*_bin` / `run_*` API surface is symmetric, and the
  MINERU_SLACK_READ_BIN vs MINERU_SLACK_REFRESH_USERS_BIN env vars
  each point at exactly one binary. A test override that shims one
  never accidentally masks the other.

READ-ONLY SLACK OBSERVER (§2.3, SECURITY.md):

  `slack-refresh-users` calls Slack's `users.list` endpoint - a pure
  read of workspace membership. The output is written to the local
  user cache; no Slack state is mutated. Consistent with the CLI's
  observer-mode policy, this is safe to run live.

Contract (mirrors slack_read.py exactly):

  - Read binary path from `MINERU_SLACK_REFRESH_USERS_BIN` env, default
    `$MINERU_HOME/bin/slack-refresh-users`.
  - Build argv as `[binary, *args]`. `slack-refresh-users` currently
    takes no arguments; the wrapper still accepts and forwards `args`
    verbatim so a future extension of the shell script doesn't need a
    wrapper change.
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr
    override. The script prints `"Fetching Slack users..."` and
    `"Cached N users to <path>"` on stdout; those reach the caller
    untouched.
  - Return the engine's exit code unchanged.
  - Missing binary -> stderr message naming resolved path + env var,
    then `typer.Exit(127)`.

Non-goals (strict):

  - No Slack Web API calls made from Python. If a future verb needs
    an endpoint slack-refresh-users doesn't cover, extend the shell
    script (or add a sibling shell script + wrapper).
  - No token handling. The shell script reads the bot token from
    macOS Keychain via `security`; the wrapper never sees it.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_overlay

# Documented default matches TOOLS.md and the P2-07 spec.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_SLACK_REFRESH_USERS_BIN = str(_MINERU_HOME / "bin" / "slack-refresh-users")

# Env var name that overrides the default (documented; tests use it).
SLACK_REFRESH_USERS_BIN_ENV = "MINERU_SLACK_REFRESH_USERS_BIN"

# POSIX "command not found" - matches the other wrappers.
MISSING_BIN_EXIT_CODE = 127

# Basename we expect argv[0] to resolve to. Used by tests as the
# invariant guard: a wrapper accidentally rewired to a different name
# would fail this check.
EXPECTED_BIN_BASENAME = "slack-refresh-users"


def resolve_slack_refresh_users_bin() -> str:
    """Return the slack-refresh-users binary path: env override or documented default.

    Empty-string env var is treated as unset.
    """
    override = os.environ.get(SLACK_REFRESH_USERS_BIN_ENV)
    if override:
        return override
    return DEFAULT_SLACK_REFRESH_USERS_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids `PermissionError` / `IsADirectoryError` traceback leaks
      when the resolved path is a directory or a non-executable file.
    - Bare name: defer to PATH via `shutil.which`, which already
      requires executability.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_slack_refresh_users_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Kept as a separate helper so tests can assert on argv[0] without
    needing to mock subprocess. Returned list is exactly
    `[resolved_binary, *args]` - no filtering, no rewriting.
    """
    return [resolve_slack_refresh_users_bin(), *args]


def _slack_env_for_ctx(ctx: Optional[Any]) -> Optional[dict]:
    """Build the env dict that pins slack-refresh-users to the active profile.

    Mirror of `slack_read._slack_env_for_ctx`: exports
    `SLACK_KEYCHAIN_ACCOUNT` from the active profile's `keychain_account`
    so the shell script's `security find-generic-password -a
    "$SLACK_KEYCHAIN_ACCOUNT"` lookup targets the right namespace on a
    multi-tenant Mac. Also stacks the three shared profile-env keys
    (MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT / MINERU_INJECT_QUEUE_DIR)
    via `profile_env_overlay` — belt-and-braces on the step-5 audit's
    Finding 7.
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


def run_slack_refresh_users(
    args: Sequence[str], ctx: Optional[Any] = None
) -> int:
    """Shell out to `slack-refresh-users <args...>` with stdio pass-through.

    Args:
        args: argv tail passed to slack-refresh-users after the program
              name. The current shell script accepts none; extras
              pass through opaquely for forward compatibility.
        ctx:  optional Typer context. When passed, and the active profile
              carries a `keychain_account`, `SLACK_KEYCHAIN_ACCOUNT` is
              exported into the child env so the Keychain lookup targets
              the right per-profile namespace.

    Returns:
        The engine's exit code (0 on success, non-zero on error).
        Callers convert to `typer.Exit(code=rc)`.

    Raises:
        typer.Exit(127): the slack-refresh-users binary at the resolved
            path does not exist. Stderr already carries an actionable
            message naming the path and the override env var.
    """
    binary = resolve_slack_refresh_users_bin()

    if not _binary_available(binary):
        typer.echo(
            f"mineru slack: slack-refresh-users engine not found at {binary!r} "
            f"(set {SLACK_REFRESH_USERS_BIN_ENV} to override, "
            f"default {DEFAULT_SLACK_REFRESH_USERS_BIN!r}).",
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
        # Race window OR a permission / directory anomaly the availability
        # check couldn't foresee. Cover the whole OSError family
        # (FileNotFoundError, PermissionError, IsADirectoryError) with one
        # friendly message instead of leaking a raw Python traceback.
        typer.echo(
            f"mineru slack: slack-refresh-users engine vanished or is not "
            f"executable at {binary!r} (set {SLACK_REFRESH_USERS_BIN_ENV} "
            f"to override, default {DEFAULT_SLACK_REFRESH_USERS_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
