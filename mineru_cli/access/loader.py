"""Loader for a profile's `access.yaml`.

Reads `<profile_root>/access.yaml`, validates the schema, cross-checks
the owner (and any guest entries) against the machine-level humans
registry, and returns an immutable `AccessConfig`.

Fail-loud contract:
  - Missing access.yaml -> `AccessError` naming the exact path.
  - Missing `owner:` -> `AccessError` naming the field.
  - `owner:` handle absent from humans.yaml -> `AccessError` naming both
    the missing handle and the humans.yaml path.
  - `authorized` entry with unknown tier -> `AccessError` listing legal tiers.
  - `authorized` entry with unknown human handle -> `AccessError`.
  - Any human referenced twice -> `AccessError`.

The loader ALWAYS materializes an owner entry with `tier=OWNER` even if
the operator forgot to list themselves under `authorized:` — the owner
is implicitly authorized, and forcing them to appear twice would be a
confusing gotcha.

Zero guest entries is valid (Phase 1 default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

import yaml

from mineru_cli.access.schema import AccessConfig, AccessEntry, AccessTier
from mineru_cli.humans.schema import HumansRegistry
from mineru_cli.profile.schema import Profile


ACCESS_YAML_FILENAME = "access.yaml"

# Legal tier values. Kept as a module constant so the error message
# stays in lockstep with the enum.
_LEGAL_TIERS = tuple(t.value for t in AccessTier)


class AccessError(RuntimeError):
    """Raised on any access.yaml loader failure.

    The `__str__` always names the exact file path or field so a Typer
    handler can render it verbatim without wrapping.
    """


def default_access_yaml_path(profile: Profile) -> Path:
    """Return the absolute path of `access.yaml` for the given profile."""
    return profile.profile_root / ACCESS_YAML_FILENAME


def load_access_config(
    profile: Profile,
    humans_registry: HumansRegistry,
    *,
    path: Optional[Path] = None,
) -> AccessConfig:
    """Load and validate the profile's access allowlist.

    Args:
        profile: the loaded active profile (used to resolve the default
            access.yaml path and cross-check `profile_name`).
        humans_registry: the machine-level humans registry. Every handle
            referenced by access.yaml (owner + authorized) MUST exist in
            this registry.
        path: override for the access.yaml path (mostly tests). Production
            callers pass `None` and let `default_access_yaml_path(profile)`
            decide.

    Returns:
        An immutable `AccessConfig` with the owner always materialized
        as an OWNER-tier entry (even if omitted from `authorized:`).

    Raises:
        AccessError: on missing file, malformed YAML, schema violation,
            unknown handle, or unknown tier.
    """
    yaml_path = path or default_access_yaml_path(profile)
    if not yaml_path.exists():
        raise AccessError(
            f"profile {profile.name!r}: access.yaml not found at {yaml_path}. "
            "Create it (see `mineru access --help` for the schema)."
        )
    try:
        raw_text = yaml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AccessError(
            f"profile {profile.name!r}: could not read access.yaml at "
            f"{yaml_path}: {type(exc).__name__}"
        ) from exc
    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise AccessError(
            f"profile {profile.name!r}: access.yaml at {yaml_path} is not "
            f"valid YAML ({type(exc).__name__}). Fix the file and retry."
        ) from exc
    if not isinstance(data, dict):
        raise AccessError(
            f"profile {profile.name!r}: access.yaml at {yaml_path} must be a "
            f"mapping at the top level; got {type(data).__name__}."
        )

    owner = _owner_from(data, yaml_path, humans_registry)
    authorized = _authorized_from(data, yaml_path, humans_registry, owner=owner)

    return AccessConfig(
        profile_name=profile.name,
        owner=owner,
        authorized=authorized,
    )


def _owner_from(
    data: dict, yaml_path: Path, humans_registry: HumansRegistry
) -> str:
    """Extract + validate the `owner:` field."""
    if "owner" not in data:
        raise AccessError(
            f"access.yaml at {yaml_path}: missing required top-level "
            "field `owner:` (a handle from humans.yaml)."
        )
    owner: Any = data["owner"]
    if not isinstance(owner, str) or not owner.strip():
        raise AccessError(
            f"access.yaml at {yaml_path}: field `owner` must be a non-empty "
            f"string handle from humans.yaml, got {owner!r}."
        )
    if owner not in humans_registry:
        raise AccessError(
            f"access.yaml at {yaml_path}: owner handle {owner!r} not found "
            f"in humans.yaml. Known handles: {humans_registry.handles()}."
        )
    return owner


def _authorized_from(
    data: dict,
    yaml_path: Path,
    humans_registry: HumansRegistry,
    *,
    owner: str,
) -> List[AccessEntry]:
    """Extract + validate the `authorized:` list.

    Zero guest entries is fine (Phase 1 default). The owner is always
    materialized as an OWNER-tier entry even if omitted from the list.
    Duplicate humans are rejected.
    """
    raw = data.get("authorized", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise AccessError(
            f"access.yaml at {yaml_path}: field `authorized` must be a list "
            f"of {{human, tier}} entries; got {type(raw).__name__}."
        )
    entries: List[AccessEntry] = []
    seen_humans: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AccessError(
                f"access.yaml at {yaml_path}: `authorized[{i}]` must be a "
                f"{{human, tier}} mapping; got {type(item).__name__}."
            )
        for required in ("human", "tier"):
            if required not in item:
                raise AccessError(
                    f"access.yaml at {yaml_path}: `authorized[{i}]` is "
                    f"missing required field {required!r}."
                )
        handle = item["human"]
        if not isinstance(handle, str):
            raise AccessError(
                f"access.yaml at {yaml_path}: `authorized[{i}].human` must "
                f"be a string handle, got {type(handle).__name__}."
            )
        if handle not in humans_registry:
            raise AccessError(
                f"access.yaml at {yaml_path}: `authorized[{i}].human` "
                f"handle {handle!r} not found in humans.yaml. Known "
                f"handles: {humans_registry.handles()}."
            )
        if handle in seen_humans:
            raise AccessError(
                f"access.yaml at {yaml_path}: human {handle!r} listed "
                "more than once under `authorized:`."
            )
        seen_humans.add(handle)
        tier_raw = item["tier"]
        if not isinstance(tier_raw, str) or tier_raw not in _LEGAL_TIERS:
            raise AccessError(
                f"access.yaml at {yaml_path}: `authorized[{i}].tier` must "
                f"be one of {list(_LEGAL_TIERS)}, got {tier_raw!r}."
            )
        # Cross-check: only the owner may carry tier=OWNER.
        if tier_raw == AccessTier.OWNER.value and handle != owner:
            raise AccessError(
                f"access.yaml at {yaml_path}: `authorized[{i}]` gives "
                f"tier=owner to {handle!r} but the profile owner is "
                f"{owner!r}. Only one owner per profile."
            )
        entries.append(
            AccessEntry(human=handle, tier=AccessTier(tier_raw))
        )
    # Materialize the owner entry if the operator omitted it from
    # `authorized:`. Idempotent: if they DID list themselves with
    # tier=owner, the seen_humans check above already covered that path
    # and the entry is already in `entries`.
    if owner not in seen_humans:
        entries.insert(
            0, AccessEntry(human=owner, tier=AccessTier.OWNER)
        )
    return entries
