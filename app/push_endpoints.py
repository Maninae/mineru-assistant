"""HTTP handlers for the `/api/push/*` endpoints.

Three routes:

  GET  /api/push/vapid-key    → the public VAPID key (base64url) the client
                                 hands to `pushManager.subscribe`.
  POST /api/push/subscribe    → persist a browser PushSubscription JSON.
  POST /api/push/unsubscribe  → drop a stored subscription by its endpoint.

All three sit behind the same posture as the rest of the app: the gate (if
enabled) + Host allowlist checks run first in the dispatcher; the two POSTs
additionally get the Content-Type=application/json + Origin write-guard via
`wants_body=True` in the route table.

This module owns NO crypto. Storage is `push_subscriptions.py`; the sender
lives in `scripts/push_send.py` (server-side, off-request-path).

Python 3.9-compatible. Stdlib only.
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, Tuple

from config import STATE_DIR
import push_subscriptions
from http_helpers import (
    DEFAULT_TEXT_ENCODING,
    MAX_JSON_BODY_BYTES,
    error_response,
    json_response,
)


logger = logging.getLogger(__name__)


VAPID_PUBLIC_KEY_PATH = STATE_DIR / "vapid-public.txt"


def read_vapid_public_key_text() -> str:
    """Return the on-disk VAPID public key (base64url), stripped of whitespace.

    Empty string if the file is missing. The endpoint below turns that into a
    503 so a caller sees "gen-vapid.py hasn't been run" clearly rather than a
    silently-broken subscribe flow.
    """
    if not VAPID_PUBLIC_KEY_PATH.exists():
        return ""
    try:
        return VAPID_PUBLIC_KEY_PATH.read_text(encoding="ascii").strip()
    except OSError as read_error:
        logger.warning("vapid-public.txt read refused (errno %s)", read_error.errno)
        return ""


# --- GET /api/push/vapid-key --------------------------------------------------

def handle_push_vapid_key(_match: re.Match, _query: Dict) -> Tuple[int, Dict[str, str], bytes]:
    """Return `{"key": "<base64url-uncompressed-P256-public>"}`.

    503 when the on-disk key is missing (deployment step not run yet). Same
    gate + Host posture as every other authenticated endpoint — the router
    ensures locked callers never reach here without a valid unlock cookie.
    """
    key_text = read_vapid_public_key_text()
    if not key_text:
        return error_response(503, "vapid public key not provisioned")
    return json_response({"key": key_text})


# --- POST /api/push/subscribe -------------------------------------------------

def handle_push_subscribe(
    _match: re.Match, _query: Dict, body: bytes, host: str = None,
) -> Tuple[int, Dict[str, str], bytes]:
    """Persist a browser PushSubscription JSON.

    Body shape (browser-native `pushManager.subscribe(...)` result):

        {"endpoint": "https://...", "keys": {"p256dh": "<b64url>", "auth": "<b64url>"}}

    Extra top-level fields are ignored (browsers may include `expirationTime`).
    Full shape/length/alphabet validation lives in `push_subscriptions.py`;
    anything malformed → 400.
    """
    if len(body) > MAX_JSON_BODY_BYTES:
        return error_response(413, "body too large")
    try:
        payload = json.loads(body.decode(DEFAULT_TEXT_ENCODING) or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response(400, "bad json")
    if not push_subscriptions.is_valid_subscription_shape(payload):
        return error_response(400, "bad subscription shape")
    count = push_subscriptions.add_subscription(payload)
    # Log the count only — never the endpoint host, never the keys (auth is a
    # per-device shared secret that decrypts every push to that device).
    logger.info("push: subscription added, %d total", count)
    return json_response({"ok": True, "count": count})


# --- POST /api/push/unsubscribe -----------------------------------------------

def handle_push_unsubscribe(
    _match: re.Match, _query: Dict, body: bytes, host: str = None,
) -> Tuple[int, Dict[str, str], bytes]:
    """Drop a stored subscription.

    Body shape: `{"endpoint": "https://..."}`. Idempotent — an unknown endpoint
    still returns 200 with `removed: false` so the client's "I'm done" call
    doesn't error just because we already pruned it after a 410 from the
    push service.
    """
    if len(body) > MAX_JSON_BODY_BYTES:
        return error_response(413, "body too large")
    try:
        payload = json.loads(body.decode(DEFAULT_TEXT_ENCODING) or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response(400, "bad json")
    if not isinstance(payload, dict):
        return error_response(400, "expected object body")
    endpoint = payload.get("endpoint")
    if not push_subscriptions.is_valid_endpoint(endpoint):
        return error_response(400, "bad endpoint")
    new_count = push_subscriptions.remove_subscription(endpoint)
    if new_count is None:
        return json_response({"ok": True, "removed": False,
                              "count": push_subscriptions.count_subscriptions()})
    logger.info("push: subscription removed, %d total", new_count)
    return json_response({"ok": True, "removed": True, "count": new_count})
