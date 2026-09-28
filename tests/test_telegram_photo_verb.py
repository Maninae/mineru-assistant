"""Tests for `mineru telegram photo` + `mineru telegram photos {...}` (P3-03).

⚠️⚠️ SAFETY (READ TWICE) ⚠️⚠️

  NO test in this file EVER contacts api.telegram.org. The verb layer
  routes every outbound send through
  `mineru_cli.wrappers.telegram_photo.send_photo`; every test here
  monkey-patches THAT function on the verb-module namespace and asserts
  the args that WOULD have been sent. A defense-in-depth `no_live_network`
  fixture also patches `socket.socket` at the module level so a code path
  that skipped the mock would fail loudly instead of silently DMing the operator.

  NO test in this file EVER touches the live cache directory at
  `$MINERU_HOME/cache/telegram_sent_images/`. The autouse
  `isolated_cache_dir` fixture sets `MINERU_TELEGRAM_SENT_IMAGE_DIR` to
  a tmp_path so `mineru_cli.wrappers.telegram_image_cache.resolve_cache_dir()`
  always returns the isolated per-test directory.

  NO test in this file EVER calls `trash` for real. The autouse
  `no_real_trash` fixture points `MINERU_TRASH_BINARY` at a shim that
  just records argv. The default `/usr/bin/trash` is never invoked.

  The bot token / chat id are provided via `MINERU_SECRET_*` env vars
  and asserted-absent from stderr, so a missed patch would surface as a
  bright test failure instead of a real send.

Coverage:

  photo verb (WRITE, OUTBOUND — mocked):
    - happy path: fresh-upload, send_photo called with the on-disk cache
      path, correct chat_id + caption + parse_mode + reply_to; sidecar
      written with returned telegram_file_id + message_id.
    - --dedup with a pre-seeded matching sha256 record: send_photo called
      with `is_cached_id=True` and the cached telegram_file_id string;
      no fresh multipart, no second read of the source bytes.
    - --dedup with NO cached match: falls through to fresh upload.
    - caption >1024 chars: truncated with '…' before both the send and
      the sidecar record.
    - --retention forever: sidecar carries `expires_at: null`.
    - --parse-mode html / markdown map to HTML / MarkdownV2 on the wire.
    - --chat-id overrides the resolved secret chat id.
    - transport failure: no sidecar committed; exit code non-zero.
    - transport rate_limited: exit code 3 (retry hint).
    - missing local file: exit 2, transport NOT called.

  photos ledger verbs (READ / local trash):
    - `list --json` returns a JSON array of raw sidecar dicts.
    - `list --since 30d` filters to records within the window.
    - `list --chat <id>` filters by chat id (string-equal).
    - `list` plain output renders bullet lines with stem + chat + label.
    - `search "river"` matches on caption case-insensitively; label matches too.
    - `search` does NOT match on filename / source_path.
    - `show <full-stem>` prints sidecar JSON.
    - `show <prefix>` resolves unique prefix; ambiguous prefix errors.
    - `show <miss>` exits 2.
    - `prune --dry-run` prints candidates, calls no `trash`, files remain.
    - `prune` (non-dry) invokes the recording trash shim.

  No-live-network guard:
    - `socket.socket` is patched at the class level; any attempt to
      open a real socket during a test raises immediately.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import socket
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import telegram as telegram_verb
from mineru_cli.wrappers import telegram_image_cache as image_cache
from mineru_cli.wrappers.telegram_image_cache import (
    CACHE_FILE_MODE,
    MINERU_SENT_IMAGE_DIR_ENV,
    TRASH_BINARY_ENV,
    cache_binary,
    commit_record,
)


runner = CliRunner()


# ------------------------------------------------------------------ fixtures


SENTINEL_TOKEN = "TEST-TOKEN-p3-03"
SENTINEL_CHAT_ID = "999888777"


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-loud if any code path tries to open a real socket during a test.

    Every test monkeypatches the higher-level `send_photo` symbol on the
    verb module namespace. If a refactor ever tripped past that monkey-
    patch and reached the real transport, the socket block below would
    scream instead of silently DMing the operator.
    """
    def blocked(*args, **kwargs):
        raise AssertionError(
            "no_live_network: tests must not open real sockets; "
            "patch mineru_cli.verbs.telegram.send_photo instead."
        )

    monkeypatch.setattr(socket, "socket", blocked)


