"""safezip.extractor — the security-hardened extraction coordinator.

Design principle: **inspect-then-write**. We never call `ZipFile.extractall()`
and we never write a member before it has cleared every check that applies to
it. Adversarial structural conditions (traversal, symlinks, declared/actual
bombs, entry-count explosion) reject the *whole* archive via `ArchiveRejected`
(exit 77). Benign-but-disallowed content (wrong extension, magic mismatch,
binary posing as text) skips just that member with a reason (still exit 0).

Why phase 1 vs phase 2 matters: phase 1 (see `validators.py`) reads only
`ZipInfo` metadata — no decompression, no disk writes — so we can bail out
cheaply. Phase 2 opens each allowed member and streams it with a hard byte
cap, which is the real bomb defense: trusting `file_size` alone is a well-
known trap (a lying header sails past phase 1 and only the bounded read
catches the actual explosion).

Cleanup / no-rm invariant: this workspace forbids `rm`, `os.remove`, and
`shutil.rmtree`. Because we inspect-then-write, nothing unsafe ever lands on
disk, so no cleanup dance is needed. If extraction bails partway through
phase 2, the already-written clean members stay in place under
`cache/safezip/<hash>/` — a retention sweep handles that cache dir.
"""
import logging
import os
import shutil
import time
import zipfile
import zlib
from pathlib import Path

from scripts.safezip.config import (
    FREE_DISK_HEADROOM_MULT,
    MAGIC_SIGNATURES,
    MAX_ARCHIVE_FILE_BYTES,
    MAX_COMPRESSION_RATIO,
    MAX_ENTRIES,
    MAX_MEMBER_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    READ_CHUNK_SIZE,
    SAFEZIP_ALLOWED_EXTENSIONS,
    SAFEZIP_DIR_MODE,
    SAFEZIP_EXTRACT_TIMEOUT_SECONDS,
    TEXT_LIKE_EXTENSIONS,
)
from scripts.safezip.content_verifier import verify_magic
from scripts.safezip.manifest import (
    ExtractedMember,
    ExtractionManifest,
    SkippedMember,
    Verdict,
)
from scripts.safezip.validators import (
    ArchiveRejected,
    ExtractionError,
    validate_structural,
)

# Re-export the exception classes so callers can `from scripts.safezip.extractor
# import ArchiveRejected, ExtractionError` without knowing they live in
# `validators.py`. Keeps the public surface tidy after the split.
__all__ = [
    "ArchiveRejected",
    "ExtractionError",
    "SafeZipExtractor",
    "classify_type",
    "SKIP_EXTENSION_NOT_ALLOWED",
    "SKIP_MAGIC_MISMATCH",
    "SKIP_TEXT_SNIFF_FAILED",
    "SKIP_EMPTY_FILE",
]

logger = logging.getLogger(__name__)


# Stable skip-reason slugs — downstream tooling keys on them.
SKIP_EXTENSION_NOT_ALLOWED: str = "extension_not_allowed"
SKIP_MAGIC_MISMATCH: str = "magic_mismatch"
SKIP_TEXT_SNIFF_FAILED: str = "text_sniff_failed"
SKIP_EMPTY_FILE: str = "empty_file"


# Type tags for the manifest — normalized human-friendly buckets, not raw ext.
_IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
})


def classify_type(extension: str) -> str:
    """Bucket an extension into a coarse type tag for the manifest."""
    ext = extension.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in TEXT_LIKE_EXTENSIONS:
        return "text"
    return "other"


