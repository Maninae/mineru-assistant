"""Tests for the sent-image retention cache (P3-02).

⚠️⚠️ SAFETY (READ TWICE) ⚠️⚠️

  NO test in this file EVER touches the live cache directory at
  `$MINERU_HOME/cache/telegram_sent_images/`. Every test sets
  `MINERU_TELEGRAM_SENT_IMAGE_DIR` to a `tmp_path` first via the
  autouse `redirect_cache_dir` fixture, so the resolver returns the
  isolated per-test directory and any accidental leak fails a bright
  assertion.

  NO test in this file EVER contacts `api.telegram.org`. The cache
  module is orthogonal to the transport in `telegram_photo.py` — it
  does no network I/O — but as belt-and-braces, this file never
  imports `telegram_photo` and never patches anything HTTP-adjacent.

  NO test calls `trash` for real either. The autouse
  `no_real_trash` fixture points `MINERU_TRASH_BINARY` at a fake shim
  written into `tmp_path` that just records its argv into a JSONL file.
  The default `/usr/bin/trash` is never invoked from a test.

Coverage:

  Cache dir management:
    - `resolve_cache_dir` honors the env override; empty string treated
      as unset; default is `$MINERU_HOME/cache/telegram_sent_images`.
    - `ensure_cache_dir` creates dir at mode 0700; idempotent on a dir
      already present; enforces 0700 on a dir with looser bits;
      raises if the path exists but isn't a dir.
    - No test writes to `$MINERU_HOME/cache/telegram_sent_images/`
      directly — asserted via a defense-in-depth path check.

  Filename + sidecar shape:
    - `cache_binary` writes a bytes file at mode 0600.
    - Filename stem is `YYYYMMDD-HHMMSS-<sha8>`; extension preserves
      the source's suffix; sha256 is the full 64-char digest.
    - `commit_record` writes a sidecar at mode 0600 with EVERY §4.2
      field name and expected shape.
    - `expires_at` = `sent_at + timedelta(days=retention_days)`
      exactly, ISO-8601 with tz offset.
    - `retention_days='forever'` writes `expires_at: null` and is
      preserved verbatim.
    - Invalid retention_days shape raises early.

  Dedup lookup:
    - `lookup_by_sha256` returns the newest matching non-expired record
      whose `telegram_file_id` is populated.
    - Skips records without `telegram_file_id`.
    - Skips records past `expires_at`.
    - Skips 'forever' records only if — no wait, they should NEVER be
      skipped; 'forever' means never expired.

  Iteration + search:
    - `iter_records(since=...)` filters by ISO timestamp.
    - `iter_records(chat_id=...)` filters by string-equal chat id.
    - `search_records("substring")` matches case-insensitively on
      caption OR label.

  Prune:
    - `prune_expired(dry_run=True)` lists but does not trash.
    - `prune_expired(dry_run=False)` invokes `trash` for both sidecar
      and bytes file (via the recording fake).
    - 'forever' entries counted in `kept_forever`, never trashed.
    - Missing `trash` binary raises with an actionable message
      (never falls back to `rm`).

  Shell rule integration:
    - `scripts/cleanup-retention.sh --dry-run` on a fresh cache dir
      exits 0 and never invokes the trash shim.
    - `scripts/cleanup-retention.sh` (non-dry-run) trashes an expired
      pair (sidecar + bytes) recorded via the trash shim.
    - `scripts/cleanup-retention.sh` (non-dry-run) preserves the
      'forever' sentinel and any not-yet-expired entry.
"""

from __future__ import annotations

import ast
import datetime
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from mineru_cli.wrappers import telegram_image_cache as tic
from mineru_cli.wrappers.telegram_image_cache import (
    CACHE_DIR_MODE,
    CACHE_FILE_MODE,
    DEFAULT_RETENTION_DAYS,
    FILENAME_TIMESTAMP_FORMAT,
    MINERU_SENT_IMAGE_DIR_ENV,
    RETENTION_FOREVER,
    TRASH_BINARY_ENV,
    PendingCacheEntry,
    PruneReport,
    SentImageCacheError,
    SentImageRecord,
    cache_binary,
    commit_record,
    ensure_cache_dir,
    iter_records,
    lookup_by_sha256,
    prune_expired,
    resolve_cache_dir,
    search_records,
)


# ==========================================================================
# Fixtures
# ==========================================================================


LIVE_CACHE_ROOT = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "cache" / "telegram_sent_images"


@pytest.fixture
def isolated_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh cache dir under tmp_path, wired via the env override.

    Every test gets its own isolated directory so no state leaks across
    tests. The env override is the same one production reads, so we
    exercise `resolve_cache_dir` end-to-end rather than passing the
    path down explicitly.
    """
    root = tmp_path / "sent_images"
    monkeypatch.setenv(MINERU_SENT_IMAGE_DIR_ENV, str(root))
    return root


@pytest.fixture(autouse=True)
def no_writes_to_live_cache_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: any test that writes into $MINERU_HOME/cache/... fails.

    The env override in `isolated_cache_dir` should already prevent
    this, but a bug in a future refactor that hardcoded the live path
    would slip through. Wrap `Path.mkdir` to reject writes anywhere
    under `$MINERU_HOME/cache/telegram_sent_images/`.

    Comparison uses resolved string prefixes rather than `Path.samefile`
    because samefile requires BOTH paths to exist — on any host that
    doesn't already have the live cache directory (i.e. every fresh
    checkout), samefile raised OSError which the guard used to swallow,
    silently reducing the fixture to a no-op. Prefix comparison works
    regardless of whether the target exists.
    """
    original_mkdir = Path.mkdir
    # Resolve the live root once, matching the resolution applied to the
    # mkdir target below. `.resolve(strict=False)` works on a non-existent
    # path and canonicalizes macOS firmlinks (e.g. /tmp -> /private/tmp) so
    # a MINERU_HOME under /tmp still prefix-matches the resolved target.
    live_prefix_str = str(LIVE_CACHE_ROOT.expanduser().resolve())

    def guarded_mkdir(self, *args, **kwargs):
        resolved = self.expanduser()
        try:
            resolved_abs = resolved.resolve()
        except (OSError, RuntimeError):
            resolved_abs = resolved
        target_str = str(resolved_abs)
        # `target` is banned when it equals the live root OR sits under it.
        # Adding a trailing separator to the prefix avoids matching a
        # sibling like `.../telegram_sent_images_backup`.
        under_live = (
            target_str == live_prefix_str
            or target_str.startswith(live_prefix_str + os.sep)
        )
        if under_live:
            raise AssertionError(
                f"test attempted to mkdir under the live cache dir: {self}. "
                "Every test MUST use MINERU_TELEGRAM_SENT_IMAGE_DIR "
                "pointing at tmp_path."
            )
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)


