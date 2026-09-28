"""Persistent store for browser Web Push subscriptions.

One subscription per (browser + PWA install). Registered when the user taps the
"Enable notifications" control in the app; validated + deduplicated + persisted
here. `push_send.py` iterates this store to reach every enabled device, and
prunes entries on 404/410 (subscription gone).

On-disk shape (`$MINERU_APP_STATE_DIR/push-subscriptions.json`, 0600):

    {
      "subscriptions": [
        {
          "endpoint": "https://web.push.apple.com/...",
          "keys": {"p256dh": "<base64url>", "auth": "<base64url>"},
          "added": 1755727123.4
        },
        ...
      ]
    }

`endpoint` is the dedup key. Adding an existing endpoint refreshes `added` but
does NOT create a duplicate. The file is treated as sensitive (per-device push
secrets — `auth` is a 16-byte shared secret that decrypts every push to that
device) and never appears in log lines.

Mirrors seen_ledger.py's atomic-write + 0600 pattern.

Python 3.9-compatible. Stdlib only.
"""

import json
import logging
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import STATE_DIR


logger = logging.getLogger(__name__)


SUBSCRIPTIONS_PATH = STATE_DIR / "push-subscriptions.json"
FILE_MODE_0600 = stat.S_IRUSR | stat.S_IWUSR
STORE_LOCK = threading.RLock()

# Sanity caps. A pathological actor can't smuggle an unbounded blob through
# the endpoint (the dispatcher already caps request body at MAX_JSON_BODY_BYTES),
# but the per-field caps below reject anything structurally wrong before it
# touches the ledger.
MAX_ENDPOINT_LENGTH = 2048
# base64url encoded lengths (unpadded):
#   p256dh: 65 raw bytes → 87 chars → cap at 96 for headroom
#   auth:   16 raw bytes → 22 chars → cap at 32 for headroom
MIN_P256DH_LENGTH = 80
MAX_P256DH_LENGTH = 96
MIN_AUTH_LENGTH = 20
MAX_AUTH_LENGTH = 32

# Hard cap on total stored subscriptions. Real usage is 1-3 devices; anything
# past this is either a bug or an attack. FIFO drop of the oldest so a new
# add still succeeds.
MAX_SUBSCRIPTIONS = 32

BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def is_valid_endpoint(candidate: Any) -> bool:
    """True iff `candidate` looks like a valid Web Push endpoint.

    - Real subscription endpoints from Apple / FCM / Mozilla are always
      https:// URLs; the browser can't hand us anything else.
    - Loopback http:// endpoints (`http://127.0.0.1:PORT/...`,
      `http://localhost:PORT/...`) are accepted ONLY when the env var
      `MINERU_ALLOW_LOOPBACK_PUSH_ENDPOINTS=1` is set — which mock push-service
      tests set for themselves. Production defaults to https-only, so a stored
      subscription can never make push_send POST to a loopback service on the
      mini (blind-SSRF hardening; security review 2026-08-20).
    """
    if not isinstance(candidate, str):
        return False
    if not (0 < len(candidate) <= MAX_ENDPOINT_LENGTH):
        return False
    if candidate.startswith("https://"):
        return True
    if os.environ.get("MINERU_ALLOW_LOOPBACK_PUSH_ENDPOINTS") == "1" and (
        candidate.startswith("http://127.0.0.1:") or candidate.startswith("http://localhost:")
    ):
        return True
    return False


def is_valid_base64url_field(candidate: Any, min_length: int, max_length: int) -> bool:
    """True iff `candidate` is a base64url-alphabet string in the length window."""
    if not isinstance(candidate, str):
        return False
    if not (min_length <= len(candidate) <= max_length):
        return False
    return all(char in BASE64URL_ALPHABET for char in candidate)