class SafeZipExtractor:
    """Security-hardened wrapper around stdlib zipfile.

    Constructor arguments override the defaults in `config.py`. Tests inject
    small caps to trigger bomb / traversal scenarios with fixture-sized zips
    instead of monkeypatching the config module (cleaner, thread-safe).

    Usage:
        extractor = SafeZipExtractor()
        manifest = extractor.extract(archive_path, dest_dir)
        if manifest.verdict is Verdict.EXTRACTED:
            ...
    """

    def __init__(
        self,
        max_entries: int = MAX_ENTRIES,
        max_total_uncompressed_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES,
        max_compression_ratio: int = MAX_COMPRESSION_RATIO,
        max_member_bytes: int = MAX_MEMBER_BYTES,
        max_archive_file_bytes: int = MAX_ARCHIVE_FILE_BYTES,
        extract_timeout_seconds: float = SAFEZIP_EXTRACT_TIMEOUT_SECONDS,
        allowed_extensions: frozenset[str] = SAFEZIP_ALLOWED_EXTENSIONS,
        magic_signatures: dict[str, tuple[int, tuple[bytes, ...]]] | None = None,
        free_disk_headroom_mult: int = FREE_DISK_HEADROOM_MULT,
    ):
        self.max_entries = max_entries
        self.max_total_uncompressed_bytes = max_total_uncompressed_bytes
        self.max_compression_ratio = max_compression_ratio
        self.max_member_bytes = max_member_bytes
        self.max_archive_file_bytes = max_archive_file_bytes
        self.extract_timeout_seconds = extract_timeout_seconds
        self.allowed_extensions = frozenset(e.lower() for e in allowed_extensions)
        self.magic_signatures = (
            magic_signatures if magic_signatures is not None else MAGIC_SIGNATURES
        )
        self.free_disk_headroom_mult = free_disk_headroom_mult

    # ---------------- public entrypoint ---------------------------------

    def extract(self, archive_path: Path, dest_dir: Path) -> ExtractionManifest:
        """Validate + extract `archive_path` into `dest_dir`.

        Returns an `ExtractionManifest`. Raises `ArchiveRejected` (adversarial
        or malformed structure) or `ExtractionError` (tool-level failure).
        """
        archive_path = Path(archive_path).resolve()
        dest_dir = Path(dest_dir).resolve()

        if not archive_path.is_file():
            raise ExtractionError(f"archive not readable: {archive_path}")

        # Upfront on-disk-size guard. `zipfile.ZipFile()` parses the entire
        # central directory into memory (one Python `ZipInfo` object per
        # entry, ~200-400 bytes), so an archive with hundreds of thousands
        # of tiny entries will OOM long before MAX_ENTRIES fires. Refusing
        # any archive above this cap turns that attack into a clean exit 78.
        try:
            file_size = archive_path.stat().st_size
        except OSError as exc:
            raise ExtractionError(f"cannot stat archive: {exc}") from exc
        if file_size > self.max_archive_file_bytes:
            raise ExtractionError(
                f"archive file too large: {file_size} > {self.max_archive_file_bytes} bytes"
            )

        self._ensure_dest_dir(dest_dir)
        self._check_free_disk(dest_dir)

        deadline = time.monotonic() + self.extract_timeout_seconds

        try:
            zf = zipfile.ZipFile(archive_path, "r")
        except zipfile.BadZipFile as exc:
            raise ExtractionError(f"not a valid zip: {exc}") from exc
        except OSError as exc:
            raise ExtractionError(f"cannot open archive: {exc}") from exc

        with zf:
            infolist = zf.infolist()
            validate_structural(
                infolist,
                dest_dir,
                self.max_entries,
                self.max_total_uncompressed_bytes,
                self.max_compression_ratio,
            )
            manifest = self._extract_members(zf, infolist, dest_dir, deadline)

        manifest.archive = str(archive_path)
        manifest.dest = str(dest_dir)
        return manifest

    # ---------------- phase 2: per-member extraction --------------------

    def _extract_members(
        self,
        zf: zipfile.ZipFile,
        infolist: list[zipfile.ZipInfo],
        dest_dir: Path,
        deadline: float,
    ) -> ExtractionManifest:
        """Stream each allowed member with a hard byte cap; skip disallowed."""
        manifest = ExtractionManifest(
            verdict=Verdict.EXTRACTED,
            archive="",  # filled by caller
            dest=str(dest_dir),
        )
        for zi in infolist:
            self._check_deadline(deadline)
            if zi.is_dir():
                # Directory entries carry no content; create the dir under
                # dest only if it has a legitimate name (already vetted by
                # phase 1). Wrap in OSError catch: a pathologically long
                # or otherwise OS-rejected path bubbles up as a tool error
                # rather than a raw crash.
                target_dir = dest_dir / zi.filename
                try:
                    self._mkdir_owner_only(target_dir, dest_dir)
                except OSError as exc:
                    raise ExtractionError(
                        f"cannot create directory entry {zi.filename}: {exc}"
                    ) from exc
                continue

            member_name = zi.filename
            extension = Path(member_name).suffix.lower()

            # Extension allowlist -> skip if miss (phase-2, not reject).
            if extension not in self.allowed_extensions:
                manifest.skipped.append(
                    SkippedMember(name=member_name, reason=SKIP_EXTENSION_NOT_ALLOWED)
                )
                continue

            # Bounded streaming read — the real bomb defense.
            # Broad catch here maps every non-adversarial malformed-member
            # failure to a clean exit 78:
            #   - `zipfile.BadZipFile`   -- corrupted central-directory link
            #   - `zlib.error`           -- corrupt DEFLATE payload
            #   - `NotImplementedError`  -- unsupported compression method
            #                                 (e.g. method 99 = WinZip AES)
            #   - `RuntimeError`         -- encrypted-without-password
            #   - `EOFError`             -- truncated stream
            #   - `OSError`              -- underlying I/O failure
            # ArchiveRejected (the per_member_bomb signal from
            # `_read_bounded`) is deliberately not in the tuple so it still
            # propagates to the phase-1/phase-2 boundary and out as exit 77.
            try:
                content = self._read_bounded(zf, zi, deadline)
            except ArchiveRejected:
                raise  # bomb -> whole-archive reject; keep bubbling
            except (
                zipfile.BadZipFile,
                zlib.error,
                NotImplementedError,
                RuntimeError,
                EOFError,
                OSError,
            ) as exc:
                raise ExtractionError(
                    f"member decompression failed ({zi.filename}): {exc}"
                ) from exc

            if len(content) == 0:
                # Empty files aren't dangerous, but they're not what a user
                # meant to send either; skip them with an obvious reason.
                manifest.skipped.append(
                    SkippedMember(name=member_name, reason=SKIP_EMPTY_FILE)
                )
                continue

            # Magic-byte / text-sniff content verification.
            if not verify_magic(extension, content, self.magic_signatures):
                reason = (
                    SKIP_TEXT_SNIFF_FAILED
                    if extension in TEXT_LIKE_EXTENSIONS
                    else SKIP_MAGIC_MISMATCH
                )
                manifest.skipped.append(
                    SkippedMember(name=member_name, reason=reason)
                )
                continue

            # All clear — write the member.
            self._write_member(dest_dir, member_name, content)
            manifest.extracted.append(
                ExtractedMember(
                    name=member_name,
                    size=len(content),
                    type=classify_type(extension),
                )
            )

        return manifest

    def _read_bounded(
        self,
        zf: zipfile.ZipFile,
        zi: zipfile.ZipInfo,
        deadline: float,
    ) -> bytes:
        """Stream a member; abort as an archive bomb if it overshoots the cap.

        We ignore `zi.file_size` and enforce our own cap on real bytes read,
        because the whole point of a compression bomb is a lying header. If
        the running total exceeds `max_member_bytes` we raise ArchiveRejected
        — a member that big is adversarial regardless of the surrounding
        archive's declared totals.
        """
        chunks: list[bytes] = []
        total = 0
        with zf.open(zi, "r") as member_fh:
            while True:
                self._check_deadline(deadline)
                chunk = member_fh.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_member_bytes:
                    raise ArchiveRejected(
                        "per_member_bomb",
                        f"{zi.filename}: real size > {self.max_member_bytes} bytes",
                    )
                chunks.append(chunk)
        return b"".join(chunks)

    def _write_member(self, dest_dir: Path, member_name: str, content: bytes) -> None:
        """Write a cleared member to disk with owner-only permissions.

        Every OS-facing call (`mkdir`, `open`) is wrapped so the tool always
        exits 78 with a clean message instead of crashing with a traceback:
          - overlong path components -> `OSError` (ENAMETOOLONG)
          - a file/dir name collision inside the same archive
            (`foo.pdf` file plus `foo.pdf/bar.pdf` dir-entry) ->
            `FileExistsError` on `mkdir` or `NotADirectoryError` on `open`
          - `ValueError('embedded null byte')` from `open()` if a crafted
            filename survived phase 1 (defense in depth; phase 1 also rejects)
        """
        target_path = dest_dir / member_name
        try:
            self._mkdir_owner_only(target_path.parent, dest_dir)
        except OSError as exc:
            raise ExtractionError(
                f"cannot create parent directory for {member_name}: {exc}"
            ) from exc
        try:
            with open(target_path, "wb") as out_fh:
                out_fh.write(content)
        except (OSError, ValueError) as exc:
            raise ExtractionError(
                f"cannot write member {member_name}: {exc}"
            ) from exc
        try:
            os.chmod(target_path, 0o600)
        except OSError:
            # Non-fatal; the parent dir is already 0o700 which is the
            # actual containment control.
            pass

    def _mkdir_owner_only(self, target_dir: Path, dest_dir: Path) -> None:
        """Create `target_dir` (with parents) and force 0o700 on every level.

        `Path.mkdir(parents=True, mode=0o700)` only applies the mode to the
        leaf; intermediate ancestors get 0o755. This helper walks each level
        from `target_dir` up to (but not including) `dest_dir` and enforces
        `SAFEZIP_DIR_MODE` on it, so a `docs/2026/august/notes.txt` member
        can't leave a world-readable `docs/` or `docs/2026/` behind.

        Idempotent: `exist_ok=True`, and chmod is retried on every call.
        """
        target_dir.mkdir(parents=True, exist_ok=True, mode=SAFEZIP_DIR_MODE)
        # Walk from target_dir upward, stopping when we reach dest_dir.
        # dest_dir itself is chmod'd by `_ensure_dest_dir`; enforcing again
        # here would be redundant and might touch a caller-supplied path.
        dest_resolved = dest_dir.resolve()
        current = target_dir.resolve()
        while current != dest_resolved:
            try:
                os.chmod(current, SAFEZIP_DIR_MODE)
            except OSError:
                # Non-fatal per-level; the outer dest is already 0o700.
                pass
            parent = current.parent
            if parent == current:
                # Reached filesystem root without finding dest_dir — should
                # not happen (target_dir is under dest_dir by construction),
                # but bail rather than looping forever.
                break
            current = parent

    # ---------------- housekeeping --------------------------------------

    def _ensure_dest_dir(self, dest_dir: Path) -> None:
        """Create the destination dir with owner-only mode, idempotently."""
        try:
            dest_dir.mkdir(parents=True, exist_ok=True, mode=SAFEZIP_DIR_MODE)
        except OSError as exc:
            raise ExtractionError(
                f"cannot create dest dir {dest_dir}: {exc}"
            ) from exc
        # `mkdir(mode=...)` only sets mode on new dirs; enforce it on existing.
        try:
            os.chmod(dest_dir, SAFEZIP_DIR_MODE)
        except OSError:
            pass

    def _check_free_disk(self, dest_dir: Path) -> None:
        """Refuse if free disk < headroom * max total uncompressed."""
        try:
            usage = shutil.disk_usage(dest_dir)
        except OSError as exc:
            raise ExtractionError(f"cannot stat dest filesystem: {exc}") from exc
        required = self.free_disk_headroom_mult * self.max_total_uncompressed_bytes
        if usage.free < required:
            raise ExtractionError(
                f"insufficient free disk: {usage.free} < {required} bytes"
            )

    def _check_deadline(self, deadline: float) -> None:
        """Wall-clock watchdog check (called between members and inside reads).

        Time-based check instead of `signal.alarm` because `alarm()` requires
        the main thread — a signal-based deadline breaks under pytest workers
        and any future embedding that runs the extractor off the main thread.
        """
        if time.monotonic() > deadline:
            raise ExtractionError(
                f"extraction exceeded {self.extract_timeout_seconds}s timeout"
            )