@pytest.fixture(autouse=True)
def no_real_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect `_resolve_trash_binary` to a fake shim that records argv.

    The shim is a python script that appends its argv to a JSONL file
    in `tmp_path`. Tests that care about what was trashed can read the
    log; tests that don't care get a silent no-op that never touches
    `~/.Trash/`.
    """
    log_path = tmp_path / "trash-shim.jsonl"
    shim_path = tmp_path / "trash-shim.py"
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        f"with open({str(log_path)!r}, 'a', encoding='utf-8') as h:\n"
        "    h.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    os.chmod(shim_path, 0o755)
    monkeypatch.setenv(TRASH_BINARY_ENV, str(shim_path))
    return log_path


@pytest.fixture
def sample_photo(tmp_path: Path) -> Path:
    """A tiny fake JPEG on disk with deterministic content."""
    p = tmp_path / "river-smile.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0MINERU_SENTINEL_IMAGE_BYTES_v1")
    return p


def _read_trash_log(log_path: Path) -> List[List[str]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


# ==========================================================================
# Belt-and-braces guard: `no_writes_to_live_cache_dir` must ACTUALLY fire.
# The previous samefile-based check silently no-op'd on any host without
# the live cache dir present; this test proves the prefix-based guard
# raises regardless of whether the target exists.
# ==========================================================================


def test_no_writes_to_live_cache_dir_guard_fires_on_live_path(tmp_path: Path) -> None:
    """A mkdir under the live cache root MUST raise AssertionError.

    Ensures the belt-and-braces guard is not dead code — a regression that
    hardcoded `$MINERU_HOME/cache/telegram_sent_images/...` in the wrapper
    would trip here even on a machine where the live directory doesn't
    exist yet.
    """
    live_target = LIVE_CACHE_ROOT.expanduser() / "regression-probe"
    with pytest.raises(AssertionError, match="live cache dir"):
        live_target.mkdir(parents=True, exist_ok=True)


def test_no_writes_to_live_cache_dir_guard_allows_tmp_path(tmp_path: Path) -> None:
    """A mkdir under tmp_path must NOT raise — only the live root is banned."""
    safe_target = tmp_path / "sibling" / "child"
    # If this raised, the guard would be over-broad and would break every
    # legitimate isolated test.
    safe_target.mkdir(parents=True, exist_ok=True)
    assert safe_target.is_dir()


# ==========================================================================
# Cache dir management
# ==========================================================================


def test_resolve_cache_dir_raises_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2 completion (2026-08-28 rev): the $MINERU_HOME fallback is retired.

    Previously `resolve_cache_dir()` with no env override returned
    `$MINERU_HOME/cache/telegram_sent_images` (the live-workspace default).
    Under the profile-isolation invariant, that fallback silently wrote
    photo bytes from any profile into the operator's workspace — a cross-profile
    leak. The new contract is: fail loud when neither `cache_dir=` was
    passed nor the env override is set. The verb layer supplies
    `cache_dir=` from `Profile.workspace_absolute`; tests set the env.
    """
    monkeypatch.delenv(MINERU_SENT_IMAGE_DIR_ENV, raising=False)
    with pytest.raises(SentImageCacheError, match="sent-image cache dir not resolved"):
        resolve_cache_dir()


def test_resolve_cache_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "override"
    monkeypatch.setenv(MINERU_SENT_IMAGE_DIR_ENV, str(override))
    resolved = resolve_cache_dir()
    # `resolve()` inside the module normalizes symlinks + relative paths.
    assert resolved == override.resolve()


def test_resolve_cache_dir_empty_env_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty-string env var matches shell semantics (unset) → same fail-loud path.

    Complement to `test_resolve_cache_dir_raises_when_env_unset`: an
    explicitly-emptied env var must not be treated as "use the legacy
    default" — the legacy default is gone.
    """
    monkeypatch.setenv(MINERU_SENT_IMAGE_DIR_ENV, "")
    with pytest.raises(SentImageCacheError, match="sent-image cache dir not resolved"):
        resolve_cache_dir()


def test_default_sent_image_dir_symbol_is_retired() -> None:
    """The legacy home-relative constant is deleted (F2 completion, 2026-08-28).

    A regression that re-added `DEFAULT_SENT_IMAGE_DIR = Path.home() /
    ".mineru" / ...` would re-open the cross-profile leak, so we pin
    the symbol's absence as a durable invariant.
    """
    from mineru_cli.wrappers import telegram_image_cache as tic_module

    assert not hasattr(tic_module, "DEFAULT_SENT_IMAGE_DIR"), (
        "DEFAULT_SENT_IMAGE_DIR was retired to prevent cross-profile "
        "writes into $MINERU_HOME — do not re-add it. Callers thread the "
        "per-profile path via `cache_dir=` or set "
        f"{MINERU_SENT_IMAGE_DIR_ENV} explicitly."
    )


def test_ensure_cache_dir_creates_at_0700(isolated_cache_dir: Path) -> None:
    assert not isolated_cache_dir.exists()
    created = ensure_cache_dir()
    assert created.exists()
    assert created.is_dir()
    mode = os.stat(created).st_mode & 0o777
    assert mode == CACHE_DIR_MODE


def test_ensure_cache_dir_tightens_loose_permissions(isolated_cache_dir: Path) -> None:
    """A pre-existing dir at 0755 gets chmod'd to 0700 on ensure."""
    isolated_cache_dir.mkdir(parents=True)
    os.chmod(isolated_cache_dir, 0o755)
    ensure_cache_dir()
    mode = os.stat(isolated_cache_dir).st_mode & 0o777
    assert mode == CACHE_DIR_MODE