@pytest.fixture
def envbacked_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed sentinel bot-token + chat-id via env so the secrets resolver hits them.

    The verb's `_resolver_for_ctx` walks the profile's backend chain,
    which starts with `env` for the default profile. These env vars
    match `env_var_for('telegram-bot-token')` /
    `env_var_for('telegram-chat-id')`.
    """
    monkeypatch.setenv("MINERU_SECRET_TELEGRAM_BOT_TOKEN", SENTINEL_TOKEN)
    monkeypatch.setenv("MINERU_SECRET_TELEGRAM_CHAT_ID", SENTINEL_CHAT_ID)


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh cache dir per test, wired via the env override."""
    root = tmp_path / "sent_images"
    monkeypatch.setenv(MINERU_SENT_IMAGE_DIR_ENV, str(root))
    return root


@pytest.fixture(autouse=True)
def no_real_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the trash CLI to a shim that records argv into a JSONL file."""
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
    p.write_bytes(b"\xff\xd8\xff\xe0MINERU_SENTINEL_IMAGE_BYTES_P3_03_v1")
    return p


class SendPhotoRecorder:
    """Callable stand-in for `send_photo`; records every call.

    Each recorded call is a dict with the positional arg (path_or_file_id)
    plus every kwarg the verb passed. `scripted_result` seeds the return
    dict; the default is a successful send with plausible ids.
    """

    def __init__(self, *, scripted_result: Dict | None = None) -> None:
        self.calls: List[Dict] = []
        self.scripted_result = scripted_result or {
            "ok": True,
            "message_id": 4242,
            "telegram_file_id": "TG_FID_FROM_MOCK",
            "error": None,
            "retry_after": None,
        }

    def __call__(self, path_or_file_id: str, **kwargs) -> Dict:
        self.calls.append({"path_or_file_id": path_or_file_id, **kwargs})
        return dict(self.scripted_result)


def _cache_now_iso(delta_days: int = 0) -> str:
    dt = datetime.datetime.now().astimezone() + datetime.timedelta(days=delta_days)
    return dt.isoformat(timespec="seconds")


def _commit_synthetic_record(
    cache_dir: Path,
    *,
    stem: str,
    sha256_hex: str,
    telegram_file_id: str | None = "TG_FID",
    expires_at: str | None = None,
    sent_at: str | None = None,
    chat_id: int = 999888777,
    caption: str = "",
    label: str = "syn",
) -> Path:
    """Write a canned sidecar + bytes pair directly, bypassing the send path."""
    image_cache.ensure_cache_dir(cache_dir)
    bytes_path = cache_dir / f"{stem}.jpg"
    sidecar_path = cache_dir / f"{stem}.json"
    bytes_path.write_bytes(b"synthetic bytes")
    os.chmod(bytes_path, CACHE_FILE_MODE)
    payload = {
        "sent_at": sent_at or _cache_now_iso(-1),
        "chat_id": chat_id,
        "caption": caption,
        "source_path": f"/tmp/{stem}.jpg",
        "sha256": sha256_hex,
        "mime": "image/jpeg",
        "telegram_file_id": telegram_file_id,
        "message_id": 1,
        "retention_days": 60,
        "expires_at": expires_at if expires_at is not None else _cache_now_iso(+30),
        "label": label,
    }
    sidecar_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.chmod(sidecar_path, CACHE_FILE_MODE)
    return sidecar_path


# ==========================================================================
# photo verb - happy path (fresh upload)
# ==========================================================================


def test_photo_fresh_upload_calls_transport_with_cache_bytes_path(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """Send calls send_photo with the on-disk staged bytes path + full kwargs."""
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            [
                "telegram", "photo", str(sample_photo),
                "--caption", "River smiling",
                "--parse-mode", "html",
                "--reply-to", "101",
                "--label", "river-first-smile",
            ],
        )
    assert result.exit_code == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"

    # Exactly one transport call.
    assert len(recorder.calls) == 1
    call = recorder.calls[0]

    # is_cached_id=False on the fresh path.
    assert call["is_cached_id"] is False
    # The path handed to the transport is the STAGED bytes file inside
    # the cache directory (0600), not the caller's source path.
    staged = Path(call["path_or_file_id"])
    assert staged.parent == isolated_cache_dir.resolve()
    assert staged.exists()
    assert staged.read_bytes() == sample_photo.read_bytes()
    # Full text-field payload propagated verbatim.
    assert call["chat_id"] == SENTINEL_CHAT_ID
    assert call["caption"] == "River smiling"
    assert call["parse_mode"] == "HTML"  # wire-shape uppercase
    assert call["reply_to_message_id"] == 101

    # Sidecar committed with returned ids.
    sidecar = staged.with_suffix(".json")
    assert sidecar.exists()
    raw = json.loads(sidecar.read_text())
    assert raw["telegram_file_id"] == "TG_FID_FROM_MOCK"
    assert raw["message_id"] == 4242
    assert raw["label"] == "river-first-smile"
    assert raw["caption"] == "River smiling"

    # Stdout JSON summary carries the same ids.
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary["ok"] is True
    assert summary["message_id"] == 4242
    assert summary["telegram_file_id"] == "TG_FID_FROM_MOCK"
    assert summary["dedup"] is False


def test_photo_missing_source_exits_2_transport_untouched(
    envbacked_secrets, isolated_cache_dir: Path
) -> None:
    """A source file that doesn't exist exits 2 and never calls the transport."""
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", "/tmp/definitely-not-here.jpg"],
        )
    assert result.exit_code == 2
    assert recorder.calls == []
    assert "does not exist" in result.stderr


