"""Write path for the secrets seam — mirrors the read backends in `backends.py`.

The read side has a pluggable `SecretsBackend` chain because reads must
compose env-var overrides, Keychain items, and (future) 1Password. The
write side is intentionally simpler: `mineru secrets set` ONLY writes to
the macOS Keychain today, because that is the only backend on the read
chain a mineru profile can also OWN (env vars are the shell operator's;
1Password is the team's password manager). A future rotation verb may
grow a backend selector; today it does not.

Public surface:
  - `KeychainWriteResult`   — outcome dataclass; carries `ok`, `rc`, a
                              safe `stderr_snippet`, and the `argv_shape`
                              with the value redacted.
  - `write_keychain_secret` — the write itself. Shells out to
                              `security add-generic-password -U -a
                              <account> -s <name> -w <value>` with the
                              same argv discipline as
                              `access.exporter.write_keychain_allowlist`.
  - `KeychainWriteError`    — raised only on caller bugs (bad type,
                              empty name). Real subprocess failures
                              return a non-ok `KeychainWriteResult`
                              (never raise) so the CLI can render a
                              clean error frame.

Security invariants (read before editing this file — the write path is
where value material touches the OS boundary):

  - The secret VALUE crosses the boundary as ONE argv element to
    `security add-generic-password -w <value>`. macOS' `security` binary
    exposes no stdin-fed write mode, so argv is unavoidable. This is the
    exact same constraint documented on
    `mineru_cli.access.exporter.write_keychain_allowlist`.
  - The value is NEVER logged, echoed, written to disk, or copied into
    another argv position. `argv_shape` on the result carries a
    `<REDACTED>` sentinel in the `-w` slot so a caller can log the shape
    without leaking the value.
  - The secret NAME may appear in argv (as `-s <name>`); it is validated
    against the strict character class from `backends._validate_secret_name`
    so a hostile name cannot smuggle shell metacharacters.
  - `stdin` is detached (`DEVNULL`) so a `security` prompt (locked
    keychain, GUI-only interaction) can never block waiting on input.
  - The subprocess is bounded by `KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS`
    so a wedged `security` cannot hang the CLI.
  - `-U` UPDATES an existing item in place OR creates it fresh, so the
    same command works on first-write and every subsequent overwrite.
    Without `-U`, `security` returns `errSecDuplicateItem` (rc 45) when
    the item already exists.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from mineru_cli.secrets.backends import (
    KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS,
    _validate_secret_name,
)


logger = logging.getLogger(__name__)


# Absolute path to Apple's `security` binary. Overridable in tests (a
# fake path that FileNotFoundErrors is how the missing-binary test
# exercises the non-macOS branch).
SECURITY_BINARY = "/usr/bin/security"

# Sentinel used in `argv_shape` to mark the position of the secret value
# without ever including the value itself. Chosen so a grep for the
# literal string in logs surfaces every place a redacted shape was
# recorded.
REDACTED_SENTINEL = "<REDACTED>"


class KeychainWriteError(RuntimeError):
    """Caller bug on the write path (empty name, wrong type, ...).

    Real subprocess failures (missing binary, timeout, non-zero exit) do
    NOT raise — they return a `KeychainWriteResult` with `ok=False` so
    the CLI can render a single clean error frame instead of an
    uncaught traceback. This exception is reserved for programming
    errors that a linter or a caller-side validation should have caught.
    """


@dataclass(frozen=True)
class KeychainWriteResult:
    """Outcome of writing one Keychain item.

    Attributes:
        ok: True iff `security add-generic-password -U` returned 0.
        rc: raw exit code (negative on subprocess errors we translated:
            -1 = binary missing, -2 = timeout).
        stderr_snippet: short safe stderr excerpt for diagnostic
            logging. Never contains the secret value.
        argv_shape: the argv shape actually invoked, with the value slot
            replaced by `REDACTED_SENTINEL`. Handy for logging + tests
            asserting "account and name went through cleanly."
    """

    ok: bool
    rc: int
    stderr_snippet: str = ""
    argv_shape: List[str] = field(default_factory=list)


# Injection seam for tests. Production callers hand in `None` and pick up
# `subprocess.run` at call time.
SubprocessRunner = Callable[..., "subprocess.CompletedProcess"]


def _default_runner(*args, **kwargs):
    return subprocess.run(*args, **kwargs)


def _decode_stderr_safely(stderr: object) -> str:
    """Best-effort decode of `security` stderr for a log line.

    Accepts bytes (subprocess default when text=False) or str (some test
    stubs). Never raises: a mis-encoded stderr falls back to lossy
    decode so a log line still lands. Never contains a value (`security`
    stderr names the item and account but not the payload).
    """
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace").strip()
    return str(stderr).strip()


def write_keychain_secret(
    name: str,
    value: str,
    *,
    account: str,
    binary: str = SECURITY_BINARY,
    runner: Optional[SubprocessRunner] = None,
    timeout_seconds: float = KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS,
) -> KeychainWriteResult:
    """Write `value` to the Keychain slot identified by `(account, name)`.

    Uses `security add-generic-password -U -a <account> -s <name> -w
    <value>`. The `-U` flag turns the write into an upsert: it creates
    the item on first write and updates in place on every subsequent
    write.

    Args:
        name: the secret name (Keychain service, `-s`). Validated
            against `_validate_secret_name`.
        value: the secret value (goes into `-w`). Must be a string.
            Empty strings are rejected here — an empty Keychain item is
            almost always a caller bug.
        account: the Keychain account namespace (`-a`), from the active
            profile's `keychain_account` field.
        binary: `security` binary path (overridable in tests).
        runner: injection point for tests. Production callers omit it.
        timeout_seconds: bounded wait on the subprocess.

    Returns:
        A `KeychainWriteResult`. Real subprocess failures resolve here
        (never raise) so the CLI can render one clean error frame.

    Raises:
        KeychainWriteError: caller bug — bad type on `name`, `value`, or
            `account`; empty `value` or `account`.
        ValueError: from `_validate_secret_name` when `name` fails the
            safe-character-class check.
    """
    if not isinstance(account, str) or not account:
        raise KeychainWriteError(
            "write_keychain_secret: account must be a non-empty string; "
            f"got {account!r}."
        )
    if not isinstance(value, str):
        raise KeychainWriteError(
            "write_keychain_secret: value must be a string; got "
            f"{type(value).__name__}."
        )
    if not value:
        raise KeychainWriteError(
            "write_keychain_secret: refusing to write an EMPTY value to "
            f"Keychain slot (name={name!r}, account={account!r}). "
            "An empty secret is almost always a bug — re-read from prompt "
            "or stdin and try again."
        )
    # Raises ValueError on a bad-shape name BEFORE any subprocess is
    # spawned. The safe-character class also protects the argv (a
    # leading `-`, whitespace, or shell metacharacters would be
    # rejected).
    _validate_secret_name(name)

    argv = [
        binary,
        "add-generic-password",
        "-U",
        "-a",
        account,
        "-s",
        name,
        "-w",
        value,
    ]
    # Same argv but with the value replaced by the sentinel — safe to
    # log, safe to return in a result object, safe to assert on in tests.
    argv_shape = [
        binary,
        "add-generic-password",
        "-U",
        "-a",
        account,
        "-s",
        name,
        "-w",
        REDACTED_SENTINEL,
    ]

    run = runner or _default_runner
    try:
        completed = run(
            argv,
            capture_output=True,
            # text=False so we read bytes; matches the read path's
            # explicit decode discipline. Value never crosses this
            # boundary in the OTHER direction anyway (stdout is empty on
            # a write hit).
            text=False,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return KeychainWriteResult(
            ok=False,
            rc=-1,
            stderr_snippet=f"{binary!r} not available (non-macOS host?).",
            argv_shape=argv_shape,
        )
    except subprocess.TimeoutExpired:
        return KeychainWriteResult(
            ok=False,
            rc=-2,
            stderr_snippet=(
                f"security add-generic-password timed out after "
                f"{timeout_seconds:.1f}s (is the login keychain locked?)."
            ),
            argv_shape=argv_shape,
        )

    rc = int(completed.returncode)
    stderr_snippet = _decode_stderr_safely(getattr(completed, "stderr", b""))
    ok = rc == 0
    if not ok:
        # Log the ARGV SHAPE (never the argv itself, which carries the
        # value). One warning per failed write is a diagnosable trace
        # without leaking material.
        logger.warning(
            "keychain write failed (rc=%d) name=%r account=%r; stderr: %s",
            rc,
            name,
            account,
            stderr_snippet or "<empty>",
        )
    return KeychainWriteResult(
        ok=ok, rc=rc, stderr_snippet=stderr_snippet, argv_shape=argv_shape
    )


__all__ = [
    "KeychainWriteError",
    "KeychainWriteResult",
    "REDACTED_SENTINEL",
    "SECURITY_BINARY",
    "SubprocessRunner",
    "write_keychain_secret",
]