def test_ensure_cache_dir_raises_if_path_exists_and_is_a_file(
    isolated_cache_dir: Path,
) -> None:
    isolated_cache_dir.parent.mkdir(parents=True, exist_ok=True)
    isolated_cache_dir.write_text("not a directory")
    with pytest.raises(SentImageCacheError):
        ensure_cache_dir()


# ==========================================================================
# cache_binary: bytes-file mode + filename + returned handle
# ==========================================================================


def test_cache_binary_writes_bytes_file_at_0600(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(sample_photo, chat_id=123)
    assert handle.bytes_path.exists()
    assert handle.bytes_path.parent == isolated_cache_dir.resolve()
    mode = os.stat(handle.bytes_path).st_mode & 0o777
    assert mode == CACHE_FILE_MODE
    # Bytes on disk are byte-identical to the source.
    assert handle.bytes_path.read_bytes() == sample_photo.read_bytes()


def test_cache_binary_stem_shape_and_sha256(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(sample_photo, chat_id=999)
    # Full 64-char hex digest.
    assert len(handle.sha256) == 64
    assert all(c in "0123456789abcdef" for c in handle.sha256)
    # Stem shape: 8-char date + '-' + 6-char time + '-' + 8-char sha slice.
    parts = handle.stem.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 8 and parts[0].isdigit()
    assert len(parts[1]) == 6 and parts[1].isdigit()
    assert parts[2] == handle.sha256[:8]
    # Extension preserved from source.
    assert handle.bytes_path.suffix == ".jpg"


def test_cache_binary_missing_source_raises(isolated_cache_dir: Path) -> None:
    with pytest.raises(SentImageCacheError):
        cache_binary("/tmp/definitely-not-a-real-file-xyz.jpg", chat_id=1)


def test_cache_binary_label_defaults_to_source_stem(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(sample_photo, chat_id=1)
    assert handle.label == sample_photo.stem


def test_cache_binary_label_explicit_wins(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(sample_photo, label="river-first-smile", chat_id=1)
    assert handle.label == "river-first-smile"


def test_cache_binary_forever_retention_has_null_expires(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(sample_photo, retention_days=RETENTION_FOREVER, chat_id=1)
    assert handle.expires_at is None
    assert handle.retention_days == RETENTION_FOREVER


def test_cache_binary_invalid_retention_raises(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    for bad in (0, -1, -30, "soon", "3d", 3.14):
        with pytest.raises(SentImageCacheError):
            cache_binary(sample_photo, retention_days=bad, chat_id=1)


def test_cache_binary_sent_at_has_tz_offset(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(sample_photo, chat_id=1)
    # Must parse as tz-aware ISO-8601 (has '+HH:MM' or '-HH:MM' suffix).
    parsed = datetime.datetime.fromisoformat(handle.sent_at)
    assert parsed.tzinfo is not None


def test_cache_binary_refuses_same_stem_different_bytes(
    isolated_cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two photos with matching timestamp + sha8 prefix but different sha256
    must not silently overwrite each other.

    Regression guard for the same-second sha8 collision bug: `_make_stem`
    keys only on the first 8 hex chars of sha256, so a distinct pair
    whose full sha256 differs but whose first 8 hex match AND that gets
    cached in the same second would previously O_TRUNC-overwrite the
    earlier record. We force the collision by pinning the clock and the
    sha8 slice and verify `cache_binary` raises loudly instead of
    clobbering.
    """
    # Two distinct payloads.
    photo_a = tmp_path / "a.jpg"
    photo_b = tmp_path / "b.jpg"
    photo_a.write_bytes(b"AAAAA")
    photo_b.write_bytes(b"BBBBB")

    # Pin the clock so both cache_binary calls land in the same second.
    frozen_now = datetime.datetime(2026, 7, 25, 15, 30, 42).astimezone()

    def fake_now_iso() -> tuple[datetime.datetime, str]:
        return frozen_now, frozen_now.isoformat(timespec="seconds")

    monkeypatch.setattr(tic, "_now_iso_with_offset", fake_now_iso)

    # Force both sha256 digests to share the first 8 hex chars while
    # differing afterwards. `_compute_sha256_hex` is the single sha256
    # helper, so patching it covers both the write and the collision
    # guard's re-hash. The second call sees the different suffix.
    shared_prefix = "cafe0001"
    payloads: dict[bytes, str] = {
        b"AAAAA": shared_prefix + "0" * 56,
        b"BBBBB": shared_prefix + "1" * 56,
    }

    def fake_sha(payload: bytes) -> str:
        return payloads[payload]

    monkeypatch.setattr(tic, "_compute_sha256_hex", fake_sha)

    handle_a = cache_binary(photo_a, chat_id=1)
    assert handle_a.bytes_path.exists()
    assert handle_a.bytes_path.read_bytes() == b"AAAAA"

    # Second cache_binary with a DIFFERENT payload but same stem must
    # raise; the earlier bytes stay put (no clobber).
    with pytest.raises(SentImageCacheError, match="stem collision"):
        cache_binary(photo_b, chat_id=1)

    # First record's bytes are still intact — no silent overwrite.
    assert handle_a.bytes_path.read_bytes() == b"AAAAA"


def test_cache_binary_idempotent_re_cache_of_identical_bytes(
    isolated_cache_dir: Path, sample_photo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-caching the SAME photo in the same second is a no-op overwrite, not a collision.

    Full sha256 matches, so the stem-collision guard treats this as an
    idempotent re-cache and lets the write proceed (this is the current
    documented behavior: `_write_bytes_600` writes the same bytes over
    themselves and `commit_record` rewrites the sidecar). No raise.
    """
    frozen_now = datetime.datetime(2026, 7, 25, 15, 30, 42).astimezone()

    def fake_now_iso() -> tuple[datetime.datetime, str]:
        return frozen_now, frozen_now.isoformat(timespec="seconds")

    monkeypatch.setattr(tic, "_now_iso_with_offset", fake_now_iso)

    handle_1 = cache_binary(sample_photo, chat_id=1)
    handle_2 = cache_binary(sample_photo, chat_id=1)
    assert handle_1.stem == handle_2.stem
    assert handle_1.bytes_path.read_bytes() == handle_2.bytes_path.read_bytes()


# ==========================================================================
# commit_record: sidecar shape + permissions
# ==========================================================================


def _canonical_sidecar_keys() -> set:
    """Every key §4.2 mandates on the sidecar JSON."""
    return {
        "sent_at",
        "chat_id",
        "caption",
        "source_path",
        "sha256",
        "mime",
        "telegram_file_id",
        "message_id",
        "retention_days",
        "expires_at",
        "label",
    }


def test_commit_record_writes_sidecar_at_0600(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(sample_photo, chat_id=42, caption="hi")
    record = commit_record(handle, telegram_file_id="TG_FID", message_id=99)
    assert record.sidecar_path.exists()
    mode = os.stat(record.sidecar_path).st_mode & 0o777
    assert mode == CACHE_FILE_MODE


def test_commit_record_sidecar_has_every_spec_field(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(
        sample_photo,
        chat_id=12345,
        caption="River smiling",
        retention_days=60,
        label="river-first-smile",
    )
    commit_record(handle, telegram_file_id="TG_FID_XYZ", message_id=456)
    raw = json.loads(handle.sidecar_path.read_text(encoding="utf-8"))
    assert set(raw.keys()) == _canonical_sidecar_keys()
    assert raw["chat_id"] == 12345
    assert raw["caption"] == "River smiling"
    assert raw["source_path"] == str(sample_photo)
    assert raw["sha256"] == handle.sha256
    assert raw["mime"] == "image/jpeg"
    assert raw["telegram_file_id"] == "TG_FID_XYZ"
    assert raw["message_id"] == 456
    assert raw["retention_days"] == 60
    assert raw["label"] == "river-first-smile"


def test_commit_record_expires_at_equals_sent_plus_retention(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(sample_photo, retention_days=60, chat_id=1)
    commit_record(handle, telegram_file_id="FID", message_id=1)
    raw = json.loads(handle.sidecar_path.read_text(encoding="utf-8"))
    sent_dt = datetime.datetime.fromisoformat(raw["sent_at"])
    exp_dt = datetime.datetime.fromisoformat(raw["expires_at"])
    delta = exp_dt - sent_dt
    assert delta == datetime.timedelta(days=60)
    # Both timestamps must carry a tz offset.
    assert sent_dt.tzinfo is not None
    assert exp_dt.tzinfo is not None


def test_commit_record_forever_writes_null_expires(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    handle = cache_binary(sample_photo, retention_days=RETENTION_FOREVER, chat_id=1)
    commit_record(handle, telegram_file_id="FID", message_id=1)
    raw = json.loads(handle.sidecar_path.read_text(encoding="utf-8"))
    assert raw["expires_at"] is None
    assert raw["retention_days"] == RETENTION_FOREVER


def test_commit_record_sidecar_has_deterministic_key_order(
    isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """`sort_keys=True` gives a stable diff-friendly ordering."""
    handle = cache_binary(sample_photo, chat_id=1)
    commit_record(handle, telegram_file_id="FID", message_id=1)
    text = handle.sidecar_path.read_text(encoding="utf-8")
    # Ordered subset of the mandated keys, in sorted order.
    positions = [text.index(f'"{key}"') for key in sorted(_canonical_sidecar_keys())]
    assert positions == sorted(positions)


# ==========================================================================
# lookup_by_sha256: dedup fast-path
# ==========================================================================


def _commit_synthetic(
    cache_dir: Path,
    *,
    sha256: str,
    telegram_file_id: str | None,
    expires_at: str | None,
    stem: str,
    sent_at: str = "2026-07-01T12:00:00-07:00",
    chat_id: int = 1,
    caption: str = "",
    label: str = "syn",
) -> Path:
    """Write a canned sidecar + bytes pair by hand.

    Bypasses `cache_binary` so we can construct records with any
    expires_at / file_id shape the test needs (past, future,
    null-file-id, etc.) without waiting for real clock time.
    """
    ensure_cache_dir(cache_dir)
    bytes_path = cache_dir / f"{stem}.jpg"
    sidecar_path = cache_dir / f"{stem}.json"
    bytes_path.write_bytes(b"synthetic bytes")
    os.chmod(bytes_path, CACHE_FILE_MODE)
    payload = {
        "sent_at": sent_at,
        "chat_id": chat_id,
        "caption": caption,
        "source_path": f"/tmp/{stem}.jpg",
        "sha256": sha256,
        "mime": "image/jpeg",
        "telegram_file_id": telegram_file_id,
        "message_id": 1,
        "retention_days": 60,
        "expires_at": expires_at,
        "label": label,
    }
    sidecar_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.chmod(sidecar_path, CACHE_FILE_MODE)
    return sidecar_path


def _now_offset_str(delta_days: int) -> str:
    """ISO-8601 timestamp `delta_days` from now with local tz offset."""
    dt = datetime.datetime.now().astimezone() + datetime.timedelta(days=delta_days)
    return dt.isoformat(timespec="seconds")


def test_lookup_returns_matching_record(isolated_cache_dir: Path) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260701-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="TG_FID",
        expires_at=_now_offset_str(+30),
    )
    hit = lookup_by_sha256("X" * 64)
    assert hit is not None
    assert hit.telegram_file_id == "TG_FID"


def test_lookup_skips_expired_records(isolated_cache_dir: Path) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20250101-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="TG_FID",
        expires_at=_now_offset_str(-1),
    )
    assert lookup_by_sha256("X" * 64) is None


def test_lookup_skips_records_missing_file_id(isolated_cache_dir: Path) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260701-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id=None,
        expires_at=_now_offset_str(+30),
    )
    assert lookup_by_sha256("X" * 64) is None


def test_lookup_returns_newest_of_multiple_matches(isolated_cache_dir: Path) -> None:
    """Two matches at different stems -> newest (highest stem) wins."""
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260701-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="OLD",
        expires_at=_now_offset_str(+30),
    )
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260801-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="NEW",
        expires_at=_now_offset_str(+30),
    )
    hit = lookup_by_sha256("X" * 64)
    assert hit is not None
    assert hit.telegram_file_id == "NEW"


def test_lookup_forever_entry_never_expires(isolated_cache_dir: Path) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20200101-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="TG_FID",
        expires_at=None,  # forever
    )
    hit = lookup_by_sha256("X" * 64)
    assert hit is not None
    assert hit.telegram_file_id == "TG_FID"


def test_lookup_no_match_returns_none(isolated_cache_dir: Path) -> None:
    assert lookup_by_sha256("Y" * 64) is None


# ==========================================================================
# iter_records + search_records
# ==========================================================================


def test_iter_records_returns_all_committed(isolated_cache_dir: Path) -> None:
    for i, stem_prefix in enumerate(("20260701", "20260702", "20260703")):
        _commit_synthetic(
            isolated_cache_dir.resolve(),
            stem=f"{stem_prefix}-120000-aaaaaaaa",
            sha256=str(i) * 64,
            telegram_file_id=f"F{i}",
            expires_at=_now_offset_str(+30),
        )
    assert len(list(iter_records())) == 3


def test_iter_records_since_filter(isolated_cache_dir: Path) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260601-120000-aaaaaaaa",
        sha256="A" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(+30),
        sent_at="2026-06-01T12:00:00-07:00",
    )
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260801-120000-bbbbbbbb",
        sha256="B" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(+30),
        sent_at="2026-08-01T12:00:00-07:00",
    )
    since = datetime.datetime.fromisoformat("2026-07-01T00:00:00-07:00")
    results = list(iter_records(since=since))
    assert len(results) == 1
    assert results[0].sha256 == "B" * 64


def test_iter_records_chat_id_filter(isolated_cache_dir: Path) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260701-120000-aaaaaaaa",
        sha256="A" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(+30),
        chat_id=111,
    )
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260701-120001-bbbbbbbb",
        sha256="B" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(+30),
        chat_id=222,
    )
    results = list(iter_records(chat_id=222))
    assert len(results) == 1
    assert results[0].sha256 == "B" * 64
    # String vs int coercion.
    results_str = list(iter_records(chat_id="222"))
    assert len(results_str) == 1


def test_search_records_case_insensitive_caption(isolated_cache_dir: Path) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260701-120000-aaaaaaaa",
        sha256="A" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(+30),
        caption="River First Smile",
    )
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260701-120001-bbbbbbbb",
        sha256="B" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(+30),
        caption="Juno sleeping",
    )
    hits = search_records("RIVER")
    assert len(hits) == 1
    assert hits[0].sha256 == "A" * 64


def test_search_records_matches_label_too(isolated_cache_dir: Path) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260701-120000-aaaaaaaa",
        sha256="A" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(+30),
        caption="",
        label="river-milestone",
    )
    hits = search_records("MILE")
    assert len(hits) == 1


