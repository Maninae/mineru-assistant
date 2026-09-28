# safezip — security-hardened zip extraction

Extracts UNTRUSTED zip archives (Telegram-forwarded documents) into a
per-archive cache dir under `cache/safezip/<hash>/`, defending against zip
bombs, symlinks, traversal, type-spoofing, and file-count exhaustion. Wraps
stdlib `zipfile` with a small threat-mapped hardening layer we own; no
third-party dependencies (that library niche is a CVE graveyard).

## Design principle: inspect-then-write

We never call `ZipFile.extractall()`. Every member is inspected first
(metadata in phase 1, streamed content in phase 2) and written only after it
clears every check. Because nothing unsafe ever lands on disk, we never need
to delete a partial artifact — which is important because this workspace
FORBIDS `rm`, `os.remove`, and `shutil.rmtree`. A failed-partway extraction
leaves already-written clean members under `cache/safezip/<hash>/` and a
separate retention sweep handles the cache.

## Pipeline

```
bin/safe-unzip
  └─> scripts.safezip.cli.main()
        1. compute default dest = cache/safezip/<sha256(path+first-4KB)[:16]>/
        2. SafeZipExtractor().extract(archive_path, dest_dir)
              phase 0 (before opening the archive):
                  - MAX_ARCHIVE_FILE_BYTES cap (defeats central-dir RAM blowup)
              phase 1 (metadata only, no disk writes):
                  - MAX_ENTRIES cap
                  - MAX_TOTAL_UNCOMPRESSED_BYTES cap (sum of file_size)
                  - MAX_COMPRESSION_RATIO cap (uncompressed / compressed)
                  - symlink / device / fifo / socket rejection (external_attr >> 16)
                  - NUL byte in filename -> reject
                  - path-traversal recheck (absolute, "..", commonpath)
              phase 2 (streamed per member):
                  - extension allowlist -> skip if miss
                  - bounded read with MAX_MEMBER_BYTES cap -> reject if overshoot
                    (broadly catches BadZipFile/zlib.error/NotImplementedError/
                     RuntimeError/EOFError/OSError -> exit 78, never a crash)
                  - magic-byte check (binary) or text-sniff (no-NUL + decodes)
                  - if all clear: write with 0o600, parent dirs 0o700 at every level
        3. render manifest (human summary or --json)
```

## Threat map (checks live in `extractor.py`)

