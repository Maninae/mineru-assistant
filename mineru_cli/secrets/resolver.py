"""First-hit resolver across a configured secret-backend chain.

`SecretsResolver.resolve(name)` walks the backends in order and returns
the first hit; `SecretsResolver.audit(names)` returns a per-name presence
report WITHOUT ever populating the value field. The factory
`build_resolver(config)` maps `SecretsConfig.backends` string names to
concrete backend instances.

Security invariants:
  - No log/print statement in this module ever includes a secret value.
    We only log backend `describe()` names and caller-visible names.
  - `audit()` explicitly zeroes the `value` field in every returned
    `SecretResolution` so a serialization mistake can't leak.
  - `SecretResolution.value` is `repr=False` so `repr()`, `str()`,
    `%s`/`{}` formatting, and any accidental log of a resolution object
    all redact the value. `__str__` is also overridden to redact.
  - `NotImplementedError` from a stub backend (today: OnePasswordBackend)
    is caught and treated as "walk on", so a documented-not-yet-built
    backend in the chain is a no-op, not a fatal error.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from mineru_cli.secrets.backends import (
    EnvBackend,
    KeychainBackend,
    OnePasswordBackend,
    SecretsBackend,
)
from mineru_cli.secrets.config import SecretsConfig


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretResolution:
    """Per-name result carried by both `resolve` and `audit`.

    `value` is populated ONLY on the `resolve` path (the caller explicitly
    wants the secret). The `audit` path always returns rows with
    `value=None` so serializing an audit report is inherently safe.

    Security note: `value` is `repr=False` so the auto-generated `__repr__`
    NEVER includes it. `__str__` is overridden below to also redact.
    Callers must access `.value` explicitly; any accidental log of the
    resolution object (e.g. `logger.info("%s", result)`) will show the
    presence bit and backend label only, never the secret payload.
    """

    name: str
    present: bool
    resolved_by: str | None       # backend.describe() on hit; None on miss
    value: str | None = field(default=None, repr=False)  # never in repr/str

    def __str__(self) -> str:
        # Match the safe repr shape exactly; hide the value.
        return (
            f"SecretResolution(name={self.name!r}, present={self.present}, "
            f"resolved_by={self.resolved_by!r}, value=<redacted>)"
        )

    def to_audit_row(self) -> dict:
        """Explicit projection for audit output — value is never included.

        Callers building JSON / text reports should use this instead of
        `dataclasses.asdict(...)`, which would include the `value` key
        (even when `None`) and could be misread by a future callsite as
        the canonical serialization shape.
        """
        return {
            "name": self.name,
            "present": self.present,
            "resolved_by": self.resolved_by,
        }


class SecretsResolver:
    """Iterate configured backends in order and return the first hit.

    First-hit wins. In the default `[env, keychain]` chain, a shell
    export shadows a Keychain item — the intended override for CI, tests,
    and dev shells.

    Backend errors are non-fatal to the resolve as a whole: a raised
    exception (locked keychain, missing CLI, stub `NotImplementedError`)
    is logged with the backend's `describe()` label and the walk
    continues to the next backend. Absent-from-every-backend returns a
    `SecretResolution(present=False, resolved_by=None)` — never raises.
    """

    def __init__(self, backends: Iterable[SecretsBackend]) -> None:
        self.backends: list[SecretsBackend] = list(backends)

    def resolve(self, name: str) -> SecretResolution:
        for backend in self.backends:
            try:
                value = backend.get(name)
            except NotImplementedError:
                # Documented stub (OnePasswordBackend today). Walk on
                # silently at INFO level — noisy at WARNING would spam
                # every lookup once 1password lands in the chain.
                logger.debug(
                    "secrets: skipping stub backend %s", backend.describe()
                )
                continue
            except Exception as exc:  # noqa: BLE001 — see class docstring
                # `exc` cannot carry the value: backend.get() returns via
                # `return`, not by raising, so any raised exception here
                # comes from before the value was retrieved. Still, we
                # only log the exception type, never str(exc), out of
                # defence in depth.
                logger.warning(
                    "secrets: backend %s raised %s during lookup for %r; "
                    "walking to next backend",
                    backend.describe(),
                    type(exc).__name__,
                    name,
                )
                continue
            if value is not None:
                return SecretResolution(
                    name=name,
                    present=True,
                    resolved_by=backend.describe(),
                    value=value,
                )
        return SecretResolution(name=name, present=False, resolved_by=None)

    def audit(self, names: Iterable[str]) -> list[SecretResolution]:
        """Per-name presence report — never carries values.

        `value` is force-cleared on every returned row so a future contract
        change in `resolve()` can't leak a secret into audit output.
        """
        report: list[SecretResolution] = []
        for name in names:
            found = self.resolve(name)
            # Defensive: `replace(..., value=None)` guarantees no value
            # material can slip into an audit row even if resolve() is
            # ever changed to populate value on miss (it doesn't today).
            report.append(replace(found, value=None))
        return report


def build_resolver(config: SecretsConfig | None = None) -> SecretsResolver:
    """Instantiate the resolver from a `SecretsConfig`.

    Maps each string in `config.backends` to a concrete backend:
      - `env`       -> `EnvBackend(env_prefix=config.env_prefix)`
      - `keychain`  -> `KeychainBackend(account=config.keychain_account)`
      - `1password` -> `OnePasswordBackend()` (stub — raises at get-time;
                                              the resolver walks past)

    Unknown backend names raise `ValueError` so a typo in the profile
    surfaces loudly instead of silently misconfiguring the chain.
    """
    config = config or SecretsConfig()
    concrete: list[SecretsBackend] = []
    for name in config.backends:
        if name == "env":
            concrete.append(EnvBackend(env_prefix=config.env_prefix))
        elif name == "keychain":
            concrete.append(KeychainBackend(account=config.keychain_account))
        elif name == OnePasswordBackend.CONFIG_KEY:  # "1password"
            concrete.append(OnePasswordBackend())
        else:
            raise ValueError(
                f"unknown secrets backend {name!r}: expected one of "
                "env, keychain, 1password"
            )
    return SecretsResolver(concrete)