# ==========================================================================
# prune_expired
# ==========================================================================


def test_prune_dry_run_does_not_trash(
    isolated_cache_dir: Path, no_real_trash: Path
) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20250101-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(-1),
    )
    report = prune_expired(dry_run=True)
    assert report.dry_run is True
    assert len(report.expired_records) == 1
    # Shim log stays empty on dry-run.
    assert _read_trash_log(no_real_trash) == []
    # Files still on disk.
    assert (isolated_cache_dir / "20250101-120000-aaaaaaaa.json").exists()
    assert (isolated_cache_dir / "20250101-120000-aaaaaaaa.jpg").exists()


def test_prune_real_run_trashes_both_pair_files(
    isolated_cache_dir: Path, no_real_trash: Path
) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20250101-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(-1),
    )
    report = prune_expired(dry_run=False)
    assert report.dry_run is False
    assert len(report.expired_records) == 1
    calls = _read_trash_log(no_real_trash)
    # Exactly one trash invocation (both files bundled in one argv), or two —
    # depending on the internals. Either way, both files must be listed.
    all_argv = [tok for call in calls for tok in call]
    assert str(isolated_cache_dir / "20250101-120000-aaaaaaaa.json") in all_argv
    assert str(isolated_cache_dir / "20250101-120000-aaaaaaaa.jpg") in all_argv


