"""Facade wrapper around the live brevity CLI at $MINERU_HOME/bin/brevity.

Phase 2 task P2-10 wire-up. `mineru brevity <url-or-file>` shells out
through this module to the Smart Brevity summarizer. Mirrors the
msearch / monarch wrapper shape exactly: single binary, env-overridable
path, stdio pass-through, exit-code propagation, missing-bin -> 127.

READ-ONLY:

  brevity is entirely read-only against external URLs and local files
  -- it fetches content (via the `summarize` CLI, which in turn drives
  Firecrawl / yt-dlp / etc.) and produces an Axios-style Smart Brevity
  summary on stdout. It never writes to any external service, never
  posts, never sends, never emails. Safe to invoke live during dev /
  test.

WHY WE POINT AT `$MINERU_HOME/bin/brevity`:

  `$MINERU_HOME/bin/brevity` is a symlink to
  `~/.claude/skills/smart-brevity/brevity`, a Bash script that shells
  out to the `summarize` CLI with a Smart Brevity prompt template
  (`brief.md` for the default ~1-min read, `extended.md` for the
  detailed ~3-5-min read). This is the same path TOOLS.md documents
  and the same path the operator's `msearch` / `gog-firewall` / `monarch` /
  `amazon-orders` all live at, so operators (and every recurring
  job) find the tool on the same workspace-tools directory.
  `MINERU_BREVITY_BIN` lets tests override it to point at a fake.

DEAD SIBLINGS -- DO NOT WRAP:

  Per §7 of the 2026-07-25 capability spec, `$MINERU_HOME/bin/
  artifact-detect` and `$MINERU_HOME/bin/artifact-remove` are DEAD
  (scheduled for removal). This module and its sibling verb file
  MUST NOT create verbs for them; leaving them unwrapped is
  intentional.

Contract (deliberately narrow, mirrors msearch.py / monarch.py /
amazon_orders.py):

  - Read binary path from `MINERU_BREVITY_BIN` env, default
    `$MINERU_HOME/bin/brevity`. Tests override the env to
    point at a fake.
  - Build the argv as `[binary, *args]` where `args` is exactly what
    brevity expects after its own program name. The live script's
    positional shape is `<url-or-file> [--extended]`; both flow
    through opaquely.
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr
    override. The engine's stdout (the Smart Brevity summary itself)
    and stderr (the `Error: Could not extract content from input.` /
    `Error: summarize CLI not found.` diagnostics) flow straight
    through to the caller's fds. This preserves the operator's
    ability to redirect the summary to a file, pipe it, or diagnose
    extraction failures without wrapper-level filtering.
  - Return the engine's exit code unchanged. brevity uses `set -e`
    and `error()` -> `exit 1` on every failure branch; success
    returns 0 (the summarize invocation's own rc); both propagate
    unchanged.
  - If the binary is missing, print an actionable error to stderr
    naming the RESOLVED path and raise `typer.Exit(127)` (POSIX
    "command not found"). Never fall back silently to a different
    binary or a re-implementation in Python.

Non-goals (strict):

  - No parsing of brevity stdout/stderr.
  - No reshaping / re-emitting engine output (the Smart Brevity
    summary text is what the caller gets, byte-for-byte).
  - No filtering of the argv tail (extras pass through opaque).
  - No prompt-template selection here (the `--extended` flag flows
    through as an opaque arg, and the Bash script picks the right
    prompt file).
  - No summarize / Firecrawl / yt-dlp handling. Those live inside
    the summarize CLI that brevity delegates to.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_for_ctx

# Documented default matches TOOLS.md (`bin/brevity` -> ...smart-brevity
# skill). The symlink points at a Bash script with `#!/bin/bash` shebang
# and executable bit, so subprocess.run can invoke it directly without
# a leading interpreter argument.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_BREVITY_BIN = str(_MINERU_HOME / "bin" / "brevity")

# Env var name that overrides the default (documented; tests use it).
BREVITY_BIN_ENV = "MINERU_BREVITY_BIN"

# POSIX "command not found" -- the semantically-right exit for a
# missing engine binary. Distinct from typical app errors (usually 1
# or 2), so callers can special-case it in shell pipelines.
MISSING_BIN_EXIT_CODE = 127

# Basename we expect argv[0] to resolve to. Used by tests as the
# invariant guard: any wrapper regression that swapped in a differently
# named binary would fail this check.
EXPECTED_BIN_BASENAME = "brevity"


def resolve_brevity_bin() -> str:
    """Return the brevity binary path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(BREVITY_BIN_ENV)
    if override:
        return override
    return DEFAULT_BREVITY_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids `PermissionError` / `IsADirectoryError` traceback leaks
      when the resolved path is a directory or a non-executable file.
    - Bare name (e.g. `brevity`): defer to PATH via `shutil.which`,
      which already requires executability.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_brevity_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Kept as a separate helper so the safety-invariant test can assert
    on argv[0] without needing to mock subprocess. The returned list
    is exactly `[resolved_binary, *args]` -- no filtering, no
    rewriting.
    """
    return [resolve_brevity_bin(), *args]


def run_brevity(args: Sequence[str], ctx: Optional[Any] = None) -> int:
    """Shell out to `brevity <args...>` with stdio pass-through.

    Args:
        args: argv tail passed to brevity after the program name.
              Example: `["https://example.com/article"]` for a default
              brief summary; `["./notes.pdf", "--extended"]` for the
              ~3-5-min extended form.
        ctx:  optional Typer context. When passed with a hydrated profile,
              the three profile-scoped env keys (MINERU_HOME /
              MINERU_KEYCHAIN_ACCOUNT / MINERU_INJECT_QUEUE_DIR) are
              exported so a second profile's brief lands in THAT profile's
              cache (step-5 audit, Finding 7). Omit `ctx` for back-compat.

    Returns:
        The engine's exit code (0 on success, non-zero on error).
        Callers convert to `typer.Exit(code=rc)` to propagate through
        the CLI.

    Raises:
        typer.Exit(127): the brevity binary at the resolved path does
            not exist. Stderr already carries an actionable message
            naming the path and the override env var.
    """
    binary = resolve_brevity_bin()

    if not _binary_available(binary):
        typer.echo(
            f"mineru brevity: brevity engine not found at {binary!r} "
            f"(set {BREVITY_BIN_ENV} to override, "
            f"default {DEFAULT_BREVITY_BIN!r}).",
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
            f"mineru brevity: brevity engine vanished or is not executable "
            f"at {binary!r} (set {BREVITY_BIN_ENV} to override, "
            f"default {DEFAULT_BREVITY_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
