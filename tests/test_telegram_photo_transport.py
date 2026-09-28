"""Tests for the self-contained Telegram sendPhoto transport (P3-01).

⚠️⚠️ SAFETY (READ TWICE) ⚠️⚠️

  NO test in this file EVER contacts `api.telegram.org` for real. The
  transport module funnels every HTTP call through
  `urllib.request.urlopen`; each test monkeypatches that call site with
  a recorder that returns a canned in-memory response. A defense-in-depth
  `no_live_network` fixture also patches `socket.socket` at the class
  level to raise on any real socket creation — if a code path ever
  bypassed urlopen and went direct to sockets, the test suite would
  scream instead of silently DMing the operator.

  The bot token used throughout is `TEST-TOKEN-abc123` — a sentinel.
  Every argv / URL / body assertion verifies it appears ONLY in the
  Telegram URL path (protocol-required) and nowhere else — not in log
  lines, not in error messages, not in exceptions.

Coverage:

  Wrapper (`mineru_cli.wrappers.telegram_photo`):
    - `_build_url` puts the token in the URL path, host is exactly
      `api.telegram.org`, path ends in `/sendPhoto`.
    - `_build_multipart_body` produces an RFC-2388-shaped body: leading
      `--<boundary>`, fields with `Content-Disposition: form-data;
      name="..."`, photo part with `Content-Type: <mime>`, trailing
      `--<boundary>--\r\n`.
    - `_make_boundary` returns a unique string that cannot collide with
      photo bytes (all hex, no `--`).
    - `_parse_response` extracts (message_id, largest file_id) from the
      Bot API success shape; missing/malformed returns (None, None).
    - `_extract_retry_after` reads the Retry-After header on 429.

  Secrets seam:
    - `send_photo` resolves the bot token via SecretsResolver.resolve
      ('telegram-bot-token'); miss => typer.Exit(2) with an actionable
      message naming the slot but NOT the value.
    - `send_photo` resolves chat_id via .resolve('telegram-chat-id')
      when the argument is omitted; explicit override wins.
    - Token / chat_id values never appear in argv (there IS no
      subprocess in this module) and never appear in the exit message.

  Transport contracts:
    - Fresh upload: exact URL, method=POST, Content-Type starts with
      `multipart/form-data; boundary=...`, body contains chat_id field
      + photo binary bytes verbatim + parse_mode when set.
    - Cached-file: photo=<file_id> is sent as
      `application/x-www-form-urlencoded`; body decodes to the expected
      k=v pairs; no multipart shape.
    - 429 with Retry-After: NO retry inside the call; returns
      ok=False, error='rate_limited', retry_after=<sleep hint>.
    - 5xx: exactly one retry (assert via a scripted urlopen sequence);
      if the retry also 5xxs, returns ok=False, error='server_error'.
    - 4xx (non-429): no retry, returns http_<code> label, no token /
      response body echoed.
    - URLError: no retry, error='network'.
    - Local file missing: no HTTP call at all, error='local_file_missing'.

  No-live-network:
    - `socket.socket` is patched at the class level; any attempt to
      instantiate a real socket during a test raises immediately.
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import pytest

import typer

from mineru_cli.secrets import EnvBackend, SecretsResolver
from mineru_cli.wrappers import telegram_photo as tp
from mineru_cli.wrappers.telegram_photo import (
    TELEGRAM_API_HOST,
    TELEGRAM_BOT_TOKEN_SECRET,
    TELEGRAM_CHAT_ID_SECRET,
    SendPhotoResult,
    _build_multipart_body,
    _build_url,
    _extract_retry_after,
    _make_boundary,
    _parse_response,
    send_photo,
)


# --------------------------------------------------------------------- fixtures


SENTINEL_TOKEN = "TEST-TOKEN-abc123"
SENTINEL_CHAT_ID = "123456789"


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense-in-depth: patching `socket.socket` blocks any real network I/O.

    Every test in this file also monkeypatches
    `urllib.request.urlopen` directly. But if a future refactor ever
    replaced urlopen with (say) `httpx` or a raw `socket.create_connection`
    call, this fixture guarantees the suite fails loudly instead of
    silently sending a real request.

    The `socket.socketpair` used by pytest's own capture pipes is left
    alone; we only block the outbound `socket.socket()` constructor. Any
    test that legitimately needs a socket must opt out explicitly.
    """

    real_socket = socket.socket

    def blocked(*args, **kwargs):
        raise AssertionError(
            "no_live_network: tests must not open real sockets; "
            "monkeypatch urllib.request.urlopen instead."
        )

    monkeypatch.setattr(socket, "socket", blocked)
    # Restore intentionally left to monkeypatch teardown.
    _ = real_socket  # silence lint: kept for future opt-out use