def test_prune_preserves_forever_entries(
    isolated_cache_dir: Path, no_real_trash: Path
) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20200101-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="F",
        expires_at=None,  # forever
    )
    report = prune_expired(dry_run=False)
    assert report.kept_forever == 1
    assert report.expired_records == []
    assert _read_trash_log(no_real_trash) == []


def test_prune_preserves_unexpired_entries(
    isolated_cache_dir: Path, no_real_trash: Path
) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20260801-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(+30),
    )
    report = prune_expired(dry_run=False)
    assert report.kept_unexpired == 1
    assert report.expired_records == []
    assert _read_trash_log(no_real_trash) == []


def test_prune_raises_when_trash_binary_missing(
    isolated_cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20250101-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(-1),
    )
    # Point the trash env at a non-existent path.
    monkeypatch.setenv(TRASH_BINARY_ENV, "/nonexistent/trash-does-not-exist-xyz")
    with pytest.raises(SentImageCacheError) as excinfo:
        prune_expired(dry_run=False)
    assert "trash" in str(excinfo.value).lower()


def test_prune_missing_cache_dir_is_noop(
    isolated_cache_dir: Path, no_real_trash: Path
) -> None:
    """A fresh install (cache dir doesn't exist yet) prunes 0 entries."""
    assert not isolated_cache_dir.exists()
    report = prune_expired(dry_run=False)
    assert report.expired_records == []
    assert report.kept_forever == 0
    assert report.kept_unexpired == 0
    assert report.failed_records == []
    assert _read_trash_log(no_real_trash) == []


