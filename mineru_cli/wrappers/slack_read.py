"""Facade wrapper around the live slack-read engine at $MINERU_HOME/bin/slack-read.

Phase 2 task P2-07 wire-up. `mineru slack read <channel>` shells out
through this module. Mirrors the msearch wrapper shape exactly: single
binary, env-overridable path, stdio pass-through, exit-code propagation,
missing-bin -> 127.

READ-ONLY SLACK OBSERVER (non-negotiable, §2.3 of the capability spec +
SECURITY.md's Slack observer policy):

  The connected Slack workspace is a strict READ-ONLY observer surface
  per SECURITY.md and the `mineru_slack` skill. The `slack-read` binary
  is a thin curl+jq wrapper around Slack's
  `conversations.history` endpoint - a pure read. It never sends,
  reacts, posts, or otherwise mutates workspace state. There is no
  companion write binary in `$MINERU_HOME/bin/`, and the CLI verb layer
  intentionally exposes NO send / post / write verb. That policy is
  enforced at the CLI-surface level (see `verbs/slack.py` docstring +
  the grep-invariant test in `tests/test_slack_verbs.py`); this
  wrapper is the read-only workhorse that surface sits on.

Contract (deliberately narrow, mirrors msearch.py):

  - Read binary path from `MINERU_SLACK_READ_BIN` env, default
    `$MINERU_HOME/bin/slack-read`. Tests override the env to
    point at a fake.
  - Build the argv as `[binary, *args]` where `args` is exactly what
    slack-read expects after its own program name. slack-read's own
    surface is minimal: `slack-read <channel_id> [limit]`. Extras from
    the verb layer flow through opaquely so any future extensions of
    the shell script don't need a wrapper change.
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr
    override. The engine's stdout (the jq-shaped JSON) and stderr (the
    "Error: Slack token not in Keychain" / "Error: User cache not
    found" diagnostics) flow straight through to the caller's fds, so
    `mineru slack read C02RAQRC10T 20` and the direct `slack-read
    C02RAQRC10T 20` produce byte-identical output.
  - Return the engine's exit code unchanged. slack-read uses `set -e`
    plus explicit `exit 1` in the two error branches (missing Keychain
    entry, missing user cache); those propagate unchanged.
  - If the binary is missing, print an actionable error to stderr
    naming the RESOLVED path and raise `typer.Exit(127)` (POSIX
    "command not found"). Never fall back silently to a different
    binary.

Non-goals (strict):

  - No parsing of slack-read stdout/stderr.
  - No reshaping / re-emitting engine output.
  - No filtering of the argv tail (extras pass through opaque).
  - No Slack Web API calls made from Python. If a future verb needs an
    endpoint slack-read doesn't cover, the answer is to extend the
    shell script (or add a sibling shell script + wrapper), not to
    smuggle curl into this file.
  - No token handling. The shell script reads the bot token from
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

# Documented default matches TOOLS.md and the P2-07 spec.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_SLACK_READ_BIN = str(_MINERU_HOME / "bin" / "slack-read")

# Env var name that overrides the default (documented; tests use it).
SLACK_READ_BIN_ENV = "MINERU_SLACK_READ_BIN"

# POSIX "command not found" - the semantically-right exit for a missing
# engine binary. Distinct from typical app errors (usually 1 or 2), so
# callers can special-case it in shell pipelines.
MISSING_BIN_EXIT_CODE = 127

# Read-only-observer-violation exit code. Matches the firewall
# convention (78 = "wrapper refused, not the engine failing") so shell
# pipelines can special-case observer-mode regressions. Used when the
# resolved binary is not slack-read — e.g. someone pointed
# MINERU_SLACK_READ_BIN at a write-capable slack shim.
OBSERVER_BASENAME_MISMATCH_EXIT_CODE = 78

# Basename we expect argv[0] to resolve to. Used both as the runtime
# guard (see run_slack_read) and by tests: any wrapper regression that
# swapped in a differently named binary (e.g. a write-capable `slack`
# binary) would fail this check.
EXPECTED_BIN_BASENAME = "slack-read"


def resolve_slack_read_bin() -> str:
    """Return the slack-read binary path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(SLACK_READ_BIN_ENV)
    if override:
        return override
    return DEFAULT_SLACK_READ_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids two surprise failure modes downstream: a directory or a
      non-executable file would previously pass `os.path.exists` and
      then trip `subprocess.run` with `PermissionError` /
      `IsADirectoryError` instead of our friendly 127 message.
    - Bare name (e.g. `slack-read`): defer to PATH via `shutil.which`,
      which already requires executability.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_slack_read_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Kept as a separate helper so the read-only-observer invariant test
    can assert on argv[0] without needing to mock subprocess. The
    returned list is exactly `[resolved_binary, *args]` - no filtering,
    no rewriting.
    """
    return [resolve_slack_read_bin(), *args]


def _slack_env_for_ctx(ctx: Optional[Any]) -> Optional[dict]:
    """Build the env dict that pins slack-read to the active profile's Keychain.

    `bin/slack-read` reads the bot token via
    `security find-generic-password -a "${SLACK_KEYCHAIN_ACCOUNT:-mineru}"`,
    so per-profile isolation is a matter of exporting the profile's
    `keychain_account` before the subprocess spawns. Also stacks the
    three shared profile-env keys (MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT
    / MINERU_INJECT_QUEUE_DIR) via `profile_env_overlay` — belt-and-
    braces on the step-5 audit's Finding 7 so any indirect shellout
    inside slack-read (curl reading proxy env, `security`) sees the
    profile-scoped MINERU_* keys too. Returns `None` when the ctx is
    missing / not-yet-hydrated (unit tests that build a ctx by hand):
    the caller then omits the `env=` kwarg and the child process
    inherits the ambient env exactly as before.
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


