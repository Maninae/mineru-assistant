"""Sent-image retention cache for `mineru telegram photo` (P3-02).

This module owns the per-image retention ledger that sits underneath
`mineru telegram photo`. It is the SIDECAR layer for the transport in
`telegram_photo.py`: the caller wires them together as

    handle = cache_binary(source_path, retention_days, label, caption, chat_id)
    result = send_photo(handle.bytes_path, chat_id=chat_id, caption=caption, ...)
    if result["ok"]:
        commit_record(handle, telegram_file_id=result["telegram_file_id"],
                      message_id=result["message_id"])

The two files that materialize per send are the bytes file and its
sidecar JSON, both mode 0600, both named after the same
YYYYMMDD-HHMMSS-<sha8> stem (spec §4.2):

    telegram_sent_images/
      20260725-153042-a1b2c3d4.jpg   (0600, mode-checked in tests)
      20260725-153042-a1b2c3d4.json  (0600; only written by commit_record)

⚠️ SAFETY (READ BEFORE EDITING) ⚠️

  This module does NO network I/O. It never imports `telegram_photo`,
  never touches `api.telegram.org`, never opens a socket. The transport
  and the cache are two orthogonal layers: the cache is safe to
  exercise in tests and dev because it cannot, on its own, DM the operator.
  Keep it that way.

  Every write is chmod'd tight: the cache dir gets mode 0700 on creation,
  every file (bytes + sidecar) gets 0600 before any bytes hit disk. A
  ledger file leak (e.g. shared world-readable Downloads folder) would
  give an attacker chat_ids + captions + the `telegram_file_id` values
  that can be replayed via `sendPhoto?photo=<file_id>` without the
  original photo bytes. The permissions are the load-bearing control.

  Every test in `tests/test_telegram_image_cache.py` sets
  `MINERU_TELEGRAM_SENT_IMAGE_DIR` to a `tmp_path`. NO test writes into
  the live `$MINERU_HOME/cache/telegram_sent_images/` — the env override is
  the guard rail.

  `prune_expired` uses the `trash` CLI, NEVER `rm`. This is the
  SOUL.md / SECURITY.md hard rule for destructive actions: a
  recoverable trash move beats an irreversible unlink every time.

The `forever` sentinel (retention_days='forever') writes
`expires_at: null` in the sidecar and is skipped by every prune path,
including the cleanup-retention.sh shell rule. Callers use it for
photos that should live indefinitely (Anthology-style keepers).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union


logger = logging.getLogger(__name__)


# --- Constants -------------------------------------------------------------

# Env override for the cache directory. EVERY test sets this to a
# `tmp_path` so no test can ever touch the live cache path. Production
# callers omit it and let the profile-scoped path (threaded in via the
# `cache_dir` kwarg) apply.
MINERU_SENT_IMAGE_DIR_ENV = "MINERU_TELEGRAM_SENT_IMAGE_DIR"

# The per-workspace cache subpath. Concatenated onto a workspace root
# (the active profile's `workspace_absolute`). Kept as a segment tuple
# so a caller can reason about it without string-split gymnastics;
# joined at read time.
CACHE_SUBPATH_SEGMENTS = ("cache", "telegram_sent_images")


def cache_dir_for_workspace(workspace_root: Path) -> Path:
    """Return the sent-image cache dir under `workspace_root`.

    Foundation isolation invariant (Phase-1 security fix): every
    profile's send-photo history lives under its OWN workspace root, so
    two co-existing profiles never see each other's file_ids, captions,
    or chat_ids. Callers (the verb layer) resolve this from
    `Profile.workspace_absolute` and pass the result explicitly to
    `cache_binary` / `iter_records` / `lookup_by_sha256` /
    `search_records` / `prune_expired` via their `cache_dir=` kwarg.
    """
    return workspace_root / Path(*CACHE_SUBPATH_SEGMENTS)

# Directory permission bits — 0700 keeps the cache readable ONLY by the
# profile's macOS user. Cross-referenced with SECURITY.md §"PII
# Protection" and §"Minimize Blast Radius".
CACHE_DIR_MODE = 0o700

# File permission bits — 0600 for both bytes file and sidecar JSON.
CACHE_FILE_MODE = 0o600

# Default retention window per §4.1 + §4.2 (seed profile default is 60d).
# A per-call retention_days argument overrides this.
DEFAULT_RETENTION_DAYS = 60

# Sentinel value for retention_days that means "never expire; skip prune".
# String form so it survives a YAML/JSON round-trip through profile config.
RETENTION_FOREVER = "forever"

# Length of the sha256 hex slice embedded in the filename. 8 chars keeps
# the filename short while making a same-second sha8 collision
# vanishingly rare for the volume the operator sends (one photo per minute at
# most, per §4.2 §"low volume expected").
FILENAME_SHA8_LENGTH = 8

# Timestamp format for the filename stem — chosen so `ls` sorts entries
# chronologically without extra sort flags and so the prefix is a valid
# POSIX filename on any filesystem.
FILENAME_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

# Path to the `trash` CLI. This is the SOUL.md-mandated destructive
# tool (recoverable move to ~/.Trash/). Kept as a module constant so a
# CI environment without `/usr/bin/trash` can override it via env for a
# test-only shim; production stays hardcoded to the macOS BSD tool.
TRASH_BINARY_ENV = "MINERU_TRASH_BINARY"
DEFAULT_TRASH_BINARY = "/usr/bin/trash"


# --- Errors ----------------------------------------------------------------


class SentImageCacheError(RuntimeError):
    """Raised on any cache-layer failure (bad handle, missing binary, IO error).

    The `__str__` always names the exact path or slot that failed so
    a downstream Typer handler can render it verbatim without wrapping.
    """


# --- Data model ------------------------------------------------------------


@dataclass(frozen=True)
class PendingCacheEntry:
    """Handle returned by `cache_binary`; input to `commit_record`.

    The bytes file is already on disk at 0600 by the time this handle
    exists; the sidecar JSON is NOT — `commit_record` writes it once the
    transport reports a message_id and file_id. If the transport fails,
    the caller can drop the handle: no sidecar means the pair is
    incomplete and every subsequent lookup treats the entry as invisible.

    Attributes:
        stem: shared filename stem (YYYYMMDD-HHMMSS-<sha8>) for both files.
        bytes_path: absolute path to the on-disk bytes file (already written).
        sidecar_path: absolute path where `commit_record` will write JSON.
        sent_at: ISO-8601 timestamp with tz offset, generated at cache time.
        sha256: full 64-char hex digest of the photo bytes.
        mime: guessed MIME type; defaults to image/jpeg on unknown ext.
        source_path: original filesystem path the bytes came from.
        chat_id: destination chat_id at the moment of caching (int or str).
        caption: optional caption text, verbatim.
        retention_days: int or 'forever' sentinel.
        expires_at: ISO-8601 with tz offset, OR None for 'forever'.
        label: caller-supplied label (defaults to source_path stem).
    """

    stem: str
    bytes_path: Path
    sidecar_path: Path
    sent_at: str
    sha256: str
    mime: str
    source_path: str
    chat_id: Union[int, str]
    caption: Optional[str]
    retention_days: Union[int, str]
    expires_at: Optional[str]
    label: str


@dataclass(frozen=True)
class SentImageRecord:
    """A committed cache entry (bytes file + sidecar both present on disk).

    Immutable snapshot the ledger verbs (`list`, `search`, `show`,
    `prune`) render / operate on. Constructed by `_load_record`; never
    written back to disk from this shape (writes always go through
    `commit_record`, which owns the schema-authoritative dict).
    """

    stem: str
    bytes_path: Path
    sidecar_path: Path
    sent_at: str
    chat_id: Union[int, str]
    caption: Optional[str]
    source_path: str
    sha256: str
    mime: str
    telegram_file_id: Optional[str]
    message_id: Optional[int]
    retention_days: Union[int, str]
    expires_at: Optional[str]
    label: str
    raw: dict  # the exact sidecar JSON, for forward-compat


# --- Cache directory management -------------------------------------------


def resolve_cache_dir() -> Path:
    """Return the sent-image cache directory from the env override.

    F2 completion (2026-08-28 rev): the legacy `DEFAULT_SENT_IMAGE_DIR`
    fallback (`$MINERU_HOME/cache/telegram_sent_images`) is RETIRED. Every
    caller must either:

      (a) pass an explicit `cache_dir=` to `cache_binary` /
          `ensure_cache_dir` / `iter_records` / `lookup_by_sha256` /
          `search_records` / `prune_expired`. The verb layer computes
          the per-profile path from `Profile.workspace_absolute` and
          threads it via kwarg; OR
      (b) set `MINERU_TELEGRAM_SENT_IMAGE_DIR` to an absolute path
          (test-only, or an explicit operator opt-in).

    Anything else raises `SentImageCacheError` so a misconfigured
    caller fails loud instead of silently writing photo bytes into
    `$MINERU_HOME` from the wrong profile. Mirrors the inject-queue
    fail-loud path (`_resolved_inject_queue_dir` in
    `mineru_cli.verbs.telegram`) — no `$MINERU_HOME` fallback.

    An empty-string env var is treated as unset (matches shell
    semantics for tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(MINERU_SENT_IMAGE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    raise SentImageCacheError(
        "sent-image cache dir not resolved: no `cache_dir=` was passed "
        f"and {MINERU_SENT_IMAGE_DIR_ENV} is not set. The legacy "
        "$MINERU_HOME fallback was retired to prevent cross-profile writes. "
        "The verb layer normally supplies `cache_dir=` via "
        "`cache_dir_for_workspace(profile.workspace_absolute)`; tests "
        f"set the {MINERU_SENT_IMAGE_DIR_ENV} env override."
    )


def ensure_cache_dir(cache_dir: Optional[Path] = None) -> Path:
    """Create the cache directory at mode 0700 if missing, then return it.

    Precedence for the target root:
      1. `cache_dir` argument (production verb-layer path).
      2. `MINERU_TELEGRAM_SENT_IMAGE_DIR` env override (test-only).
      3. Neither → `resolve_cache_dir` raises `SentImageCacheError` so
         the caller sees a loud, actionable failure instead of a silent
         write into `$MINERU_HOME`. The legacy home-relative fallback is
         gone (2026-08-28 F2 completion).

    Idempotent: if the directory already exists, we chmod it to 0700 so a
    prior lax creation (e.g. `mkdir` without `-m 700`) is corrected. If
    the path exists but is not a directory, we raise so the caller can
    surface an actionable message rather than silently overwriting.
    """
    root = cache_dir if cache_dir is not None else resolve_cache_dir()
    if root.exists():
        if not root.is_dir():
            raise SentImageCacheError(
                f"sent-image cache path {root} exists but is not a directory. "
                "Move the file aside or point MINERU_TELEGRAM_SENT_IMAGE_DIR "
                "elsewhere."
            )
        # Enforce 0700 idempotently. Prior-existing dirs with looser
        # bits get tightened; a dir already at 0700 is untouched.
        os.chmod(root, CACHE_DIR_MODE)
        return root
    # Parent may or may not exist; parents=True is defensive for tests
    # that hand us `tmp_path / "nested" / "sent"`.
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, CACHE_DIR_MODE)
    return root


# --- Filename / metadata helpers ------------------------------------------


def _now_iso_with_offset() -> tuple[datetime.datetime, str]:
    """Return (aware datetime, ISO-8601 string with UTC offset).

    Uses the local timezone via `datetime.now().astimezone()` — matches
    §4.2's example `2026-07-25T15:30:42-07:00`. The two-value return
    lets `cache_binary` reuse the datetime for `expires_at` math without
    re-parsing the string.
    """
    now = datetime.datetime.now().astimezone()
    return now, now.isoformat(timespec="seconds")


def _detect_mime(path: Path) -> str:
    """Return the best-effort mime type for the photo.

    Defaults to `image/jpeg` on unknown extension so the cache still
    records a plausible value even for exotic sources (screenshots
    saved without an extension, etc.). Telegram validates the actual
    bytes on its side; the cache only records what we told Telegram
    the type was.
    """
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "image/jpeg"


def _extension_for(source_path: Path, mime: str) -> str:
    """Return the filename extension to use, without the leading dot.

    Prefer the source path's own extension when present (so an
    `.HEIC` original doesn't silently become `.jpg`); fall back to
    `mimetypes.guess_extension(mime)`; final fallback is `jpg` for
    the same reason `_detect_mime` defaults to image/jpeg.
    """
    if source_path.suffix:
        return source_path.suffix.lstrip(".").lower()
    guessed = mimetypes.guess_extension(mime)
    if guessed:
        return guessed.lstrip(".").lower()
    return "jpg"


def _compute_sha256_hex(payload: bytes) -> str:
    """Full 64-char hex digest of the payload."""
    return hashlib.sha256(payload).hexdigest()


def _compute_expires_at(
    sent_at_dt: datetime.datetime, retention_days: Union[int, str]
) -> Optional[str]:
    """Return the ISO-8601 expires_at string, or None for 'forever'.

    A non-'forever' value that is not a positive int raises — a caller
    that passes `retention_days=-5` or `retention_days='soon'` gets an
    early, loud failure rather than an ambiguous cache entry.
    """
    if _is_forever(retention_days):
        return None
    if not isinstance(retention_days, int) or retention_days <= 0:
        raise SentImageCacheError(
            f"retention_days must be a positive int or the string "
            f"{RETENTION_FOREVER!r}; got {retention_days!r}."
        )
    expires_dt = sent_at_dt + datetime.timedelta(days=retention_days)
    return expires_dt.isoformat(timespec="seconds")


def _is_forever(retention_days: Union[int, str]) -> bool:
    """True iff the caller opted out of expiry with the 'forever' sentinel."""
    return isinstance(retention_days, str) and retention_days.lower() == RETENTION_FOREVER


def _make_stem(sent_at_dt: datetime.datetime, sha256_hex: str) -> str:
    """Return the shared filename stem `YYYYMMDD-HHMMSS-<sha8>`.

    Naive local-time timestamp (already carried in the aware
    `sent_at_dt` for tz context) — the sidecar JSON's `sent_at` field
    is the authoritative timestamp with offset; the filename is just a
    sort key.
    """
    ts = sent_at_dt.strftime(FILENAME_TIMESTAMP_FORMAT)
    sha8 = sha256_hex[:FILENAME_SHA8_LENGTH]
    return f"{ts}-{sha8}"


# --- Core cache writer + committer -----------------------------------------


def cache_binary(
    source_path: Union[str, Path],
    *,
    retention_days: Union[int, str] = DEFAULT_RETENTION_DAYS,
    label: Optional[str] = None,
    caption: Optional[str] = None,
    chat_id: Union[int, str] = 0,
    cache_dir: Optional[Path] = None,
) -> PendingCacheEntry:
    """Stage a photo into the cache — bytes file only; sidecar comes later.

    Reads the photo bytes, computes sha256 + mime + stem, writes the
    bytes file at 0600, and returns a `PendingCacheEntry` handle
    carrying every field the sidecar will need. The sidecar is NOT
    written here; the caller invokes `commit_record` once the transport
    returns a message_id and file_id. This split keeps a failed send
    from leaving a phantom ledger entry that a later `--dedup` lookup
    would incorrectly trust.

    Args:
        source_path: absolute or user-relative path to the photo file.
        retention_days: expiry window in days, or the sentinel
            `'forever'`. Default 60 per §4.1.
        label: caller-supplied label; defaults to `source_path.stem`.
        caption: optional caption text, verbatim.
        chat_id: destination chat_id at cache-time (int or str). The
            caller's verb layer resolves the profile default chat_id
            before invoking us.
        cache_dir: override for the resolved cache directory. Tests
            typically omit this and set `MINERU_TELEGRAM_SENT_IMAGE_DIR`
            instead so the resolution path is exercised end-to-end.

    Returns:
        A `PendingCacheEntry` handle. The `bytes_path` is on disk;
        `sidecar_path` is the target for `commit_record`.

    Raises:
        SentImageCacheError: source file missing / not readable, or
            retention_days shape is invalid.
    """
    root = ensure_cache_dir(cache_dir)
    src = Path(source_path).expanduser()
    if not src.exists() or not src.is_file():
        raise SentImageCacheError(
            f"cache_binary: source photo {src} does not exist or is not a file."
        )

    payload = src.read_bytes()
    sha256_hex = _compute_sha256_hex(payload)
    mime = _detect_mime(src)
    ext = _extension_for(src, mime)
    sent_at_dt, sent_at_iso = _now_iso_with_offset()
    stem = _make_stem(sent_at_dt, sha256_hex)
    bytes_path = root / f"{stem}.{ext}"
    sidecar_path = root / f"{stem}.json"

    # Guard against stem collision (spec §4.2 §"low volume expected"):
    # two distinct photos whose FULL sha256 differs but whose first-8-hex
    # matches AND that get cached in the same second would otherwise
    # silently overwrite each other. Cheap check — we only re-hash on a
    # collision candidate, which is a ~1/2^32-per-second event in
    # practice. If bytes match, the file is a genuine idempotent re-cache
    # (identical photo, identical second) and we overwrite as before.
    if bytes_path.exists():
        existing_sha256 = _compute_sha256_hex(bytes_path.read_bytes())
        if existing_sha256 != sha256_hex:
            raise SentImageCacheError(
                f"cache_binary: stem collision on {stem} — a different photo "
                f"(sha256={existing_sha256[:16]}…) already occupies {bytes_path}; "
                f"new photo (sha256={sha256_hex[:16]}…) refuses to overwrite. "
                "Retry the send in the next second or widen FILENAME_SHA8_LENGTH."
            )

    # Write bytes file with 0600 mode from the start. `os.open` +
    # `os.fdopen` guarantees the mode is applied BEFORE the payload
    # is written — a plain `open(...).write()` would create the file
    # at the process umask (typically 0644) and only chmod after.
    _write_bytes_600(bytes_path, payload)

    expires_at = _compute_expires_at(sent_at_dt, retention_days)
    effective_label = label if label else src.stem

    return PendingCacheEntry(
        stem=stem,
        bytes_path=bytes_path,
        sidecar_path=sidecar_path,
        sent_at=sent_at_iso,
        sha256=sha256_hex,
        mime=mime,
        source_path=str(src),
        chat_id=chat_id,
        caption=caption,
        retention_days=retention_days,
        expires_at=expires_at,
        label=effective_label,
    )


def commit_record(
    handle: PendingCacheEntry,
    *,
    telegram_file_id: Optional[str],
    message_id: Optional[int],
) -> SentImageRecord:
    """Write the sidecar JSON at 0600 and return the committed record.

    Called only on transport success — a failed send should NOT
    invoke this, so the ledger never carries an entry with
    `telegram_file_id: null` that a later `--dedup` lookup would
    mistake for a valid cached upload.

    The JSON schema is exactly §4.2:

        {
          "sent_at": "...", "chat_id": ..., "caption": "...",
          "source_path": "...", "sha256": "...", "mime": "...",
          "telegram_file_id": "...", "message_id": ...,
          "retention_days": ..., "expires_at": "..." or null,
          "label": "..."
        }

    Args:
        handle: the `PendingCacheEntry` returned by `cache_binary`.
        telegram_file_id: the largest photo variant's file_id, as
            returned by the transport layer.
        message_id: the sent message's id, as returned by Telegram.

    Returns:
        A `SentImageRecord` snapshot the caller can log / render.
    """
    sidecar = {
        "sent_at": handle.sent_at,
        "chat_id": handle.chat_id,
        "caption": handle.caption,
        "source_path": handle.source_path,
        "sha256": handle.sha256,
        "mime": handle.mime,
        "telegram_file_id": telegram_file_id,
        "message_id": message_id,
        "retention_days": handle.retention_days,
        "expires_at": handle.expires_at,
        "label": handle.label,
    }
    _write_json_600(handle.sidecar_path, sidecar)
    return SentImageRecord(
        stem=handle.stem,
        bytes_path=handle.bytes_path,
        sidecar_path=handle.sidecar_path,
        sent_at=handle.sent_at,
        chat_id=handle.chat_id,
        caption=handle.caption,
        source_path=handle.source_path,
        sha256=handle.sha256,
        mime=handle.mime,
        telegram_file_id=telegram_file_id,
        message_id=message_id,
        retention_days=handle.retention_days,
        expires_at=handle.expires_at,
        label=handle.label,
        raw=sidecar,
    )


# --- Tight-permission file writers ----------------------------------------


def _cleanup_partial_write(target: Path) -> None:
    """Move a genuinely-partial `_write_bytes_600` output to Trash, loud on failure.

    Called only from `_write_bytes_600`'s exception path, after a mid-write
    failure has left a zero-length / partial file behind. That file has
    never been surfaced to any caller (the sidecar has not been written
    yet, so `iter_records` / `lookup_by_sha256` won't see it), but leaving
    it on disk still risks confusing a later same-stem write. We route
    through the `trash` CLI so the SOUL.md "trash, never rm" rule holds
    for every destructive path in this module — not just `prune_expired`.

    Cleanup failures are logged at WARNING (never swallowed silently).
    We do NOT re-raise: the original write exception is more useful to
    the caller than a cleanup failure, so we let `_write_bytes_600`'s
    outer `raise` propagate the real cause. If the trash step failed the
    partial file remains as an orphan (mode 0600, no sidecar — invisible
    to every ledger read); the warning tells the operator to sweep it.
    """
    if not target.exists():
        return
    binary = _resolve_trash_binary()
    if not (Path(binary).exists() or shutil.which(binary)):
        logger.warning(
            "failed to clean up partial write %s: trash binary %r not found; "
            "partial file left on disk as orphan (mode 0600, no sidecar).",
            target,
            binary,
        )
        return
    result = subprocess.run(
        [binary, str(target)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        logger.warning(
            "failed to trash partial write %s: %r exited %d; stderr=%r; "
            "partial file left on disk as orphan (mode 0600, no sidecar).",
            target,
            binary,
            result.returncode,
            (result.stderr or "").strip(),
        )


def _write_bytes_600(target: Path, payload: bytes) -> None:
    """Create `target` with mode 0600 and write `payload` atomically-ish.

    Uses `os.open(O_WRONLY|O_CREAT|O_TRUNC, mode=0o600)` so the mode
    bits apply BEFORE the write. Same-stem collision is guarded by the
    caller (`cache_binary` compares full sha256 before invoking us and
    raises on a genuine mismatch), so an existing target here is either
    an idempotent re-cache of identical bytes or a caller-side path
    reuse — the O_TRUNC overwrite is safe in both.

    On mid-write failure the partial file is routed through the `trash`
    CLI via `_cleanup_partial_write` (SOUL.md rule: never `rm`). Cleanup
    failures are logged, never swallowed silently — the original write
    exception is always re-raised so the caller sees the real cause.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(target), flags, CACHE_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
    except Exception:
        _cleanup_partial_write(target)
        raise
    # Chmod defensively: some filesystems (network mounts) ignore the
    # `mode` arg to `os.open`. This second call is a no-op on a
    # well-behaved local disk and a correctness step on the others.
    os.chmod(target, CACHE_FILE_MODE)


def _write_json_600(target: Path, payload: dict) -> None:
    """Serialize `payload` as pretty JSON at mode 0600."""
    # Deterministic key order + trailing newline: matches SOUL.md's "no
    # magic differences between two identical calls" and lets a human
    # `diff` two sidecars without JSON reordering noise.
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    encoded = text.encode("utf-8")
    _write_bytes_600(target, encoded)


# --- Ledger reads (lookup / iter / search) --------------------------------


def _iter_sidecar_paths(cache_dir: Optional[Path] = None) -> Iterator[Path]:
    """Yield every `*.json` sidecar in the cache directory, sorted.

    Missing cache dir yields nothing (safe for fresh installs and for
    dry-run cleanup on an untouched profile). Non-sidecar files (bytes
    files, stray hidden files) are skipped.
    """
    root = cache_dir or resolve_cache_dir()
    if not root.exists() or not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.suffix == ".json":
            yield entry


def _load_record(sidecar_path: Path) -> Optional[SentImageRecord]:
    """Read a sidecar JSON and return the record, or None on malformed.

    A malformed sidecar (bad JSON, missing fields) is skipped rather
    than raised — the ledger reads must remain robust against a
    half-written file (e.g. process killed mid-commit) so the healthy
    entries stay browsable. But each skip is logged at WARNING so a
    corrupt sidecar doesn't silently drop out of `iter_records`,
    `search_records`, `lookup_by_sha256`, and `prune_expired` without
    any operator-visible signal. `prune_expired`'s _is_expired treats
    an unparseable `expires_at` as expired, so a broken sidecar is
    reclaimable on the next prune run — the log line points the
    operator at the file.

    The bytes file is looked up by scanning siblings that share the
    stem prefix; the extension is not fixed to a single MIME.
    """
    try:
        raw_text = sidecar_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "malformed sidecar %s (unreadable: %s); skipping.",
            sidecar_path,
            exc,
        )
        return None
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "malformed sidecar %s (invalid JSON: %s); skipping.",
            sidecar_path,
            exc,
        )
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "malformed sidecar %s (top-level is %s, expected dict); skipping.",
            sidecar_path,
            type(raw).__name__,
        )
        return None

    stem = sidecar_path.stem
    # Sibling bytes file: same stem, any extension EXCEPT .json.
    bytes_path = _find_bytes_sibling(sidecar_path)

    return SentImageRecord(
        stem=stem,
        bytes_path=bytes_path if bytes_path is not None else sidecar_path,
        sidecar_path=sidecar_path,
        sent_at=str(raw.get("sent_at", "")),
        chat_id=raw.get("chat_id", ""),
        caption=raw.get("caption"),
        source_path=str(raw.get("source_path", "")),
        sha256=str(raw.get("sha256", "")),
        mime=str(raw.get("mime", "")),
        telegram_file_id=raw.get("telegram_file_id"),
        message_id=raw.get("message_id"),
        retention_days=raw.get("retention_days", DEFAULT_RETENTION_DAYS),
        expires_at=raw.get("expires_at"),
        label=str(raw.get("label", "")),
        raw=raw,
    )


def _find_bytes_sibling(sidecar_path: Path) -> Optional[Path]:
    """Return the bytes file that shares the sidecar's stem, or None."""
    parent = sidecar_path.parent
    stem = sidecar_path.stem
    for candidate in parent.glob(f"{stem}.*"):
        if candidate.suffix == ".json":
            continue
        if candidate.is_file():
            return candidate
    return None


def _is_expired(record: SentImageRecord, *, now: Optional[datetime.datetime] = None) -> bool:
    """True iff the record's expires_at is in the past.

    `expires_at is None` means "forever" — treated as never expired.
    A missing / unparseable string is treated as expired so a corrupt
    entry gets swept eventually rather than lingering forever.
    """
    if record.expires_at is None:
        return False
    try:
        expires_dt = datetime.datetime.fromisoformat(record.expires_at)
    except ValueError:
        return True
    reference = now or datetime.datetime.now().astimezone()
    if expires_dt.tzinfo is None:
        # Sidecar was written by an older process without tz — treat as
        # local time for the comparison.
        expires_dt = expires_dt.replace(tzinfo=reference.tzinfo)
    return expires_dt <= reference


def lookup_by_sha256(
    sha256_hex: str,
    *,
    cache_dir: Optional[Path] = None,
    now: Optional[datetime.datetime] = None,
) -> Optional[SentImageRecord]:
    """Return the newest non-expired record matching sha256_hex, or None.

    Dedup fast-path used by `mineru telegram photo --dedup`: if a
    matching record exists AND has a populated `telegram_file_id` AND
    has not expired, the caller can skip the multipart upload and POST
    `sendPhoto?photo=<file_id>` instead.

    Skips:
      - records with an empty / missing `telegram_file_id` (send failed
        or was never committed — treat as if not in cache).
      - records whose `expires_at` is in the past (Telegram may have
        evicted its own cached copy anyway; safer to re-upload).

    Newest = highest sort order on the filename stem (which starts with
    the ISO date + time). Deterministic, no clock re-check.
    """
    matches: List[SentImageRecord] = []
    for sidecar in _iter_sidecar_paths(cache_dir):
        record = _load_record(sidecar)
        if record is None:
            continue
        if record.sha256 != sha256_hex:
            continue
        if not record.telegram_file_id:
            continue
        if _is_expired(record, now=now):
            continue
        matches.append(record)
    if not matches:
        return None
    matches.sort(key=lambda r: r.stem, reverse=True)
    return matches[0]


def iter_records(
    *,
    since: Optional[datetime.datetime] = None,
    chat_id: Optional[Union[int, str]] = None,
    cache_dir: Optional[Path] = None,
) -> Iterator[SentImageRecord]:
    """Yield committed records, optionally filtered by `since` / `chat_id`.

    `since` filters on `sent_at` parsed as ISO-8601; a record with an
    unparseable timestamp is skipped when a `since` filter is active
    (defensive — a broken sidecar shouldn't accidentally surface). A
    `chat_id` filter compares as strings (Telegram accepts both int
    and str shapes and the sidecar preserves whichever the caller
    handed in).
    """
    for sidecar in _iter_sidecar_paths(cache_dir):
        record = _load_record(sidecar)
        if record is None:
            continue
        if since is not None:
            try:
                sent_dt = datetime.datetime.fromisoformat(record.sent_at)
            except ValueError:
                continue
            reference = since
            if sent_dt.tzinfo is None and reference.tzinfo is not None:
                sent_dt = sent_dt.replace(tzinfo=reference.tzinfo)
            if reference.tzinfo is None and sent_dt.tzinfo is not None:
                reference = reference.replace(tzinfo=sent_dt.tzinfo)
            if sent_dt < reference:
                continue
        if chat_id is not None and str(record.chat_id) != str(chat_id):
            continue
        yield record


def search_records(
    substring: str,
    *,
    cache_dir: Optional[Path] = None,
) -> List[SentImageRecord]:
    """Return records whose caption OR label contains `substring` (case-insensitive).

    Case-insensitive substring match is intentional — the operator types
    captions freely from Telegram; a case-sensitive search would miss
    the common "juno smile" vs "Juno Smile" style mismatch.
    Empty-string substring returns every committed record (parity with
    `iter_records()` with no filters).
    """
    needle = substring.lower()
    hits: List[SentImageRecord] = []
    for record in iter_records(cache_dir=cache_dir):
        haystack_caption = (record.caption or "").lower()
        haystack_label = (record.label or "").lower()
        if needle in haystack_caption or needle in haystack_label:
            hits.append(record)
    return hits


# --- Pruning (trash-based; NEVER rm) --------------------------------------


def _resolve_trash_binary() -> str:
    """Return the trash CLI path (env override or the /usr/bin default)."""
    override = os.environ.get(TRASH_BINARY_ENV)
    if override:
        return override
    return DEFAULT_TRASH_BINARY


def _trash_pair(record: SentImageRecord) -> None:
    """Move both the sidecar and bytes file to macOS Trash via `trash`.

    Uses the `trash` CLI (SOUL.md hard rule: `rm` is banned). If the
    binary is not available we raise so the caller sees a loud failure
    rather than silently skipping — a missing `trash` means the retention
    policy is NOT being enforced, and that should never be quiet.

    If the `trash` invocation returns a non-zero exit code (permission
    denied, target locked, unknown flag on a wrong binary) we raise
    `SentImageCacheError` naming the failing target and the exit code.
    Silently swallowing a non-zero returncode would leave the sidecar
    + bytes on disk while the ledger reports a successful trash — a
    classic fail-quiet corruption the caller can't detect.
    """
    binary = _resolve_trash_binary()
    if not (Path(binary).exists() or shutil.which(binary)):
        raise SentImageCacheError(
            f"prune_expired: trash binary not found at {binary!r}. "
            f"Install via `brew install trash` or set {TRASH_BINARY_ENV} to "
            "an alternate path. Refusing to use `rm` — that would be "
            "irreversible."
        )
    targets: List[str] = [str(record.sidecar_path)]
    if record.bytes_path != record.sidecar_path and record.bytes_path.exists():
        targets.append(str(record.bytes_path))
    completed = subprocess.run(
        [binary, *targets], check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:
        stderr_frag = (completed.stderr or "").strip()
        detail = f"; stderr={stderr_frag!r}" if stderr_frag else ""
        raise SentImageCacheError(
            f"prune_expired: trash binary {binary!r} exited "
            f"{completed.returncode} on targets {targets!r}{detail}. "
            "Refusing to report a successful trash when the CLI failed — "
            "the ledger would silently drift from the on-disk state."
        )


@dataclass(frozen=True)
class PruneReport:
    """Result of a `prune_expired` invocation.

    `dry_run` tells the caller whether anything was actually trashed;
    `expired_records` are the entries that met (or would have met) the
    prune threshold. `failed_records` (name, `SentImageCacheError`)
    surfaces per-record trash failures so operators see partial failures
    instead of a silent success — the ledger will still hold the sidecar
    for these entries until the underlying trash issue is fixed.
    `kept_forever` counts entries with the `forever` sentinel — surfaced
    explicitly so a caller can log "N forever entries preserved"
    alongside the trash count.
    """

    dry_run: bool
    expired_records: List[SentImageRecord]
    kept_forever: int
    kept_unexpired: int
    failed_records: List[Tuple[SentImageRecord, str]] = field(default_factory=list)


def prune_expired(
    *,
    dry_run: bool = False,
    cache_dir: Optional[Path] = None,
    now: Optional[datetime.datetime] = None,
) -> PruneReport:
    """Trash every cache entry whose `expires_at` is in the past.

    `'forever'` entries (sidecar `expires_at: null`) are ALWAYS
    preserved and counted separately in the returned report.
    `dry_run=True` walks the ledger and lists what would be trashed
    without touching the filesystem — matches the `--dry-run` flag on
    `cleanup-retention.sh`.

    Per-record trash failures (non-zero exit from the `trash` CLI on a
    specific pair) are caught and appended to `failed_records`; the walk
    continues so a single locked file can't stop the whole prune. A
    completely missing / unusable `trash` binary still raises loudly at
    the FIRST record — that's a global failure, not a per-record one.

    Returns a `PruneReport` the caller can render.
    """
    expired: List[SentImageRecord] = []
    forever_count = 0
    unexpired_count = 0
    for sidecar in _iter_sidecar_paths(cache_dir):
        record = _load_record(sidecar)
        if record is None:
            continue
        if record.expires_at is None:
            forever_count += 1
            continue
        if _is_expired(record, now=now):
            expired.append(record)
        else:
            unexpired_count += 1

    trashed: List[SentImageRecord] = []
    failed: List[Tuple[SentImageRecord, str]] = []
    if not dry_run:
        for record in expired:
            try:
                _trash_pair(record)
            except SentImageCacheError as exc:
                # "trash binary missing" is a global failure the caller
                # cannot recover from — re-raise so `prune_expired` still
                # fails loudly on a broken environment. Per-target trash
                # failures (non-zero exit on a specific pair) carry the
                # binary-name + exit-code frame from `_trash_pair` and
                # are collected as `failed_records`.
                if "trash binary not found" in str(exc):
                    raise
                failed.append((record, str(exc)))
                continue
            trashed.append(record)
    else:
        trashed = list(expired)

    return PruneReport(
        dry_run=dry_run,
        expired_records=trashed,
        kept_forever=forever_count,
        kept_unexpired=unexpired_count,
        failed_records=failed,
    )


# --- Public re-exports ----------------------------------------------------

__all__ = [
    "CACHE_DIR_MODE",
    "CACHE_FILE_MODE",
    "CACHE_SUBPATH_SEGMENTS",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_TRASH_BINARY",
    "FILENAME_TIMESTAMP_FORMAT",
    "MINERU_SENT_IMAGE_DIR_ENV",
    "PendingCacheEntry",
    "PruneReport",
    "RETENTION_FOREVER",
    "SentImageCacheError",
    "SentImageRecord",
    "TRASH_BINARY_ENV",
    "cache_binary",
    "cache_dir_for_workspace",
    "commit_record",
    "ensure_cache_dir",
    "iter_records",
    "lookup_by_sha256",
    "prune_expired",
    "resolve_cache_dir",
    "search_records",
]