def test_prune_surfaces_trash_failure_per_record(
    isolated_cache_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `trash` CLI that exits non-zero MUST show up as a failed record.

    Regression guard for the fail-quiet bug where `_trash_pair` used
    `check=False` and the caller assumed every candidate was trashed.
    We wire the env override at a shim that always exits 1; the record
    lands in `failed_records`, NOT in `expired_records`, and the ledger
    file survives so the operator can retry after fixing the shim.
    """
    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20250101-120000-aaaaaaaa",
        sha256="X" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(-1),
    )
    # A trash shim that always fails (writes a stderr line + exits 1).
    failing_shim = tmp_path / "trash-fail.sh"
    failing_shim.write_text("#!/bin/sh\necho 'permission denied' >&2\nexit 1\n")
    os.chmod(failing_shim, 0o755)
    monkeypatch.setenv(TRASH_BINARY_ENV, str(failing_shim))

    report = prune_expired(dry_run=False)
    # Failure surfaces per-record, does NOT masquerade as success.
    assert report.expired_records == []
    assert len(report.failed_records) == 1
    failed_record, err_msg = report.failed_records[0]
    assert failed_record.stem == "20250101-120000-aaaaaaaa"
    assert "exited 1" in err_msg
    # Ledger files still on disk (nothing was trashed).
    assert (isolated_cache_dir / "20250101-120000-aaaaaaaa.json").exists()
    assert (isolated_cache_dir / "20250101-120000-aaaaaaaa.jpg").exists()


def test_prune_verb_exits_nonzero_when_trash_fails(
    isolated_cache_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mineru telegram photos prune` must exit non-zero on partial failure.

    A failing prune that reported exit 0 would let a cron continue as
    if the cache was clean. The verb surfaces the failed records on
    stderr and exits 2.
    """
    from typer.testing import CliRunner
    from mineru_cli.app import app

    _commit_synthetic(
        isolated_cache_dir.resolve(),
        stem="20250101-120000-bbbbbbbb",
        sha256="Y" * 64,
        telegram_file_id="F",
        expires_at=_now_offset_str(-1),
    )
    failing_shim = tmp_path / "trash-fail.sh"
    failing_shim.write_text("#!/bin/sh\nexit 1\n")
    os.chmod(failing_shim, 0o755)
    monkeypatch.setenv(TRASH_BINARY_ENV, str(failing_shim))

    runner = CliRunner()
    result = runner.invoke(app, ["telegram", "photos", "prune"])
    assert result.exit_code == 2
    # `result.output` carries the combined stdout+stderr regardless of
    # the CliRunner's mix_stderr default (Click >= 8.2 removed the flag).
    assert "FAILED to trash" in (result.output or "")


# ==========================================================================
# Static invariants against the module source
# ==========================================================================


import mineru_cli.wrappers.telegram_image_cache as _tic_source_module

MODULE_SRC = Path(_tic_source_module.__file__).read_text(encoding="utf-8")


def _find_rm_syscall_calls(source: str) -> list[str]:
    """AST-walk `source` and collect every `.unlink()` / `os.remove()` call.

    A prior version of this test greps for the literal strings
    `Path.unlink` / `os.remove`, but the ACTUAL call sites are
    `target.unlink()` and `os.remove(...)` — neither shows up as its
    fully-qualified name in code, so the substring grep never caught the
    real thing. Walk the AST instead: look for any `Call` whose func is
    `<anything>.unlink` or `os.remove`. Returns a list of
    "file:line:snippet" strings so a failure names the exact call site.
    """
    tree = ast.parse(source)
    src_lines = source.splitlines()
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        attr = func.attr
        # `<anything>.unlink()` — matches `target.unlink()`,
        # `sidecar_path.unlink()`, `Path(...).unlink()`, etc.
        if attr == "unlink":
            snippet = src_lines[node.lineno - 1].strip() if node.lineno <= len(src_lines) else "<n/a>"
            hits.append(f"line {node.lineno}: {snippet}")
            continue
        # `os.remove(...)` specifically — `shutil.remove` doesn't exist,
        # so this is the only remove call worth catching.
        if attr == "remove":
            value = func.value
            if isinstance(value, ast.Name) and value.id == "os":
                snippet = src_lines[node.lineno - 1].strip() if node.lineno <= len(src_lines) else "<n/a>"
                hits.append(f"line {node.lineno}: {snippet}")
    return hits


def test_module_never_uses_rm_for_destructive_action() -> None:
    """SOUL.md hard rule: `rm` is banned; every destructive path uses `trash`.

    Enforced via an AST walk (not substring grep) so a real
    `target.unlink()` call at some `<expr>.unlink()` site actually gets
    caught. The prior tautological version asserted
    `MODULE_SRC.count("Path.unlink") == 0` while a `target.unlink()` sat
    at line 501 of the source — the grep passed because nobody writes
    `Path.unlink` as a bare string in method-call code.
    """
    calls = _find_rm_syscall_calls(MODULE_SRC)
    assert calls == [], (
        "telegram_image_cache.py must NEVER call `.unlink()` or `os.remove()` "
        "(SOUL.md hard rule: use the `trash` CLI, never `rm`). Offenders:\n  - "
        + "\n  - ".join(calls)
    )
    # `subprocess.run(["rm", ...])` is the direct-shell-rm form the rule
    # also forbids. Catch a hardcoded `["rm"` prefix — rare in practice but
    # trivial to guard.
    assert 'subprocess.run(["rm"' not in MODULE_SRC
    assert 'subprocess.run(["rm ' not in MODULE_SRC
    # The trash binary reference IS required.
    assert "DEFAULT_TRASH_BINARY" in MODULE_SRC
    assert "trash" in MODULE_SRC


def test_load_record_logs_warning_on_invalid_json(
    isolated_cache_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt sidecar must surface a WARNING — silent drop hides the ledger degrading.

    Before this guard, a half-written JSON blob or hand-edited invalid
    sidecar disappeared from `iter_records` / `lookup_by_sha256` /
    `prune_expired` with no operator-visible signal. Regression guard:
    the module's `logger` MUST fire on the malformed-JSON path.
    """
    ensure_cache_dir(isolated_cache_dir)
    bad_sidecar = isolated_cache_dir.resolve() / "20260701-120000-aaaaaaaa.json"
    bad_sidecar.write_text("{not valid json,,,")
    caplog.set_level("WARNING", logger="mineru_cli.wrappers.telegram_image_cache")
    result = list(iter_records())
    assert result == []
    assert any(
        "malformed sidecar" in record.message and "invalid JSON" in record.message
        for record in caplog.records
    ), f"expected WARNING on malformed sidecar, got: {[r.message for r in caplog.records]!r}"


def test_load_record_logs_warning_on_non_dict_top_level(
    isolated_cache_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Well-formed JSON whose top level isn't a dict (e.g. a list) must warn too."""
    ensure_cache_dir(isolated_cache_dir)
    bad_sidecar = isolated_cache_dir.resolve() / "20260701-120000-bbbbbbbb.json"
    bad_sidecar.write_text('["not", "a", "dict"]')
    caplog.set_level("WARNING", logger="mineru_cli.wrappers.telegram_image_cache")
    assert list(iter_records()) == []
    assert any(
        "malformed sidecar" in record.message and "expected dict" in record.message
        for record in caplog.records
    )


def test_write_bytes_600_uses_trash_on_partial_write(
    isolated_cache_dir: Path,
    no_real_trash: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-write failure must route the partial file through `trash`, not `unlink`.

    Regression guard for Finding 4: `_write_bytes_600` previously called
    `target.unlink()` (banned by SOUL.md) and swallowed OSError silently.
    The new implementation routes the partial through the trash CLI and
    logs any cleanup failure. We simulate a mid-write failure by making
    `os.fdopen(...).write` raise, then verify the trash shim recorded
    the partial file's path.
    """
    ensure_cache_dir(isolated_cache_dir)
    target = isolated_cache_dir.resolve() / "partial.bin"

    # Force write() to raise. `_write_bytes_600` has already created the
    # empty file at 0600 by the time we get here, so the cleanup path
    # will observe it on disk.
    real_fdopen = os.fdopen

    class RaisingFile:
        def __init__(self, real_handle):
            self._real = real_handle

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._real.close()
            return False

        def write(self, payload):
            raise OSError("simulated disk full")

    def fake_fdopen(fd, mode):
        return RaisingFile(real_fdopen(fd, mode))

    monkeypatch.setattr(tic.os, "fdopen", fake_fdopen)

    with pytest.raises(OSError, match="simulated disk full"):
        tic._write_bytes_600(target, b"never lands")

    # Trash shim recorded the partial file.
    calls = _read_trash_log(no_real_trash)
    all_argv = [tok for call in calls for tok in call]
    assert str(target) in all_argv, f"expected {target!s} to be trashed, got {all_argv!r}"


def test_write_bytes_600_logs_when_trash_binary_missing(
    isolated_cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Partial-write cleanup logs at WARNING when the trash binary is unavailable.

    The old code did `except OSError: pass` — invisible failures. New
    behavior: cleanup goes through `trash`; if trash is missing, we log
    a warning naming the orphaned partial file. The write failure itself
    still propagates (the caller cares about the write error, not the
    cleanup one).
    """
    ensure_cache_dir(isolated_cache_dir)
    target = isolated_cache_dir.resolve() / "partial.bin"
    monkeypatch.setenv(TRASH_BINARY_ENV, "/nonexistent/trash-missing-xyz")

    real_fdopen = os.fdopen

    class RaisingFile:
        def __init__(self, real_handle):
            self._real = real_handle

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._real.close()
            return False

        def write(self, payload):
            raise OSError("simulated write failure")

    def fake_fdopen(fd, mode):
        return RaisingFile(real_fdopen(fd, mode))

    monkeypatch.setattr(tic.os, "fdopen", fake_fdopen)
    caplog.set_level("WARNING", logger="mineru_cli.wrappers.telegram_image_cache")

    with pytest.raises(OSError, match="simulated write failure"):
        tic._write_bytes_600(target, b"never lands")

    assert any(
        "trash binary" in record.message and "not found" in record.message
        for record in caplog.records
    ), f"expected WARNING when trash unavailable, got: {[r.message for r in caplog.records]!r}"


def test_module_default_env_names_are_stable() -> None:
    """The env override name is part of the public test contract."""
    assert MINERU_SENT_IMAGE_DIR_ENV == "MINERU_TELEGRAM_SENT_IMAGE_DIR"
    assert TRASH_BINARY_ENV == "MINERU_TRASH_BINARY"


# ==========================================================================
# cleanup-retention.sh integration: rule #5
# ==========================================================================


CLEANUP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cleanup-retention.sh"


def _run_cleanup_script(
    *,
    workspace_dir: Path,
    trash_shim: Path,
    dry_run: bool,
) -> subprocess.CompletedProcess:
    """Run cleanup-retention.sh with HOME pointing at a synthetic workspace.

    The script `cd`s to `${MINERU_HOME:-$HOME/.mineru}`, so we build that layout under
    `workspace_dir` and set `HOME=workspace_dir`. `PATH` is prepended
    with a dir containing a `trash` shim so `run trash ...` in the
    script routes through our recorder rather than `/usr/bin/trash`.
    """
    # Layout: workspace_dir/.mineru/{cache,logs,briefs_...}
    fake_home = workspace_dir
    mineru = fake_home / ".mineru"
    mineru.mkdir(parents=True, exist_ok=True)
    # cleanup-retention.sh requires logs/ to exist for the find call
    # (find handles the missing case gracefully via `2>/dev/null`, but
    # be explicit).
    (mineru / "logs").mkdir(exist_ok=True)

    # `trash` shim on PATH — the script calls `trash "$f"`, so the name
    # `trash` must resolve on PATH.
    path_dir = workspace_dir / "shim-path"
    path_dir.mkdir(exist_ok=True)
    trash_alias = path_dir / "trash"
    trash_alias.symlink_to(trash_shim)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    # The genericized cleanup script honors ${MINERU_HOME:-${MINERU_HOME:-$HOME/.mineru}};
    # point it at the synthetic workspace so it never touches a real one.
    env["MINERU_HOME"] = str(mineru)
    env["PATH"] = f"{path_dir}:{env.get('PATH', '')}"

    cmd = ["bash", str(CLEANUP_SCRIPT)]
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)


def test_cleanup_script_dry_run_fresh_cache_exit_0(
    tmp_path: Path, no_real_trash: Path
) -> None:
    """--dry-run on a completely empty workspace exits 0, no trash calls."""
    result = _run_cleanup_script(
        workspace_dir=tmp_path / "wksp",
        trash_shim=Path(os.environ[TRASH_BINARY_ENV]),
        dry_run=True,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"
    # Trash shim was never called.
    assert _read_trash_log(no_real_trash) == []


def test_cleanup_script_trashes_expired_sent_image_pair(
    tmp_path: Path, no_real_trash: Path
) -> None:
    """Non-dry-run trashes both files of an expired sent-image pair."""
    wksp = tmp_path / "wksp"
    sent_dir = wksp / ".mineru" / "cache" / "telegram_sent_images"
    sent_dir.mkdir(parents=True)
    # Expired sidecar.
    stem = "20250101-120000-aaaaaaaa"
    (sent_dir / f"{stem}.jpg").write_bytes(b"expired bytes")
    (sent_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "sent_at": "2025-01-01T12:00:00-07:00",
                "chat_id": 1,
                "caption": "",
                "source_path": "/tmp/x.jpg",
                "sha256": "a" * 64,
                "mime": "image/jpeg",
                "telegram_file_id": "F",
                "message_id": 1,
                "retention_days": 60,
                "expires_at": _now_offset_str(-1),
                "label": "old",
            }
        )
    )

    result = _run_cleanup_script(
        workspace_dir=wksp,
        trash_shim=Path(os.environ[TRASH_BINARY_ENV]),
        dry_run=False,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"

    # The shell script `cd`s into ${MINERU_HOME:-$HOME/.mineru}, so it invokes trash with
    # workspace-relative paths (e.g. `cache/telegram_sent_images/...`).
    # We assert on the trailing tail of the path to stay independent of
    # the wksp prefix.
    calls = _read_trash_log(no_real_trash)
    all_argv = [tok for call in calls for tok in call]
    relative_jpg = f"cache/telegram_sent_images/{stem}.jpg"
    relative_json = f"cache/telegram_sent_images/{stem}.json"
    assert relative_jpg in all_argv, f"expected {relative_jpg!r} in {all_argv!r}"
    assert relative_json in all_argv, f"expected {relative_json!r} in {all_argv!r}"


def test_cleanup_script_preserves_forever_and_unexpired(
    tmp_path: Path, no_real_trash: Path
) -> None:
    """A 'forever' entry AND a still-valid entry are NEVER trashed by the shell rule."""
    wksp = tmp_path / "wksp"
    sent_dir = wksp / ".mineru" / "cache" / "telegram_sent_images"
    sent_dir.mkdir(parents=True)

    # Forever entry.
    stem_f = "20200101-120000-ffffffff"
    (sent_dir / f"{stem_f}.jpg").write_bytes(b"forever")
    (sent_dir / f"{stem_f}.json").write_text(
        json.dumps(
            {
                "sent_at": "2020-01-01T12:00:00-07:00",
                "chat_id": 1,
                "caption": "",
                "source_path": "/tmp/f.jpg",
                "sha256": "f" * 64,
                "mime": "image/jpeg",
                "telegram_file_id": "F",
                "message_id": 1,
                "retention_days": "forever",
                "expires_at": None,
                "label": "keep-forever",
            }
        )
    )
    # Unexpired entry.
    stem_u = "20260801-120000-bbbbbbbb"
    (sent_dir / f"{stem_u}.jpg").write_bytes(b"recent")
    (sent_dir / f"{stem_u}.json").write_text(
        json.dumps(
            {
                "sent_at": "2026-08-01T12:00:00-07:00",
                "chat_id": 1,
                "caption": "",
                "source_path": "/tmp/r.jpg",
                "sha256": "b" * 64,
                "mime": "image/jpeg",
                "telegram_file_id": "F",
                "message_id": 1,
                "retention_days": 60,
                "expires_at": _now_offset_str(+30),
                "label": "recent",
            }
        )
    )

    result = _run_cleanup_script(
        workspace_dir=wksp,
        trash_shim=Path(os.environ[TRASH_BINARY_ENV]),
        dry_run=False,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    # Neither file (in any path form the script might use) should have
    # been fed to `trash`.
    all_argv = [tok for call in _read_trash_log(no_real_trash) for tok in call]
    for stem in (stem_f, stem_u):
        rel_jpg = f"cache/telegram_sent_images/{stem}.jpg"
        rel_json = f"cache/telegram_sent_images/{stem}.json"
        assert str(sent_dir / f"{stem}.jpg") not in all_argv
        assert str(sent_dir / f"{stem}.json") not in all_argv
        assert rel_jpg not in all_argv
        assert rel_json not in all_argv
    # Files still on disk.
    assert (sent_dir / f"{stem_f}.json").exists()
    assert (sent_dir / f"{stem_u}.json").exists()


def test_cleanup_script_dry_run_prints_but_does_not_call_trash(
    tmp_path: Path, no_real_trash: Path
) -> None:
    """--dry-run on an expired pair prints intent, calls no trash."""
    wksp = tmp_path / "wksp"
    sent_dir = wksp / ".mineru" / "cache" / "telegram_sent_images"
    sent_dir.mkdir(parents=True)
    stem = "20250101-120000-aaaaaaaa"
    (sent_dir / f"{stem}.jpg").write_bytes(b"x")
    (sent_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "sent_at": "2025-01-01T12:00:00-07:00",
                "chat_id": 1,
                "caption": "",
                "source_path": "/tmp/x.jpg",
                "sha256": "a" * 64,
                "mime": "image/jpeg",
                "telegram_file_id": "F",
                "message_id": 1,
                "retention_days": 60,
                "expires_at": _now_offset_str(-1),
                "label": "old",
            }
        )
    )
    result = _run_cleanup_script(
        workspace_dir=wksp,
        trash_shim=Path(os.environ[TRASH_BINARY_ENV]),
        dry_run=True,
    )
    assert result.returncode == 0
    assert _read_trash_log(no_real_trash) == []
    # Files remain on disk.
    assert (sent_dir / f"{stem}.jpg").exists()
    assert (sent_dir / f"{stem}.json").exists()