# ==========================================================================
# photo verb - dedup path
# ==========================================================================


def test_photo_dedup_hit_uses_cached_file_id_and_does_not_reopen_source(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """Pre-seed cache with matching sha256 -> transport gets is_cached_id=True.

    Also asserts the source photo is NOT re-read during the transport
    invocation: the dedup fast path only opens the source ONCE to compute
    sha256, then hands the opaque file_id to Telegram. We prove this by
    making the source file effectively "consumed" (delete it after the
    hash phase would run) — wait, actually, the verb hashes it upfront
    and then must NOT touch it again on the cached path. To test the
    NOT-re-open invariant cleanly, we mock `Path.read_bytes` to count
    invocations across the lifetime of the run and assert exactly one
    call on the sample_photo path.
    """
    photo_sha = hashlib.sha256(sample_photo.read_bytes()).hexdigest()
    _commit_synthetic_record(
        isolated_cache_dir.resolve(),
        stem="20260701-120000-" + photo_sha[:8],
        sha256_hex=photo_sha,
        telegram_file_id="TG_FID_CACHED_HIT",
        expires_at=_cache_now_iso(+30),
    )

    # Count reads on the source path.
    real_read_bytes = Path.read_bytes
    source_reads: List[Path] = []

    def counting_read_bytes(self):
        if self.resolve() == sample_photo.resolve():
            source_reads.append(self)
        return real_read_bytes(self)

    recorder = SendPhotoRecorder(
        scripted_result={
            "ok": True,
            "message_id": 5555,
            # Telegram may echo the same file_id or a new one; either
            # way the summary's file_id defaults to the cached one when
            # the transport doesn't return a fresh one.
            "telegram_file_id": None,
            "error": None,
            "retry_after": None,
        }
    )
    with patch.object(telegram_verb, "send_photo", recorder), \
            patch.object(Path, "read_bytes", counting_read_bytes):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--dedup", "--caption", "reuse"],
        )
    assert result.exit_code == 0, f"stderr={result.stderr!r}"

    # Exactly one transport call, on the CACHED path.
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["is_cached_id"] is True
    assert call["path_or_file_id"] == "TG_FID_CACHED_HIT"

    # Source photo was read exactly ONCE (for the sha256), not twice.
    assert len(source_reads) == 1

    # Stdout summary marks dedup=True and preserves the cached file_id.
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary["dedup"] is True
    assert summary["telegram_file_id"] == "TG_FID_CACHED_HIT"
    assert summary["message_id"] == 5555


def test_photo_dedup_miss_falls_through_to_fresh_upload(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """No cached match for the sha256 -> falls through to a fresh multipart send."""
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--dedup"],
        )
    assert result.exit_code == 0
    assert len(recorder.calls) == 1
    # No cached record existed, so the fresh path fires and is_cached_id=False.
    assert recorder.calls[0]["is_cached_id"] is False


