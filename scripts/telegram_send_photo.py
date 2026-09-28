#!/usr/bin/env python3
"""Send a single photo + caption to the operator's Telegram chat via the Bot API.

Companion to `scripts/deliver-output.py` (which handles text-only briefs). Uses the
same Keychain-first credential path so we don't split a second copy of that logic.

Public entry points:
    send_photo(image_path, caption) -> bool           # actually POSTs to Telegram
    build_send_photo_request(image_path, caption, token, chat_id) -> PreparedRequest
        # returns (url, headers, multipart_body: bytes) WITHOUT sending — for tests.

Uses stdlib only: urllib + a hand-rolled multipart/form-data body. No `requests` dep.
"""

import json
import logging
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Reuse the credential loader from deliver-output.py so token / allowlist logic
# stays in exactly one place. The dashed filename means a normal `import` fails,
# so we go via importlib.
import importlib.util as _importlib_util
_SPEC = _importlib_util.spec_from_file_location(
    "deliver_output_module",
    Path(__file__).resolve().parent / "deliver-output.py",
)
_deliver_output = _importlib_util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_deliver_output)  # type: ignore[union-attr]
load_telegram_config = _deliver_output.load_telegram_config
keychain_get = _deliver_output.keychain_get

logger = logging.getLogger(__name__)

# Telegram sendPhoto: caption is 1024 chars max (vs 4096 for a text message).
TELEGRAM_PHOTO_CAPTION_LIMIT = 1024
TELEGRAM_PHOTO_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class PreparedPhotoRequest:
    """Everything needed to POST a Bot API sendPhoto call, minus the network I/O.

    Kept as a plain dataclass so tests can inspect every field before we ever
    actually hit Telegram — the "build vs send" split is the entire reason this
    module exists in v1 (we're gated from sending until the main session reviews).
    """
    url: str
    headers: dict[str, str]
    body: bytes
    boundary: str
    caption_used: str
    image_path: Path


def _truncate_caption(caption: str, limit: int = TELEGRAM_PHOTO_CAPTION_LIMIT) -> str:
    """Fit a caption to Telegram's 1024-char sendPhoto limit, adding an ellipsis if we trim."""
    if len(caption) <= limit:
        return caption
    return caption[: limit - 1].rstrip() + "…"


def _guess_mime(image_path: Path) -> str:
    """Best-effort MIME type for the image; default to image/png for the collage output."""
    guess, _ = mimetypes.guess_type(str(image_path))
    return guess or "image/png"


def _build_multipart_body(
    chat_id: str,
    caption: str,
    parse_mode: Optional[str],
    image_path: Path,
) -> tuple[bytes, str]:
    """Assemble a multipart/form-data body for sendPhoto. Returns (body_bytes, boundary)."""
    # `uuid` gives us a random boundary that will never appear inside a JPEG payload.
    boundary = f"----MineruBoundary{uuid.uuid4().hex}"
    line = b"\r\n"

    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(f"--{boundary}".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"))
        parts.append(b"")
        parts.append(value.encode("utf-8"))

    field("chat_id", chat_id)
    if caption:
        field("caption", caption)
    if parse_mode:
        field("parse_mode", parse_mode)

    # The photo file part: name="photo", filename=<basename>, Content-Type set explicitly.
    with open(image_path, "rb") as f:
        file_bytes = f.read()
    parts.append(f"--{boundary}".encode("utf-8"))
    # Telegram ignores the sent-side filename, so hardcode a safe constant instead of
    # interpolating image_path.name — a name containing " / CR / LF would otherwise
    # corrupt the Content-Disposition header and let a new header line be injected.
    parts.append(b'Content-Disposition: form-data; name="photo"; filename="collage.png"')
    parts.append(f"Content-Type: {_guess_mime(image_path)}".encode("utf-8"))
    parts.append(b"")
    parts.append(file_bytes)

    # Trailing boundary sentinel.
    parts.append(f"--{boundary}--".encode("utf-8"))
    parts.append(b"")

    body = line.join(parts)
    return body, boundary


def build_send_photo_request(
    image_path: Path,
    caption: str,
    token: str,
    chat_id: str,
    parse_mode: Optional[str] = "HTML",
) -> PreparedPhotoRequest:
    """Build the sendPhoto request WITHOUT sending. Used both by send_photo and by tests."""
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"image not found: {image_path}")

    caption_used = _truncate_caption(caption)
    body, boundary = _build_multipart_body(chat_id, caption_used, parse_mode, image_path)
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    return PreparedPhotoRequest(
        url=url,
        headers=headers,
        body=body,
        boundary=boundary,
        caption_used=caption_used,
        image_path=image_path,
    )


def send_photo(
    image_path: Path,
    caption: str,
    parse_mode: Optional[str] = "HTML",
) -> bool:
    """Actually POST the photo to Telegram. Returns True on success.

    This is the ONLY code path in this module that touches the network. Callers gated
    from sending (e.g. v1 of the collage feature) should use `build_send_photo_request`
    directly and inspect the PreparedPhotoRequest instead.
    """
    token, chat_id = load_telegram_config()
    prepared = build_send_photo_request(image_path, caption, token, chat_id, parse_mode=parse_mode)

    req = urllib.request.Request(prepared.url, data=prepared.body, headers=prepared.headers)
    try:
        with urllib.request.urlopen(req, timeout=TELEGRAM_PHOTO_TIMEOUT_SECONDS) as resp:
            payload = resp.read()
            result = json.loads(payload)
            if not result.get("ok"):
                logger.error("Telegram sendPhoto API error: %s", result)
                return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error("Telegram HTTP %s: %s", e.code, body)
        return False
    except urllib.error.URLError as e:
        logger.error("Telegram network error: %s", e.reason)
        return False
    logger.info("Delivered photo to chat %s (%d bytes, %d-char caption)", chat_id, len(prepared.body), len(prepared.caption_used))
    return True


def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Send a photo + caption to the operator's Telegram chat.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--caption", default="", help="Caption text; will be HTML-parsed by default.")
    parser.add_argument("--dry-run", action="store_true", help="Build the request, print stats, DO NOT send.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.dry_run:
        # Dry-run uses placeholder token+chat_id so it doesn't even touch Keychain —
        # useful for smoke-testing multipart formation on a machine without secrets.
        prepared = build_send_photo_request(args.image, args.caption, token="DUMMY", chat_id="0")
        print(f"URL:            {prepared.url}")
        print(f"Content-Type:   {prepared.headers['Content-Type']}")
        print(f"Content-Length: {prepared.headers['Content-Length']}")
        print(f"Caption chars:  {len(prepared.caption_used)}")
        return

    ok = send_photo(args.image, args.caption)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _cli()
