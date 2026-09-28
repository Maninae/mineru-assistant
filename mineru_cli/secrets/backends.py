"""Concrete secret-source backends for the pluggable secrets seam.

Each backend implements `SecretsBackend`:
  - `get(name) -> str | None`  — return the secret value or `None` on miss.
  - `describe() -> str`        — short stable label used in audit output.

Security invariants (this file is the enforcement point — read before editing):

- The secret VALUE is NEVER written to a file, logged, echoed, or passed as
  an argv element to any subprocess. It only ever crosses the boundary as
  the return value of `get()`, captured from a subprocess's stdout in the
  Keychain case or read from `os.environ` in the env case.
- The secret NAME may appear in argv (e.g. as `security -s <name>`) — that
  is by construction and is validated against a strict character class.
- Absent is a normal outcome and returns `None`; it MUST NOT raise. Real
  errors (a locked keychain, `security` missing, an unimplemented backend)
  may raise so the resolver can log + walk to the next backend.
"""

import logging
import os
import re
import subprocess
from abc import ABC, abstractmethod


logger = logging.getLogger(__name__)


# Bounded wait on the `security` subprocess. macOS' `security` binary can hang
# indefinitely on a locked login keychain waiting for GUI unlock, on a stuck
# securityd, on a Full Disk Access prompt, or on a stalled disk. Every mineru
# invocation that touches the resolver inherits any hang here, so we cap it.
KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS = 5.0

# Empirical macOS exit codes for `security -w`:
#   0   -> hit (value on stdout)
#   44  -> SecKeychainItemNotFound (normal miss)
#   36  -> SecAuthFailed (locked keychain / user interaction not allowed)
KEYCHAIN_EXIT_CODE_HIT = 0
KEYCHAIN_EXIT_CODE_ITEM_NOT_FOUND = 44
KEYCHAIN_EXIT_CODE_LOCKED = 36


# Secret NAMES (never values) are validated against this pattern before being
# used as any subprocess argument. Allowing only letters, digits, and a small
# punctuation set keeps a mistaken caller from smuggling shell metacharacters
# in through the `-s` field. The pattern intentionally does not include
# spaces or shell special chars. The first character MUST be alphanumeric so
# a leading `-` cannot be smuggled into argv as an option flag (`-w`, `--foo`).
# `_` is intentionally EXCLUDED so `env_var_for` (which folds separators into
# `_`) is unambiguous: `foo-bar`, `foo.bar`, `foo:bar`, `foo/bar` all map to
# distinct env vars because a real `_` in a secret name can never occur.
_VALID_SECRET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:/-]*$")


def _validate_secret_name(name: str) -> None:
    """Reject secret names containing shell/argv-risky characters.

    Guards only the NAME (never the value; values never touch argv anyway).
    A bad name is a caller bug, so we raise instead of silently returning None.

    Rejects:
      - Empty string.
      - Any name with characters outside `[A-Za-z0-9.:/-]`.
      - A leading `-` (would look like an option flag to `security`).
      - `_` anywhere (would collide with the separator-folding of
        `env_var_for`; use `-`, `.`, `:`, or `/` instead).
    """
    if not name or not _VALID_SECRET_NAME.match(name):
        raise ValueError(
            f"invalid secret name {name!r}: names must start with a letter "
            "or digit and use only letters, digits, and '.:/-' as separators "
            "(underscores are reserved because '-', '.', ':', and '/' all "
            "fold to '_' in the env-var mapping)"
        )


def env_var_for(name: str, prefix: str) -> str:
    """Map a secret name to its env var name: `<prefix><UPPER_SNAKE(name)>`.

    Dashes, dots, colons, and slashes in the secret name become underscores;
    the result is upper-cased and glued to the prefix.
    e.g. `telegram-bot-token` with the default `MINERU_SECRET_` prefix
    becomes `MINERU_SECRET_TELEGRAM_BOT_TOKEN`.

    Exposed at module scope so callers (and tests) can reason about the
    mapping without instantiating a backend.

    Note: `_` is not accepted inside a secret name (see `_validate_secret_name`),
    which keeps this mapping unambiguous. `foo-bar` and `foo.bar` map to
    distinct env vars because `foo_bar` is not a valid secret name in the
    first place.
    """
    normalized = re.sub(r"[-./:]", "_", name).upper()
    return f"{prefix}{normalized}"


