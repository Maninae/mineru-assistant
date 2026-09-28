"""Per-profile access allowlist (Phase 1 multi-profile framework).

An `access.yaml` at `<profiles_base>/<name>/access.yaml` declares who is
allowed to talk to that agent profile: an `owner` (a human handle from
the machine-level `humans.yaml`) plus an `authorized` list of
`{human, tier}` entries.

Phase 1 ships OWNER-ONLY: the `guest` tier is present in the enum and
schema as a documented DEFERRED slot so downstream tooling can already
name it, but no guest-tier enforcement is wired in this phase. The
Landline daemon enforcement hook is a separate chunk.

Schema (see `schema.py` for the dataclass form):

    # access.yaml at <profiles_base>/<name>/access.yaml
    owner: sam            # handle from humans.yaml
    authorized:
      - {human: sam, tier: owner}
      # (guest entries here would be TOLERATED but NOT enforced yet)

Public surface:
  - `AccessTier`           — `(str, Enum)` with at least `OWNER`; `GUEST`
                             is present as a DEFERRED, unenforced slot.
  - `AccessEntry`          — one authorized `{human, tier}` pair.
  - `AccessConfig`         — the full per-profile allowlist.
  - `AccessError`          — fail-loud loader error.
  - `load_access_config(...)` — parse the profile's access.yaml and
                                cross-validate the owner against the
                                humans registry.
  - `default_access_yaml_path(profile)` — the on-disk path used by the loader.
"""

from mineru_cli.access.exporter import (
    AllowlistDiff,
    AllowlistExportError,
    KeychainReadResult,
    KeychainWriteResult,
    TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE,
    diff_allowlist,
    format_allowlist_value,
    parse_keychain_allowlist,
    read_keychain_allowlist,
    resolve_allowlist_ids,
    write_keychain_allowlist,
)
from mineru_cli.access.loader import (
    AccessError,
    default_access_yaml_path,
    load_access_config,
)
from mineru_cli.access.schema import (
    AccessConfig,
    AccessEntry,
    AccessTier,
)

__all__ = [
    "AccessConfig",
    "AccessEntry",
    "AccessError",
    "AccessTier",
    "AllowlistDiff",
    "AllowlistExportError",
    "KeychainReadResult",
    "KeychainWriteResult",
    "TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE",
    "default_access_yaml_path",
    "diff_allowlist",
    "format_allowlist_value",
    "load_access_config",
    "parse_keychain_allowlist",
    "read_keychain_allowlist",
    "resolve_allowlist_ids",
    "write_keychain_allowlist",
]