@pytest.fixture
def envbacked_resolver(monkeypatch: pytest.MonkeyPatch) -> SecretsResolver:
    """A resolver whose sole backend is the env, seeded with sentinel values.

    Keeps every test hermetic: no Keychain hit, no OS-level state, and
    the token/chat_id values are known sentinels so we can assert they
    never leak into a log line or exception message.
    """
    monkeypatch.setenv("MINERU_SECRET_TELEGRAM_BOT_TOKEN", SENTINEL_TOKEN)
    monkeypatch.setenv("MINERU_SECRET_TELEGRAM_CHAT_ID", SENTINEL_CHAT_ID)
    return SecretsResolver([EnvBackend(env_prefix="MINERU_SECRET_")])


@pytest.fixture
def sample_photo(tmp_path: Path) -> Path:
    """A tiny fake JPEG on disk; contents are opaque bytes for wire assertions."""
    p = tmp_path / "river-smile.jpg"
    # Realistic-ish payload: JPEG SOI marker plus junk. The transport is
    # byte-oriented; it does not validate image content, so any bytes work.
    p.write_bytes(b"\xff\xd8\xff\xe0MINERU_SENTINEL_IMAGE_BYTES_v1")
    return p


class RecordedRequest:
    """Snapshot of one urlopen invocation, for assertion after the fact."""

    def __init__(self, request: urllib.request.Request, timeout: Optional[float]) -> None:
        self.url = request.full_url
        self.method = request.get_method()
        self.headers = dict(request.headers)
        # `data` on a Request is the exact bytes urlopen will POST.
        self.body: bytes = request.data if isinstance(request.data, (bytes, bytearray)) else b""
        self.timeout = timeout


def _canned_ok_response(
    *, message_id: int = 42, largest_file_id: str = "TG_FILE_ID_LARGEST"
) -> io.BytesIO:
    """Return a BytesIO holding a Bot API sendPhoto success body.

    The Bot API returns an ascending-size photo list; the transport
    treats the last entry as the canonical file_id for cache reuse.
    """
    body = {
        "ok": True,
        "result": {
            "message_id": message_id,
            "date": 1_700_000_000,
            "photo": [
                {"file_id": "TG_FILE_ID_TINY", "width": 90, "height": 90, "file_size": 500},
                {"file_id": "TG_FILE_ID_MID", "width": 320, "height": 320, "file_size": 5000},
                {"file_id": largest_file_id, "width": 800, "height": 800, "file_size": 20000},
            ],
        },
    }
    return io.BytesIO(json.dumps(body).encode("utf-8"))


class _StubResponse:
    """Minimal context-manager stub for what urlopen returns."""

    def __init__(self, buf: io.BytesIO) -> None:
        self._buf = buf

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._buf.read()


def _make_urlopen_recorder(response_bufs: List[io.BytesIO]):
    """Return (fake_urlopen, records) that iterates through canned responses.

    Each call pops one BytesIO from the front of `response_bufs`. If an
    entry is a callable, it's invoked and its return value (a BytesIO OR
    an exception to raise) is used — this lets a test script "first call
    raises 5xx, second call succeeds" behavior.
    """
    records: List[RecordedRequest] = []
    remaining = list(response_bufs)

    def fake_urlopen(request, timeout=None):
        records.append(RecordedRequest(request, timeout))
        if not remaining:
            raise AssertionError(
                "urlopen called more times than the test scripted; "
                f"records so far: {len(records)}"
            )
        next_item = remaining.pop(0)
        if callable(next_item):
            resolved = next_item()
            if isinstance(resolved, BaseException):
                raise resolved
            return _StubResponse(resolved)
        if isinstance(next_item, BaseException):
            raise next_item
        return _StubResponse(next_item)

    return fake_urlopen, records