class SecretsBackend(ABC):
    """Abstract read-only secret source.

    Contract:
      - `get(name)` returns the secret value on hit, `None` on absent.
        Absent MUST NOT raise: the resolver treats `None` as "walk on".
      - `describe()` returns a short stable label safe for audit output
        and logs. Never contains a secret value.
    """

    @abstractmethod
    def get(self, name: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def describe(self) -> str:
        raise NotImplementedError


class EnvBackend(SecretsBackend):
    """Environment-variable secret backend.

    Reads `<env_prefix><UPPER_SNAKE(name)>` from `os.environ`. Positioned
    first in the default backend chain so a shell export shadows any
    Keychain item — the intended override for CI, dev shells, and
    per-invocation testing.
    """

    def __init__(self, env_prefix: str = "MINERU_SECRET_") -> None:
        self.env_prefix = env_prefix

    def get(self, name: str) -> str | None:
        _validate_secret_name(name)
        value = os.environ.get(env_var_for(name, self.env_prefix))
        # Empty string is treated as absent so the resolver walks on;
        # a caller who explicitly wants "" as a value can set the env
        # var to a single space or similar sentinel.
        return value if value else None

    def describe(self) -> str:
        return f"env({self.env_prefix}*)"


class KeychainBackend(SecretsBackend):
    """macOS Keychain secret backend.

    Shells out to `security find-generic-password -a <account> -s <name> -w`.
    The account (`-a`) is set at construction time and matches the profile's
    keychain namespace (default `mineru`, matching every hardcoded `-a mineru`
    call site the audit inventoried).

    Security invariants (verify before editing this class):
      - The secret VALUE never appears in argv. Only the account and name do.
      - `security -w` writes ONLY the value to stdout on hit; we capture that
        stream as raw bytes and decode explicitly (so a mis-encoded item
        surfaces a diagnosable error instead of vanishing under a locale
        UnicodeDecodeError).
      - We never log the value, store it on the instance, or write it to disk.
      - stderr is captured only so it doesn't pollute the CLI; its content
        (e.g. 'user interaction is not allowed' on a locked keychain) is used
        only for structured logging, never re-emitted verbatim to the user.
      - The subprocess call is bounded by `KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS`
        so a wedged `security` binary can never hang the CLI. `stdin` is
        detached (DEVNULL) so it can never block waiting on input.
    """

    def __init__(
        self, account: str = "mineru", binary: str = "/usr/bin/security"
    ) -> None:
        self.account = account
        self.binary = binary

    def get(self, name: str) -> str | None:
        _validate_secret_name(name)
        try:
            completed = subprocess.run(
                [
                    self.binary,
                    "find-generic-password",
                    "-a",
                    self.account,
                    "-s",
                    name,
                    "-w",
                ],
                capture_output=True,
                # text=False so we read raw bytes and decode explicitly below.
                # text=True asks subprocess to decode with the process locale,
                # which raises UnicodeDecodeError on any non-UTF-8 stored
                # secret and silently swallows the item under the resolver's
                # blanket exception handler.
                text=False,
                check=False,
                # Detach stdin so the child cannot block reading from it if
                # some flag path decides to prompt.
                stdin=subprocess.DEVNULL,
                timeout=KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            # Not on macOS, or the `security` binary is at a different
            # path. Treat as absent so the resolver walks on.
            return None
        except subprocess.TimeoutExpired:
            # A wedged keychain / securityd / GUI prompt. Log a diagnosable
            # warning naming the item (never a value) and treat as absent so
            # the resolver walks to the next backend.
            logger.warning(
                "keychain lookup timed out after %.1fs for name=%r account=%r "
                "(is your login keychain locked?)",
                KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS,
                name,
                self.account,
            )
            return None
        if completed.returncode == KEYCHAIN_EXIT_CODE_LOCKED:
            # SecAuthFailed. Distinct from a real miss (44): the item may
            # exist but the user must unlock the keychain to see it. Surface
            # an actionable warning; return None so the resolver walks on.
            stderr_text = _decode_stderr_safely(completed.stderr)
            logger.warning(
                "keychain locked (rc=%d) looking up name=%r account=%r; "
                "unlock your login keychain and retry (security stderr: %s)",
                KEYCHAIN_EXIT_CODE_LOCKED,
                name,
                self.account,
                stderr_text or "<empty>",
            )
            return None
        if completed.returncode != KEYCHAIN_EXIT_CODE_HIT:
            # Normal miss (44) or any other non-zero code. Never log the
            # stderr verbatim to the user; the resolver walks on.
            return None
        # `security -w` appends a single trailing newline; strip exactly one.
        # Decode explicitly so a malformed stored value fails loud (with a
        # named warning) instead of vanishing behind a locale error.
        try:
            value = completed.stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            logger.warning(
                "keychain item name=%r account=%r is not valid UTF-8; "
                "cannot decode. Rotate the item or store as UTF-8.",
                name,
                self.account,
            )
            return None
        if value.endswith("\n"):
            value = value[:-1]
        return value if value else None

    def describe(self) -> str:
        return f"keychain(a={self.account})"


def _decode_stderr_safely(stderr_bytes: bytes) -> str:
    """Best-effort decode of `security` stderr for a log line.

    Never raises: if the stderr is not valid UTF-8, fall back to a lossy
    decode so the log line still lands. Never contains a value (stderr from
    `security -w` names the item but not the stored payload).
    """
    if not stderr_bytes:
        return ""
    return stderr_bytes.decode("utf-8", errors="replace").strip()


class OnePasswordBackend(SecretsBackend):
    """1Password Service Account backend — DOCUMENTED STUB (not built).

    The foundation increment ships this class so the pluggable seam is
    complete: `SecretsResolver` accepts it, `SecretsConfig` names it under
    `1password`, and the `build_resolver` factory instantiates it when the
    profile lists it. `get()` raises `NotImplementedError` at call time and
    the resolver walks past it to the next backend.

    Implementation plan (future increment):
      - Read `OP_SERVICE_ACCOUNT_TOKEN` from the calling shell / launchd
        env. NEVER stored on disk by mineru — the caller (or their systemd
        unit / launchd plist) owns it.
      - Invoke `op read op://<vault>/<item_prefix><name>/password` under
        that token to fetch a value. Alternatively `op run --env-file=...`
        to hydrate a subprocess with multiple secrets in one shot.
      - Capture value from `op`'s stdout, mirror the KeychainBackend
        argv/stdout discipline (name in argv, value in stdout).

    Profile config keys this backend WILL read once implemented
    (planned for `profile/secrets.yaml`, §5.5 of the capability spec):
      - `onepassword.vault`        — vault name (e.g. "mineru")
      - `onepassword.item_prefix`  — optional prefix mapping a secret name
                                     to its op:// URL (default "")
      - `onepassword.op_binary`    — path to the `op` CLI
                                     (default: /opt/homebrew/bin/op)

    Activation order once built: add `1password` to `backends:` in
    `profile/secrets.yaml` — place it AFTER `env` (so a shell override
    still shadows) and BEFORE `keychain` (so 1Password becomes the
    team-shareable source of truth while Keychain covers gaps).
    """

    # Named constant so the profile-layer schema (future increment) can
    # refer to the same key without a magic string. Matches the
    # `profile/secrets.yaml` shape in §5.5: `backends: [env, 1password, keychain]`.
    CONFIG_KEY = "1password"
    OP_READ_COMMAND_TEMPLATE = "op read op://{vault}/{item_prefix}{name}/password"

    def __init__(
        self,
        *,
        vault: str | None = None,
        item_prefix: str = "",
        op_binary: str = "/opt/homebrew/bin/op",
    ) -> None:
        self.vault = vault
        self.item_prefix = item_prefix
        self.op_binary = op_binary

    def get(self, name: str) -> str | None:
        raise NotImplementedError(
            "OnePasswordBackend is a documented stub in the foundation "
            "increment. When implemented, it will invoke "
            f"{self.OP_READ_COMMAND_TEMPLATE!r} under OP_SERVICE_ACCOUNT_TOKEN; "
            "see class docstring for the profile/secrets.yaml keys it will "
            "consume."
        )

    def describe(self) -> str:
        return f"1password(vault={self.vault or 'unset'})"
