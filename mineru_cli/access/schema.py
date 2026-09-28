"""Dataclasses + enum for the per-profile access allowlist.

`AccessTier` is a `(str, Enum)` per the project Python style guide
(never `IntEnum`; the explicit two-base form is the standard here).

Phase 1 (owner-only):
  - `OWNER` is enforced.
  - `GUEST` is present in the enum as a documented, DEFERRED slot so
    downstream code can already reference `AccessTier.GUEST`; no
    guest-tier enforcement is wired in this phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class AccessTier(str, Enum):
    """Access tier for one authorized human.

    Values are stringly-typed for stable YAML serialization (a numeric
    enum value would drift if the enum grew mid-phase).

    Members:
        OWNER: the profile's owner. Full access, no scoping. Exactly one
            human per profile carries this tier.
        GUEST: DEFERRED — reserved for later phases. Landline will scope
            guest-tier humans to a toolset subset; this phase does NOT
            enforce anything for guests. Present in the enum so
            access.yaml can already name the tier.
    """

    OWNER = "owner"
    GUEST = "guest"  # DEFERRED — reserved for later, unenforced in Phase 1


@dataclass(frozen=True)
class AccessEntry:
    """One authorized `{human, tier}` pair from access.yaml."""

    human: str  # a handle from humans.yaml
    tier: AccessTier


@dataclass(frozen=True)
class AccessConfig:
    """Per-profile access allowlist.

    Attributes:
        profile_name: the profile this allowlist belongs to (matches the
            containing directory name). Cross-validated on load.
        owner: handle of the profile owner (present in humans.yaml).
        authorized: full list of `{human, tier}` entries, including the
            owner (the loader ensures the owner appears in the list with
            `tier=OWNER`, so downstream consumers can iterate uniformly).
    """

    profile_name: str
    owner: str
    authorized: List[AccessEntry] = field(default_factory=list)

    def owner_entry(self) -> AccessEntry:
        """Return the owner's `AccessEntry` (guaranteed to exist)."""
        for entry in self.authorized:
            if entry.human == self.owner and entry.tier == AccessTier.OWNER:
                return entry
        # Loader guarantees this exists; treat as an internal invariant.
        raise RuntimeError(
            f"access config for profile {self.profile_name!r} has no OWNER "
            f"entry for {self.owner!r} — loader invariant violated."
        )

    def guest_entries(self) -> List[AccessEntry]:
        """Return every non-owner entry (`tier=GUEST`).

        Phase 1: no enforcement. Provided so downstream code can already
        introspect the list without waiting for the enforcement wiring.
        """
        return [e for e in self.authorized if e.tier == AccessTier.GUEST]
