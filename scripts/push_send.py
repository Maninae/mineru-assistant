#!/usr/bin/env python3
"""Send a Web Push notification to every stored subscription.

CLI:
    push_send.py --title "..." --body "..." [--url "#brief/feed/file.md"] [--tag "..."]

Loads the VAPID private key from macOS Keychain, walks the persisted
subscription list, encrypts one aes128gcm record per subscription (fresh
ephemeral EC key + random salt each), signs one VAPID JWT per subscription
(aud = scheme+host of that subscription's endpoint), and POSTs to the push
service. On 404/410 the subscription is dropped from the store — the browser
has told us it's gone.

Exit codes:
    0 — at least attempted (even if every attempt failed). Non-zero exit would
        cause the cron delivery wrapper to alert on a benign "no devices" state.
    1 — usage error, missing VAPID key, or a fatal load-time failure that
        prevented ANY attempt (crypto module import failed, subscriptions
        file corrupt in a way we can't parse at all).

The push payload is a small JSON blob the service worker consumes:
    {"title": "...", "body": "...", "url": "#brief/...", "tag": "..."}

Contents are non-sensitive on purpose: title + one-line teaser + deep-link
hash. The full brief stays behind the gated app. Payload is truncated to the
push-service 4 KB limit if a caller ever supplies more.

Nothing about the payload, the VAPID JWT, the CEK, the nonce, or the auth
secret is ever logged. Only counts + status codes + endpoint scheme+host go
into the per-run summary on stderr.

Python 3.9-compatible. Requires the already-installed `cryptography` (49.0.0).
"""

import argparse
import base64
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# App modules ship with the workspace; keep imports at top per PYTHON_STYLE.md.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import push_subscriptions   # noqa: E402
import webpush_crypto   # noqa: E402

from cryptography.hazmat.primitives import serialization   # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec   # noqa: E402


logger = logging.getLogger("push_send")


# --- Keychain wiring (must match gen-vapid.py exactly) -----------------------

# Keychain service name is the WebPush VAPID slot. On a multi-tenant Mac
# the SLOT name is the same across profiles; the per-profile isolation
# happens on the ACCOUNT axis, which reads from `MINERU_KEYCHAIN_ACCOUNT`
# (default `"mineru"` for the framework namespace). Fix for the step-5
# audit's Finding 10: without this env seam, every profile's Web Push
# signed with the OWNER's VAPID key and pushed to the OWNER's subscribers.
KEYCHAIN_SERVICE = "mineru-webpush-vapid-private"
KEYCHAIN_ACCOUNT = os.environ.get("MINERU_KEYCHAIN_ACCOUNT", "mineru")
SECURITY_BINARY = "/usr/bin/security"
KEYCHAIN_TIMEOUT_SECONDS = 5

# Public key + subscriptions state live under APP_DIR. Fix for Finding 10:
# read APP_DIR off `MINERU_HOME` so a non-owner profile's push signs with
# its OWN VAPID key and pushes to its OWN subscribers. Default keeps the
# in-repo layout working (this file's parent-of-parent is the repo root,
# which contains `app/`) so local dev + tests need no env override.
def _resolve_app_dir() -> Path:
    """Compute APP_DIR from MINERU_HOME, falling back to file-relative."""
    home = os.environ.get("MINERU_HOME")
    if home:
        return Path(home).expanduser() / "app"
    return Path(__file__).resolve().parent.parent / "app"


APP_DIR = _resolve_app_dir()
# Runtime state lives outside the code tree; app/config.py uses the same rule.
STATE_DIR = Path(
    os.environ.get(
        "MINERU_APP_STATE_DIR",
        str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))).expanduser() / "app-state"),
    )
)
VAPID_PUBLIC_KEY_PATH = STATE_DIR / "vapid-public.txt"

# --- VAPID JWT parameters (RFC 8292) -----------------------------------------

# `sub` per RFC 8292 §2.1 — a `mailto:` or an `https:` contact URI the push
# service can use to reach the operator. Env-driven so each install ships its
# own contact; the default is a neutral placeholder, never a real address.
VAPID_JWT_SUB = os.environ.get("MINERU_WEBPUSH_CONTACT", "mailto:admin@example.com")
# 12 hours: well inside RFC 8292's 24h cap; comfortably wider than any single
# push send's flight time so a burst of retries against a slow endpoint won't
# expire the JWT mid-loop.
VAPID_JWT_LIFETIME_SECONDS = 12 * 60 * 60

# --- Web Push send parameters ------------------------------------------------

# Max size of the JSON payload we hand to encrypt_aes128gcm_web_push. Well
# above the ~200 byte title+teaser+url we actually send; leaves the record
# size (4096) with ample headroom for future longer titles.
MAX_PLAINTEXT_BYTES = 2048
# Per-request TTL in seconds. If the phone is offline for longer than this
# the push service drops the message; a fresh brief supersedes a stale one
# anyway, so 24h keeps offline devices covered without hoarding stale notifs.
PUSH_TTL_SECONDS = 24 * 60 * 60
PUSH_URGENCY = "normal"

