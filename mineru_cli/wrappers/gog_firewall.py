"""Facade wrapper around the live gog-firewall engine at $MINERU_HOME/bin/gog-firewall.

Foundation task F5 wire-up. All firewall-preserving Gmail / Calendar / other
Google Workspace READ verbs shell out through this module.

INVARIANT — FIREWALL PRESERVATION (§0 + §3.1 of the capability spec):

  The raw `gog` binary at `/opt/homebrew/bin/gog` is NEVER called by any
  read verb in this codebase. Only the firewalled wrapper
  (`gog-firewall`) is invoked, so external content is always screened for
  prompt-injection before it reaches the LLM context.

  This is enforced two ways:
    1. This wrapper is the SINGLE argv[0] source of truth for Gmail reads.
       Every verb that reads mail routes through `run_gog_firewall(...)`;
       none construct a `gog` subprocess themselves.
    2. `tests/test_gmail_wrapper.py::test_argv0_resolves_to_gog_firewall`
       asserts the built argv[0]'s basename is `gog-firewall`, so a
       regression that swapped the binary for bare `gog` would fail the
       test suite immediately.

  Explicitly out of scope for the foundation increment:
    - `--raw` (does NOT actually bypass screening; see GMAIL.md Gotcha 9).
    - `--unsafe-strip-invisible` (that's a deliberate WRITE-time capability;
      the foundation only wires READ verbs).
    - Any direct `/opt/homebrew/bin/gog` fallback (the read-redacted-email
      escape hatch is workspace-level tooling, not CLI-level).

Contract (deliberately narrow, mirrors msearch.py):

  - Read binary path from `MINERU_GOG_FIREWALL_BIN` env, default
    `$MINERU_HOME/bin/gog-firewall`. Tests override the env to
    point at a fake.
  - Build the argv as `[binary, *args]` where `args` is exactly what
    gog-firewall expects after its own program name (e.g. `["gmail",
    "search", "newer_than:1d", "--json"]`).
  - Run `subprocess.run(cmd, check=False)` with NO stdout/stderr override.
    The engine's stdout/stderr flow straight through to the caller's fds,
    so `mineru gmail search 'newer_than:1d' --json` and the direct
    `gog-firewall gmail search 'newer_than:1d' --json` produce
    byte-identical output. Critically, this means the firewall's stderr
    notices (`redacted N of M units`) reach the user untouched.
  - Return the engine's exit code unchanged. Callers convert to
    `typer.Exit`. Firewall convention: 0 delivered, 77 all-blocked,
    78 firewall error — none of them get remapped here.
  - If the binary is missing, print an actionable error to stderr naming
    the RESOLVED path and raise `typer.Exit(127)`. Never fall back to a
    different binary; a missing firewall is a security-relevant failure,
    not a "use the raw tool instead" moment.

Non-goals (strict):

  - No parsing of gog-firewall stdout/stderr.
  - No reshaping / re-emitting engine output.
  - No filtering of the argv tail (extras pass through opaque).
  - No injection of `--raw`, `--unsafe-strip-invisible`, or any other
    firewall-bypassing flag.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, List, Optional, Sequence

import typer

from mineru_cli.wrappers._profile_env import profile_env_for_ctx

# Documented default matches TOOLS.md / GMAIL.md and the F1 skeleton comment.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
DEFAULT_GOG_FIREWALL_BIN = str(_MINERU_HOME / "bin" / "gog-firewall")

# Env var name that overrides the default (documented; tests use it).
GOG_FIREWALL_BIN_ENV = "MINERU_GOG_FIREWALL_BIN"

# POSIX "command not found" — the semantically-right exit for a missing
# engine binary. Distinct from typical app errors (usually 1 or 2), so
# callers can special-case it in shell pipelines.
MISSING_BIN_EXIT_CODE = 127

# Firewall-error exit code (matches gog-firewall's own convention). Used
# when a runtime guard here refuses to shell out because the resolved
# binary is not a gog-firewall — for example, MINERU_GOG_FIREWALL_BIN
# points at raw `gog` or a look-alike shim that would bypass screening.
FIREWALL_BASENAME_MISMATCH_EXIT_CODE = 78

# Basename we expect argv[0] to resolve to. Used both as the runtime
# guard (see run_gog_firewall) and by tests: a wrapper accidentally
# rewired to bare `gog` would fail this check.
EXPECTED_BIN_BASENAME = "gog-firewall"


def resolve_gog_firewall_bin() -> str:
    """Return the gog-firewall binary path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(GOG_FIREWALL_BIN_ENV)
    if override:
        return override
    return DEFAULT_GOG_FIREWALL_BIN


