"""Profile layer for the mineru CLI (Phase 1 multi-profile framework).

Public surface:
  - `Profile`                    — immutable dataclass modeling §5.3 fields
                                   the foundation needs, plus the two
                                   `secrets.*` knobs F2's resolver consumes.
  - `ProfileError`               — raised on missing/invalid profile.yaml
                                   or a missing/invalid active-profile symlink.
  - `load_active_profile(...)`   — resolve + load the active profile per
                                   the Phase-1 order: `--profile` flag >
                                   `MINERU_PROFILE` env >
                                   `active` symlink (legacy `current`
                                   fallback) > fail loud.
  - `secrets_config_from_profile` — bridge that hands F2's `SecretsResolver`
                                   the backend order + keychain account
                                   named by the loaded profile.
  - `default_workspace_root()`   — workspace root where `profiles/`,
                                   `humans.yaml`, and the `active` symlink
                                   live. Defaults to the `MINERU_HOME`
                                   seam (default `~/.mineru`).
  - `default_profiles_base_dir()` — the on-disk base under which
                                   `<name>/profile.yaml` lives (defaults
                                   to `<workspace_root>/profiles/`).
  - `active_symlink_path(...)`   — absolute path of the active-profile
                                   pointer symlink at the workspace root
                                   (was `current_symlink_path` before
                                   2026-09-16; that name is still
                                   exported as a back-compat alias).

Design invariants (Phase 1, revives the multi-tenant machinery dropped
in §0 of the 2026-07-25 capability spec):

  - Multiple profiles can co-exist under `<workspace_root>/profiles/`.
    An `active` symlink picks the active one (renamed from `current` on
    2026-09-16 per audit §2A F4; a legacy `current` symlink is still
    read as a fallback so pre-rename workspaces keep resolving).
    `mineru profile use <name>` atomically re-points it.
  - Owner-only per profile FOR NOW: the access data model can hold
    guest tiers later, but no guest-tier enforcement is built yet.
  - The command + package are `mineru` / `mineru_cli` — the framework
    brand is Mineru, permanently (no rename pass).
  - No hardcoded `.mineru` in code paths — everything is
    workspace-root-relative.

  - Extra fields in `profile.yaml` are TOLERATED (forward-compat). The
    loader only validates the foundation-required fields and drops
    everything else onto `Profile.extras`.

  - Fail loud on miss: a missing `profile.yaml` or a missing required
    field raises `ProfileError`.
"""

from mineru_cli.profile.loader import (
    ACTIVE_SYMLINK_NAME,
    CURRENT_SYMLINK_NAME,
    DEFAULT_ASSISTANT_NAME,
    ENGINE_ROOT_ENV_VAR,
    NoActiveProfileError,
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    ProfileError,
    WORKSPACE_ROOT_ENV_VAR,
    active_symlink_path,
    current_symlink_path,
    default_engine_root,
    default_profiles_base_dir,
    default_workspace_root,
    get_profile,
    legacy_current_symlink_path,
    load_active_profile,
    resolve_assistant_name_for_help,
    resolve_profile_name,
    secrets_config_from_profile,
)
from mineru_cli.profile.schema import Profile

__all__ = [
    "ACTIVE_SYMLINK_NAME",
    "CURRENT_SYMLINK_NAME",
    "DEFAULT_ASSISTANT_NAME",
    "ENGINE_ROOT_ENV_VAR",
    "NoActiveProfileError",
    "PROFILE_BASE_DIR_ENV_VAR",
    "PROFILE_NAME_ENV_VAR",
    "Profile",
    "ProfileError",
    "WORKSPACE_ROOT_ENV_VAR",
    "active_symlink_path",
    "current_symlink_path",
    "default_engine_root",
    "default_profiles_base_dir",
    "default_workspace_root",
    "get_profile",
    "legacy_current_symlink_path",
    "load_active_profile",
    "resolve_assistant_name_for_help",
    "resolve_profile_name",
    "secrets_config_from_profile",
]
