"""Self-contained Telegram sendPhoto transport for `mineru telegram photo`.

Phase-3 P3-01 (spec §4.1 transport half). This module is a self-contained
outbound transport for Telegram `sendPhoto` — it does NOT wrap an existing
engine binary. It sits alongside the other `mineru_cli.wrappers.*` modules
because it plays the same architectural role: the single narrow HTTP
boundary the verb layer routes through. Callers get one function,
`send_photo`, that returns a structured result dict; the retention cache
layer built on top of it (§4.2) persists the returned telegram_file_id.

⚠️⚠️ SAFETY (READ BEFORE EDITING) ⚠️⚠️

  During dev and test NO real POST to `api.telegram.org` ever fires. Every
  test in `tests/test_telegram_photo_transport.py` monkeypatches
  `urllib.request.urlopen` (this module's only network entry point) with
  a recorder and asserts the exact multipart body, boundary, Content-Type,
  URL path, and secret-material discipline of the request that WOULD be
  sent. Any change to this module that breaks that mocking contract is a
  regression: a real invocation would DM the operator.

  The secrets seam is the only source of the bot token and chat id. The
  token NEVER appears in argv (this module makes no subprocess calls),
  NEVER in a log line, NEVER in an exception message. The Telegram URL
  path carries the token by protocol design; that string is built inside
  `_send_once` and passed only to `urlopen`. We never .format() or f-
  string it into anything else.

Contract:

  send_photo(
      path_or_file_id,       # str: absolute file path OR a Telegram file_id
      chat_id=None,          # int|str|None: overrides secrets 'telegram-chat-id'
      caption=None,          # str|None: <=1024 chars per Telegram; caller trims
      parse_mode=None,       # 'HTML'|'MarkdownV2'|None (see Bot API §sendMessage)
      reply_to_message_id=None,  # int|None: reply threading
      is_cached_id=False,    # bool: True -> photo=<file_id> via form-urlencoded
      secrets_resolver=None, # SecretsResolver|None: injected for tests
  ) -> dict with keys:
      ok               : bool
      message_id       : int | None
      telegram_file_id : str | None   # the largest photo variant returned
      error            : str | None   # a short, secret-free error label
      retry_after      : int | None   # 429 Retry-After seconds, sleep hint

Retry policy (single-request scope):
  - 429 with Retry-After: NO retry inside this call. Return
    ok=False, error='rate_limited', retry_after=<seconds>. The caller
    decides whether to sleep + resend (the cache layer will).
  - 5xx: one silent retry after a fixed 0.5s sleep. If the retry also
    5xxs, return ok=False, error='server_error'. This mirrors the current
    Landline transport's shape (bounded, predictable, cache-friendly).
  - Any other HTTP error: no retry, return ok=False, error='http_<code>'.
  - urllib.error.URLError (DNS, refused, TLS): no retry, error='network'.
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import typer

from mineru_cli.secrets import SecretsResolver, build_resolver


logger = logging.getLogger(__name__)


# --- Constants -------------------------------------------------------------

# Canonical secret names. These are secret NAMES (safe to log), never
# values. The resolver chain (env -> keychain -> 1password) decides where
# each one comes from at runtime.
TELEGRAM_BOT_TOKEN_SECRET = "telegram-bot-token"
TELEGRAM_CHAT_ID_SECRET = "telegram-chat-id"

# Telegram Bot API host. Baked as a constant here (a) so tests can grep for
# it as a static invariant, and (b) so a caller cannot smuggle a different
# host through the argv. Any override would require an env var read here,
# which we deliberately do not add — no proxy hooks by design.
TELEGRAM_API_HOST = "api.telegram.org"

# Maximum caption length per Bot API `sendPhoto`; documented for the
# caller's benefit. We do NOT truncate here — the caller (the verb layer)
# owns that policy so the truncation shows up in the sidecar cache too.
TELEGRAM_CAPTION_MAX_CHARS = 1024

# Time to sleep between the initial send and the single 5xx retry. Kept
# small: this is a fast-path retry for a transient upstream blip, not a
# proper backoff (the cache layer's caller loop does the long backoffs).
RETRY_5XX_SLEEP_SECONDS = 0.5

# Read timeout on the HTTP call. Telegram accepts up to 10 MB photo
# uploads; 60s is generous but not indefinite (a hung urlopen would block
# a cron job's whole slot otherwise).
DEFAULT_HTTP_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class SendPhotoResult:
    """Structured send result. Never carries the bot token."""

    ok: bool
    message_id: Optional[int] = None
    telegram_file_id: Optional[str] = None
    error: Optional[str] = None
    retry_after: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "message_id": self.message_id,
            "telegram_file_id": self.telegram_file_id,
            "error": self.error,
            "retry_after": self.retry_after,
        }


# --- Secret resolution -----------------------------------------------------


def _resolver_or_default(resolver: Optional[SecretsResolver]) -> SecretsResolver:
    """Return the passed-in resolver or a fresh default-chain resolver.

    Tests inject a `SecretsResolver` built over an EnvBackend so the whole
    call chain stays hermetic. Production callers pass the ctx-hydrated
    resolver from the profile layer.
    """
    if resolver is not None:
        return resolver
    return build_resolver()


def _resolve_bot_token(resolver: SecretsResolver) -> str:
    """Return the Telegram bot token or exit 2 with an actionable message.

    The exit message names the Keychain SLOT (a secret NAME, safe to
    print) and the resolver chain's `describe()` — never the value.
    """
    result = resolver.resolve(TELEGRAM_BOT_TOKEN_SECRET)
    if not result.present or not result.value:
        chain_desc = ", ".join(b.describe() for b in resolver.backends) or "(none)"
        typer.echo(
            "mineru telegram photo: no backend resolved "
            f"{TELEGRAM_BOT_TOKEN_SECRET!r}. Chain: {chain_desc}. "
            f"Store it in Keychain: `security add-generic-password "
            f"-a mineru -s {TELEGRAM_BOT_TOKEN_SECRET} -w '<token>' -U`.",
            err=True,
        )
        raise typer.Exit(code=2)
    return result.value


def _resolve_chat_id(
    resolver: SecretsResolver, override: Optional[Union[int, str]]
) -> str:
    """Return the effective chat id as a string (Telegram accepts both).

    Passed-in `override` wins (verb layer's --chat-id). Otherwise resolve
    via the secrets chain. Missing -> exit 2 with an actionable message.
    """
    if override is not None and str(override).strip():
        return str(override).strip()
    result = resolver.resolve(TELEGRAM_CHAT_ID_SECRET)
    if not result.present or not result.value:
        chain_desc = ", ".join(b.describe() for b in resolver.backends) or "(none)"
        typer.echo(
            "mineru telegram photo: no backend resolved "
            f"{TELEGRAM_CHAT_ID_SECRET!r}. Chain: {chain_desc}. "
            f"Store it in Keychain: `security add-generic-password "
            f"-a mineru -s {TELEGRAM_CHAT_ID_SECRET} -w '<chat-id>' -U`.",
            err=True,
        )
        raise typer.Exit(code=2)
    return result.value.strip()


# --- Multipart body assembly ----------------------------------------------


def _detect_mime(path: Path) -> str:
    """Return the best-effort mime type for a photo path.

    Telegram accepts image/jpeg, image/png, image/webp, image/gif. We
    default to image/jpeg on an unknown extension so the send doesn't
    fail — Telegram will still validate the actual bytes on its side.
    """
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "image/jpeg"


# Bytes that would let a caller-controlled `name` or `photo_filename`
# escape the surrounding `name="..."`/`filename="..."` header attribute
# and inject arbitrary multipart headers or truncate the photo part. Any
# occurrence is a header-splitting attempt; we treat it as a hard bug in
# the caller and refuse to build the body.
_HEADER_SPLITTING_CHARS = ("\r", "\n", "\x00", '"')


def _reject_header_splitting(value: str, *, field_role: str) -> None:
    """Raise ValueError if `value` contains any header-splitting byte.

    Fail-loud: an unsafe field name / filename means someone is trying
    to inject `\\r\\nContent-Type: ...` or a stray `"` into the multipart
    header attribute. We do NOT want to silently strip and forward: the
    caller's intent is unrecoverable, and forwarding a mutated string
    would mask the bug. The verb layer currently only passes hex-safe
    stems, so this is defense in depth for future callers.
    """
    for ch in _HEADER_SPLITTING_CHARS:
        if ch in value:
            raise ValueError(
                f"multipart {field_role} {value!r} contains a header-splitting "
                f"character ({ch!r}); refusing to build body."
            )


def _build_multipart_body(
    *,
    boundary: str,
    fields: dict,
    photo_bytes: bytes,
    photo_filename: str,
    photo_mime: str,
) -> bytes:
    """Assemble a multipart/form-data body.

    Deliberately stdlib-only (no `requests` dep, no `email.mime`
    convenience — those add newline-encoding surprises for binary
    payloads). RFC 2388 shape:

        --<boundary>\r\n
        Content-Disposition: form-data; name="<field>"\r\n\r\n
        <text-value>\r\n
        ...
        --<boundary>\r\n
        Content-Disposition: form-data; name="photo"; filename="<name>"\r\n
        Content-Type: <mime>\r\n\r\n
        <binary bytes>\r\n
        --<boundary>--\r\n

    Text field values are UTF-8-encoded verbatim; the caller is
    responsible for stringifying non-string values (chat_id, reply_to_
    message_id) before passing them in.

    Field NAMES and the `photo_filename` are validated against a small
    set of header-splitting bytes (`\\r`, `\\n`, `\\x00`, `"`) — any hit
    raises `ValueError` before a single byte is written. This module is
    the "single narrow HTTP boundary" per the top-of-file docstring, so
    the safety of the multipart layer must not depend on every future
    caller pre-sanitizing its inputs.
    """
    # Validate the caller-controlled strings that land inside a header
    # attribute BEFORE we start assembling bytes. A single `\r\n` in the
    # filename would splice a fake `Content-Type:` header (or worse) into
    # the request. `photo_mime` is caller-controlled too, but today it
    # only ever comes from `mimetypes.guess_type` (all-ASCII, no CR/LF),
    # so we validate it defensively too.
    _reject_header_splitting(photo_filename, field_role="photo_filename")
    _reject_header_splitting(photo_mime, field_role="photo_mime")

    buf = io.BytesIO()
    b_boundary = boundary.encode("ascii")

    for name, value in fields.items():
        _reject_header_splitting(str(name), field_role="field name")
        if value is None:
            # None means "field not set"; skip so Telegram sees only the
            # fields the caller actually wanted to send.
            continue
        buf.write(b"--" + b_boundary + b"\r\n")
        buf.write(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        buf.write(str(value).encode("utf-8"))
        buf.write(b"\r\n")

    # The photo binary part — always last so a caller reading the wire
    # capture sees the text fields first, then the (potentially large)
    # bytes.
    buf.write(b"--" + b_boundary + b"\r\n")
    buf.write(
        (
            f'Content-Disposition: form-data; name="photo"; '
            f'filename="{photo_filename}"\r\n'
        ).encode("utf-8")
    )
    buf.write(f"Content-Type: {photo_mime}\r\n\r\n".encode("utf-8"))
    buf.write(photo_bytes)
    buf.write(b"\r\n")

    buf.write(b"--" + b_boundary + b"--\r\n")
    return buf.getvalue()


def _make_boundary() -> str:
    """Return a unique multipart boundary, stable across a single send.

    A uuid4-derived hex is well under RFC 2046's 70-char cap and cannot
    collide with any bytes we or Telegram would put in an image payload
    (all hex, no `--`).
    """
    return f"mineruboundary{uuid.uuid4().hex}"


# --- HTTP transport --------------------------------------------------------


def _build_url(bot_token: str) -> str:
    """Return the sendPhoto URL for the given token.

    The token appears in the URL path by Telegram Bot API protocol design
    — that's how bots authenticate. This function is the ONLY place the
    token is joined into a URL string; keep it that way so future audits
    can grep for a single call site.
    """
    return f"https://{TELEGRAM_API_HOST}/bot{bot_token}/sendPhoto"


def _parse_response(response_bytes: bytes) -> tuple[Optional[int], Optional[str]]:
    """Extract (message_id, largest_photo_file_id) from a Telegram JSON body.

    Telegram's sendPhoto success shape (abridged):
        {"ok": true,
         "result": {
             "message_id": 42,
             "photo": [
                 {"file_id": "...", "width":  90, "height":  90, ...},
                 {"file_id": "...", "width": 320, "height": 320, ...},
                 {"file_id": "...", "width": 800, "height": 800, ...}
             ], ...}}

    The largest variant sits at the END of the `photo` list. We return
    its file_id for the cache to persist — that's the id `sendPhoto` will
    happily accept for a resend without a re-upload.

    Robust to shape drift: missing keys return (None, None) so a partial
    response never crashes the send-and-cache pipeline.
    """
    try:
        data = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict) or not data.get("ok"):
        return None, None
    result = data.get("result") or {}
    message_id = result.get("message_id") if isinstance(result, dict) else None
    photo = result.get("photo") if isinstance(result, dict) else None
    file_id: Optional[str] = None
    if isinstance(photo, list) and photo:
        largest = photo[-1]
        if isinstance(largest, dict):
            candidate = largest.get("file_id")
            if isinstance(candidate, str):
                file_id = candidate
    return (
        message_id if isinstance(message_id, int) else None,
        file_id,
    )


def _extract_retry_after(exc: urllib.error.HTTPError) -> Optional[int]:
    """Return the Retry-After header value (seconds) from a 429 response.

    Telegram's Retry-After is always a decimal integer number of seconds
    (never HTTP-date form). Missing / unparseable defaults to None, which
    the caller treats as "no hint; use its own backoff".
    """
    header = exc.headers.get("Retry-After") if exc.headers else None
    if not header:
        return None
    try:
        return int(str(header).strip())
    except ValueError:
        return None


def _send_once(
    *,
    url: str,
    body: bytes,
    content_type: str,
    timeout: float,
) -> tuple[Optional[int], Optional[str], Optional[str], Optional[int]]:
    """Perform one HTTP POST and classify the outcome.

    Returns a 4-tuple:
        (message_id, telegram_file_id, error_label, retry_after)

    On success both message_id/file_id are populated and error_label is
    None. On failure exactly one of the error paths sets error_label and
    the id fields stay None. `retry_after` is only ever set on 429.

    The `error_label` values are stable strings the caller (and cache
    layer) can branch on: 'rate_limited', 'server_error', 'http_<code>',
    'network', 'bad_response'. None of them ever include the token or
    the response body — a bad response could echo caption content
    verbatim, so we swallow the body and only surface the label.
    """
    request = urllib.request.Request(
        url=url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        # `urlopen` is the ONE network entry point of this module. Tests
        # monkeypatch it; leaks past this call site would bypass the
        # mock, so keep every HTTP action funneled through here.
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = resp.read()
        message_id, file_id = _parse_response(payload)
        if message_id is None:
            return None, None, "bad_response", None
        return message_id, file_id, None, None
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return None, None, "rate_limited", _extract_retry_after(exc)
        if 500 <= exc.code < 600:
            return None, None, "server_error", None
        # Do NOT surface exc.reason or the body — either could echo
        # caller-supplied content back at us.
        return None, None, f"http_{exc.code}", None
    except urllib.error.URLError:
        # DNS failure, connection refused, TLS handshake failure. Do NOT
        # surface exc.reason (may include the host string with the token
        # already stripped, but defence-in-depth: no error text at all).
        return None, None, "network", None


def _post_form_urlencoded(
    *,
    url: str,
    fields: dict,
    timeout: float,
) -> tuple[Optional[int], Optional[str], Optional[str], Optional[int]]:
    """POST a cached-file sendPhoto as application/x-www-form-urlencoded.

    Used only when `is_cached_id=True`: the payload is small (chat_id,
    photo=<file_id>, caption, ...), so multipart overhead is wasted.
    """
    encoded = urllib.parse.urlencode(
        {k: v for k, v in fields.items() if v is not None}
    ).encode("utf-8")
    return _send_once(
        url=url,
        body=encoded,
        content_type="application/x-www-form-urlencoded",
        timeout=timeout,
    )


def _post_multipart(
    *,
    url: str,
    fields: dict,
    photo_bytes: bytes,
    photo_filename: str,
    photo_mime: str,
    timeout: float,
) -> tuple[Optional[int], Optional[str], Optional[str], Optional[int]]:
    """POST a fresh-upload sendPhoto as multipart/form-data."""
    boundary = _make_boundary()
    body = _build_multipart_body(
        boundary=boundary,
        fields=fields,
        photo_bytes=photo_bytes,
        photo_filename=photo_filename,
        photo_mime=photo_mime,
    )
    return _send_once(
        url=url,
        body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
        timeout=timeout,
    )


# --- Public entry point ---------------------------------------------------


def send_photo(
    path_or_file_id: str,
    *,
    chat_id: Optional[Union[int, str]] = None,
    caption: Optional[str] = None,
    parse_mode: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
    is_cached_id: bool = False,
    secrets_resolver: Optional[SecretsResolver] = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> dict:
    """Send a photo to Telegram via the Bot API; return a structured result.

    ⚠️ OUTBOUND in production. Every dev/test caller MUST monkeypatch
    `urllib.request.urlopen`; a live invocation is a real send to the operator's
    Telegram chat via the configured bot token. See the module SAFETY
    block.

    Args:
        path_or_file_id: absolute path to an image file, OR (when
            `is_cached_id=True`) a Telegram `file_id` string returned by
            a previous send. The cache layer uses the file_id path for
            SHA256-based dedup.
        chat_id: overrides the `telegram-chat-id` secret. Accepts int or
            str (Telegram accepts both; groups start with `-100...`).
        caption: optional caption. The caller trims to
            TELEGRAM_CAPTION_MAX_CHARS if it exceeds; this function does
            not truncate.
        parse_mode: 'HTML' | 'MarkdownV2' | None (see Bot API).
        reply_to_message_id: optional threading reply target.
        is_cached_id: True => send as form-urlencoded with photo=<file_id>
            (no binary re-upload). False => read the file at
            `path_or_file_id` and send as multipart/form-data.
        secrets_resolver: injected resolver for tests. Production callers
            typically omit this and let the module build one from the
            active profile's SecretsConfig via `build_resolver()`.
        timeout: HTTP timeout in seconds.

    Returns:
        A dict shaped like `SendPhotoResult.as_dict()`. The cache layer
        will persist `telegram_file_id` + `message_id` on success and
        will re-schedule (with `retry_after` honored) on 429.

    Raises:
        typer.Exit(code=2): the bot token or chat id could not be
            resolved. Stderr already carries an actionable message
            naming the Keychain slot; the value is never echoed.
    """
    resolver = _resolver_or_default(secrets_resolver)
    bot_token = _resolve_bot_token(resolver)
    resolved_chat_id = _resolve_chat_id(resolver, chat_id)
    url = _build_url(bot_token)

    # Text-field payload shared across both transport shapes. Telegram
    # tolerates missing optional fields; `_build_multipart_body` and
    # `_post_form_urlencoded` both skip None-valued entries.
    fields: dict = {
        "chat_id": resolved_chat_id,
        "caption": caption,
        "parse_mode": parse_mode,
        "reply_to_message_id": reply_to_message_id,
    }

    if is_cached_id:
        # Cached-file fast path. photo carries the file_id string.
        fields["photo"] = path_or_file_id
        attempt_1 = _post_form_urlencoded(
            url=url, fields=fields, timeout=timeout
        )
    else:
        # Fresh upload path. Read bytes; hand to multipart assembler.
        photo_path = Path(path_or_file_id)
        if not photo_path.exists() or not photo_path.is_file():
            # Local filesystem miss is a caller bug, not a Telegram
            # error. Return an unambiguous label so the cache layer
            # doesn't cache a phantom entry.
            return SendPhotoResult(
                ok=False, error="local_file_missing"
            ).as_dict()
        photo_bytes = photo_path.read_bytes()
        photo_mime = _detect_mime(photo_path)
        photo_filename = photo_path.name
        attempt_1 = _post_multipart(
            url=url,
            fields=fields,
            photo_bytes=photo_bytes,
            photo_filename=photo_filename,
            photo_mime=photo_mime,
            timeout=timeout,
        )

    message_id, file_id, error, retry_after = attempt_1

    if error == "server_error":
        # One-shot 5xx retry. Same code path, same bytes, same URL.
        # No token / secret material in this log line — only the
        # resolver names appear if we chose to log at all.
        logger.info(
            "mineru telegram photo: 5xx from api.telegram.org, retrying once"
        )
        time.sleep(RETRY_5XX_SLEEP_SECONDS)
        if is_cached_id:
            attempt_2 = _post_form_urlencoded(
                url=url, fields=fields, timeout=timeout
            )
        else:
            attempt_2 = _post_multipart(
                url=url,
                fields=fields,
                photo_bytes=photo_bytes,  # noqa: F821 — set on the else branch above
                photo_filename=photo_filename,  # noqa: F821
                photo_mime=photo_mime,  # noqa: F821
                timeout=timeout,
            )
        message_id, file_id, error, retry_after = attempt_2

    return SendPhotoResult(
        ok=(error is None and message_id is not None),
        message_id=message_id,
        telegram_file_id=file_id,
        error=error,
        retry_after=retry_after,
    ).as_dict()


# --- Public re-exports for verb / cache callers ---------------------------

__all__ = [
    "TELEGRAM_API_HOST",
    "TELEGRAM_BOT_TOKEN_SECRET",
    "TELEGRAM_CAPTION_MAX_CHARS",
    "TELEGRAM_CHAT_ID_SECRET",
    "SendPhotoResult",
    "send_photo",
]