REQUEST_TIMEOUT_SECONDS = 10

# 201 Created is the RFC 8030 success. Some services still return 200/202 on
# accepted; treat any 2xx as success and let the diagnostics carry the exact.
SUBSCRIPTION_GONE_STATUSES = (404, 410)


# --- Keychain read (VAPID private) -------------------------------------------

def read_vapid_private_key_from_keychain() -> Optional[ec.EllipticCurvePrivateKey]:
    """Load the VAPID private key stored by `app/deploy/gen-vapid.py`.

    Returns None if the Keychain item is absent. Raises RuntimeError on any
    ambiguous failure (subprocess error, malformed value) so callers fail loud.
    """
    result = subprocess.run(
        [SECURITY_BINARY, "find-generic-password",
         "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
        check=False, capture_output=True, text=True,
        timeout=KEYCHAIN_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        if "could not be found" in (result.stderr or "").lower():
            return None
        raise RuntimeError(f"security find-generic-password failed: exit {result.returncode}")
    try:
        der_pkcs8 = base64.b64decode(result.stdout.strip())
    except (ValueError, base64.binascii.Error) as decode_error:
        raise RuntimeError(f"stored VAPID key is not valid base64: {decode_error}")
    return serialization.load_der_private_key(der_pkcs8, password=None)


def read_vapid_public_key_text() -> str:
    """Return the on-disk VAPID public key (base64url), or '' if missing."""
    if not VAPID_PUBLIC_KEY_PATH.exists():
        return ""
    return VAPID_PUBLIC_KEY_PATH.read_text(encoding="ascii").strip()


# --- Push payload assembly ---------------------------------------------------

def build_push_payload_bytes(title: str, body: str, url: Optional[str],
                             tag: Optional[str]) -> bytes:
    """Serialize the {title, body, url, tag} payload the service worker consumes.

    Only non-empty fields are emitted. If the total exceeds MAX_PLAINTEXT_BYTES
    the body is truncated (title / url / tag stay intact so the deep-link
    still works) and an ellipsis is appended.
    """
    payload: Dict[str, str] = {"title": title, "body": body}
    if url:
        payload["url"] = url
    if tag:
        payload["tag"] = tag
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= MAX_PLAINTEXT_BYTES:
        return encoded
    # Slack out of the body (never the title/url/tag) until the whole payload
    # fits. Each unicode-safe truncate is a byte-window shrink; re-encode to
    # confirm the result still fits before returning.
    overhead = len(encoded) - len(body.encode("utf-8"))
    room = max(0, MAX_PLAINTEXT_BYTES - overhead - 3)   # -3 for the ellipsis
    truncated_body = body.encode("utf-8")[:room].decode("utf-8", errors="ignore") + "..."
    payload["body"] = truncated_body
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# --- Per-subscription send ---------------------------------------------------

def compute_vapid_aud_for_endpoint(endpoint_url: str) -> str:
    """RFC 8292 §2: `aud` MUST be scheme+host of the subscription endpoint.

    E.g. `https://web.push.apple.com/QaB...` → `https://web.push.apple.com`.
    """
    split = urllib.parse.urlsplit(endpoint_url)
    if not split.scheme or not split.netloc:
        raise ValueError(f"malformed subscription endpoint: {endpoint_url!r}")
    return f"{split.scheme}://{split.netloc}"


def build_request_for_subscription(
    subscription: Dict[str, Any],
    plaintext: bytes,
    vapid_private_key: ec.EllipticCurvePrivateKey,
    vapid_public_b64url: str,
) -> urllib.request.Request:
    """Build the ready-to-send urllib Request for one subscription.

    Fresh ephemeral EC key + fresh 16-byte salt + fresh VAPID JWT per call —
    all short-lived and per-message per RFC 8291 / 8292.
    """
    endpoint = subscription["endpoint"]
    ua_public = webpush_crypto.b64url_decode(subscription["keys"]["p256dh"])
    ua_auth = webpush_crypto.b64url_decode(subscription["keys"]["auth"])

    application_ephemeral_key = ec.generate_private_key(ec.SECP256R1())
    salt = secrets.token_bytes(16)
    encrypted_body = webpush_crypto.encrypt_aes128gcm_web_push(
        plaintext=plaintext,
        ua_public_uncompressed=ua_public,
        ua_auth_secret=ua_auth,
        application_server_private=application_ephemeral_key,
        salt=salt,
    )

    aud = compute_vapid_aud_for_endpoint(endpoint)
    exp = int(time.time()) + VAPID_JWT_LIFETIME_SECONDS
    jwt = webpush_crypto.sign_vapid_jwt(
        aud=aud, sub=VAPID_JWT_SUB, exp_epoch=exp,
        application_server_private=vapid_private_key,
    )
    authorization_header = webpush_crypto.build_vapid_authorization_header(
        jwt_compact=jwt,
        application_server_public_b64url=vapid_public_b64url,
    )

    return urllib.request.Request(
        endpoint,
        data=encrypted_body,
        method="POST",
        headers={
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(encrypted_body)),
            "TTL": str(PUSH_TTL_SECONDS),
            "Urgency": PUSH_URGENCY,
            "Authorization": authorization_header,
        },
    )


def send_one(subscription: Dict[str, Any], plaintext: bytes,
             vapid_private_key: ec.EllipticCurvePrivateKey,
             vapid_public_b64url: str) -> Tuple[int, str]:
    """Send one push. Returns (http_status, short_diagnostic).

    Never raises: all urllib exceptions are caught and mapped to a status +
    diagnostic. Callers loop over subscriptions and never let one bad service
    kill the run.
    """
    endpoint_host = urllib.parse.urlsplit(subscription["endpoint"]).netloc or "?"
    try:
        request = build_request_for_subscription(
            subscription, plaintext, vapid_private_key, vapid_public_b64url,
        )
    except (ValueError, TypeError) as prep_error:
        return 0, f"[{endpoint_host}] request prep failed: {prep_error}"

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return resp.status, f"[{endpoint_host}] {resp.status}"
    except urllib.error.HTTPError as http_error:
        # The push service returned a body — read a short excerpt for diagnostics.
        # Never log the payload we sent (title/body/url), only the service's
        # complaint. Excerpt cap keeps a hostile service from spamming logs.
        try:
            excerpt = http_error.read(200).decode("utf-8", errors="replace")
        except Exception:
            excerpt = ""
        return http_error.code, f"[{endpoint_host}] {http_error.code} {excerpt.strip()!r}"
    except urllib.error.URLError as url_error:
        return 0, f"[{endpoint_host}] network error: {url_error.reason}"
    except Exception as unexpected_error:   # last-resort guard so one bad send doesn't kill the loop
        return 0, f"[{endpoint_host}] unexpected: {unexpected_error!r}"


# --- Top-level orchestration -------------------------------------------------

def send_all(title: str, body: str, url: Optional[str], tag: Optional[str]) -> int:
    """Send `title/body/url/tag` to every stored subscription. Returns exit code."""
    logging.basicConfig(
        format="%(asctime)s push_send %(levelname)s %(message)s", level=logging.INFO,
        stream=sys.stderr,
    )

    try:
        vapid_private_key = read_vapid_private_key_from_keychain()
    except RuntimeError as keychain_error:
        logger.error("VAPID keychain read failed: %s", keychain_error)
        return 1
    if vapid_private_key is None:
        logger.error("VAPID private key not in Keychain — run app/deploy/gen-vapid.py first")
        return 1

    vapid_public_b64url = read_vapid_public_key_text()
    if not vapid_public_b64url:
        logger.error("VAPID public key file missing — run app/deploy/gen-vapid.py first")
        return 1

    subscriptions = push_subscriptions.load_subscriptions()
    if not subscriptions:
        logger.info("no subscriptions stored; nothing to send")
        return 0

    plaintext = build_push_payload_bytes(title=title, body=body, url=url, tag=tag)
    logger.info("sending to %d subscription(s) (payload=%d bytes)",
                len(subscriptions), len(plaintext))

    ok = 0
    gone = 0
    other = 0
    for subscription in subscriptions:
        status, diagnostic = send_one(
            subscription, plaintext, vapid_private_key, vapid_public_b64url,
        )
        logger.info("send: %s", diagnostic)
        if 200 <= status < 300:
            ok += 1
        elif status in SUBSCRIPTION_GONE_STATUSES:
            gone += 1
            new_count = push_subscriptions.remove_subscription(subscription["endpoint"])
            logger.info("pruned expired subscription (status %d), %s remain",
                        status, new_count if new_count is not None else "?")
        else:
            other += 1
    logger.info("summary: %d ok, %d gone (pruned), %d other", ok, gone, other)
    return 0


def parse_args(argv: List[str]) -> argparse.Namespace:
    """CLI: --title / --body required, --url / --tag optional."""
    parser = argparse.ArgumentParser(description="Send a Web Push notification to every stored subscription.")
    parser.add_argument("--title", required=True, help="Notification title.")
    parser.add_argument("--body", required=True, help="One-line teaser / body text.")
    parser.add_argument("--url", default=None, help="Deep-link URL fragment, e.g. '#brief/morning/2026-08-20.md'.")
    parser.add_argument("--tag", default=None,
                        help="Notification tag — replaces an earlier notification with the same tag on the device.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return send_all(title=args.title, body=args.body, url=args.url, tag=args.tag)


if __name__ == "__main__":
    sys.exit(main())
