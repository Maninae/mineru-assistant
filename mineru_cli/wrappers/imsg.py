"""Outbound-only wrapper around the raw `imsg` binary at /opt/homebrew/bin/imsg.

Phase 2 task P2-06 wire-up. Every iMessage WRITE verb (send / react /
edit / unsend / delete / mark-read / typing / notify / chat lifecycle /
launch / rpc) shells out through this module. Reads live in
`mineru_cli.wrappers.imsg_firewall` and route through the injection
firewall.

WHY A SEPARATE FILE FROM `imsg_firewall.py`:

  The read/write boundary is grep-visible on purpose. Any
  `subprocess.run` call in this file uses `imsg` (Homebrew binary,
  no firewall); any `subprocess.run` call in `imsg_firewall.py` uses
  `imsg-firewall`. The two responsibilities never cross. A reviewer
  can `grep -n 'subprocess.run' wrappers/imsg*.py` and see instantly
  which side of the firewall boundary each subprocess call lives on.

WHY OUTBOUND DOES NOT NEED THE FIREWALL (per SECURITY.md § "Never Trust
External Content"):

  The injection firewall exists to screen INBOUND content on its way
  into the LLM context - a hostile text from a stranger cannot smuggle
  a prompt into an agent read via `imsg-firewall`. Outbound sends are
  the opposite direction: bytes the LLM already produced and the user
  approved, flowing OUT to Messages. There is no external-content
  channel to screen, so `imsg send`, `imsg react`, `imsg edit`, etc.
  route through this direct wrapper. TOOLS.md's "Sending: Use `imsg`
  CLI directly - skip the firewall (outbound has no injection risk)"
  codifies this.

  Every call site (i.e. every verb in `mineru_cli.verbs.imessage`
  that lands here) is a state-changing outbound action. Per §0 of the
  capability spec + AGENTS.md's "External Comms" rule, those actions
  are draft-first: the mineru CLI never fires one without the operator's
  explicit approval. During development and code review this wrapper
  is exercised via mocked tests ONLY - no test or dev-loop invocation
  ever actually contacts Messages.

WHY WE POINT AT `/opt/homebrew/bin/imsg` DIRECTLY (not `$MINERU_HOME/bin/imsg`):

  `imsg` is a Homebrew-installed Mach-O binary (verified `which imsg` ->
  `/opt/homebrew/bin/imsg`). `$MINERU_HOME/bin/` holds only wrappers
  (`imsg-firewall`, `imsg-named`); there is no `imsg` binary there. A
  path like `$MINERU_HOME/bin/imsg` would fail with "No such file or
  directory". `MINERU_IMSG_BIN` lets tests override this.

Contract (mirrors gog_firewall.py):

  - Read binary path from `MINERU_IMSG_BIN` env, default
    `/opt/homebrew/bin/imsg`. Tests override the env to point at a fake.
  - Build the argv as `[binary, *args]` where `args` is exactly what
    imsg expects after its own program name (e.g. `["send", "--to",
    "+14155551212", "--text", "hi"]`).
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr
    override. The engine's stdout/stderr flow straight through so
    error diagnostics reach the caller untouched.
  - Return the engine's exit code unchanged. Callers convert to
    `typer.Exit`.
  - If the binary is missing, print an actionable error to stderr
    naming the RESOLVED path and raise `typer.Exit(127)`.

Non-goals (strict):

  - No parsing of imsg stdout/stderr.
  - No reshaping / re-emitting engine output.
  - No filtering of the argv tail (extras pass through opaque).
  - No fallback to `imsg-firewall` or `imsg-named`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_for_ctx

# Homebrew-installed `imsg` Mach-O binary (verified via `which imsg`).
DEFAULT_IMSG_BIN = "/opt/homebrew/bin/imsg"

# Env var name that overrides the default (documented; tests use it).
IMSG_BIN_ENV = "MINERU_IMSG_BIN"

# POSIX "command not found" - matches the firewall wrapper.
MISSING_BIN_EXIT_CODE = 127

# Basename we expect argv[0] to resolve to. Note this is `imsg` and NOT
# `imsg-firewall`: this wrapper is deliberately the write-side path.
EXPECTED_BIN_BASENAME = "imsg"


def resolve_imsg_bin() -> str:
    """Return the imsg binary path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(IMSG_BIN_ENV)
    if override:
        return override
    return DEFAULT_IMSG_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids `PermissionError` / `IsADirectoryError` traceback leaks
      when the resolved path is a directory or a non-executable file.
    - Bare name (e.g. `imsg`): defer to PATH via `shutil.which`, which
      already requires executability.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_imsg_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Kept as a separate helper so tests can assert on argv[0] without
    needing to mock subprocess. The returned list is exactly
    `[resolved_binary, *args]` - no filtering, no rewriting.
    """
    return [resolve_imsg_bin(), *args]


def run_imsg(args: Sequence[str], ctx: Optional[Any] = None) -> int:
    """Shell out to `imsg <args...>` with stdio pass-through.

    Outbound only. Reads MUST go through
    `mineru_cli.wrappers.imsg_firewall.run_imsg_firewall` instead.

    Args:
        args: argv tail passed to imsg after the program name. Example:
              `["send", "--to", "+14155551212", "--text", "hi"]`.
        ctx:  optional Typer context. When passed with a hydrated profile,
              the three profile-scoped env keys (MINERU_HOME /
              MINERU_KEYCHAIN_ACCOUNT / MINERU_INJECT_QUEUE_DIR) are
              exported into the child env — belt-and-braces on the
              step-5 audit's Finding 7. Omit `ctx` to inherit the
              ambient env unchanged (back-compat).

    Returns:
        The engine's exit code (0 on success, non-zero on error).
        Callers convert to `typer.Exit(code=rc)` to propagate through
        the CLI.

    Raises:
        typer.Exit(127): the imsg binary at the resolved path does not
            exist. Stderr already carries an actionable message naming
            the path and the override env var.
    """
    binary = resolve_imsg_bin()

    if not _binary_available(binary):
        typer.echo(
            f"mineru imessage: imsg engine not found at {binary!r} "
            f"(set {IMSG_BIN_ENV} to override, "
            f"default {DEFAULT_IMSG_BIN!r}).",
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
            f"mineru imessage: imsg engine vanished or is not executable at "
            f"{binary!r} (set {IMSG_BIN_ENV} to override, "
            f"default {DEFAULT_IMSG_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