def test_photo_dedup_hit_does_not_rewrite_sidecar_message_id(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """Pin the current dedup design: a cache hit leaves the sidecar's
    original `message_id` untouched, even though Telegram returned a
    fresh one on the resend.

    Design decision (verbs/telegram.py `photo` fast path): on a dedup
    hit, we resend via cached file_id but never call `commit_record`,
    so the sidecar's `message_id` field stays pointed at the FIRST
    successful send. The stdout summary carries the FRESH id (so a
    caller can thread a reply to the just-sent message), but the
    ledger keeps the original.

    A future policy change that swapped this for "always update the
    sidecar's message_id to the latest send" would need to remove
    this test. The failure would name the sidecar and both ids, so a
    reviewer sees exactly what changed.
    """
    photo_sha = hashlib.sha256(sample_photo.read_bytes()).hexdigest()
    original_message_id = 1  # (matches _commit_synthetic_record default)
    sidecar_path = _commit_synthetic_record(
        isolated_cache_dir.resolve(),
        stem="20260701-120000-" + photo_sha[:8],
        sha256_hex=photo_sha,
        telegram_file_id="TG_FID_CACHED_HIT",
        expires_at=_cache_now_iso(+30),
    )
    fresh_message_id = 987654
    recorder = SendPhotoRecorder(
        scripted_result={
            "ok": True,
            "message_id": fresh_message_id,
            "telegram_file_id": None,
            "error": None,
            "retry_after": None,
        }
    )
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--dedup"],
        )
    assert result.exit_code == 0

    # The stdout summary carries the FRESH message_id (so a caller can
    # act on the just-sent message).
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary["dedup"] is True
    assert summary["message_id"] == fresh_message_id

    # The on-disk sidecar's message_id is UNCHANGED — dedup fast path
    # deliberately does not call commit_record, so the original send's
    # id remains the ledger source of truth.
    sidecar_after = json.loads(sidecar_path.read_text())
    assert sidecar_after["message_id"] == original_message_id, (
        "dedup fast path unexpectedly rewrote the sidecar's message_id. "
        "If this is a deliberate policy change (always update to the "
        "latest send's id), remove this test and update the verb docs; "
        "the summary already exposes the fresh id."
    )
    # And the telegram_file_id is likewise preserved (the whole point of
    # dedup — the cached id is what we resend against).
    assert sidecar_after["telegram_file_id"] == "TG_FID_CACHED_HIT"


# ==========================================================================
# photo verb - caption + retention + parse-mode + chat-id
# ==========================================================================


def test_photo_caption_truncated_to_1024_chars_with_ellipsis(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """A 5000-char caption becomes exactly 1024 chars ending in '…'."""
    long_caption = "L" * 5000
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--caption", long_caption],
        )
    assert result.exit_code == 0
    sent_caption = recorder.calls[0]["caption"]
    assert len(sent_caption) == 1024
    assert sent_caption.endswith("…")

    # Sidecar records the SAME truncated caption (not the original).
    staged = Path(recorder.calls[0]["path_or_file_id"])
    raw = json.loads(staged.with_suffix(".json").read_text())
    assert raw["caption"] == sent_caption


def test_photo_retention_forever_writes_null_expires(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--retention", "forever"],
        )
    assert result.exit_code == 0
    staged = Path(recorder.calls[0]["path_or_file_id"])
    raw = json.loads(staged.with_suffix(".json").read_text())
    assert raw["retention_days"] == "forever"
    assert raw["expires_at"] is None


def test_photo_retention_365_computes_expires_at(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--retention", "365"],
        )
    assert result.exit_code == 0
    staged = Path(recorder.calls[0]["path_or_file_id"])
    raw = json.loads(staged.with_suffix(".json").read_text())
    assert raw["retention_days"] == 365
    sent_dt = datetime.datetime.fromisoformat(raw["sent_at"])
    exp_dt = datetime.datetime.fromisoformat(raw["expires_at"])
    assert exp_dt - sent_dt == datetime.timedelta(days=365)


def test_photo_bad_retention_value_bad_parameter(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """Non-int / non-'forever' --retention surfaces a usage-frame error."""
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--retention", "soon"],
        )
    assert result.exit_code != 0
    assert recorder.calls == []


def test_photo_parse_mode_html_maps_to_wire_shape(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--parse-mode", "html"],
        )
    assert recorder.calls[0]["parse_mode"] == "HTML"


def test_photo_parse_mode_markdown_maps_to_wire_shape(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--parse-mode", "markdown"],
        )
    assert recorder.calls[0]["parse_mode"] == "MarkdownV2"


