"""Facade wrapper around the live monarch CLI at $MINERU_HOME/bin/monarch.

Phase 2 task P2-09 wire-up. Every `mineru finance ...` verb shells out
through this module to the Monarch Money CLI. Mirrors the msearch /
imsg wrapper shape exactly: single binary, env-overridable path, stdio
pass-through, exit-code propagation, missing-bin -> 127.

⚠️ SAFETY (during dev / test) ⚠️

  The Monarch CLI mixes safe reads (`accounts list`, `transactions
  list`, `budgets list`, `cashflow summary`, ...) with real writes
  against Monarch's servers (`accounts create/update/delete`,
  `transactions create/update/delete/splits`, `budgets set`,
  `categories create/delete`, `tags create/set`, `auth login/logout`).

  READ verbs may be invoked live; WRITE verbs are executed live ONLY
  when the operator explicitly types them. Every test in
  `tests/test_finance_verbs.py` patches the wrapper function with a
  recorder and asserts the argv that WOULD be sent to the underlying
  CLI. `subprocess.run` is never actually invoked against the live
  monarch binary during dev / test.

  The wrapper itself does no read/write classification -- that's the
  verb layer's job (which verbs it wires up as read-safe vs
  write-only) and the operator's job (whether to type the write verb).
  The wrapper is a pure pass-through.

WHY WE POINT AT `$MINERU_HOME/bin/monarch`:

  The `monarch` command in `$MINERU_HOME/bin/` is a Python venv shim
  (script text executable pointing at the monarch-money-cli venv's
  python). This is the same path `TOOLS.md` documents and the same
  path the operator's `msearch` / `imsg-firewall` / `gog-firewall` all live
  at, so operators (and every recurring job) find monarch on the same
  workspace-tools directory. `MINERU_MONARCH_BIN` lets tests override
  it to point at a fake.

Contract (deliberately narrow, mirrors msearch.py / imsg.py):

  - Read binary path from `MINERU_MONARCH_BIN` env, default
    `$MINERU_HOME/bin/monarch`. Tests override the env to
    point at a fake.
  - Build the argv as `[binary, *args]` where `args` is exactly what
    monarch expects after its own program name (e.g. `["transactions",
    "list", "--limit", "10", "--format", "json"]`).
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr
    override. The engine's stdout (JSON / table renderings) and
    stderr (auth errors, MFA prompts on `auth login`, network
    failures) flow straight through to the caller's fds. This
    preserves interactive behavior for `auth login` (which needs a
    real TTY for MFA + trusted-device prompts) and keeps JSON output
    byte-identical to running the CLI directly.
  - Return the engine's exit code unchanged. Callers convert to
    `typer.Exit(code=rc)` to propagate through the CLI.
  - If the binary is missing, print an actionable error to stderr
    naming the RESOLVED path and raise `typer.Exit(127)` (POSIX
    "command not found"). Never fall back silently to a different
    binary or a re-implementation in Python.

Non-goals (strict):

  - No parsing of monarch stdout/stderr.
  - No reshaping / re-emitting engine output (JSON stays JSON, table
    stays table).
  - No filtering of the argv tail (extras pass through opaque).
  - No auth handling. `monarch auth login` reads its own credentials
    interactively and stashes a session token in the monarch CLI's
    own credential store; the wrapper never sees the password.
  - No injection of `--format json` (leave the format choice to the
    caller / underlying CLI default, which is already `json` for the
    subverbs that support it).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_for_ctx

# Documented default matches TOOLS.md ("bin/ ... monarch"). The shim is
# a Python script (see `file` output on $MINERU_HOME/bin/monarch) with an
# executable bit + interpreter line, so subprocess.run can invoke it
# directly without a leading interpreter argument.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_MONARCH_BIN = str(_MINERU_HOME / "bin" / "monarch")

# Env var name that overrides the default (documented; tests use it).
MONARCH_BIN_ENV = "MINERU_MONARCH_BIN"

# POSIX "command not found" -- the semantically-right exit for a
# missing engine binary. Distinct from typical app errors (usually 1
# or 2), so callers can special-case it in shell pipelines.
MISSING_BIN_EXIT_CODE = 127

# Basename we expect argv[0] to resolve to. Used by tests as the
# invariant guard: any wrapper regression that swapped in a differently
# named binary would fail this check.
EXPECTED_BIN_BASENAME = "monarch"


# ---------------------------------------------------------------------------
# Per-profile runtime isolation — DEFERRED for monarch.
#
# The gog / Slack / deliver-output wrappers were updated (multi-tenant Arc 2,
# step 3) to accept `ctx=ctx` and thread per-profile Keychain account /
# `--account=<email>` through to the underlying tool. `run_monarch` was
# deliberately NOT changed. Reason: the underlying `monarch` CLI persists its
# session token in the single-user location `~/.monarch/session.json` and has
# no `--account` / `--session-file` flag we can forward. A proper per-profile
# isolation needs an UPSTREAM PATCH on the monarch-money-cli repo (an env var
# or flag to select the session file), which sits outside this codebase.
# Until then, `mineru finance ...` is single-account by design; a second
# profile that wants its own finance data waits on the upstream fix.
# ---------------------------------------------------------------------------


def resolve_monarch_bin() -> str:
    """Return the monarch binary path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(MONARCH_BIN_ENV)
    if override:
        return override
    return DEFAULT_MONARCH_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids `PermissionError` / `IsADirectoryError` traceback leaks
      when the resolved path is a directory or a non-executable file.
    - Bare name (e.g. `monarch`): defer to PATH via `shutil.which`,
      which already requires executability.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_monarch_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Kept as a separate helper so the safety-invariant test can assert
    on argv[0] without needing to mock subprocess. The returned list
    is exactly `[resolved_binary, *args]` -- no filtering, no
    rewriting.
    """
    return [resolve_monarch_bin(), *args]


def run_monarch(args: Sequence[str], ctx: Optional[Any] = None) -> int:
    """Shell out to `monarch <args...>` with stdio pass-through.

    Args:
        args: argv tail passed to monarch after the program name.
              Example: `["transactions", "list", "--limit", "10"]`
              for a transaction pull; `["accounts", "delete", "abc",
              "--yes"]` for an irreversible write.
        ctx:  optional Typer context. When passed with a hydrated profile,
              MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT / MINERU_INJECT_QUEUE_DIR
              are exported so a second profile's `monarch` call reads THAT
              profile's cached Monarch session cookie / token (step-5
              audit, Finding 7). Omit `ctx` for legacy back-compat.

    Returns:
        The engine's exit code (0 on success, non-zero on error).
        Callers convert to `typer.Exit(code=rc)` to propagate through
        the CLI.

    Raises:
        typer.Exit(127): the monarch binary at the resolved path does
            not exist. Stderr already carries an actionable message
            naming the path and the override env var.
    """
    binary = resolve_monarch_bin()

    if not _binary_available(binary):
        typer.echo(
            f"mineru finance: monarch engine not found at {binary!r} "
            f"(set {MONARCH_BIN_ENV} to override, "
            f"default {DEFAULT_MONARCH_BIN!r}).",
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
            f"mineru finance: monarch engine vanished or is not executable "
            f"at {binary!r} (set {MONARCH_BIN_ENV} to override, "
            f"default {DEFAULT_MONARCH_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