def is_valid_subscription_shape(payload: Any) -> bool:
    """Full-shape validation of a browser PushSubscription JSON.

    Rejects anything that isn't `{endpoint: str, keys: {p256dh: str, auth: str}}`
    with all three strings matching our length/alphabet windows.
    """
    if not isinstance(payload, dict):
        return False
    endpoint = payload.get("endpoint")
    keys = payload.get("keys")
    if not is_valid_endpoint(endpoint):
        return False
    if not isinstance(keys, dict):
        return False
    if not is_valid_base64url_field(keys.get("p256dh"), MIN_P256DH_LENGTH, MAX_P256DH_LENGTH):
        return False
    if not is_valid_base64url_field(keys.get("auth"), MIN_AUTH_LENGTH, MAX_AUTH_LENGTH):
        return False
    return True


def atomic_write_json_600(target: Path, data: Any) -> None:
    """Same-dir NamedTemporaryFile + fsync + os.replace, chmod 600.

    Duplicated from unlock_gate rather than imported so a crashed unlock_gate
    can never take out this write path.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(STATE_DIR),
        prefix=target.stem + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.chmod(tmp.name, FILE_MODE_0600)
    os.replace(tmp.name, target)


def load_subscriptions() -> List[Dict[str, Any]]:
    """Return the list of stored subscriptions, or [] if the file is missing.

    Per-item validation drops anything malformed rather than raising, so a
    hand-edited or partially-corrupt file doesn't break sends across the board.
    """
    with STORE_LOCK:
        if not SUBSCRIPTIONS_PATH.exists():
            return []
        try:
            with open(SUBSCRIPTIONS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as read_error:
            logger.warning("push-subscriptions.json unreadable, treating as empty: %s", read_error)
            return []
        if not isinstance(data, dict):
            return []
        raw = data.get("subscriptions")
        if not isinstance(raw, list):
            return []
        clean: List[Dict[str, Any]] = []
        for entry in raw:
            if not is_valid_subscription_shape(entry):
                continue
            added = entry.get("added")
            if not isinstance(added, (int, float)):
                added = 0.0
            clean.append({
                "endpoint": entry["endpoint"],
                "keys": {"p256dh": entry["keys"]["p256dh"], "auth": entry["keys"]["auth"]},
                "added": float(added),
            })
        return clean


def save_subscriptions(subscriptions: List[Dict[str, Any]]) -> None:
    """Atomic 0600 write of the full subscription list."""
    atomic_write_json_600(SUBSCRIPTIONS_PATH, {"subscriptions": subscriptions})


def add_subscription(subscription: Dict[str, Any]) -> int:
    """Insert or refresh `subscription`, return the new total count.

    Deduplication is by endpoint: adding an already-present endpoint replaces
    the entry (fresh `added` timestamp; auth/p256dh may have been re-issued by
    the browser on re-subscribe). If the store is at MAX_SUBSCRIPTIONS the
    oldest entry is FIFO-dropped so the new add still succeeds.

    Caller MUST have already run `is_valid_subscription_shape(subscription)`.
    """
    with STORE_LOCK:
        current = load_subscriptions()
        endpoint = subscription["endpoint"]
        # Drop any existing entry for this endpoint; the fresh one wins.
        current = [entry for entry in current if entry["endpoint"] != endpoint]
        entry = {
            "endpoint": endpoint,
            "keys": {
                "p256dh": subscription["keys"]["p256dh"],
                "auth": subscription["keys"]["auth"],
            },
            "added": time.time(),
        }
        current.append(entry)
        # FIFO cap. Sorted by `added` so the oldest goes first.
        if len(current) > MAX_SUBSCRIPTIONS:
            current.sort(key=lambda item: item.get("added", 0.0))
            current = current[-MAX_SUBSCRIPTIONS:]
        save_subscriptions(current)
        return len(current)


def remove_subscription(endpoint: str) -> Optional[int]:
    """Drop the subscription with `endpoint`. Returns new total count, or None if absent.

    Used by the /api/push/unsubscribe endpoint AND by push_send.py when a
    push service returns 404/410 (subscription expired / user disabled it).
    """
    if not is_valid_endpoint(endpoint):
        return None
    with STORE_LOCK:
        current = load_subscriptions()
        remaining = [entry for entry in current if entry["endpoint"] != endpoint]
        if len(remaining) == len(current):
            return None
        save_subscriptions(remaining)
        return len(remaining)


def count_subscriptions() -> int:
    """Return the current subscription total (post-validation)."""
    with STORE_LOCK:
        return len(load_subscriptions())