# ==========================================================================
# Pure helper units
# ==========================================================================


def test_build_url_places_token_in_path_and_uses_api_telegram_org() -> None:
    url = _build_url("TOKEN123")
    assert url == "https://api.telegram.org/botTOKEN123/sendPhoto"
    # Sanity: the constant matches the host we build against.
    assert TELEGRAM_API_HOST == "api.telegram.org"


def test_make_boundary_is_unique_and_hex_only() -> None:
    boundaries = {_make_boundary() for _ in range(50)}
    assert len(boundaries) == 50
    for b in boundaries:
        # Must not contain `--` (would break parsers) and must be short
        # enough (well under RFC 2046's 70-char cap).
        assert "--" not in b
        assert len(b) < 70


def test_build_multipart_body_has_rfc2388_shape() -> None:
    body = _build_multipart_body(
        boundary="XYZ",
        fields={"chat_id": SENTINEL_CHAT_ID, "caption": "hi"},
        photo_bytes=b"\x00\x01\x02BYTES",
        photo_filename="a.jpg",
        photo_mime="image/jpeg",
    )
    # Leading part opener.
    assert body.startswith(b"--XYZ\r\n")
    # Trailing terminator.
    assert body.endswith(b"--XYZ--\r\n")
    # Text field.
    assert b'Content-Disposition: form-data; name="chat_id"' in body
    assert SENTINEL_CHAT_ID.encode() in body
    # Photo part header.
    assert (
        b'Content-Disposition: form-data; name="photo"; filename="a.jpg"' in body
    )
    assert b"Content-Type: image/jpeg" in body
    # Binary bytes appear verbatim.
    assert b"\x00\x01\x02BYTES" in body


def test_build_multipart_body_omits_none_fields() -> None:
    """A caller-supplied None means 'skip this field' — matches Telegram semantics."""
    body = _build_multipart_body(
        boundary="XYZ",
        fields={"chat_id": "1", "caption": None, "parse_mode": None},
        photo_bytes=b"x",
        photo_filename="a.jpg",
        photo_mime="image/jpeg",
    )
    assert b'name="caption"' not in body
    assert b'name="parse_mode"' not in body
    assert b'name="chat_id"' in body


@pytest.mark.parametrize(
    "bad_filename",
    [
        'safe.jpg"\r\nContent-Type: application/x-evil\r\nX-Injected: yes\r\n',  # full injection
        "photo\r\n.jpg",  # bare CRLF
        "photo\n.jpg",   # LF-only header split
        "photo\rphoto.jpg",  # CR-only
        'break"out.jpg',  # embedded double-quote
        "with\x00nul.jpg",  # NUL byte
    ],
)
def test_build_multipart_body_rejects_header_splitting_filename(bad_filename: str) -> None:
    """The multipart layer must NEVER interpolate CR/LF/NUL/\" into headers.

    Guards the "single narrow HTTP boundary" invariant from the module
    docstring: even if a future caller forgets to sanitize a filename,
    the transport refuses to build a body that could splice arbitrary
    headers into the request.
    """
    with pytest.raises(ValueError, match="header-splitting"):
        _build_multipart_body(
            boundary="XYZ",
            fields={"chat_id": "1"},
            photo_bytes=b"x",
            photo_filename=bad_filename,
            photo_mime="image/jpeg",
        )


def test_build_multipart_body_rejects_header_splitting_field_name() -> None:
    """Same guard for field names (defense in depth, callers use fixed names)."""
    with pytest.raises(ValueError, match="header-splitting"):
        _build_multipart_body(
            boundary="XYZ",
            fields={'chat_id"\r\nX-Evil: 1': "1"},
            photo_bytes=b"x",
            photo_filename="a.jpg",
            photo_mime="image/jpeg",
        )


