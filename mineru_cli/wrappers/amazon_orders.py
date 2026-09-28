"""Facade wrapper around the live amazon-orders CLI at $MINERU_HOME/bin/amazon-orders.

Phase 2 task P2-10 wire-up. Every `mineru amazon ...` verb shells out
through this module. Mirrors the msearch / monarch wrapper shape
exactly: single binary, env-overridable path, stdio pass-through,
exit-code propagation, missing-bin -> 127.

⚠️ SAFETY (during dev / test) ⚠️

  The amazon-orders CLI mixes safe reads (`history`, `order`,
  `invoice`, `transactions`, `check-session`) with interactive /
  stateful writes (`login`, `logout`). READ verbs may be invoked
  live; WRITE verbs (`login`, `logout`) are executed live ONLY when
  the operator explicitly types them. Every WRITE test in
  `tests/test_amazon_verbs.py` patches this wrapper with a recorder
  and asserts the argv only. `subprocess.run` is never actually
  invoked against the live amazon-orders binary for a write during
  dev / test.

  The wrapper itself does no read/write classification -- that's the
  verb layer's job (which verbs it wires up as read-safe vs
  write-only) and the operator's job (whether to type the write verb).
  The wrapper is a pure pass-through.

WHY WE POINT AT `$MINERU_HOME/bin/amazon-orders`:

  The `amazon-orders` command in `$MINERU_HOME/bin/` is a Bash shim
  that activates the `$MINERU_HOME/.venv-amazon` venv and exec's the
  underlying `amazon-orders` Click CLI. This is the same path
  TOOLS.md documents and the same path the operator's `msearch` /
  `gog-firewall` / `monarch` all live at, so operators (and every
  recurring job) find the tool on the same workspace-tools
  directory. `MINERU_AMAZON_ORDERS_BIN` lets tests override it to
  point at a fake.

DEAD SIBLINGS -- DO NOT WRAP:

  Per §7 of the 2026-07-25 capability spec, `$MINERU_HOME/bin/
  artifact-detect` and `$MINERU_HOME/bin/artifact-remove` are DEAD
  (scheduled for removal). This module and its sibling verb file
  MUST NOT create verbs for them; leaving them unwrapped is
  intentional.

Contract (deliberately narrow, mirrors msearch.py / monarch.py):

  - Read binary path from `MINERU_AMAZON_ORDERS_BIN` env, default
    `$MINERU_HOME/bin/amazon-orders`. Tests override the
    env to point at a fake.
  - Build the argv as `[binary, *args]` where `args` is exactly
    what amazon-orders expects after its own program name (e.g.
    `["history", "--year", "2026"]` or `["check-session"]`).
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr
    override. The engine's stdout (order history rendering, invoice
    text) and stderr (auth prompts on `login`, network failures)
    flow straight through to the caller's fds. This preserves
    interactive behavior for `login` (which prompts for username /
    password / OTP on a real TTY) and keeps output byte-identical
    to running the CLI directly.
  - Return the engine's exit code unchanged. Callers convert to
    `typer.Exit(code=rc)` to propagate through the CLI.
  - If the binary is missing, print an actionable error to stderr
    naming the RESOLVED path and raise `typer.Exit(127)` (POSIX
    "command not found"). Never fall back silently to a different
    binary or a re-implementation in Python.

Non-goals (strict):

  - No parsing of amazon-orders stdout/stderr.
  - No reshaping / re-emitting engine output.
  - No filtering of the argv tail (extras pass through opaque).
  - No auth handling. `amazon-orders login` prompts for creds
    interactively and stashes cookies in
    `~/.config/amazonorders/cookies.json`; the wrapper never sees
    the password.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_for_ctx

# Documented default matches TOOLS.md and the P2-10 task description
# (`$MINERU_HOME/bin/amazon-orders`). The shim has a `#!/bin/bash` shebang
# with executable bit, so subprocess.run can invoke it directly without
# a leading interpreter argument.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_AMAZON_ORDERS_BIN = str(_MINERU_HOME / "bin" / "amazon-orders")

# Env var name that overrides the default (documented; tests use it).
AMAZON_ORDERS_BIN_ENV = "MINERU_AMAZON_ORDERS_BIN"

# POSIX "command not found" -- the semantically-right exit for a
# missing engine binary. Distinct from typical app errors (usually 1
# or 2), so callers can special-case it in shell pipelines.
MISSING_BIN_EXIT_CODE = 127

# Basename we expect argv[0] to resolve to. Used by tests as the
# invariant guard: any wrapper regression that swapped in a differently
# named binary would fail this check.
EXPECTED_BIN_BASENAME = "amazon-orders"


# ---------------------------------------------------------------------------
# Per-profile runtime isolation — DEFERRED for amazon-orders.
#
# The gog / Slack / deliver-output wrappers were updated (multi-tenant Arc 2,
# step 3) to accept `ctx=ctx` and thread per-profile credentials through to
# the underlying tool. `run_amazon_orders` was deliberately NOT changed.
# Reason: the underlying `amazon-orders` Python library persists its session
# cookies at the single-user location `~/.config/amazonorders/cookies.json`
# and has no per-account cookie-file switch. A proper per-profile fix needs
# an UPSTREAM PATCH on the amazon-orders repo (an env var or CLI flag to
# select the cookie store). Until then, `mineru amazon ...` is single-account
# by design; a second profile that wants its own Amazon data waits on the
# upstream fix.
# ---------------------------------------------------------------------------


def resolve_amazon_orders_bin() -> str:
    """Return the amazon-orders binary path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(AMAZON_ORDERS_BIN_ENV)
    if override:
        return override
    return DEFAULT_AMAZON_ORDERS_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids `PermissionError` / `IsADirectoryError` traceback leaks
      when the resolved path is a directory or a non-executable file.
    - Bare name (e.g. `amazon-orders`): defer to PATH via `shutil.which`,
      which already requires executability.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_amazon_orders_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Kept as a separate helper so the safety-invariant test can assert
    on argv[0] without needing to mock subprocess. The returned list
    is exactly `[resolved_binary, *args]` -- no filtering, no
    rewriting.
    """
    return [resolve_amazon_orders_bin(), *args]


def run_amazon_orders(args: Sequence[str], ctx: Optional[Any] = None) -> int:
    """Shell out to `amazon-orders <args...>` with stdio pass-through.

    Args:
        args: argv tail passed to amazon-orders after the program name.
              Example: `["history", "--year", "2026"]` for a year pull;
              `["login"]` for an interactive login (which reads password
              from a real TTY).
        ctx:  optional Typer context. When passed with a hydrated profile,
              MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT / MINERU_INJECT_QUEUE_DIR
              are exported into the child env. The underlying
              amazon-orders library still stores cookies at a single
              per-user path (see the deferred-fix comment above); the env
              overlay is defense-in-depth for any sibling script the
              shim eventually consults.

    Returns:
        The engine's exit code (0 on success, non-zero on error).
        Callers convert to `typer.Exit(code=rc)` to propagate through
        the CLI.

    Raises:
        typer.Exit(127): the amazon-orders binary at the resolved path
            does not exist. Stderr already carries an actionable message
            naming the path and the override env var.
    """
    binary = resolve_amazon_orders_bin()

    if not _binary_available(binary):
        typer.echo(
            f"mineru amazon: amazon-orders engine not found at {binary!r} "
            f"(set {AMAZON_ORDERS_BIN_ENV} to override, "
            f"default {DEFAULT_AMAZON_ORDERS_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    cmd: List[str] = [binary, *args]
    env = profile_env_for_ctx(ctx)
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
            f"mineru amazon: amazon-orders engine vanished or is not "
            f"executable at {binary!r} (set {AMAZON_ORDERS_BIN_ENV} to "
            f"override, default {DEFAULT_AMAZON_ORDERS_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
