"""Static configuration for the secrets resolver.

Foundation-increment shape: a dataclass carrying the backend order plus
per-backend knobs (env prefix, keychain account). The profile layer
(future increment) will hydrate this from `profile/secrets.yaml` per §5.5
of the capability spec; today, callers construct `SecretsConfig()` for
defaults or pass explicit overrides.
"""

from dataclasses import dataclass, field


DEFAULT_ENV_PREFIX = "MINERU_SECRET_"
DEFAULT_KEYCHAIN_ACCOUNT = "mineru"
# Default backend chain: env first (so shell overrides shadow Keychain
# during CI/dev), then Keychain (long-lived host secrets). 1Password sits
# behind its stub and joins the chain once implemented.
DEFAULT_BACKENDS: list[str] = ["env", "keychain"]


@dataclass(frozen=True)
class SecretsConfig:
    """Backend chain + per-backend knobs.

    Mirrors the `profile/secrets.yaml` shape described in §5.5:

        backends:
          - env
          - keychain
        env_prefix: MINERU_SECRET_
        keychain:
          account: mineru

    Frozen so a resolver's config can't drift under it at runtime; make
    a new `SecretsConfig` and a new resolver if the profile changes.
    """

    backends: list[str] = field(default_factory=lambda: list(DEFAULT_BACKENDS))
    env_prefix: str = DEFAULT_ENV_PREFIX
    keychain_account: str = DEFAULT_KEYCHAIN_ACCOUNT
