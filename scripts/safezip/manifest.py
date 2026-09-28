"""safezip.manifest — typed extraction result shared between extractor and CLI.

Kept as a leaf module (no imports from elsewhere in the package) so extractor.py
and cli.py both depend on it without risking an import cycle. The JSON emitted
by the CLI is a direct serialization of `ExtractionManifest.to_dict()`, so this
file is also the source of truth for the CLI output contract.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """Wholesale verdict for the archive.

    EXTRACTED: at least the extraction attempt reached phase-2 without a
        wholesale-reject condition. Individual members may still have been
        skipped (extension/magic/text-sniff). Exit code 0.
    REJECTED: the archive tripped a phase-1 or bomb / traversal / symlink
        check. Nothing was written. Exit code 77.
    """

    EXTRACTED = "extracted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ExtractedMember:
    """A member that passed every check and was written to disk."""

    name: str      # workspace-relative path inside the extraction dir
    size: int      # bytes actually written (may differ from declared file_size)
    type: str      # normalized type tag: "pdf", "png", "text", "image", ...


@dataclass(frozen=True)
class SkippedMember:
    """A member that was inspected and refused, but did NOT reject the archive.

    Skip reasons cover: extension not on allowlist, magic-bytes mismatch, text-
    sniff failed. Adversarial conditions (traversal, symlink, bomb) reject the
    whole archive instead of populating this list.
    """

    name: str
    reason: str    # short, machine-friendly reason ("extension_not_allowed", ...)


@dataclass(frozen=True)
class Totals:
    """Aggregate counts across the extraction pass."""

    extracted: int
    skipped: int
    total_uncompressed_bytes: int


@dataclass
class ExtractionManifest:
    """Structured result of a `SafeZipExtractor.extract()` call.

    `reject_reason` is None for EXTRACTED and a short slug for REJECTED
    (e.g. "too_many_entries", "path_traversal", "symlink_entry",
    "declared_size_bomb", "compression_ratio_bomb", "per_member_bomb"). This
    is deliberately a *slug*, not free text, so downstream tooling can key on it.
    """

    verdict: Verdict
    archive: str
    dest: str
    reject_reason: str | None = None
    extracted: list[ExtractedMember] = field(default_factory=list)
    skipped: list[SkippedMember] = field(default_factory=list)

    @property
    def totals(self) -> Totals:
        """Derived totals; recomputed on read so lists stay the source of truth."""
        return Totals(
            extracted=len(self.extracted),
            skipped=len(self.skipped),
            total_uncompressed_bytes=sum(m.size for m in self.extracted),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (the CLI's --json output contract)."""
        totals = self.totals
        return {
            "verdict": self.verdict.value,
            "archive": self.archive,
            "dest": self.dest,
            "reject_reason": self.reject_reason,
            "extracted": [
                {"name": m.name, "size": m.size, "type": m.type}
                for m in self.extracted
            ],
            "skipped": [
                {"name": m.name, "reason": m.reason} for m in self.skipped
            ],
            "totals": {
                "extracted": totals.extracted,
                "skipped": totals.skipped,
                "total_uncompressed_bytes": totals.total_uncompressed_bytes,
            },
        }