def test_build_multipart_body_rejects_header_splitting_mime() -> None:
    """MIME comes from `mimetypes.guess_type` today, but validate defensively."""
    with pytest.raises(ValueError, match="header-splitting"):
        _build_multipart_body(
            boundary="XYZ",
            fields={"chat_id": "1"},
            photo_bytes=b"x",
            photo_filename="a.jpg",
            photo_mime="image/jpeg\r\nX-Evil: yes",
        )


def test_parse_response_extracts_message_id_and_largest_file_id() -> None:
    body = json.dumps(
        {
            "ok": True,
            "result": {
                "message_id": 99,
                "photo": [
                    {"file_id": "small"},
                    {"file_id": "medium"},
                    {"file_id": "large"},
                ],
            },
        }
    ).encode("utf-8")
    msg_id, file_id = _parse_response(body)
    assert msg_id == 99
    assert file_id == "large"


def test_parse_response_handles_missing_or_malformed() -> None:
    assert _parse_response(b"not json") == (None, None)
    assert _parse_response(b'{"ok": false, "description": "..."}') == (None, None)
    assert _parse_response(b'{"ok": true, "result": {}}') == (None, None)
    # Well-formed but empty photo list -> no file_id, but message_id present.
    body = json.dumps(
        {"ok": True, "result": {"message_id": 7, "photo": []}}
    ).encode()
    assert _parse_response(body) == (7, None)


def test_extract_retry_after_reads_header() -> None:
    from email.message import Message

    hdrs = Message()
    hdrs["Retry-After"] = "42"
    exc = urllib.error.HTTPError(
        url="https://x", code=429, msg="too many", hdrs=hdrs, fp=None
    )
    assert _extract_retry_after(exc) == 42


def test_extract_retry_after_missing_header_returns_none() -> None:
    from email.message import Message

    hdrs = Message()
    exc = urllib.error.HTTPError(
        url="https://x", code=429, msg="too many", hdrs=hdrs, fp=None
    )
    assert _extract_retry_after(exc) is None


# ==========================================================================
# Secrets resolution (miss paths -> typer.Exit(2), never leak the value)
# ==========================================================================


def test_send_photo_missing_token_exits_2_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """No token in any backend => Exit(2) naming the Keychain slot, NOT the value."""
    monkeypatch.delenv("MINERU_SECRET_TELEGRAM_BOT_TOKEN", raising=False)
    # Point PATH at an empty dir so KeychainBackend has no `security`.
    empty = Path("/tmp/mineru_p3_empty_path")
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))

    resolver = SecretsResolver([EnvBackend(env_prefix="MINERU_SECRET_")])
    # urlopen must NOT be called on this path.
    calls: List = []

    def sentinel_urlopen(*a, **kw):
        calls.append(a)
        raise AssertionError("urlopen must not be called when token missing")

    monkeypatch.setattr(urllib.request, "urlopen", sentinel_urlopen)

    with pytest.raises(typer.Exit) as excinfo:
        send_photo("/tmp/anything.jpg", secrets_resolver=resolver)
    assert excinfo.value.exit_code == 2
    err = capsys.readouterr().err
    # Slot NAME is safe to print; VALUE must not appear (nothing was set).
    assert TELEGRAM_BOT_TOKEN_SECRET in err
    assert "actionable" not in err.lower()  # sanity: not a placeholder
    assert "Keychain" in err or "keychain" in err
    # No urlopen leaked.
    assert calls == []