def _binary_available(path: str) -> bool:
    """True iff `path` refers to an existing, executable regular file.

    - Absolute path or path with a separator: require the path to be a
      regular file (not a directory) with the executable bit set. This
      avoids two surprise failure modes downstream: (1) a directory
      passes `os.path.exists` and then `subprocess.run` raises
      `PermissionError` / `IsADirectoryError` instead of our friendly
      127 message; (2) a non-executable file passes `os.path.exists`
      and then `subprocess.run` raises `PermissionError`.
    - Bare name (e.g. `gog-firewall`): defer to PATH via `shutil.which`,
      which already requires executability. This lets tests set
      MINERU_GOG_FIREWALL_BIN=/tmp/mineru_fixtures/gog-firewall to point
      at a known-good fake and also lets a user drop `gog-firewall` on PATH.
    """
    if os.path.isabs(path) or os.path.sep in path:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def build_gog_firewall_argv(args: Sequence[str]) -> List[str]:
    """Build the argv list that would be passed to subprocess.

    Kept as a separate helper so the invariant test can assert on argv[0]
    without needing to mock subprocess. The returned list is exactly
    `[resolved_binary, *args]` — no filtering, no rewriting.
    """
    return [resolve_gog_firewall_bin(), *args]


def _google_account_from_ctx(ctx: Optional[Any]) -> Optional[str]:
    """Read the active profile's `google_account` off a Typer context.

    Returns `None` when the ctx is missing / not-yet-hydrated / carries a
    profile that never set `google_account`. Callers are expected to have
    already called `get_profile(ctx)` (fail-loud on a bogus `--profile`);
    this helper is only the pure lookup, so tests that build a ctx by
    hand without the loader still work.
    """
    if ctx is None:
        return None
    obj = getattr(ctx, "obj", None) or {}
    profile = obj.get("profile_obj")
    if profile is None:
        return None
    return getattr(profile, "google_account", None)


def _inject_account_flag(
    account: Optional[str], args: Sequence[str]
) -> List[str]:
    """Prepend `--account=<email>` to `args` when the active profile pins one.

    `--account` is a gog GLOBAL flag and must precede the subcommand
    (e.g. `gog --account=you@x gmail search ...`). This helper prepends
    it exactly ONCE, and only when the argv doesn't already carry an
    operator-supplied `--account` (raw `--account VAL`, `--account=VAL`,
    or its short-form fallback). The operator-supplied override wins,
    matching the general last-flag / explicit-flag-wins convention the
    rest of the CLI follows.
    """
    incoming = list(args)
    if not account:
        return incoming
    for token in incoming:
        if token == "--account" or token.startswith("--account="):
            return incoming
    return [f"--account={account}", *incoming]


