"""safezip.config — caps, allowlist, and magic-byte signatures.

Every knob the extractor honors lives here. Tests inject overrides via the
`SafeZipExtractor` constructor rather than monkeypatching, so these are the
production defaults — do not lower them for a test's convenience, pass explicit
constructor args instead.

Threat map (the caps map 1:1 to the checks in extractor.py):
  - MAX_ENTRIES               file-count exhaustion (millions of empty entries)
  - MAX_TOTAL_UNCOMPRESSED_BYTES   declared-size zip bomb (sum of file_size)
  - MAX_COMPRESSION_RATIO     compression-ratio bomb (small archive, huge unpack)
  - MAX_MEMBER_BYTES          real zip bomb where file_size lies (bounded read)
  - SAFEZIP_EXTRACT_TIMEOUT_SECONDS   pathological CPU cost during decompress
  - SAFEZIP_ALLOWED_EXTENSIONS         only known-benign, non-executable types
  - MAGIC_SIGNATURES           extension-vs-content spoofing (binary types)
  - TEXT_LIKE_EXTENSIONS       extensions with no reliable magic; sniffed instead
"""
import os
from pathlib import Path

# ============================================================================
# Structural caps (phase-1 pre-validation)
# ============================================================================

# Max number of entries in the archive. Bigger than any legitimate
# small-doc dump we ship over Telegram; small enough to reject an
# entry-explosion attack that would just OOM the ZipInfo list itself.
MAX_ENTRIES: int = 1024

# Max sum of declared uncompressed sizes across all members.
# 100 MB is comfortably above real user payloads (a book-length PDF is ~30 MB)
# and well below the disk-headroom guard (2x this value).
MAX_TOTAL_UNCOMPRESSED_BYTES: int = 100 * 1024 * 1024

# Compression-ratio bomb defense: sum(file_size) / sum(compress_size).
# 100:1 is a comfortable ceiling — text compresses ~4-6:1, PDF ~2-3:1,
# already-compressed images ~1:1. A ratio above 100 is essentially always
# either a zip of zeros or an adversarial nested-compression bomb.
MAX_COMPRESSION_RATIO: int = 100

# ============================================================================
# Per-member caps (phase-2 read-time)
# ============================================================================

# Hard upper bound on decompressed bytes we will ever read for a single member.
# Enforced during zf.open() streaming, so a member with a lying file_size that
# passed phase-1 still gets caught the moment it exceeds this on the wire.
# 50 MB is above any single-file allowlisted asset we expect (a heavy PDF)
# yet still bounded so no member can eat all available RAM.
MAX_MEMBER_BYTES: int = 50 * 1024 * 1024

# Chunk size for the bounded per-member read. 64 KiB is the usual sweet spot
# for stream reads (page-aligned, cache-friendly, cheap loop overhead).
READ_CHUNK_SIZE: int = 64 * 1024

# ============================================================================
# Upfront archive-file guard (before any parsing)
# ============================================================================

# Hard cap on the on-disk size of the archive itself, checked BEFORE we hand
# the path to `zipfile.ZipFile()`. Without this, an adversary can craft a
# small-ish archive with hundreds of thousands of tiny entries; stdlib parses
# the entire central directory into RAM (each ZipInfo is a Python object,
# ~200-400 bytes), amplifying memory ~7x before MAX_ENTRIES ever fires. Any
# archive we're asked to inspect that is larger than this is refused as a
# tool error (exit 78) — Telegram-forwarded docs are nowhere near this.
MAX_ARCHIVE_FILE_BYTES: int = 100 * 1024 * 1024

# ============================================================================
# Wall-clock and disk guards
# ============================================================================

# Total extraction budget. Bounded via monotonic-clock checks between members
# and inside the bounded per-member read loop (no signals, so it works from
# any thread and from pytest). A pathological codec can still burn a few
# seconds inside a single zf.read() call; the cap here is per-tool, not per-syscall.
SAFEZIP_EXTRACT_TIMEOUT_SECONDS: float = 30.0

