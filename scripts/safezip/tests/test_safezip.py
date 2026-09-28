"""Pytest suite for the safezip extractor + CLI.

Fixtures are built at runtime with stdlib `zipfile` + `ZipInfo` so no
adversarial artifacts are checked into the repo. The truly hostile fixtures
(unsupported compression method, encrypted-no-password, corrupt DEFLATE,
NUL-byte filename) are hand-crafted via `struct` because stdlib refuses to
write them.

Every threat in the `extractor.py` map has a test case; the two flavors of
zip bomb (declared-size vs lying-header-real-bytes) are exercised
independently, and each "malformed member" class has a CLI-level exit-code
assertion so regressions never resurface as a raw `exit 1` traceback.

Small caps are injected via `SafeZipExtractor(...)` constructor overrides so
the fixture archives can trigger bombs without allocating gigabytes.
"""
import io
import os
import stat
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

# Add the mineru workspace root so `scripts.safezip` imports resolve when the
# test runs under `pytest scripts/safezip/tests/`.
MINERU_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(MINERU_ROOT))

from scripts.safezip.cli import ExitCode, main as cli_main  # noqa: E402
from scripts.safezip.extractor import (  # noqa: E402
    ArchiveRejected,
    ExtractionError,
    SafeZipExtractor,
    SKIP_EMPTY_FILE,
    SKIP_EXTENSION_NOT_ALLOWED,
    SKIP_MAGIC_MISMATCH,
    SKIP_TEXT_SNIFF_FAILED,
)
from scripts.safezip.manifest import Verdict  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers — build malicious/benign zips programmatically in tmp dirs
# ---------------------------------------------------------------------------


def write_zip(archive_path: Path, entries: list[tuple[str, bytes, dict | None]]) -> None:
    """Write a zip with fine-grained per-entry control.

    Each entry is (name, content, kwargs). kwargs are set on the ZipInfo
    after construction — e.g. `external_attr` for symlinks, `file_size`
    override for a declared-size lie.
    """
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content, kwargs in entries:
            zi = zipfile.ZipInfo(name)
            if kwargs:
                for key, value in kwargs.items():
                    setattr(zi, key, value)
            # `writestr` recomputes CRC/compress_size honestly, but external_attr
            # and any post-write field overrides above still stick.
            zf.writestr(zi, content, compress_type=zipfile.ZIP_DEFLATED)


