"""safezip.content_verifier — phase-2 per-member content checks.

Two pure functions: `verify_magic` for binary types (leading bytes must match
a known signature for the extension) and `sniff_text` for text-like types
(no NUL byte in the sniffed prefix, decodes as UTF-8 or Latin-1). The goal
of both is defeating extension spoofing: a `.png` that's actually a shell
script or a `.txt` that's actually a binary payload gets rejected before the
file is written to disk.

Kept as free functions in a leaf module so `extractor.py` calls into them
without any circular-import risk. No logging here; the caller reports the
skip reason to the manifest.
"""
import logging

from scripts.safezip.config import (
    TEXT_LIKE_EXTENSIONS,
    TEXT_SNIFF_BYTES,
    WEBP_TAG,
    WEBP_TAG_OFFSET,
)

logger = logging.getLogger(__name__)


def verify_magic(
    extension: str,
    content: bytes,
    magic_signatures: dict[str, tuple[int, tuple[bytes, ...]]],
) -> bool:
    """Return True if `content` matches a known magic signature for `extension`.

    Text-like extensions are delegated to `sniff_text` — they don't have
    reliable magic bytes.

    An allowlisted extension that has no magic entry is a config bug: we fail
    closed (return False) rather than silently let it through. The extractor
    logs the config warning at the call site.
    """
    if extension in TEXT_LIKE_EXTENSIONS:
        return sniff_text(content)

    signature = magic_signatures.get(extension)
    if signature is None:
        logger.warning("no magic signature for allowlisted extension %s", extension)
        return False

    offset, acceptable_prefixes = signature
    max_prefix_len = max(len(p) for p in acceptable_prefixes)
    window = content[offset : offset + max_prefix_len]
    if not any(window.startswith(p) for p in acceptable_prefixes):
        return False

    # WEBP is the one two-part signature: RIFF at offset 0 AND WEBP tag at 8.
    # We treat the offset-8 check as an add-on constraint alongside the
    # config's offset-0 RIFF match.
    if extension == ".webp":
        tag_window = content[WEBP_TAG_OFFSET : WEBP_TAG_OFFSET + len(WEBP_TAG)]
        if tag_window != WEBP_TAG:
            return False

    return True


def sniff_text(content: bytes) -> bool:
    """Heuristic text detector for extensions with no reliable magic.

    Rejects on any NUL byte in the sampled prefix (a strong binary tell), and
    requires the sample decodes as UTF-8 or Latin-1. Latin-1 is the fallback
    because it can decode ANY byte sequence — so the NUL check does the
    actual work and the decode gate mostly rejects impossibly-malformed UTF-8
    in files that also lack NULs.
    """
    sample = content[:TEXT_SNIFF_BYTES]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    try:
        sample.decode("latin-1")
        return True
    except UnicodeDecodeError:
        return False