# Disk-headroom multiplier. Before writing anything we require
# shutil.disk_usage(dest).free >= FREE_DISK_HEADROOM_MULT * MAX_TOTAL_UNCOMPRESSED_BYTES.
# 2x lets a max-size archive fully land AND leaves headroom for whatever else
# the machine is doing (Landline daemon, logs, other caches).
FREE_DISK_HEADROOM_MULT: int = 2

# ============================================================================
# Allowlist — what we agree to extract at all
# ============================================================================

# Case-insensitive extension allowlist. Docs the Telegram daemon actually
# forwards (pdf/txt/md/csv/json/log) plus common multimodal image types
# (png/jpg/jpeg/gif/webp/heic/heif) plus a couple structured-text extras we
# routinely see (tsv/yaml/yml). Deliberately excludes archives, executables,
# scripts, office formats with macros, and anything else that could either
# recurse (nested zip) or execute on open.
SAFEZIP_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".log",
    ".tsv",
    ".yaml",
    ".yml",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
})

# Extensions with no reliable magic bytes. These are sniffed by "no NUL byte
# in the sampled prefix + decodes as UTF-8 or Latin-1" instead of by magic
# match. Rejecting a binary masquerading as .txt is the goal.
TEXT_LIKE_EXTENSIONS: frozenset[str] = frozenset({
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".log",
    ".tsv",
    ".yaml",
    ".yml",
})

# ============================================================================
# Magic-byte signatures — extension-vs-content verification for binary types
# ============================================================================

# Each entry is (offset, list-of-acceptable-byte-prefixes). A member matches
# if any prefix in the list is present at the given offset. Signatures cross-
# checked against filesignature.org and the ISOBMFF/HEIF spec (ftyp brands
# heic/heix/hevc/hevx for HEIC; mif1/msf1/heif/heim/heix for HEIF).
#
# WEBP is special: 4-byte "RIFF" at offset 0 AND 4-byte "WEBP" at offset 8.
# We express this as an offset-0 check for RIFF and enforce the WEBP tag in
# `verify_magic()` in the extractor (kept alongside the check, not the config).
MAGIC_SIGNATURES: dict[str, tuple[int, tuple[bytes, ...]]] = {
    ".pdf":  (0, (b"%PDF-",)),
    ".png":  (0, (b"\x89PNG\r\n\x1a\n",)),
    ".jpg":  (0, (b"\xff\xd8\xff",)),
    ".jpeg": (0, (b"\xff\xd8\xff",)),
    ".gif":  (0, (b"GIF87a", b"GIF89a")),
    ".webp": (0, (b"RIFF",)),  # WEBP tag at offset 8 verified in extractor
    ".heic": (4, (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftyphevx", b"ftypmif1")),
    ".heif": (4, (b"ftypmif1", b"ftypmsf1", b"ftypheif", b"ftypheim", b"ftypheic", b"ftypheix")),
}

# Byte sequence expected at offset 8 for a well-formed WEBP (RIFF container).
WEBP_TAG_OFFSET: int = 8
WEBP_TAG: bytes = b"WEBP"

# ============================================================================
# Content-sniffing bounds for text-like types
# ============================================================================

# Max bytes to sniff for the text-vs-binary decision. 8 KiB is plenty to
# catch a NUL byte or a garbled binary header; going higher just spends CPU
# on files that already look fine.
TEXT_SNIFF_BYTES: int = 8 * 1024

# ============================================================================
# Cache directory (per-archive extraction dirs land under this)
# ============================================================================

MINERU_ROOT: Path = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
SAFEZIP_CACHE_DIR: Path = MINERU_ROOT / "cache" / "safezip"

# Owner-only mode for the cache root and per-archive dirs. Mirrors the firewall
# cache invariant (0o700) so nothing in this tree is world-readable.
SAFEZIP_DIR_MODE: int = 0o700