| Threat | Defense | Fires |
|---|---|---|
| Path traversal (`../../evil`) | absolute-path reject + `..` component reject + `commonpath` recheck | wholesale reject, exit 77 |
| Symlink / device / fifo entry | `stat.S_ISLNK/BLK/CHR/FIFO/SOCK` on `external_attr >> 16` | wholesale reject, exit 77 |
| Declared-size bomb | `sum(file_size) > MAX_TOTAL_UNCOMPRESSED_BYTES` | wholesale reject, exit 77 |
| Compression-ratio bomb | `sum(file_size) / sum(compress_size) > MAX_COMPRESSION_RATIO` | wholesale reject, exit 77 |
| Real bomb (lying header) | streamed read aborts when member exceeds `MAX_MEMBER_BYTES` | wholesale reject, exit 77 |
| Entry-count exhaustion | `len(infolist) > MAX_ENTRIES` | wholesale reject, exit 77 |
| Central-directory RAM blowup (huge entry count before `MAX_ENTRIES` fires) | on-disk file size `> MAX_ARCHIVE_FILE_BYTES` refused before `zipfile.ZipFile()` | tool error, exit 78 |
| NUL byte in filename (crafted to survive stdlib parsing) | phase-1 traversal check rejects `\x00` in name | wholesale reject, exit 77 |
| Malformed compression (`zlib.error`, method 99, encrypted, truncated) | broad catch around `zf.open`/`member.read` -> `ExtractionError` | tool error, exit 78 |
| File/dir name collision inside one archive (`foo.pdf` file + `foo.pdf/x` dir) | broad catch around `mkdir`/`open` in `_write_member` | tool error, exit 78 |
| Deep-nested / overlong path | broad catch around `mkdir`/`open` | tool error, exit 78 |
| Type spoofing (`.png` that's really an executable) | magic-byte prefix check per binary extension | skip member, exit 0 |
| Binary posing as text | NUL-byte + UTF-8/Latin-1 sniff on text-like extensions | skip member, exit 0 |
| Nested-archive recursion | `.zip/.tar/.gz` not on allowlist; no auto-recursion path exists | skip member, exit 0 |
| CPU/time exhaustion during decompress | wall-clock deadline checked between members and inside per-member read loop | tool error, exit 78 |
| Disk exhaustion | `shutil.disk_usage(dest) < 2 * MAX_TOTAL_UNCOMPRESSED_BYTES` before writing | tool error, exit 78 |

## Module responsibilities

| File | One job |
|------|---------|
| `config.py` | All caps + allowlist + magic signatures. UPPER_SNAKE constants only. |
| `manifest.py` | Typed `ExtractionManifest` shared between extractor and CLI. Leaf module (imports nothing else in package) to prevent cycles. |
| `extractor.py` | `SafeZipExtractor`: the security engine. All validation + streaming lives here. Raises `ArchiveRejected` (adversarial) or `ExtractionError` (tool). |
| `cli.py` | Argparse, dest-dir selection, JSON/human rendering, exit-code mapping. |
| `bin/safe-unzip` | `sys.path` bootstrap shim → `scripts.safezip.cli.main`. |
| `tests/test_safezip.py` | pytest suite covering every threat in the map, including a real-bytes bomb where `file_size` lies. |

## Exit codes (mirrors `scripts/firewall/wrapper_common.py`)

| Code | Meaning |
|------|---------|
| 0    | Extracted. Some members may have been skipped; check the `skipped` list in the manifest. |
| 77   | Archive rejected wholesale. `reject_reason` is a stable slug (e.g. `path_traversal`, `per_member_bomb`). |
| 78   | Tool error: corrupt zip, unreadable file, disk full, timeout. |

## Tuning

### Change a cap

Edit the constant in `config.py`. Values are the production defaults; tests
inject overrides via the `SafeZipExtractor` constructor rather than
monkeypatching the module, so lowering a cap for a test's sake is unnecessary.

### Allow a new extension

1. Add the lowercased extension to `SAFEZIP_ALLOWED_EXTENSIONS` in `config.py`.
2. If it's binary, add a `MAGIC_SIGNATURES[".ext"] = (offset, (prefix, ...))`
   entry — verify the prefix bytes against an authoritative source
   (filesignature.org, the format spec, or a well-known library like Pillow).
3. If it's text, add it to `TEXT_LIKE_EXTENSIONS` (uses the NUL-byte + decode
   sniff instead of magic).
4. Add a test to `tests/test_safezip.py` (positive: allowed extract; negative:
   an obviously-wrong content payload gets skipped).

### Deliberately excluded

- **`.zip`, `.tar`, `.gz`, `.7z`, `.rar`** — no nested-archive recursion.
- **`.docx`, `.xlsx`, `.pptx`** — Office formats can carry macros; also they
  are secretly zips themselves, which is the recursion problem.
- **`.svg`, `.html`, `.xml`, `.svg`** — script-execution surface in downstream
  viewers.
- **Anything executable / script** — `.sh`, `.py`, `.js`, `.exe`, `.dll`.

## Cache dir invariant

`cache/safezip/`, each per-archive subdir, AND every intermediate directory
created for a nested member are all `0o700`; extracted files are `0o600`.
`Path.mkdir(parents=True, mode=0o700)` only sets the mode on the leaf, so
the `_mkdir_owner_only` helper in `extractor.py` walks the just-created
ancestors back to the per-archive dir and chmods each level. Mirrors the
firewall cache invariant so nothing here is world-readable.

## Tests

Requires Python 3.10+ (uses PEP-604 `X | None` syntax and `frozenset[str]`
generics). On this Mac, `/usr/bin/python3` is 3.9 and will crash at import
time; use Homebrew's `python3` (3.12+):

```bash
python3 -m pytest scripts/safezip/tests/ -q
```

Every threat in the map has a case, including the two-flavor bomb test
(declared-size vs lying-header/real-bytes) and the exit-contract cases
(malformed compression, encrypted-no-password, corrupt DEFLATE, path
collisions, deep-nested paths, oversize archive files). Fixtures are built
at runtime with stdlib `zipfile` + `ZipInfo` (or hand-crafted headers via
`struct` for the truly adversarial ones) so the repo carries no adversarial
artifacts.
