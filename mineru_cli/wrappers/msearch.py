"""Facade wrapper around the live msearch engine at $MINERU_HOME/bin/msearch.

Foundation task F4 wire-up. Everything the `mineru memory search / tags / query`
verbs need to shell out lives here so the verb file stays a thin router.

Contract (deliberately narrow):

  - Read binary path from `MINERU_MSEARCH_BIN` env, default
    `$MINERU_HOME/bin/msearch`. Tests override the env to point at
    a fake.
  - Build the argv as `[binary, *args]` where `args` is exactly what
    msearch expects after its own program name (e.g. `["keyword", "robin",
    "--pretty"]`).
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr override.
    The engine's stdout/stderr flow straight through to the caller's fds,
    so `mineru memory search river --pretty` and the direct
    `msearch keyword river --pretty` produce byte-identical output.
  - Return the engine's exit code unchanged. Callers convert to typer.Exit.
  - If the binary is missing, print an actionable error to stderr naming
    the RESOLVED path and raise `typer.Exit(127)` (POSIX "command not
    found"). Never fall back silently to a different binary.

Non-goals (strict):

  - No parsing of msearch stdout/stderr.
  - No reshaping / re-emitting engine output.
  - No filtering of the argv tail (extras pass through opaque).
  - No writes to $MINERU_HOME state. The wrapper only shells out to msearch;
    if msearch touches its own index cache under $MINERU_HOME/, that is the
    engine's business, not ours.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_for_ctx

# Documented default matches TOOLS.md and the F1 skeleton comment.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_MSEARCH_BIN = str(_MINERU_HOME / "bin" / "msearch")

# Env var name that overrides the default (documented; tests use it).
MSEARCH_BIN_ENV = "MINERU_MSEARCH_BIN"

# POSIX "command not found" — the semantically-right exit for a missing
# engine binary. Distinct from typical app errors (usually 1 or 2), so
# callers can special-case it in shell pipelines.
MISSING_BIN_EXIT_CODE = 127


def resolve_msearch_bin() -> str:
    """Return the msearch binary path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(MSEARCH_BIN_ENV)
    if override:
        return override
    return DEFAULT_MSEARCH_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids `PermissionError` / `IsADirectoryError` traceback leaks
      when the resolved path is a directory or a non-executable file.
    - Bare name (e.g. `msearch`): defer to PATH via `shutil.which`,
      which already requires executability.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def run_msearch(args: Sequence[str], ctx: Optional[Any] = None) -> int:
    """Shell out to `msearch <args...>` with stdio pass-through.

    Args:
        args: argv tail passed to msearch after the program name. Example:
              `["keyword", "robin", "--pretty"]`.
        ctx:  optional Typer context. When passed with a hydrated profile,
              MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT / MINERU_INJECT_QUEUE_DIR
              are exported into the child env so a second profile's msearch
              targets THAT profile's memory tree, not the owner's default
              (step-5 audit, Finding 7). Omit `ctx` to inherit the ambient
              env exactly (back-compat with legacy callers).

    Returns:
        The engine's exit code (0 on success, non-zero on error). Callers
        convert to `typer.Exit(code=rc)` to propagate through the CLI.

    Raises:
        typer.Exit(127): the msearch binary at the resolved path does not
            exist. Stderr already carries an actionable message naming the
            path and the override env var.
    """
    binary = resolve_msearch_bin()

    if not _binary_available(binary):
        typer.echo(
            f"mineru memory: msearch engine not found at {binary!r} "
            f"(set {MSEARCH_BIN_ENV} to override, "
            f"default {DEFAULT_MSEARCH_BIN!r}).",
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
            f"mineru memory: msearch engine vanished or is not executable "
            f"at {binary!r} (set {MSEARCH_BIN_ENV} to override, "
            f"default {DEFAULT_MSEARCH_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
