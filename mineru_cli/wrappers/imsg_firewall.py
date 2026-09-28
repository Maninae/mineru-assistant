"""Facade wrapper around the live imsg-firewall engine at $MINERU_HOME/bin/imsg-firewall.

Phase 2 task P2-06 wire-up. Every firewall-preserving iMessage READ verb
(chats / history / group / search / watch / whois / nickname / status)
shells out through this module.

INVARIANT - FIREWALL PRESERVATION (§0 + §3.1 of the capability spec):

  The raw `imsg` binary at `/opt/homebrew/bin/imsg` is NEVER called by
  any read verb in this codebase. The `imsg-named` contact-resolving
  wrapper at `$MINERU_HOME/bin/imsg-named` is ALSO never called directly by
  any read verb - TOOLS.md explicitly marks `imsg-named` as FORBIDDEN
  for reads (the symlink is kept for the operator's manual override, but agents
  must never invoke it). Only the firewalled wrapper (`imsg-firewall`)
  is invoked for reads, so external message content is always screened
  for prompt-injection before it reaches the LLM context.

  This is enforced two ways:
    1. This wrapper is the SINGLE argv[0] source of truth for iMessage
       reads. Every verb that reads message content routes through
       `run_imsg_firewall(...)`; none construct an `imsg` subprocess
       themselves.
    2. `tests/test_imsg_firewall_wrapper.py::test_argv0_resolves_to_imsg_firewall`
       asserts the built argv[0]'s basename is `imsg-firewall`, so a
       regression that swapped the binary for bare `imsg` OR the
       forbidden `imsg-named` path would fail the test suite
       immediately.

  Outbound sends live in a separate module (`mineru_cli.wrappers.imsg`)
  because SECURITY.md's per-unit screening only applies to inbound
  content (things reaching the LLM context). Outbound `imsg send` calls
  are already user-authored, so they route around the firewall by
  design. The write-side wrapper is a physically distinct file so the
  read/write boundary is grep-visible: any subprocess.run call in this
  file uses `imsg-firewall`, any subprocess.run call in `imsg.py` uses
  `imsg` - never crossed.

Contract (deliberately narrow, mirrors gog_firewall.py exactly):

  - Read binary path from `MINERU_IMSG_FIREWALL_BIN` env, default
    `$MINERU_HOME/bin/imsg-firewall`. Tests override the env to
    point at a fake.
  - Build the argv as `[binary, *args]` where `args` is exactly what
    imsg-firewall expects after its own program name (e.g. `["chats",
    "--limit", "10", "--json"]` or `["history", "--chat-id", "1",
    "--json"]`).
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr override.
    The engine's stdout/stderr flow straight through to the caller's fds,
    so `mineru imessage chats --limit 10` and the direct
    `imsg-firewall chats --limit 10` produce byte-identical output.
    Critically, this means the firewall's stderr notices (`redacted N of
    M units`) reach the user untouched.
  - Return the engine's exit code unchanged. Callers convert to
    `typer.Exit`. Firewall convention: 0 delivered, 77 all-blocked,
    78 firewall error - none of them get remapped here.
  - If the binary is missing, print an actionable error to stderr naming
    the RESOLVED path and raise `typer.Exit(127)`. Never fall back to a
    different binary; a missing firewall is a security-relevant failure,
    not a "use the raw tool instead" moment.

Non-goals (strict):

  - No parsing of imsg-firewall stdout/stderr.
  - No reshaping / re-emitting engine output.
  - No filtering of the argv tail (extras pass through opaque).
  - No fallback to `/opt/homebrew/bin/imsg` or `$MINERU_HOME/bin/imsg-named`.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_for_ctx

# Documented default matches TOOLS.md and the P2-06 spec.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_IMSG_FIREWALL_BIN = str(_MINERU_HOME / "bin" / "imsg-firewall")

# Env var name that overrides the default (documented; tests use it).
IMSG_FIREWALL_BIN_ENV = "MINERU_IMSG_FIREWALL_BIN"

# POSIX "command not found" - the semantically-right exit for a missing
# engine binary. Distinct from typical app errors (usually 1 or 2), so
# callers can special-case it in shell pipelines.
MISSING_BIN_EXIT_CODE = 127

# Firewall-error exit code (matches imsg-firewall's own convention). Used
# when a runtime guard here refuses to shell out because the resolved
# binary is not an imsg-firewall — for example, MINERU_IMSG_FIREWALL_BIN
# points at raw `imsg` or the FORBIDDEN `imsg-named` path.
FIREWALL_BASENAME_MISMATCH_EXIT_CODE = 78

# Basename we expect argv[0] to resolve to. Used both as the runtime
# guard (see run_imsg_firewall) and by tests: a wrapper accidentally
# rewired to bare `imsg` or `imsg-named` would fail this check.
EXPECTED_BIN_BASENAME = "imsg-firewall"


def resolve_imsg_firewall_bin() -> str:
    """Return the imsg-firewall binary path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(IMSG_FIREWALL_BIN_ENV)
    if override:
        return override
    return DEFAULT_IMSG_FIREWALL_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids two surprise failure modes downstream: (1) a directory
      passes `os.path.exists` and then `subprocess.run` raises
      `PermissionError` / `IsADirectoryError` instead of our friendly
      127 message; (2) a non-executable file passes `os.path.exists`
      and then `subprocess.run` raises `PermissionError`.
    - Bare name (e.g. `imsg-firewall`): defer to PATH via `shutil.which`,
      which already requires executability.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_imsg_firewall_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Kept as a separate helper so the invariant test can assert on argv[0]
    without needing to mock subprocess. The returned list is exactly
    `[resolved_binary, *args]` - no filtering, no rewriting.
    """
    return [resolve_imsg_firewall_bin(), *args]


def run_imsg_firewall(args: Sequence[str], ctx: Optional[Any] = None) -> int:
    """Shell out to `imsg-firewall <args...>` with stdio pass-through.

    Args:
        args: argv tail passed to imsg-firewall after the program name.
              Example: `["chats", "--limit", "10", "--json"]`.
        ctx:  optional Typer context. When passed with a hydrated profile,
              MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT / MINERU_INJECT_QUEUE_DIR
              are exported into the child env — belt-and-braces on the
              step-5 audit's Finding 7. Omit `ctx` to inherit the ambient
              env (back-compat).

    Returns:
        The engine's exit code (0 delivered, 77 all-blocked, 78 firewall
        error, or any other engine-defined code). Callers convert to
        `typer.Exit(code=rc)` to propagate through the CLI. All three of
        the firewall's documented exit codes propagate unchanged.

    Raises:
        typer.Exit(127): the imsg-firewall binary at the resolved path
            does not exist. Stderr already carries an actionable message
            naming the path and the override env var.
    """
    binary = resolve_imsg_firewall_bin()

    # Runtime enforcement of the firewall-preservation invariant.
    # Setting MINERU_IMSG_FIREWALL_BIN=/opt/homebrew/bin/imsg (or the
    # FORBIDDEN-for-reads `$MINERU_HOME/bin/imsg-named`) would previously
    # exec that binary and bypass every per-unit prompt-injection screen.
    # Reject the shellout with the firewall's own 78 (firewall error).
    basename = os.path.basename(binary)
    if basename != EXPECTED_BIN_BASENAME:
        typer.echo(
            f"mineru imessage: {IMSG_FIREWALL_BIN_ENV} must point at a "
            f"{EXPECTED_BIN_BASENAME!r}-named binary; got {binary!r} "
            f"(basename {basename!r}). Refusing to shell out: pointing at "
            f"raw `imsg` or the forbidden `imsg-named` path would bypass "
            f"prompt-injection screening. Default: "
            f"{DEFAULT_IMSG_FIREWALL_BIN!r}.",
            err=True,
        )
        raise typer.Exit(code=FIREWALL_BASENAME_MISMATCH_EXIT_CODE)

    if not _binary_available(binary):
        typer.echo(
            f"mineru imessage: imsg-firewall engine not found at {binary!r} "
            f"(set {IMSG_FIREWALL_BIN_ENV} to override, "
            f"default {DEFAULT_IMSG_FIREWALL_BIN!r}).",
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
            f"mineru imessage: imsg-firewall engine vanished or is not "
            f"executable at {binary!r} (set {IMSG_FIREWALL_BIN_ENV} to "
            f"override, default {DEFAULT_IMSG_FIREWALL_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