def run_gog_firewall(
    args: Sequence[str], ctx: Optional[Any] = None
) -> int:
    """Shell out to `gog-firewall <args...>` with stdio pass-through.

    Args:
        args: argv tail passed to gog-firewall after the program name.
              Example: `["gmail", "search", "newer_than:1d", "--json"]`.
        ctx:  optional Typer context. When passed, and the active profile
              carries a `google_account`, `--account=<email>` is prepended
              to `args` so gog targets that account (per-profile isolation
              on a multi-tenant Mac). Omit `ctx` at internal call sites
              that already handle the account themselves; the wrapper is
              still safe to call as `run_gog_firewall(args)`.

    Returns:
        The engine's exit code (0 delivered, 77 all-blocked, 78 firewall
        error, or any other engine-defined code). Callers convert to
        `typer.Exit(code=rc)` to propagate through the CLI. All three of
        the firewall's documented exit codes propagate unchanged.

    Raises:
        typer.Exit(127): the gog-firewall binary at the resolved path does
            not exist. Stderr already carries an actionable message naming
            the path and the override env var.
    """
    account = _google_account_from_ctx(ctx)
    args = _inject_account_flag(account, args)
    binary = resolve_gog_firewall_bin()

    # Runtime enforcement of the firewall-preservation invariant.
    # Docstring §0+§3.1 says the raw `gog` binary is NEVER called by any
    # read verb, but until now that invariant was enforced only by unit
    # tests. Setting MINERU_GOG_FIREWALL_BIN=/opt/homebrew/bin/gog (or
    # bare `gog`) would previously exec the raw binary and bypass every
    # per-unit prompt-injection screen. Reject the shellout here with an
    # actionable message and the firewall's own 78 (firewall error) exit
    # code so shell pipelines can distinguish it from POSIX 127.
    basename = os.path.basename(binary)
    if basename != EXPECTED_BIN_BASENAME:
        typer.echo(
            f"mineru: {GOG_FIREWALL_BIN_ENV} must point at a "
            f"{EXPECTED_BIN_BASENAME!r}-named binary; got {binary!r} "
            f"(basename {basename!r}). Refusing to shell out: pointing at "
            f"raw `gog` or a look-alike shim would bypass prompt-injection "
            f"screening. Default: {DEFAULT_GOG_FIREWALL_BIN!r}.",
            err=True,
        )
        raise typer.Exit(code=FIREWALL_BASENAME_MISMATCH_EXIT_CODE)

    if not _binary_available(binary):
        # No verb prefix here: this wrapper backs every Google Workspace
        # noun (gmail, calendar, drive, docs, sheets, contacts, tasks,
        # people, groups), so hard-coding "gmail" would misreport the
        # engine as gmail-specific to every other noun's operator.
        typer.echo(
            f"mineru: gog-firewall engine not found at {binary!r} "
            f"(set {GOG_FIREWALL_BIN_ENV} to override, "
            f"default {DEFAULT_GOG_FIREWALL_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    cmd: List[str] = [binary, *args]
    # Belt-and-braces on the step-5 audit's Finding 7: even when a
    # subprocess doesn't share process env (some launcher / cron path
    # scrubs env), pass an explicit dict pinned to the active profile's
    # MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT / MINERU_INJECT_QUEUE_DIR so
    # gog-firewall's internal shellouts (curl for VAPID, `security` for
    # Keychain reads under `gogcli`, the injection firewall's Ollama
    # probe) all target the right profile's namespace.
    env = profile_env_for_ctx(ctx)
    run_kwargs: dict = {"check": False}
    if env is not None:
        run_kwargs["env"] = env
    try:
        completed = subprocess.run(cmd, **run_kwargs)
    except OSError:
        # Race window OR a permission / directory anomaly the availability
        # check couldn't foresee (existed at check-time then vanished /
        # became non-executable / turned into a directory). Cover the whole
        # OSError family (FileNotFoundError, PermissionError,
        # IsADirectoryError) with one friendly message instead of leaking a
        # raw Python traceback.
        typer.echo(
            f"mineru: gog-firewall engine vanished or is not executable at "
            f"{binary!r} (set {GOG_FIREWALL_BIN_ENV} to override, "
            f"default {DEFAULT_GOG_FIREWALL_BIN!r}).",
            err=True,
        )
        raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

    return completed.returncode