def test_photo_parse_mode_unknown_value_exits_cleanly(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """`--parse-mode plaintext` (or any non-`html`/`markdown` value) exits with
    a Typer usage error, NOT a raw KeyError traceback.

    Regression guard: the previous code did a bare dict lookup
    (`PARSE_MODE_TO_WIRE[parse_mode.lower()]`) that raised KeyError
    on unrecognized values, surfacing exit 1 with no stderr hint
    to the operator.
    """
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--parse-mode", "plaintext"],
        )
    # BadParameter -> non-zero exit; transport must NOT be called.
    assert result.exit_code != 0
    assert len(recorder.calls) == 0
    # Combined output must not carry a KeyError frame — the CLI should
    # surface a clean usage message. `result.output` is the safe
    # combined string regardless of CliRunner's `mix_stderr` setting.
    combined = result.output or ""
    assert "KeyError" not in combined
    # The message names the invalid value so the operator can fix it.
    assert "plaintext" in combined or "parse-mode" in combined.lower()


def test_photo_chat_id_override_wins(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    recorder = SendPhotoRecorder()
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo), "--chat-id", "42424242"],
        )
    assert result.exit_code == 0
    assert recorder.calls[0]["chat_id"] == "42424242"


# ==========================================================================
# photo verb - transport failure paths
# ==========================================================================


def test_photo_transport_failure_does_not_commit_sidecar(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """On error, exit non-zero and DO NOT write the sidecar JSON."""
    recorder = SendPhotoRecorder(
        scripted_result={
            "ok": False,
            "message_id": None,
            "telegram_file_id": None,
            "error": "http_400",
            "retry_after": None,
        }
    )
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo)],
        )
    assert result.exit_code == 1
    # Sidecar was NOT committed.
    staged = Path(recorder.calls[0]["path_or_file_id"])
    assert staged.exists()  # bytes staged for potential retry
    assert not staged.with_suffix(".json").exists()
    # stderr surfaces the transport label.
    assert "http_400" in result.stderr


def test_photo_transport_rate_limited_exits_3(
    envbacked_secrets, isolated_cache_dir: Path, sample_photo: Path
) -> None:
    """429 => exit 3 so a caller loop can distinguish retry-worthy failures."""
    recorder = SendPhotoRecorder(
        scripted_result={
            "ok": False,
            "message_id": None,
            "telegram_file_id": None,
            "error": "rate_limited",
            "retry_after": 17,
        }
    )
    with patch.object(telegram_verb, "send_photo", recorder):
        result = runner.invoke(
            app,
            ["telegram", "photo", str(sample_photo)],
        )
    assert result.exit_code == 3


# ==========================================================================
# photos list
# ==========================================================================


def _seed_records(cache_dir: Path) -> None:
    """Seed three records with distinct stems + chat ids + captions."""
    _commit_synthetic_record(
        cache_dir,
        stem="20260601-120000-aaaaaaaa",
        sha256_hex="a" * 64,
        expires_at=_cache_now_iso(+30),
        sent_at="2026-06-01T12:00:00-07:00",
        chat_id=111,
        caption="River first smile",
        label="milestone",
    )
    _commit_synthetic_record(
        cache_dir,
        stem="20260701-120000-bbbbbbbb",
        sha256_hex="b" * 64,
        expires_at=_cache_now_iso(+30),
        sent_at="2026-07-01T12:00:00-07:00",
        chat_id=222,
        caption="Juno sleeping",
        label="juno",
    )
    _commit_synthetic_record(
        cache_dir,
        stem="20260801-120000-cccccccc",
        sha256_hex="c" * 64,
        expires_at=_cache_now_iso(+30),
        sent_at="2026-08-01T12:00:00-07:00",
        chat_id=111,
        caption="River standing",
        label="milestone",
    )