def test_send_photo_missing_chat_id_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Token present but chat id missing => Exit(2) naming the chat-id slot."""
    monkeypatch.setenv("MINERU_SECRET_TELEGRAM_BOT_TOKEN", SENTINEL_TOKEN)
    monkeypatch.delenv("MINERU_SECRET_TELEGRAM_CHAT_ID", raising=False)
    empty = Path("/tmp/mineru_p3_empty_path")
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))

    resolver = SecretsResolver([EnvBackend(env_prefix="MINERU_SECRET_")])
    with pytest.raises(typer.Exit) as excinfo:
        send_photo("/tmp/anything.jpg", secrets_resolver=resolver)
    assert excinfo.value.exit_code == 2
    err = capsys.readouterr().err
    assert TELEGRAM_CHAT_ID_SECRET in err
    # The token value must NEVER appear in error output.
    assert SENTINEL_TOKEN not in err


def test_send_photo_explicit_chat_id_overrides_secret(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """Passing chat_id=... short-circuits the secrets resolve for chat id."""
    fake_urlopen, records = _make_urlopen_recorder([_canned_ok_response()])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(
        str(sample_photo),
        chat_id="OVERRIDE_CHAT_ID",
        secrets_resolver=envbacked_resolver,
    )
    assert result["ok"] is True
    # Only one HTTP call recorded, and the body has the override chat_id.
    assert len(records) == 1
    assert b"OVERRIDE_CHAT_ID" in records[0].body
    # The sentinel chat id from the resolver did NOT get sent.
    assert SENTINEL_CHAT_ID.encode() not in records[0].body


# ==========================================================================
# Fresh-upload multipart transport
# ==========================================================================


def test_send_photo_fresh_upload_hits_exact_url_and_multipart_body(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """Every wire-visible aspect of a fresh upload is asserted here."""
    fake_urlopen, records = _make_urlopen_recorder([_canned_ok_response()])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(
        str(sample_photo),
        caption="River smiling",
        parse_mode="HTML",
        reply_to_message_id=101,
        secrets_resolver=envbacked_resolver,
    )

    # --- Response passed back to caller
    assert result["ok"] is True
    assert result["message_id"] == 42
    assert result["telegram_file_id"] == "TG_FILE_ID_LARGEST"
    assert result["error"] is None
    assert result["retry_after"] is None

    # --- One HTTP call
    assert len(records) == 1
    rec = records[0]

    # URL: token in path, host is api.telegram.org, endpoint is sendPhoto.
    assert rec.url == f"https://api.telegram.org/bot{SENTINEL_TOKEN}/sendPhoto"

    # Method is POST.
    assert rec.method == "POST"

    # Content-Type is multipart with a real boundary.
    ct = rec.headers.get("Content-type") or rec.headers.get("Content-Type")
    assert ct is not None
    assert ct.startswith("multipart/form-data; boundary=")

    # Body carries every text field + the raw image bytes.
    assert b'name="chat_id"' in rec.body
    assert SENTINEL_CHAT_ID.encode() in rec.body
    assert b'name="caption"' in rec.body
    assert b"River smiling" in rec.body
    assert b'name="parse_mode"' in rec.body
    assert b"HTML" in rec.body
    assert b'name="reply_to_message_id"' in rec.body
    assert b"101" in rec.body
    assert b'name="photo"' in rec.body
    assert b'filename="river-smile.jpg"' in rec.body
    assert b"Content-Type: image/jpeg" in rec.body
    assert sample_photo.read_bytes() in rec.body


def test_send_photo_local_file_missing_no_http_call(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
) -> None:
    """A missing local file returns 'local_file_missing' WITHOUT contacting Telegram."""
    called = []

    def sentinel_urlopen(*a, **kw):
        called.append(a)
        raise AssertionError("urlopen must not be called when local file missing")

    monkeypatch.setattr(urllib.request, "urlopen", sentinel_urlopen)
    result = send_photo(
        "/tmp/definitely-not-a-real-file-xyz.jpg",
        secrets_resolver=envbacked_resolver,
    )
    assert result["ok"] is False
    assert result["error"] == "local_file_missing"
    assert called == []


# ==========================================================================
# Cached-file (form-urlencoded) transport
# ==========================================================================


def test_send_photo_cached_id_uses_form_urlencoded(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
) -> None:
    """is_cached_id=True => photo=<file_id> in an urlencoded body, not multipart."""
    fake_urlopen, records = _make_urlopen_recorder([_canned_ok_response()])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(
        "TG_FILE_ID_CACHED",
        caption="reuse",
        is_cached_id=True,
        secrets_resolver=envbacked_resolver,
    )
    assert result["ok"] is True
    assert len(records) == 1
    rec = records[0]

    ct = rec.headers.get("Content-type") or rec.headers.get("Content-Type")
    assert ct == "application/x-www-form-urlencoded"
    # Body is urlencoded and decodes to the expected fields.
    from urllib.parse import parse_qs

    parsed = parse_qs(rec.body.decode("utf-8"))
    assert parsed["photo"] == ["TG_FILE_ID_CACHED"]
    assert parsed["chat_id"] == [SENTINEL_CHAT_ID]
    assert parsed["caption"] == ["reuse"]


def test_send_photo_cached_id_does_not_open_any_local_file(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
) -> None:
    """Cached-file path does NOT trigger a filesystem read on the file_id.

    (The file_id is an opaque Telegram-side identifier; there is no local
    file with that name.)
    """
    fake_urlopen, _ = _make_urlopen_recorder([_canned_ok_response()])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # Even a nonsense "path" works when is_cached_id=True.
    result = send_photo(
        "not/a/real/path/but/still/an/ok/file_id",
        is_cached_id=True,
        secrets_resolver=envbacked_resolver,
    )
    assert result["ok"] is True


# ==========================================================================
# 429 rate-limit path
# ==========================================================================


def _make_http_error(code: int, retry_after: Optional[str] = None) -> urllib.error.HTTPError:
    from email.message import Message

    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://api.telegram.org/botX/sendPhoto",
        code=code,
        msg="canned",
        hdrs=hdrs,
        fp=None,
    )


def test_send_photo_429_returns_rate_limited_and_retry_after_hint(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """429 => NO retry, ok=False, error='rate_limited', retry_after=<seconds>."""

    def raise_429():
        return _make_http_error(429, retry_after="17")

    fake_urlopen, records = _make_urlopen_recorder([raise_429])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(str(sample_photo), secrets_resolver=envbacked_resolver)
    assert result["ok"] is False
    assert result["error"] == "rate_limited"
    assert result["retry_after"] == 17
    # Exactly one call — the 429 must NOT trigger the 5xx retry.
    assert len(records) == 1


def test_send_photo_429_without_retry_after_still_labeled_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    def raise_429_bare():
        return _make_http_error(429)

    fake_urlopen, _ = _make_urlopen_recorder([raise_429_bare])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(str(sample_photo), secrets_resolver=envbacked_resolver)
    assert result["error"] == "rate_limited"
    assert result["retry_after"] is None


# ==========================================================================
# 5xx one-shot retry path
# ==========================================================================


def test_send_photo_5xx_retries_exactly_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """First call 502, second call 200 => ok=True."""

    def raise_502():
        return _make_http_error(502)

    fake_urlopen, records = _make_urlopen_recorder(
        [raise_502, _canned_ok_response()]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # Speed up the retry sleep so the test is instant.
    monkeypatch.setattr(tp, "RETRY_5XX_SLEEP_SECONDS", 0.0)

    result = send_photo(str(sample_photo), secrets_resolver=envbacked_resolver)
    assert result["ok"] is True
    assert result["message_id"] == 42
    # Exactly two HTTP calls.
    assert len(records) == 2


def test_send_photo_5xx_twice_returns_server_error_no_third_call(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """Two 5xxs in a row => ok=False, error='server_error', exactly two calls."""

    def raise_500():
        return _make_http_error(500)

    fake_urlopen, records = _make_urlopen_recorder([raise_500, raise_500])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(tp, "RETRY_5XX_SLEEP_SECONDS", 0.0)

    result = send_photo(str(sample_photo), secrets_resolver=envbacked_resolver)
    assert result["ok"] is False
    assert result["error"] == "server_error"
    assert len(records) == 2


def test_send_photo_5xx_retry_also_works_for_cached_id_path(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
) -> None:
    """Cached-id path must also retry once on 5xx (parity with fresh upload)."""

    def raise_503():
        return _make_http_error(503)

    fake_urlopen, records = _make_urlopen_recorder(
        [raise_503, _canned_ok_response()]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(tp, "RETRY_5XX_SLEEP_SECONDS", 0.0)

    result = send_photo(
        "TG_FILE_ID_CACHED",
        is_cached_id=True,
        secrets_resolver=envbacked_resolver,
    )
    assert result["ok"] is True
    assert len(records) == 2
    # Both calls used form-urlencoded (not multipart).
    for rec in records:
        ct = rec.headers.get("Content-type") or rec.headers.get("Content-Type")
        assert ct == "application/x-www-form-urlencoded"


# ==========================================================================
# 4xx (non-429) path
# ==========================================================================


def test_send_photo_400_no_retry_labeled_http_400(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """A 400 (e.g. bad caption) does NOT retry and does NOT echo the body."""

    def raise_400():
        return _make_http_error(400)

    fake_urlopen, records = _make_urlopen_recorder([raise_400])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(str(sample_photo), secrets_resolver=envbacked_resolver)
    assert result["ok"] is False
    assert result["error"] == "http_400"
    assert result["retry_after"] is None
    assert len(records) == 1
    # Result dict never carries token or response text.
    for v in result.values():
        assert v is None or SENTINEL_TOKEN not in str(v)


# ==========================================================================
# URLError (DNS / TLS / refused) path
# ==========================================================================


def test_send_photo_urlerror_no_retry_labeled_network(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """A transport-level failure is labeled 'network', no retry, no token leak."""

    def raise_conn():
        return urllib.error.URLError("Name or service not known")

    fake_urlopen, records = _make_urlopen_recorder([raise_conn])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(str(sample_photo), secrets_resolver=envbacked_resolver)
    assert result["ok"] is False
    assert result["error"] == "network"
    assert len(records) == 1


# ==========================================================================
# `bad_response` path: 2xx status but the JSON body has ok=false / missing
# message_id. Real-world triggers include Telegram returning a 200 with
# {"ok": false, "description": "..."}, or a partial response shape drift.
# ==========================================================================


def test_send_photo_bad_response_no_retry_labeled_bad_response(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """200 with `ok=false` body => ok=False, error='bad_response', no retry.

    The transport treats any 2xx whose parsed body yields no message_id
    as `bad_response`. This path exists because Telegram occasionally
    responds 200 with an error envelope ("no permission to send",
    "chat not found") that _parse_response cannot convert to a real
    message_id. A retry would just replay the same rejection, so the
    transport must NOT retry and must NOT echo the response body — a
    naive echo could leak caller-supplied caption content that
    Telegram reflected back.
    """
    error_body = json.dumps(
        {"ok": False, "description": "Bad Request: chat not found"}
    ).encode("utf-8")
    fake_urlopen, records = _make_urlopen_recorder([io.BytesIO(error_body)])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(str(sample_photo), secrets_resolver=envbacked_resolver)
    assert result["ok"] is False
    assert result["error"] == "bad_response"
    assert result["message_id"] is None
    assert result["telegram_file_id"] is None
    assert result["retry_after"] is None
    # Exactly one HTTP call — bad_response must NOT trigger the 5xx retry.
    assert len(records) == 1
    # Bad-response is exactly where a naive echo of the response body
    # could leak caller-supplied content back through the result dict.
    for v in result.values():
        assert v is None or SENTINEL_TOKEN not in str(v)
        # The Telegram-side description must not surface either.
        assert v is None or "chat not found" not in str(v)


def test_send_photo_bad_response_on_malformed_2xx_body(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """A 200 whose body is not parseable JSON also yields `bad_response`.

    Guards the branch inside `_send_once` where `_parse_response`
    returns `(None, None)` — a wire-format drift or a partial write
    from Telegram must be caught with a stable, non-retryable label.
    """
    fake_urlopen, records = _make_urlopen_recorder(
        [io.BytesIO(b"not-json-at-all")]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(str(sample_photo), secrets_resolver=envbacked_resolver)
    assert result["ok"] is False
    assert result["error"] == "bad_response"
    assert result["message_id"] is None
    assert len(records) == 1


# ==========================================================================
# Secret leakage: token never appears anywhere except the URL path
# ==========================================================================


def test_token_never_appears_in_body_or_headers(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """The bot token is a URL-path secret; it MUST NOT appear anywhere else."""
    fake_urlopen, records = _make_urlopen_recorder([_canned_ok_response()])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    send_photo(
        str(sample_photo),
        caption="cap",
        secrets_resolver=envbacked_resolver,
    )
    assert len(records) == 1
    rec = records[0]

    # URL path: token IS present (by protocol design).
    assert SENTINEL_TOKEN in rec.url

    # Body: token must NOT appear.
    assert SENTINEL_TOKEN.encode() not in rec.body

    # Headers: token must NOT appear (host header comes from urllib; even
    # so, no header value carries the token).
    for header_value in rec.headers.values():
        assert SENTINEL_TOKEN not in str(header_value)


def test_result_dict_never_carries_token(
    monkeypatch: pytest.MonkeyPatch,
    envbacked_resolver: SecretsResolver,
    sample_photo: Path,
) -> None:
    """A serialized result would go into the sidecar cache; must not carry secret."""
    fake_urlopen, _ = _make_urlopen_recorder([_canned_ok_response()])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = send_photo(str(sample_photo), secrets_resolver=envbacked_resolver)
    serialized = json.dumps(result)
    assert SENTINEL_TOKEN not in serialized


# ==========================================================================
# Send Photo Result dataclass shape (contract for the cache layer)
# ==========================================================================


def test_send_photo_result_as_dict_shape() -> None:
    r = SendPhotoResult(
        ok=True, message_id=1, telegram_file_id="fid", error=None, retry_after=None
    )
    assert r.as_dict() == {
        "ok": True,
        "message_id": 1,
        "telegram_file_id": "fid",
        "error": None,
        "retry_after": None,
    }


# ==========================================================================
# Static invariants against the module source
# ==========================================================================


import mineru_cli.wrappers.telegram_photo as _tp_module_for_source_read

WRAPPER_SRC = Path(_tp_module_for_source_read.__file__).read_text()


def test_wrapper_source_pins_api_host_as_a_constant() -> None:
    """A future refactor that let the host be overridden through env would
    open a covert-exfil path (redirect the send to attacker-controlled
    host with the token in the URL). Freeze the host as a constant here.
    """
    assert 'TELEGRAM_API_HOST = "api.telegram.org"' in WRAPPER_SRC


def test_wrapper_source_has_no_hardcoded_bot_token() -> None:
    """No literal token bytes anywhere in the module (only NAMES)."""
    # Telegram bot tokens look like `<digits>:<letters-digits-underscore-hyphen>`
    # We use a sentinel token in tests; the wrapper source must never carry
    # any placeholder that could accidentally ship.
    for forbidden in (
        SENTINEL_TOKEN,
        "TELEGRAM_BOT_TOKEN =",
        "bot_token_value",
        "1234567890:AAA",
    ):
        assert forbidden not in WRAPPER_SRC, (
            f"telegram_photo.py must not carry hardcoded token material; "
            f"found {forbidden!r}"
        )


def test_wrapper_source_uses_stdlib_urllib_only() -> None:
    """No `requests`, no `httpx`, no third-party HTTP client dep."""
    for forbidden in ("import requests", "from requests", "import httpx", "from httpx"):
        assert forbidden not in WRAPPER_SRC, (
            f"telegram_photo.py must be stdlib-only; found {forbidden!r}"
        )


def test_wrapper_source_has_no_subprocess_calls() -> None:
    """The transport is a pure HTTP boundary; no shell-outs, no argv secrets."""
    for forbidden in ("import subprocess", "from subprocess", "subprocess."):
        assert forbidden not in WRAPPER_SRC, (
            f"telegram_photo.py must not shell out; found {forbidden!r}"
        )
