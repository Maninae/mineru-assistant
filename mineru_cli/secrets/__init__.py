"""Pluggable secrets seam for the mineru CLI (foundation).

Public surface:
  - `SecretsBackend`                                        (abstract base)
  - `EnvBackend`, `KeychainBackend`, `OnePasswordBackend`   (concrete backends)
  - `SecretsConfig`                                         (chain + knobs)
  - `SecretsResolver`                                       (first-hit walk)
  - `SecretResolution`                                      (per-name result)
  - `build_resolver`                                        (config -> resolver)

Backend chain and per-backend knobs default to `[env, keychain]` with
`keychain_account="mineru"` — mirroring the profile/secrets.yaml shape
described in §5.5 of the capability spec. The 1Password backend is
shipped as a documented stub in this increment; adding `1password` to
the chain is a no-op today (the resolver walks past it) and becomes
active once its `get()` is implemented.

Security invariants (see backends.py and resolver.py for enforcement):
  - Secret VALUES never appear in argv, logs, or files. Only names do.
  - Absent-from-every-backend returns `SecretResolution(present=False)`;
    it never raises.
  - `SecretsResolver.audit(...)` never populates `value`, ever.
"""

from mineru_cli.secrets.backends import (
    EnvBackend,
    KeychainBackend,
    OnePasswordBackend,
    SecretsBackend,
    env_var_for,
)
from mineru_cli.secrets.config import (
    DEFAULT_BACKENDS,
    DEFAULT_ENV_PREFIX,
    DEFAULT_KEYCHAIN_ACCOUNT,
    SecretsConfig,
)
from mineru_cli.secrets.resolver import (
    SecretResolution,
    SecretsResolver,
    build_resolver,
)
from mineru_cli.secrets.writer import (
    KeychainWriteError,
    KeychainWriteResult,
    REDACTED_SENTINEL,
    SECURITY_BINARY,
    write_keychain_secret,
)

__all__ = [
    "DEFAULT_BACKENDS",
    "DEFAULT_ENV_PREFIX",
    "DEFAULT_KEYCHAIN_ACCOUNT",
    "EnvBackend",
    "KeychainBackend",
    "KeychainWriteError",
    "KeychainWriteResult",
    "OnePasswordBackend",
    "REDACTED_SENTINEL",
    "SECURITY_BINARY",
    "SecretResolution",
    "SecretsBackend",
    "SecretsConfig",
    "SecretsResolver",
    "build_resolver",
    "env_var_for",
    "write_keychain_secret",
]
