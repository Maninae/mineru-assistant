"""`Human` + `HumansRegistry` dataclasses for the machine-level registry.

Humans are DELIBERATELY thin: a handle, a Telegram identity, and a
display name. No per-user directories, memories, or cron. That state
lives on agent profiles instead. Keeping humans thin means adding a
family member or guest is a one-line YAML edit, not a `profile init`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List


@dataclass(frozen=True)
class Human:
    """Immutable record for one entry in `humans.yaml`.

    Attributes:
        handle: short, safe identifier the access allowlist keys off
            (e.g. `"sam"`, `"mira"`). Matches `[A-Za-z0-9_-]+`.
        telegram_id: numeric Telegram user ID. Load-bearing: this is
            the ONLY identity the Landline daemon uses to authorize
            an inbound message when access enforcement lands.
        display_name: human-facing label ("Sam Rivera"). Never used as
            an identifier; free-form.
    """

    handle: str
    telegram_id: int
    display_name: str


@dataclass(frozen=True)
class HumansRegistry:
    """Immutable collection of every `Human` known to this machine.

    Backed by an internal dict keyed by handle for O(1) lookups. Also
    iterable so callers can list all humans in insertion order (YAML
    file order).
    """

    entries_by_handle: Dict[str, Human] = field(default_factory=dict)

    def __iter__(self) -> Iterator[Human]:
        return iter(self.entries_by_handle.values())

    def __len__(self) -> int:
        return len(self.entries_by_handle)

    def __contains__(self, handle: object) -> bool:
        return isinstance(handle, str) and handle in self.entries_by_handle

    def get(self, handle: str) -> Human:
        """Return the `Human` with `handle`, or raise `KeyError`.

        A missing handle is a genuine "not found" — the caller should
        translate to a domain-specific error (e.g. an access.yaml that
        names an unknown owner should raise `AccessError`, not KeyError).
        """
        return self.entries_by_handle[handle]

    def handles(self) -> List[str]:
        """Return every handle in insertion order."""
        return list(self.entries_by_handle.keys())

    def as_iter(self) -> Iterable[Human]:
        """Explicit iteration entry point (mirrors registry conventions elsewhere)."""
        return self.entries_by_handle.values()