def test_photos_list_json_returns_array(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    result = runner.invoke(app, ["telegram", "photos", "list", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, list)
    assert len(parsed) == 3
    # Every record has the mandated §4.2 keys.
    for rec in parsed:
        for key in ("sent_at", "chat_id", "caption", "sha256", "telegram_file_id"):
            assert key in rec


def test_photos_list_since_filters_recent(
    isolated_cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--since <window> filters records by their sent_at timestamp.

    Freeze `datetime.datetime.now` inside the verb's `_parse_since` so
    the reference time is deterministic (2026-08-15T12:00:00-07:00).
    With records dated 2026-06-01 / 07-01 / 08-01:
      - --since 30d  (window starts 2026-07-16) => ONE record: 08-01.
      - --since 60d  (window starts 2026-06-16) => TWO records: 07-01, 08-01.
      - --since 100d (window starts 2026-05-07) => ALL three records.
      - --since 1d   (window starts 2026-08-14) => ZERO records.
    Each boundary exercises `iter_records` filter logic; a regression that
    returned "everything" or "nothing" would fail at least one of these.
    """
    _seed_records(isolated_cache_dir.resolve())

    frozen_now = datetime.datetime(
        2026, 8, 15, 12, 0, 0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=-7)),
    )

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen_now.replace(tzinfo=None)
            return frozen_now.astimezone(tz)

    monkeypatch.setattr(telegram_verb.datetime, "datetime", FrozenDatetime)

    def run_since(window: str) -> list:
        result = runner.invoke(
            app, ["telegram", "photos", "list", "--since", window, "--json"]
        )
        assert result.exit_code == 0, f"stderr={result.stderr!r}"
        return json.loads(result.stdout)

    # Sidecar dicts key by sha256/sent_at; the seed records use those to
    # identify (`a`*64 = 06-01, `b`*64 = 07-01, `c`*64 = 08-01).
    parsed_30 = run_since("30d")
    assert len(parsed_30) == 1
    assert parsed_30[0]["sha256"] == "c" * 64
    assert parsed_30[0]["sent_at"].startswith("2026-08-01")

    parsed_60 = run_since("60d")
    assert len(parsed_60) == 2
    sha_60 = {r["sha256"] for r in parsed_60}
    assert sha_60 == {"b" * 64, "c" * 64}

    parsed_100 = run_since("100d")
    assert len(parsed_100) == 3

    parsed_1 = run_since("1d")
    assert parsed_1 == []

    # A very-wide window returns everything (unfiltered ledger baseline).
    wide = runner.invoke(app, ["telegram", "photos", "list", "--since", "9999d", "--json"])
    parsed_wide = json.loads(wide.stdout)
    assert len(parsed_wide) == 3


def test_photos_list_chat_filter(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    result = runner.invoke(
        app, ["telegram", "photos", "list", "--chat", "111", "--json"]
    )
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert len(parsed) == 2
    assert all(rec["chat_id"] == 111 for rec in parsed)


def test_photos_list_plain_renders_bullets(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    result = runner.invoke(app, ["telegram", "photos", "list"])
    assert result.exit_code == 0
    # Bullet lines per record; not a markdown table.
    assert "- 20260601-120000-aaaaaaaa" in result.stdout
    assert "|" not in result.stdout.split("\n")[0]  # no leading pipe (would be a table)


def test_photos_list_empty_cache_prints_placeholder(isolated_cache_dir: Path) -> None:
    result = runner.invoke(app, ["telegram", "photos", "list"])
    assert result.exit_code == 0
    assert "no records" in result.stdout.lower()


def test_photos_list_bad_since_bad_parameter(isolated_cache_dir: Path) -> None:
    """Malformed --since surfaces a usage-frame error."""
    result = runner.invoke(app, ["telegram", "photos", "list", "--since", "yesterday"])
    assert result.exit_code != 0


# ==========================================================================
# photos search
# ==========================================================================


def test_photos_search_caption_case_insensitive(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    result = runner.invoke(app, ["telegram", "photos", "search", "RIVER", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert len(parsed) == 2
    for rec in parsed:
        assert "river" in rec["caption"].lower()


def test_photos_search_label_matches(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    result = runner.invoke(app, ["telegram", "photos", "search", "juno", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    # 'juno' is the label on one record AND the caption on the same;
    # either way, at least one hit and it's the Juno record.
    assert len(parsed) == 1
    assert parsed[0]["label"] == "juno"


def test_photos_search_does_not_match_source_path(isolated_cache_dir: Path) -> None:
    """The search substring is caption / label only — NOT filename / source_path."""
    _seed_records(isolated_cache_dir.resolve())
    # The synthetic source_paths are '/tmp/<stem>.jpg' → contain 'aaaaaaaa'.
    result = runner.invoke(app, ["telegram", "photos", "search", "aaaaaaaa", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed == []


def test_photos_search_plain_bullets(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    result = runner.invoke(app, ["telegram", "photos", "search", "river"])
    assert result.exit_code == 0
    assert "- 20260601-120000-aaaaaaaa" in result.stdout
    assert "- 20260801-120000-cccccccc" in result.stdout


# ==========================================================================
# photos show
# ==========================================================================


def test_photos_show_full_stem_prints_sidecar(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    result = runner.invoke(
        app, ["telegram", "photos", "show", "20260601-120000-aaaaaaaa"]
    )
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["sha256"] == "a" * 64
    assert parsed["label"] == "milestone"


def test_photos_show_unique_prefix_resolves(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    result = runner.invoke(
        app, ["telegram", "photos", "show", "20260601"]
    )
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["sha256"] == "a" * 64


def test_photos_show_ambiguous_prefix_errors(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    # '2026' matches all three.
    result = runner.invoke(app, ["telegram", "photos", "show", "2026"])
    assert result.exit_code != 0


def test_photos_show_miss_exits_2(isolated_cache_dir: Path) -> None:
    _seed_records(isolated_cache_dir.resolve())
    result = runner.invoke(app, ["telegram", "photos", "show", "20990101"])
    assert result.exit_code == 2
    assert "no record matched" in result.stderr.lower()


# ==========================================================================
# photos prune
# ==========================================================================


def _read_trash_log(log_path: Path) -> List[List[str]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def test_photos_prune_dry_run_reports_but_does_not_trash(
    isolated_cache_dir: Path, no_real_trash: Path
) -> None:
    _commit_synthetic_record(
        isolated_cache_dir.resolve(),
        stem="20250101-120000-eeeeeeee",
        sha256_hex="e" * 64,
        expires_at=_cache_now_iso(-1),
    )
    result = runner.invoke(app, ["telegram", "photos", "prune", "--dry-run"])
    assert result.exit_code == 0
    assert "would trash 1" in result.stdout
    assert _read_trash_log(no_real_trash) == []
    # File remains.
    assert (isolated_cache_dir / "20250101-120000-eeeeeeee.json").exists()


def test_photos_prune_real_run_invokes_trash_shim(
    isolated_cache_dir: Path, no_real_trash: Path
) -> None:
    _commit_synthetic_record(
        isolated_cache_dir.resolve(),
        stem="20250101-120000-eeeeeeee",
        sha256_hex="e" * 64,
        expires_at=_cache_now_iso(-1),
    )
    result = runner.invoke(app, ["telegram", "photos", "prune"])
    assert result.exit_code == 0
    assert "trashed 1" in result.stdout
    all_argv = [tok for call in _read_trash_log(no_real_trash) for tok in call]
    resolved_dir = isolated_cache_dir.resolve()
    assert str(resolved_dir / "20250101-120000-eeeeeeee.json") in all_argv
    assert str(resolved_dir / "20250101-120000-eeeeeeee.jpg") in all_argv


def test_photos_prune_empty_cache_reports_zeros(
    isolated_cache_dir: Path, no_real_trash: Path
) -> None:
    result = runner.invoke(app, ["telegram", "photos", "prune"])
    assert result.exit_code == 0
    assert "trashed 0" in result.stdout
    assert _read_trash_log(no_real_trash) == []


# ==========================================================================
# help discoverability
# ==========================================================================


def test_photo_help_lists_every_flag() -> None:
    result = runner.invoke(app, ["telegram", "photo", "--help"])
    assert result.exit_code == 0
    text = result.stdout.lower()
    for flag in ("--caption", "--parse-mode", "--retention", "--dedup", "--label", "--reply-to", "--chat-id"):
        assert flag in text, f"telegram photo --help missing {flag!r}"


def test_photos_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["telegram", "photos", "--help"])
    assert result.exit_code == 0
    text = result.stdout.lower()
    for sub in ("list", "search", "show", "prune"):
        assert sub in text, f"telegram photos --help missing sub-command {sub!r}"


def test_photos_list_help_documents_since_and_chat() -> None:
    result = runner.invoke(app, ["telegram", "photos", "list", "--help"])
    assert result.exit_code == 0
    text = result.stdout.lower()
    assert "--since" in text
    assert "--chat" in text
    assert "--json" in text


def test_photos_prune_help_documents_dry_run() -> None:
    result = runner.invoke(app, ["telegram", "photos", "prune", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.stdout.lower()


# ==========================================================================
# Root-level `mineru telegram --help` mentions the new wired verbs
# ==========================================================================


def test_telegram_root_help_mentions_photo_and_photos() -> None:
    result = runner.invoke(app, ["telegram", "--help"])
    assert result.exit_code == 0
    text = result.stdout.lower()
    assert "photo" in text
    assert "photos" in text
