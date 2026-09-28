"""safezip.cli — argparse frontend for `bin/safe-unzip`.

The CLI is a thin veneer over `SafeZipExtractor`: it picks a destination
directory (either `--dest` or a per-archive slot under
`cache/safezip/<hash>/`), invokes the extractor, and renders either a human
summary or a JSON manifest. All security logic lives in extractor.py; this
module only decides where things go and how the result is printed.

Exit code contract (matches the firewall wrapper convention):
    0  extracted (some members may have been skipped with reasons)
    77 archive rejected wholesale (bomb / traversal / symlink / adversarial)
    78 tool error (not a valid zip, unreadable, disk full, timeout)
"""
import argparse
import hashlib
import json
import logging
import sys
from enum import Enum
from pathlib import Path

from scripts.safezip.config import SAFEZIP_CACHE_DIR
from scripts.safezip.extractor import (
    ArchiveRejected,
    ExtractionError,
    SafeZipExtractor,
)
from scripts.safezip.manifest import ExtractionManifest, Verdict

logger = logging.getLogger(__name__)


class ExitCode(int, Enum):
    """Exit codes exposed to the shell. Mirrors the firewall wrapper contract."""

    OK = 0
    REJECTED = 77
    TOOL_ERROR = 78


# 16 hex chars of SHA-256 is more than enough to disambiguate archives without
# turning the path into an unreadable wall of hex.
HASH_PREFIX_LEN: int = 16
HASH_CONTENT_SAMPLE: int = 4 * 1024  # first 4 KiB of file content contributes to the hash


def archive_slug(archive_path: Path) -> str:
    """Stable per-archive slug used to derive the default dest dir.

    Uses SHA-256 over the absolute path plus the first 4 KiB of file content
    so two files with the same name but different content don't collide, and
    two identical files reuse the same dir (idempotent re-run).
    """
    hasher = hashlib.sha256()
    hasher.update(str(archive_path.resolve()).encode("utf-8"))
    try:
        with open(archive_path, "rb") as fh:
            hasher.update(fh.read(HASH_CONTENT_SAMPLE))
    except OSError:
        # If we can't read for hashing, we won't succeed at extraction either;
        # let the extractor produce the tool-error exit code.
        pass
    return hasher.hexdigest()[:HASH_PREFIX_LEN]


def default_dest_dir(archive_path: Path) -> Path:
    """Compute the cache/safezip/<hash>/ slot for this archive."""
    return SAFEZIP_CACHE_DIR / archive_slug(archive_path)


def render_human(manifest: ExtractionManifest) -> str:
    """Human-readable manifest summary for stdout (default output mode)."""
    lines: list[str] = []
    verdict_tag = "EXTRACTED" if manifest.verdict is Verdict.EXTRACTED else "REJECTED"
    lines.append(f"safe-unzip: {verdict_tag}")
    lines.append(f"  archive : {manifest.archive}")
    lines.append(f"  dest    : {manifest.dest}")
    if manifest.reject_reason:
        lines.append(f"  reason  : {manifest.reject_reason}")
    totals = manifest.totals
    lines.append(
        f"  totals  : {totals.extracted} extracted, "
        f"{totals.skipped} skipped, "
        f"{totals.total_uncompressed_bytes} bytes"
    )
    if manifest.extracted:
        lines.append("  extracted:")
        for m in manifest.extracted:
            lines.append(f"    - {m.name}  ({m.type}, {m.size} bytes)")
    if manifest.skipped:
        lines.append("  skipped:")
        for s in manifest.skipped:
            lines.append(f"    - {s.name}  [{s.reason}]")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Argparse for `safe-unzip`.

    Kept as a module-level helper so tests can invoke it independently of
    stdout/stderr. `argv` follows the sys.argv[1:] convention.
    """
    parser = argparse.ArgumentParser(
        prog="safe-unzip",
        description=(
            "Extract an untrusted zip archive with structural + content-level "
            "safety checks. Rejects zip bombs, traversal, symlinks, and "
            "type-spoofing; skips members that aren't on the allowlist."
        ),
        epilog=(
            "Exit codes: 0 extracted; 77 archive rejected; 78 tool error. "
            "Default dest is $MINERU_HOME/cache/safezip/<hash>/."
        ),
    )
    parser.add_argument("archive", help="Path to the zip file to extract.")
    parser.add_argument(
        "--dest",
        type=str,
        default=None,
        help="Destination directory (default: cache/safezip/<hash>/).",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit machine-readable JSON manifest instead of the human summary.",
    )
    return parser.parse_args(argv)


def emit(manifest: ExtractionManifest, output_json: bool) -> None:
    """Print the manifest to stdout in the requested format."""
    if output_json:
        print(json.dumps(manifest.to_dict(), indent=2))
    else:
        print(render_human(manifest))


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the exit code (int) rather than sys.exit-ing.

    Returning the code (instead of calling sys.exit) makes the function
    testable — tests can capture the manifest via `emit` mocks or just read
    the returned code. The shim in `bin/safe-unzip` does the actual sys.exit.
    """
    args = parse_args(argv)

    archive_path = Path(args.archive).expanduser()
    if not archive_path.exists():
        print(f"safe-unzip: archive not found: {archive_path}", file=sys.stderr)
        return ExitCode.TOOL_ERROR

    dest_dir = (
        Path(args.dest).expanduser().resolve()
        if args.dest
        else default_dest_dir(archive_path)
    )

    extractor = SafeZipExtractor()

    try:
        manifest = extractor.extract(archive_path, dest_dir)
    except ArchiveRejected as exc:
        rejected = ExtractionManifest(
            verdict=Verdict.REJECTED,
            archive=str(archive_path.resolve()),
            dest=str(dest_dir),
            reject_reason=exc.reason,
        )
        emit(rejected, args.output_json)
        return ExitCode.REJECTED
    except ExtractionError as exc:
        print(f"safe-unzip: tool error: {exc}", file=sys.stderr)
        return ExitCode.TOOL_ERROR

    emit(manifest, args.output_json)
    return ExitCode.OK


if __name__ == "__main__":
    raise SystemExit(main())