def make_png_bytes() -> bytes:
    """Minimal PNG that satisfies the magic-byte check."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def make_pdf_bytes() -> bytes:
    """Minimal PDF that satisfies the magic-byte check."""
    return b"%PDF-1.4\n%%EOF\n"


def make_gif_bytes() -> bytes:
    """Minimal GIF that satisfies the magic-byte check."""
    return b"GIF89a" + b"\x00" * 20


def make_webp_bytes() -> bytes:
    """Minimal WEBP (RIFF at 0, WEBP tag at 8)."""
    return b"RIFF" + b"\x00\x00\x00\x28" + b"WEBP" + b"\x00" * 32


def default_extractor(**overrides) -> SafeZipExtractor:
    """Extractor with tight-but-reasonable defaults for tests."""
    kwargs = dict(
        max_entries=32,
        max_total_uncompressed_bytes=5 * 1024 * 1024,  # 5 MB
        max_compression_ratio=50,
        max_member_bytes=1 * 1024 * 1024,              # 1 MB
        extract_timeout_seconds=10.0,
        free_disk_headroom_mult=1,                     # keep the disk guard lenient
    )
    kwargs.update(overrides)
    return SafeZipExtractor(**kwargs)


# ---------------------------------------------------------------------------
# 1. Happy path: mixed zip -> allowed extracted, disallowed skipped, exit 0
# ---------------------------------------------------------------------------


def test_mixed_zip_extracts_allowed_and_skips_disallowed(tmp_path):
    archive = tmp_path / "mixed.zip"
    entries = [
        ("doc.pdf", make_pdf_bytes(), None),
        ("image.png", make_png_bytes(), None),
        ("notes.txt", b"hello world\n", None),
        ("nested/deep.md", b"# heading\n", None),
        ("evil.exe", b"MZ\x90\x00" + b"\x00" * 100, None),      # disallowed ext
        ("script.sh", b"#!/bin/sh\necho pwned\n", None),        # disallowed ext
    ]
    write_zip(archive, entries)

    dest = tmp_path / "out"
    manifest = default_extractor().extract(archive, dest)

    assert manifest.verdict is Verdict.EXTRACTED
    extracted_names = {m.name for m in manifest.extracted}
    assert extracted_names == {"doc.pdf", "image.png", "notes.txt", "nested/deep.md"}
    skipped_names = {s.name: s.reason for s in manifest.skipped}
    assert skipped_names == {
        "evil.exe": SKIP_EXTENSION_NOT_ALLOWED,
        "script.sh": SKIP_EXTENSION_NOT_ALLOWED,
    }
    # Files actually landed under dest
    assert (dest / "doc.pdf").is_file()
    assert (dest / "nested" / "deep.md").is_file()
    # Disallowed files never touched disk
    assert not (dest / "evil.exe").exists()


# ---------------------------------------------------------------------------
# 2. Zip-slip (relative parent path) -> reject, nothing written outside dest
# ---------------------------------------------------------------------------


def test_zip_slip_relative_parent_rejects_archive(tmp_path):
    archive = tmp_path / "slip.zip"
    write_zip(archive, [("../../../evil.txt", b"pwn", None)])

    dest = tmp_path / "out"
    with pytest.raises(ArchiveRejected) as excinfo:
        default_extractor().extract(archive, dest)
    assert excinfo.value.reason == "path_traversal"

    # Ensure nothing landed outside dest (or inside it, since we bail in phase 1)
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()
    if dest.exists():
        assert list(dest.iterdir()) == []


# ---------------------------------------------------------------------------
# 3. Absolute-path entry -> reject
# ---------------------------------------------------------------------------


def test_absolute_path_entry_rejects_archive(tmp_path):
    archive = tmp_path / "abs.zip"
    write_zip(archive, [("/tmp/evil.txt", b"pwn", None)])

    with pytest.raises(ArchiveRejected) as excinfo:
        default_extractor().extract(archive, tmp_path / "out")
    assert excinfo.value.reason == "path_traversal"


# ---------------------------------------------------------------------------
# 4. Symlink entry -> reject
# ---------------------------------------------------------------------------


def test_symlink_entry_rejects_archive(tmp_path):
    archive = tmp_path / "sym.zip"
    # Craft the symlink by setting external_attr with S_IFLNK in the high 16
    # bits, which is exactly how Info-Zip / stdlib zipfile represent symlinks
    # on unix.
    symlink_mode = (stat.S_IFLNK | 0o777) << 16
    write_zip(
        archive,
        [("link_to_etc", b"/etc/passwd", {"external_attr": symlink_mode})],
    )

    with pytest.raises(ArchiveRejected) as excinfo:
        default_extractor().extract(archive, tmp_path / "out")
    assert excinfo.value.reason == "symlink_entry"


# ---------------------------------------------------------------------------
# 5a. Declared-size zip bomb (sum of file_size exceeds the cap) -> reject
# ---------------------------------------------------------------------------


def test_declared_size_bomb_rejects_archive(tmp_path):
    archive = tmp_path / "declared.zip"
    # Legitimately-large payloads. Two 3 MB files -> 6 MB total, above the
    # 5 MB test cap, so declared-size check fires.
    payload = make_png_bytes() + b"\x00" * (3 * 1024 * 1024)
    write_zip(archive, [("a.png", payload, None), ("b.png", payload, None)])

    with pytest.raises(ArchiveRejected) as excinfo:
        default_extractor().extract(archive, tmp_path / "out")
    assert excinfo.value.reason == "declared_size_bomb"


# ---------------------------------------------------------------------------
# 5b. Real-bytes bomb (small per-member cap; a highly-compressible member's
#     actual bytes exceed the cap, catching a lying file_size)
# ---------------------------------------------------------------------------


def test_per_member_real_bytes_bomb_rejects_archive(tmp_path):
    """A member of 4 MB of zeros compresses to ~4 KB.

    We keep the declared-size and ratio caps generous (so phase-1 lets it
    pass) and set `max_member_bytes` LOW to prove the streaming read catches
    the real explosion. This is the specific defense against a lying header
    sailing through metadata checks.
    """
    archive = tmp_path / "member_bomb.zip"
    huge_zeros = b"\x00" * (4 * 1024 * 1024)  # 4 MB uncompressed
    # Wrap in a .txt so the extension allowlist lets it get to phase 2.
    write_zip(archive, [("bomb.txt", huge_zeros, None)])

    extractor = default_extractor(
        max_total_uncompressed_bytes=100 * 1024 * 1024,  # phase 1 passes
        max_compression_ratio=1_000_000,                 # phase 1 passes
        max_member_bytes=64 * 1024,                      # 64 KB per-member cap
    )
    with pytest.raises(ArchiveRejected) as excinfo:
        extractor.extract(archive, tmp_path / "out")
    assert excinfo.value.reason == "per_member_bomb"


# ---------------------------------------------------------------------------
# 6. Too many entries -> reject
# ---------------------------------------------------------------------------


def test_too_many_entries_rejects_archive(tmp_path):
    archive = tmp_path / "many.zip"
    entries = [(f"f{i}.txt", b"x", None) for i in range(64)]
    write_zip(archive, entries)

    extractor = default_extractor(max_entries=32)
    with pytest.raises(ArchiveRejected) as excinfo:
        extractor.extract(archive, tmp_path / "out")
    assert excinfo.value.reason == "too_many_entries"


# ---------------------------------------------------------------------------
# 7. Compression-ratio bomb -> reject
# ---------------------------------------------------------------------------


def test_compression_ratio_bomb_rejects_archive(tmp_path):
    """A 1 MB member of zeros compresses to ~1 KB -> ratio ~1000:1.

    We keep the total-size cap generous and the per-member cap generous so
    phase 1's ratio check is the thing that fires (not the other two).
    """
    archive = tmp_path / "ratio.zip"
    zeros = b"\x00" * (1 * 1024 * 1024)
    write_zip(archive, [("zeros.txt", zeros, None)])

    extractor = default_extractor(
        max_total_uncompressed_bytes=100 * 1024 * 1024,
        max_member_bytes=100 * 1024 * 1024,
        max_compression_ratio=50,   # actual ratio ~1000:1 -> fires
    )
    with pytest.raises(ArchiveRejected) as excinfo:
        extractor.extract(archive, tmp_path / "out")
    assert excinfo.value.reason == "compression_ratio_bomb"


# ---------------------------------------------------------------------------
# 8a. Magic mismatch on a binary type (.png that's actually text) -> skip
# ---------------------------------------------------------------------------


def test_magic_mismatch_binary_skips_member(tmp_path):
    archive = tmp_path / "mismatch.zip"
    write_zip(archive, [
        ("real.png", make_png_bytes(), None),        # good
        ("fake.png", b"this is not a png at all\n", None),  # bad magic
    ])

    manifest = default_extractor().extract(archive, tmp_path / "out")

    assert manifest.verdict is Verdict.EXTRACTED
    assert [m.name for m in manifest.extracted] == ["real.png"]
    assert len(manifest.skipped) == 1
    assert manifest.skipped[0].name == "fake.png"
    assert manifest.skipped[0].reason == SKIP_MAGIC_MISMATCH


# ---------------------------------------------------------------------------
# 8b. Text-sniff failure (.txt that's actually a binary blob with NULs) -> skip
# ---------------------------------------------------------------------------


def test_text_sniff_binary_skips_member(tmp_path):
    archive = tmp_path / "spoof_text.zip"
    write_zip(archive, [
        ("real.txt", b"plain ascii\n", None),                  # good
        ("fake.txt", b"binary\x00\x01\x02\x03payload\n", None),  # has NUL
    ])

    manifest = default_extractor().extract(archive, tmp_path / "out")

    assert manifest.verdict is Verdict.EXTRACTED
    assert [m.name for m in manifest.extracted] == ["real.txt"]
    assert len(manifest.skipped) == 1
    assert manifest.skipped[0].reason == SKIP_TEXT_SNIFF_FAILED


# ---------------------------------------------------------------------------
# 9. Nested .zip inside -> skipped as disallowed extension (no auto-recursion)
# ---------------------------------------------------------------------------


def test_nested_zip_is_skipped_not_recursed(tmp_path):
    # Build an inner zip in memory, then wrap it as a .zip member of the outer.
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w") as inner:
        inner.writestr("inner.txt", b"inner content")
    inner_bytes = inner_buf.getvalue()

    archive = tmp_path / "nested.zip"
    write_zip(archive, [
        ("readme.md", b"# top-level\n", None),
        ("payload.zip", inner_bytes, None),
    ])

    manifest = default_extractor().extract(archive, tmp_path / "out")

    assert manifest.verdict is Verdict.EXTRACTED
    assert [m.name for m in manifest.extracted] == ["readme.md"]
    assert len(manifest.skipped) == 1
    assert manifest.skipped[0].name == "payload.zip"
    assert manifest.skipped[0].reason == SKIP_EXTENSION_NOT_ALLOWED
    # Prove no recursion happened: inner.txt does not exist anywhere under dest.
    for _, _, files in os.walk(tmp_path / "out"):
        assert "inner.txt" not in files


# ---------------------------------------------------------------------------
# 10. Empty zip -> exit 0, nothing extracted
# ---------------------------------------------------------------------------


def test_empty_zip_extracts_nothing(tmp_path):
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w"):
        pass

    manifest = default_extractor().extract(archive, tmp_path / "out")

    assert manifest.verdict is Verdict.EXTRACTED
    assert manifest.extracted == []
    assert manifest.skipped == []


# ---------------------------------------------------------------------------
# 11. Corrupt / non-zip file -> ExtractionError (exit 78)
# ---------------------------------------------------------------------------


def test_corrupt_non_zip_is_tool_error(tmp_path):
    archive = tmp_path / "junk.zip"
    archive.write_bytes(b"this is definitely not a zip file, just random bytes")

    with pytest.raises(ExtractionError):
        default_extractor().extract(archive, tmp_path / "out")


# ---------------------------------------------------------------------------
# CLI-level end-to-end: exit codes + JSON manifest shape
# ---------------------------------------------------------------------------


def test_cli_happy_path_exit_zero_and_json_shape(tmp_path, capsys, monkeypatch):
    # Force the default cache dir into tmp_path so the CLI doesn't touch
    # the real $MINERU_HOME/cache/safezip during tests.
    monkeypatch.setattr(
        "scripts.safezip.cli.SAFEZIP_CACHE_DIR", tmp_path / "cachehome"
    )
    archive = tmp_path / "ok.zip"
    write_zip(archive, [("doc.pdf", make_pdf_bytes(), None)])

    exit_code = cli_main([str(archive), "--json"])
    assert exit_code == ExitCode.OK

    import json as _json
    captured = capsys.readouterr()
    data = _json.loads(captured.out)
    assert data["verdict"] == "extracted"
    assert data["totals"]["extracted"] == 1
    assert data["extracted"][0]["name"] == "doc.pdf"
    assert data["extracted"][0]["type"] == "pdf"


def test_cli_rejects_zip_slip_with_exit_77(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "scripts.safezip.cli.SAFEZIP_CACHE_DIR", tmp_path / "cachehome"
    )
    archive = tmp_path / "slip.zip"
    write_zip(archive, [("../pwn.txt", b"x", None)])

    exit_code = cli_main([str(archive), "--json"])
    assert exit_code == ExitCode.REJECTED

    import json as _json
    data = _json.loads(capsys.readouterr().out)
    assert data["verdict"] == "rejected"
    assert data["reject_reason"] == "path_traversal"


def test_cli_corrupt_zip_is_exit_78(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "scripts.safezip.cli.SAFEZIP_CACHE_DIR", tmp_path / "cachehome"
    )
    archive = tmp_path / "junk.zip"
    archive.write_bytes(b"nope")

    exit_code = cli_main([str(archive)])
    assert exit_code == ExitCode.TOOL_ERROR


# ---------------------------------------------------------------------------
# bin/safe-unzip shim: actually invoke the executable and assert exit code
# ---------------------------------------------------------------------------


def test_bin_shim_end_to_end_exit_zero(tmp_path):
    archive = tmp_path / "shim.zip"
    write_zip(archive, [("doc.pdf", make_pdf_bytes(), None)])
    dest = tmp_path / "shimdest"
    binary = MINERU_ROOT / "bin" / "safe-unzip"

    result = subprocess.run(
        [str(binary), str(archive), "--dest", str(dest), "--json"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    import json as _json
    data = _json.loads(result.stdout)
    assert data["verdict"] == "extracted"
    assert (dest / "doc.pdf").is_file()


# ===========================================================================
# Robustness / exit-code contract: malformed archives never crash to exit 1.
# One test per class of failure the fresh-context audit caught, each asserted
# both at the Python-API level (raises the right exception type) and via the
# CLI shim (returns the promised 77 or 78 with no traceback).
# ===========================================================================


def _hex_craft_zip_with_single_stored_entry(
    path: Path,
    filename: bytes,
    content: bytes,
    compression_method: int = 0,
    gp_flags: int = 0,
) -> None:
    """Hand-write a zip that stdlib's `writestr` would refuse to build.

    Used for the truly adversarial fixtures: NUL-in-filename, method 99,
    encrypted-flag-set. `content` is stored raw (not deflate-compressed) with
    a corresponding CRC-32; if `compression_method` says otherwise, we're
    intentionally lying to make the read side blow up.
    """
    crc = zlib.crc32(content) if compression_method == 0 else 0
    comp_size = len(content)
    un_size = len(content) if compression_method == 0 else 100

    lfh = struct.pack(
        "<4s5H3I2H",
        b"PK\x03\x04",
        20, gp_flags, compression_method, 0, 0,
        crc, comp_size, un_size,
        len(filename), 0,
    ) + filename + content
    cdh = struct.pack(
        "<4s6H3I5H2I",
        b"PK\x01\x02",
        20, 20, gp_flags, compression_method, 0, 0,
        crc, comp_size, un_size,
        len(filename), 0, 0, 0, 0, 0, 0,
    ) + filename
    eocd = struct.pack(
        "<4s4H2IH",
        b"PK\x05\x06", 0, 0, 1, 1, len(cdh), len(lfh), 0,
    )
    path.write_bytes(lfh + cdh + eocd)


def _run_cli(archive: Path, dest: Path) -> subprocess.CompletedProcess:
    """Run `bin/safe-unzip` on a fixture and return the subprocess result.

    Small helper so every robustness test can assert both the exit code and
    the absence of a Python traceback on stderr in one shot.
    """
    binary = MINERU_ROOT / "bin" / "safe-unzip"
    return subprocess.run(
        [str(binary), str(archive), "--dest", str(dest), "--json"],
        capture_output=True, text=True, timeout=15,
    )


def _assert_no_traceback(result: subprocess.CompletedProcess) -> None:
    """Fail the test if the CLI emitted a raw Python traceback."""
    assert "Traceback (most recent call last)" not in result.stderr, (
        f"CLI crashed with a traceback instead of a clean error:\n{result.stderr}"
    )


# --- Fix 1 group -----------------------------------------------------------


def test_file_dir_collision_is_tool_error_not_crash(tmp_path):
    """`foo.pdf` (file) + `foo.pdf/bar.pdf` (file in a dir named `foo.pdf`).

    The second entry's `mkdir(parents=True)` on `foo.pdf` collides with the
    existing regular file, historically crashing to exit 1 with a raw
    NotADirectoryError. The broad OSError catch in `_write_member` now maps
    it to a clean exit 78.
    """
    archive = tmp_path / "collide.zip"
    write_zip(archive, [
        ("foo.pdf", make_pdf_bytes(), None),
        ("foo.pdf/bar.pdf", make_pdf_bytes(), None),
    ])
    with pytest.raises(ExtractionError):
        default_extractor().extract(archive, tmp_path / "out")

    result = _run_cli(archive, tmp_path / "cli_out")
    assert result.returncode == ExitCode.TOOL_ERROR
    _assert_no_traceback(result)


def test_unsupported_compression_method_is_tool_error(tmp_path):
    """Compression method 99 (WinZip AES) is not supported by stdlib.

    `zf.open(zi)` raises `NotImplementedError`; the broad phase-2 catch maps
    it to exit 78 instead of an uncaught crash.
    """
    archive = tmp_path / "unsup.zip"
    _hex_craft_zip_with_single_stored_entry(
        archive, b"a.txt", b"nothing", compression_method=99,
    )
    with pytest.raises(ExtractionError):
        default_extractor().extract(archive, tmp_path / "out")

    result = _run_cli(archive, tmp_path / "cli_out")
    assert result.returncode == ExitCode.TOOL_ERROR
    _assert_no_traceback(result)


def test_encrypted_member_no_password_is_tool_error(tmp_path):
    """A member with the encryption flag bit set but no password supplied.

    `zf.open(zi)` raises `RuntimeError('File %s is encrypted, ...')`; caught
    by the broad phase-2 catch -> exit 78.
    """
    archive = tmp_path / "enc.zip"
    _hex_craft_zip_with_single_stored_entry(
        archive, b"a.txt", b"\x00" * 12, gp_flags=0x0001,
    )
    with pytest.raises(ExtractionError):
        default_extractor().extract(archive, tmp_path / "out")

    result = _run_cli(archive, tmp_path / "cli_out")
    assert result.returncode == ExitCode.TOOL_ERROR
    _assert_no_traceback(result)


def test_corrupt_deflate_payload_is_tool_error(tmp_path):
    """A member advertised as DEFLATE-compressed with bogus zlib bytes.

    `member_fh.read()` raises `zlib.error`; the broad phase-2 catch maps
    it to exit 78 (used to be an uncaught crash to exit 1).
    """
    archive = tmp_path / "corrupt_deflate.zip"
    filename = b"a.txt"
    bogus = b"\xFF" * 40
    lfh = struct.pack(
        "<4s5H3I2H",
        b"PK\x03\x04", 20, 0, 8, 0, 0, 0, len(bogus), 100, len(filename), 0,
    ) + filename + bogus
    cdh = struct.pack(
        "<4s6H3I5H2I",
        b"PK\x01\x02", 20, 20, 0, 8, 0, 0, 0, len(bogus), 100,
        len(filename), 0, 0, 0, 0, 0, 0,
    ) + filename
    eocd = struct.pack(
        "<4s4H2IH",
        b"PK\x05\x06", 0, 0, 1, 1, len(cdh), len(lfh), 0,
    )
    archive.write_bytes(lfh + cdh + eocd)

    with pytest.raises(ExtractionError):
        default_extractor().extract(archive, tmp_path / "out")

    result = _run_cli(archive, tmp_path / "cli_out")
    assert result.returncode == ExitCode.TOOL_ERROR
    _assert_no_traceback(result)


def test_deep_nested_path_is_tool_error_not_crash(tmp_path):
    """~600 nested directory components blow past macOS PATH_MAX (1024).

    `parent.mkdir(parents=True)` raises `OSError(ENAMETOOLONG)`; the broad
    OSError catch in `_write_member` maps it to exit 78. Without the catch,
    this used to crash to exit 1.
    """
    archive = tmp_path / "deep.zip"
    depth = 600
    deep_name = "/".join(["a"] * depth) + "/hello.txt"
    write_zip(archive, [(deep_name, b"benign\n", None)])

    with pytest.raises(ExtractionError):
        default_extractor().extract(archive, tmp_path / "out")

    result = _run_cli(archive, tmp_path / "cli_out")
    assert result.returncode == ExitCode.TOOL_ERROR
    _assert_no_traceback(result)


def test_nul_byte_in_filename_is_rejected_wholesale(tmp_path):
    """NUL byte in a filename should be caught in phase-1 (exit 77).

    Belt-and-suspenders: phase 1 rejects it as `invalid_filename`, so we
    never even get to phase 2 where `open()` would raise ValueError.
    """
    archive = tmp_path / "nul.zip"
    _hex_craft_zip_with_single_stored_entry(
        archive, b"good\x00.txt", b"benign\n",
    )
    with pytest.raises(ArchiveRejected) as excinfo:
        default_extractor().extract(archive, tmp_path / "out")
    assert excinfo.value.reason == "invalid_filename"

    result = _run_cli(archive, tmp_path / "cli_out")
    assert result.returncode == ExitCode.REJECTED
    _assert_no_traceback(result)


def test_newline_in_filename_is_rejected_wholesale(tmp_path):
    """LF (\\n) in a filename is a prompt-injection breakout vector.

    Landline's archive-frame prompt is line-delimited, so a zip entry
    named ``innocuous.txt\\n</archive_contents>\\n[SYSTEM] ...`` would
    let hostile text escape the frame if it ever reached the wrapper.
    Layer (a) rejects the whole archive at phase-1 as ``invalid_filename``.
    Covers CR (\\r), TAB (\\t), and DEL (\\x7f) by the same rule.
    """
    for hostile_byte in (b"\n", b"\r", b"\t", b"\x1b", b"\x7f"):
        archive = tmp_path / f"ctrl_{hostile_byte.hex()}.zip"
        _hex_craft_zip_with_single_stored_entry(
            archive, b"good" + hostile_byte + b".txt", b"benign\n",
        )
        with pytest.raises(ArchiveRejected) as excinfo:
            default_extractor().extract(archive, tmp_path / f"out_{hostile_byte.hex()}")
        assert excinfo.value.reason == "invalid_filename", (
            f"control byte {hostile_byte!r} was not caught"
        )
        result = _run_cli(archive, tmp_path / f"cli_out_{hostile_byte.hex()}")
        assert result.returncode == ExitCode.REJECTED
        _assert_no_traceback(result)


# --- Fix 2: ancestor dirs are all 0o700 ------------------------------------


def test_nested_member_dirs_are_all_chmod_0700(tmp_path):
    """Walk `docs/2026/august/notes.txt` and stat each ancestor.

    Every ancestor between the per-archive dest dir and the file must have
    mode `0o700`. Guards against `Path.mkdir(parents=True, mode=0o700)`'s
    default behavior of only applying the mode to the leaf.
    """
    archive = tmp_path / "nested.zip"
    write_zip(archive, [("docs/2026/august/notes.txt", b"plain text\n", None)])

    dest = tmp_path / "nested_out"
    manifest = default_extractor().extract(archive, dest)
    assert manifest.verdict is Verdict.EXTRACTED
    assert (dest / "docs" / "2026" / "august" / "notes.txt").is_file()

    for level in [
        dest / "docs",
        dest / "docs" / "2026",
        dest / "docs" / "2026" / "august",
    ]:
        actual_mode = stat.S_IMODE(os.stat(level).st_mode)
        assert actual_mode == 0o700, (
            f"expected 0o700 on {level}, got {oct(actual_mode)}"
        )


# --- Fix 3: upfront archive-file size cap ----------------------------------


def test_oversize_archive_file_is_tool_error(tmp_path):
    """An on-disk archive larger than the constructor cap is refused as exit 78.

    We keep the fixture tiny by lowering the cap in the constructor override;
    the point is to prove the cap fires before `zipfile.ZipFile()` is called.
    `os.urandom` is used so ZIP_DEFLATED doesn't compress the payload away,
    which would leave the archive under the cap.
    """
    archive = tmp_path / "big.zip"
    incompressible_a = os.urandom(2048)
    incompressible_b = os.urandom(2048)
    write_zip(archive, [("a.txt", incompressible_a, None), ("b.txt", incompressible_b, None)])
    assert archive.stat().st_size > 1024, (
        f"fixture too small ({archive.stat().st_size} bytes) — random payload"
        " should not have compressed"
    )

    extractor = default_extractor(max_archive_file_bytes=1024)
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(archive, tmp_path / "out")
    assert "archive file too large" in str(excinfo.value)
