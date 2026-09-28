#!/usr/bin/env python3
"""Generate the assistant web-app VAPID keypair (one-time).

Web Push identifies the application server (this app) via VAPID (RFC 8292):
a P-256 keypair whose public key is baked into the browser subscription and
whose private key signs a short-lived JWT on every push send. This script
mints that keypair, stores the private key in the macOS Keychain, and writes
the public key (uncompressed-point, base64url) to $MINERU_APP_STATE_DIR/vapid-public.txt (default $MINERU_HOME/app-state).

Idempotent: if the Keychain already holds a webpush-vapid-private Keychain
item AND the on-disk public matches its derivative, we do nothing. A missing
public file with a Keychain entry gets rewritten from the private key so the
two stay in sync.

Verification (runs unconditionally): reads the private key back from Keychain,
re-derives the public key, and asserts it matches the on-disk public bytes.
This proves the Keychain round-trip works and the two halves belong together
before the crypto pipeline ever depends on them.

Storage:
  Private:  Keychain (service from $WEBPUSH_KEYCHAIN_SERVICE, account from $WEBPUSH_KEYCHAIN_ACCOUNT).
            Value is the DER-encoded PKCS8 private key, base64-encoded (so the
            `security` CLI, which wants a text-shaped `-w` value, is happy).
  Public:   $MINERU_APP_STATE_DIR/vapid-public.txt (default $MINERU_HOME/app-state), mode 0644 (public info, safe to serve).
            Value is the uncompressed P-256 point (65 bytes, leading 0x04),
            encoded base64url without padding, one line, no trailing newline.

Python 3.9-compatible. Requires the already-installed `cryptography` package
(49.0.0) — no new pip install.
"""

import base64
import os
import stat
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


KEYCHAIN_SERVICE = os.environ.get(
    "WEBPUSH_KEYCHAIN_SERVICE", "mineru-webpush-vapid-private"
)
KEYCHAIN_ACCOUNT = os.environ.get("WEBPUSH_KEYCHAIN_ACCOUNT", "mineru")
SECURITY_BINARY = "/usr/bin/security"
KEYCHAIN_TIMEOUT_SECONDS = 5

MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))).expanduser()
# Runtime state lives outside the code tree (app/ is a symlink into the engine checkout).
STATE_DIR = Path(os.environ.get("MINERU_APP_STATE_DIR", str(MINERU_HOME / "app-state")))
PUBLIC_KEY_PATH = STATE_DIR / "vapid-public.txt"
PUBLIC_KEY_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH


def b64url_no_pad(raw: bytes) -> str:
    """base64url-encode without trailing `=` padding — the JOSE / Web Push convention."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def read_private_key_from_keychain():
    """Return the stored EC private key object, or None if the Keychain item is missing.

    Raises RuntimeError on any ambiguous failure — never returns a partial or
    malformed value silently.
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
        raise RuntimeError(f"security find-generic-password failed: {result.stderr!r}")
    try:
        der_pkcs8 = base64.b64decode(result.stdout.strip())
    except (ValueError, base64.binascii.Error) as decode_error:
        raise RuntimeError(f"stored VAPID private key is not valid base64: {decode_error}")
    return serialization.load_der_private_key(der_pkcs8, password=None)


def write_private_key_to_keychain(private_key) -> None:
    """Store the EC private key in the Keychain as PKCS8-DER, base64-encoded.

    Uses `-U` so a rerun (should never happen with the idempotence guard, but
    just in case) updates the existing item instead of failing with "already
    exists".
    """
    der_pkcs8 = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    b64_text = base64.b64encode(der_pkcs8).decode("ascii")
    result = subprocess.run(
        [SECURITY_BINARY, "add-generic-password",
         "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE,
         "-w", b64_text, "-U",
         "-D", "assistant VAPID (Web Push)",
         "-j", "Application server key for the assistant web app's Web Push sender."],
        check=False, capture_output=True, text=True,
        timeout=KEYCHAIN_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"security add-generic-password failed: {result.stderr!r}")


def uncompressed_public_bytes(private_key) -> bytes:
    """Return the 65-byte uncompressed P-256 point (leading 0x04) for the public key."""
    public_key = private_key.public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )


def write_public_key_file(public_b64url: str) -> None:
    """Write the base64url public key to $MINERU_APP_STATE_DIR/vapid-public.txt (default $MINERU_HOME/app-state) (0644, one line)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = PUBLIC_KEY_PATH.with_suffix(PUBLIC_KEY_PATH.suffix + ".tmp")
    tmp_path.write_text(public_b64url, encoding="ascii")
    os.chmod(tmp_path, PUBLIC_KEY_MODE)
    os.replace(tmp_path, PUBLIC_KEY_PATH)


def load_public_key_file() -> str:
    """Return the on-disk public key as a base64url string, or '' if missing."""
    if not PUBLIC_KEY_PATH.exists():
        return ""
    return PUBLIC_KEY_PATH.read_text(encoding="ascii").strip()


def main() -> int:
    existing = read_private_key_from_keychain()

    if existing is None:
        print("[gen-vapid] no existing key, generating P-256 keypair", file=sys.stderr)
        private_key = ec.generate_private_key(ec.SECP256R1())
        write_private_key_to_keychain(private_key)
    else:
        print("[gen-vapid] existing key in Keychain, reusing", file=sys.stderr)
        private_key = existing

    public_b64url = b64url_no_pad(uncompressed_public_bytes(private_key))

    on_disk = load_public_key_file()
    if on_disk != public_b64url:
        print(f"[gen-vapid] writing public key to {PUBLIC_KEY_PATH}", file=sys.stderr)
        write_public_key_file(public_b64url)
    else:
        print(f"[gen-vapid] public key already on disk matches Keychain private", file=sys.stderr)

    # Verification round-trip: fresh Keychain read → re-derive public → assert
    # equality against on-disk. Proves both halves belong together and the
    # Keychain path works end-to-end.
    verify_key = read_private_key_from_keychain()
    if verify_key is None:
        print("[gen-vapid] FAIL: Keychain returned None immediately after store", file=sys.stderr)
        return 2
    verify_public = b64url_no_pad(uncompressed_public_bytes(verify_key))
    if verify_public != load_public_key_file():
        print("[gen-vapid] FAIL: round-trip public key does not match on-disk", file=sys.stderr)
        return 2
    if verify_public != public_b64url:
        print("[gen-vapid] FAIL: fresh Keychain read yields different public key", file=sys.stderr)
        return 2

    print(f"[gen-vapid] OK, public key ({len(public_b64url)} chars b64url): {public_b64url}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