def run_slack_read(
    args: Sequence[str], ctx: Optional[Any] = None
) -> int:
    """Shell out to `slack-read <args...>` with stdio pass-through.

    Args:
        args: argv tail passed to slack-read after the program name.
              Example: `["C02RAQRC10T", "20"]` for "read channel
              C02RAQRC10T's last 20 messages".
        ctx:  optional Typer context. When passed, and the active profile
              carries a `keychain_account`, `SLACK_KEYCHAIN_ACCOUNT` is
              exported into the child env so the shell script's
              `security find-generic-password -a "$SLACK_KEYCHAIN_ACCOUNT"`
              lookup targets the RIGHT profile's Keychain namespace.
              Omit `ctx` to inherit the ambient env unchanged.

    Returns:
        The engine's exit code (0 on success, non-zero on error).
        Callers convert to `typer.Exit(code=rc)` to propagate through
        the CLI. slack-read's two failure branches
        (Keychain-missing-token, user-cache-missing) both `exit 1`.

    Raises:
        typer.Exit(127): the slack-read binary at the resolved path
            does not exist. Stderr already carries an actionable message
            naming the path and the override env var.
    """
    binary = resolve_slack_read_bin()

    # Runtime enforcement of the read-only-observer invariant. Setting
    # MINERU_SLACK_READ_BIN at a write-capable `slack` shim would
    # silently give the CLI the ability to post/react/mutate workspace
    # state. Reject the shellout with the shared 78 (wrapper-refused)
    # exit code so pipelines can distinguish it from POSIX 127.
    basename = os.path.basename(binary)
    if basename != EXPECTED_BIN_BASENAME:
        typer.echo(
            f"mineru slack: {SLACK_READ_BIN_ENV} must point at a "
            f"{EXPECTED_BIN_BASENAME!r}-named binary; got {binary!r} "
            f"(basename {basename!r}). Refusing to shell out: the True "
            f"North workspace is a strict read-only observer surface; "
            f"pointing at a write-capable shim would breach that policy. "
            f"Default: {DEFAULT_SLACK_READ_BIN!r}.",
            err=True,
        )
        raise typer.Exit(code=OBSERVER_BASENAME_MISMATCH_EXIT_CODE)

    if not _binary_available(binary):
        typer.echo(
            f"mineru slack: slack-read engine not found at {binary!r} "
            f"(set {SLACK_READ_BIN_ENV} to override, "
            f"default {DEFAULT_SLACK_READ_BIN!r}).",
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
            f"mineru slack: slack-read engine vanished or is not "
            f"executable at {binary!r} (set {SLACK_READ_BIN_ENV} to "
            f"override, default {DEFAULT_SLACK_READ_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
