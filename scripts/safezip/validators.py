"""safezip.validators — phase-1 metadata-only security checks.

Every function here is pure over `zipfile.ZipInfo` metadata (no decompression,
no disk writes). Failure raises `ArchiveRejected`, which the coordinator maps
to exit code 77. Keeping these as free functions (not methods) makes them
trivial to audit top-to-bottom and independently unit-testable.

Two exception classes live here so they can be imported without pulling in
`extractor.py` (which imports us).
"""
import os
import stat
import zipfile
from pathlib import Path


class ArchiveRejected(Exception):
    """Wholesale-reject condition. Maps to exit code 77.

    The `reason` slug is stable, machine-friendly text (e.g. "path_traversal");
    downstream tooling keys on it.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason: str = reason
        self.detail: str = detail


class ExtractionError(Exception):
    """Tool-level failure (corrupt zip, unreadable, disk full, timeout).

    Maps to exit code 78. Distinct from `ArchiveRejected`: we could not make
    a security judgment at all, so we neither declare the archive safe nor
    declare it adversarial.
    """


def reject_if_too_many_entries(infolist: list[zipfile.ZipInfo], max_entries: int) -> None:
    """Cap entry count to defend against file-count exhaustion."""
    if len(infolist) > max_entries:
        raise ArchiveRejected(
            "too_many_entries", f"{len(infolist)} > {max_entries}"
        )


def reject_if_declared_size_bomb(
    infolist: list[zipfile.ZipInfo], max_total_uncompressed_bytes: int
) -> None:
    """Cap sum of declared uncompressed sizes.

    This catches the "declared bomb" flavor where the header is honest but
    still asking us to write far more than we're willing to. The lying-header
    flavor is caught by the streaming read cap in `extractor.py`.
    """
    total = sum(zi.file_size for zi in infolist)
    if total > max_total_uncompressed_bytes:
        raise ArchiveRejected(
            "declared_size_bomb",
            f"{total} bytes > {max_total_uncompressed_bytes}",
        )


def reject_if_compression_ratio_bomb(
    infolist: list[zipfile.ZipInfo], max_compression_ratio: int
) -> None:
    """Cap uncompressed/compressed ratio.

    An all-zero-byte archive has compress_size 0 and we skip the check to
    avoid a divide-by-zero — an empty archive is not a bomb.
    """
    total_uncompressed = sum(zi.file_size for zi in infolist)
    total_compressed = sum(zi.compress_size for zi in infolist)
    if total_compressed <= 0:
        return
    ratio = total_uncompressed / total_compressed
    if ratio > max_compression_ratio:
        raise ArchiveRejected(
            "compression_ratio_bomb",
            f"ratio={ratio:.1f}x > {max_compression_ratio}x",
        )


def reject_if_special_file(zi: zipfile.ZipInfo) -> None:
    """Reject symlink, block, char, fifo, socket entries.

    Unix mode lives in the top 16 bits of `external_attr`. If those bits are
    zero (Windows-created zip), there is nothing to check — Windows zips can't
    legally carry a symlink entry via mode bits. Verified against the Python
    packaging discussion of Info-Zip's on-disk representation.
    """
    mode = zi.external_attr >> 16
    if mode == 0:
        return
    if stat.S_ISLNK(mode):
        raise ArchiveRejected("symlink_entry", f"{zi.filename} is a symlink")
    if (
        stat.S_ISBLK(mode)
        or stat.S_ISCHR(mode)
        or stat.S_ISFIFO(mode)
        or stat.S_ISSOCK(mode)
    ):
        raise ArchiveRejected(
            "special_file_entry", f"{zi.filename} is a device/fifo/socket"
        )


def reject_if_traversal(zi: zipfile.ZipInfo, dest_dir: Path, dest_real: str) -> None:
    """Belt+suspenders path-traversal check on top of stdlib's sanitizer.

    We do the check ourselves because (a) we want to reject the whole archive
    on any attempt (stdlib silently rewrites), and (b) we don't want to have
    to trust that the running Python's version of the sanitizer is a fixed one.
    """
    name = zi.filename
    orig = getattr(zi, "orig_filename", "") or ""
    # ANY ASCII control character in a filename is adversarial. C0 controls
    # (\x00-\x1f) include NUL, TAB, LF, CR — and a filename with an embedded
    # newline is a prompt-injection breakout vector (Landline's archive
    # frame is line-delimited). \x7f (DEL) is the C0 boundary companion.
    # Check BOTH `zi.filename` (CP437-decoded, which silently truncates at
    # NUL) AND `orig_filename` (raw bytes) — a crafted `good\x00.txt` gets
    # `.filename == "good"`, but `.orig_filename` carries the honest signal.
    def _has_ctrl(s) -> bool:
        # bytes on some paths, str on most — normalize both to a chr scan.
        if isinstance(s, bytes):
            return any(b <= 0x1f or b == 0x7f for b in s)
        return any(ord(ch) <= 0x1f or ord(ch) == 0x7f for ch in s)

    if _has_ctrl(name) or _has_ctrl(orig):
        raise ArchiveRejected(
            "invalid_filename",
            f"control char in name: {name!r}",
        )
    if name.startswith("/") or name.startswith("\\") or Path(name).is_absolute():
        raise ArchiveRejected("path_traversal", f"absolute path: {name}")
    # Split on both separators so a Windows-style entry inside a zip on
    # macOS still trips.
    parts = name.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        raise ArchiveRejected("path_traversal", f"parent-dir component: {name}")
    candidate = os.path.realpath(os.path.join(str(dest_dir), name))
    try:
        common = os.path.commonpath([candidate, dest_real])
    except ValueError:
        # Different drives on Windows -> obviously not under dest.
        raise ArchiveRejected("path_traversal", f"escapes dest: {name}")
    if common != dest_real:
        raise ArchiveRejected("path_traversal", f"escapes dest: {name}")


def validate_structural(
    infolist: list[zipfile.ZipInfo],
    dest_dir: Path,
    max_entries: int,
    max_total_uncompressed_bytes: int,
    max_compression_ratio: int,
) -> None:
    """Run every phase-1 metadata-only check in order.

    Order matters: cheapest and most-informative checks first (entry count,
    declared totals) so we fail fast without walking the member list twice.
    """
    reject_if_too_many_entries(infolist, max_entries)
    reject_if_declared_size_bomb(infolist, max_total_uncompressed_bytes)
    reject_if_compression_ratio_bomb(infolist, max_compression_ratio)
    dest_real = os.path.realpath(dest_dir)
    for zi in infolist:
        reject_if_special_file(zi)
        reject_if_traversal(zi, dest_dir, dest_real)
