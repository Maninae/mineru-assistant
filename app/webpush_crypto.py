"""Web Push crypto primitives — VAPID (RFC 8292) + Message Encryption (RFC 8291).

Pure functions, no I/O. The caller (scripts/push_send.py, tests) supplies keys
and subscription material; this module returns bytes / headers ready for the
POST to the push service endpoint.

Two pieces:

  - `sign_vapid_jwt(...)`: ES256-sign a `{aud, exp, sub}` JWT with the VAPID
    private key. Returns the compact-serialization JWT string. Used to build
    the `Authorization: vapid t=<jwt>,k=<pub>` header.

  - `encrypt_aes128gcm_web_push(...)`: RFC 8291 payload encryption. Given the
    subscription's p256dh + auth, an application-server ephemeral EC private
    key, a 16-byte salt, and the plaintext bytes, returns the full aes128gcm
    record body (salt || rs || idlen || ephemeral_pub || ciphertext).

Correctness is verified against the RFC 8291 §5 known-answer test vector in
`tests/test_webpush_rfc8291.py` — that test is the primary proof that the
implementation matches every other Web Push sender in the world byte-for-byte.

Python 3.9-compatible. Uses `cryptography` (>=42, capped in pyproject.toml).
"""

import base64
import json
import struct
from typing import Tuple

from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand


# --- Constants (RFC 8291 + RFC 8188) -----------------------------------------

# aes128gcm content-encoding header fields.
AES128GCM_HEADER_RECORD_SIZE_BYTES = 4    # big-endian uint32
AES128GCM_HEADER_IDLEN_BYTES = 1
AES128GCM_KEY_BYTES = 16
AES128GCM_NONCE_BYTES = 12
AES128GCM_TAG_BYTES = 16

# Uncompressed P-256 public key: 0x04 || X (32) || Y (32) = 65 bytes.
UNCOMPRESSED_P256_POINT_BYTES = 65

# Default record size advertised in the header. Matches the RFC 8291 §5 test
# vector; well above our real payload (a title + short teaser, < 200 bytes).
DEFAULT_RECORD_SIZE = 4096

# Info strings for the two HKDF stages. Trailing NUL is mandated by RFC 8291
# §3.4 (WebPush: info) and RFC 8188 §2.2 (Content-Encoding: aes128gcm / nonce).
WEBPUSH_INFO_LABEL = b"WebPush: info\x00"
AES128GCM_CEK_INFO = b"Content-Encoding: aes128gcm\x00"
AES128GCM_NONCE_INFO = b"Content-Encoding: nonce\x00"

# Padding delimiter for the LAST record (0x02) per RFC 8188 §2. Non-last
# records use 0x01; Web Push always has exactly one record so 0x02 always.
LAST_RECORD_PADDING_DELIMITER = b"\x02"

# HKDF-Extract output length for SHA-256.
HKDF_SHA256_PRK_BYTES = 32

# RFC 8292 caps `exp` at 24h in the future. We ship 12h so a caller's clock
# skew has plenty of headroom either side.
VAPID_JWT_MAX_LIFETIME_SECONDS = 12 * 60 * 60


# --- base64url helpers -------------------------------------------------------

def b64url_encode_no_pad(raw: bytes) -> str:
    """base64url-encode without trailing `=` padding (JOSE / Web Push convention)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """base64url-decode, restoring stripped `=` padding."""
    if isinstance(text, bytes):
        text = text.decode("ascii")
    padding = (-len(text)) % 4
    return base64.urlsafe_b64decode(text + ("=" * padding))


# --- HKDF (RFC 5869) — extract-then-expand ------------------------------------
#
# The `cryptography` API exposes HKDF-Expand as `HKDFExpand`, but the Extract
# step is a plain HMAC-SHA-256(salt, IKM). We implement it directly rather than
# using `HKDF` (which does extract+expand in one call) so the intermediate PRK
# is available separately: aes128gcm derives BOTH the CEK and the NONCE from
# the same PRK, so we extract once and expand twice.

def hkdf_extract_sha256(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract with SHA-256. Returns 32-byte PRK."""
    if not salt:
        salt = b"\x00" * HKDF_SHA256_PRK_BYTES
    mac = hmac.HMAC(salt, hashes.SHA256())
    mac.update(ikm)
    return mac.finalize()


def hkdf_expand_sha256(prk: bytes, info: bytes, length: int) -> bytes:
    """HKDF-Expand with SHA-256. Returns `length` bytes of output keying material."""
    return HKDFExpand(algorithm=hashes.SHA256(), length=length, info=info).derive(prk)


# --- P-256 key helpers -------------------------------------------------------

def load_p256_public_from_uncompressed(uncompressed_bytes: bytes) -> ec.EllipticCurvePublicKey:
    """Load a P-256 public key from its 65-byte uncompressed X9.62 encoding."""
    if len(uncompressed_bytes) != UNCOMPRESSED_P256_POINT_BYTES:
        raise ValueError(
            f"expected {UNCOMPRESSED_P256_POINT_BYTES}-byte uncompressed P-256 point, "
            f"got {len(uncompressed_bytes)}"
        )
    if uncompressed_bytes[0] != 0x04:
        raise ValueError("uncompressed P-256 point must start with 0x04")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), uncompressed_bytes)


def load_p256_private_from_scalar(scalar_bytes: bytes) -> ec.EllipticCurvePrivateKey:
    """Load a P-256 private key from its 32-byte raw scalar (base64url decoded)."""
    if len(scalar_bytes) != 32:
        raise ValueError(f"expected 32-byte P-256 scalar, got {len(scalar_bytes)}")
    scalar_int = int.from_bytes(scalar_bytes, "big")
    return ec.derive_private_key(scalar_int, ec.SECP256R1())


def uncompressed_public_bytes(private_or_public_key) -> bytes:
    """Return the 65-byte uncompressed X9.62 encoding of a P-256 public key.

    Accepts either a private key (calls `.public_key()` internally) or a
    public key directly.
    """
    public_key = (
        private_or_public_key.public_key()
        if isinstance(private_or_public_key, ec.EllipticCurvePrivateKey)
        else private_or_public_key
    )
    return public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )


# --- RFC 8291 payload encryption (aes128gcm) ---------------------------------

def build_key_info(ua_public_uncompressed: bytes, as_public_uncompressed: bytes) -> bytes:
    """The `WebPush: info\\x00 || ua_public || as_public` info string (144 bytes).

    Used as the HKDF-Expand info for deriving the IKM in RFC 8291 §3.4.
    """
    if len(ua_public_uncompressed) != UNCOMPRESSED_P256_POINT_BYTES:
        raise ValueError("ua_public must be 65 uncompressed bytes")
    if len(as_public_uncompressed) != UNCOMPRESSED_P256_POINT_BYTES:
        raise ValueError("as_public must be 65 uncompressed bytes")
    return WEBPUSH_INFO_LABEL + ua_public_uncompressed + as_public_uncompressed


def derive_web_push_ikm(
    application_server_private: ec.EllipticCurvePrivateKey,
    ua_public_key: ec.EllipticCurvePublicKey,
    ua_auth_secret: bytes,
) -> bytes:
    """RFC 8291 §3.4: derive the 32-byte IKM used as HKDF salt-input downstream.

      shared        = ECDH(as_private, ua_public)                          # 32 B
      key_info      = "WebPush: info\\x00" || ua_public || as_public         # 144 B
      IKM           = HKDF-SHA256(salt=auth_secret, IKM=shared, info=key_info, L=32)
    """
    shared_secret = application_server_private.exchange(ec.ECDH(), ua_public_key)
    key_info = build_key_info(
        uncompressed_public_bytes(ua_public_key),
        uncompressed_public_bytes(application_server_private),
    )
    prk_key = hkdf_extract_sha256(salt=ua_auth_secret, ikm=shared_secret)
    return hkdf_expand_sha256(prk_key, key_info, HKDF_SHA256_PRK_BYTES)


def derive_content_encryption_key_and_nonce(ikm: bytes, salt: bytes) -> Tuple[bytes, bytes]:
    """RFC 8188 §2.2: derive (CEK, NONCE) from the aes128gcm IKM + record salt.

      PRK   = HKDF-Extract(salt=record_salt, IKM=ikm)
      CEK   = HKDF-Expand(PRK, "Content-Encoding: aes128gcm\\x00", 16)
      NONCE = HKDF-Expand(PRK, "Content-Encoding: nonce\\x00", 12)
    """
    prk_content = hkdf_extract_sha256(salt=salt, ikm=ikm)
    cek = hkdf_expand_sha256(prk_content, AES128GCM_CEK_INFO, AES128GCM_KEY_BYTES)
    nonce = hkdf_expand_sha256(prk_content, AES128GCM_NONCE_INFO, AES128GCM_NONCE_BYTES)
    return cek, nonce


def pad_last_record(plaintext: bytes, pad_to_length: int) -> bytes:
    """Append `0x02` (last-record delimiter) then NUL-pad to `pad_to_length`.

    Web Push always sends exactly one record, so the delimiter is always the
    last-record 0x02. `pad_to_length` is the total padded-plaintext length
    BEFORE AES-GCM adds its 16-byte tag; the caller decides how much padding
    to add (minimal = len(plaintext)+1; or up to rs-16-header for a fully
    length-hiding record).
    """
    min_padded_length = len(plaintext) + len(LAST_RECORD_PADDING_DELIMITER)
    if pad_to_length < min_padded_length:
        raise ValueError(
            f"pad_to_length {pad_to_length} is less than minimum {min_padded_length}"
        )
    return plaintext + LAST_RECORD_PADDING_DELIMITER + b"\x00" * (pad_to_length - min_padded_length)


def encrypt_aes128gcm_web_push(
    plaintext: bytes,
    ua_public_uncompressed: bytes,
    ua_auth_secret: bytes,
    application_server_private: ec.EllipticCurvePrivateKey,
    salt: bytes,
    record_size: int = DEFAULT_RECORD_SIZE,
    pad_to_length: int = None,
) -> bytes:
    """Encrypt `plaintext` into a complete aes128gcm Web Push record body.

    Returns the bytes the HTTP POST body must carry (Content-Encoding: aes128gcm):

      body = salt (16) || rs (4, BE uint32) || idlen=65 (1)
           || as_public_uncompressed (65) || AES-128-GCM(plaintext_padded)

    Args:
        plaintext: the message bytes (typically a small JSON blob).
        ua_public_uncompressed: 65-byte P-256 subscription public key (p256dh).
        ua_auth_secret: 16-byte per-subscription auth secret.
        application_server_private: ephemeral (per-message) P-256 private key.
        salt: 16-byte random salt (per-message).
        record_size: value written to the header's `rs` field (default 4096).
        pad_to_length: total padded-plaintext length before the AES-GCM tag;
            defaults to `len(plaintext) + 1` (minimal padding — matches the
            RFC 8291 §5 test vector byte-for-byte).

    Raises:
        ValueError: sizes / shapes don't match RFC requirements.
    """
    if len(salt) != AES128GCM_KEY_BYTES:
        raise ValueError(f"salt must be {AES128GCM_KEY_BYTES} bytes, got {len(salt)}")
    if len(ua_auth_secret) != AES128GCM_KEY_BYTES:
        raise ValueError(f"ua_auth_secret must be {AES128GCM_KEY_BYTES} bytes, got {len(ua_auth_secret)}")
    if pad_to_length is None:
        pad_to_length = len(plaintext) + len(LAST_RECORD_PADDING_DELIMITER)
    # rs bounds the maximum record size including the auth tag; enforce the
    # invariant so we never produce a record the receiver would refuse.
    max_plaintext_in_record = record_size - AES128GCM_TAG_BYTES
    if pad_to_length > max_plaintext_in_record:
        raise ValueError(
            f"pad_to_length {pad_to_length} exceeds record_size-tag "
            f"({max_plaintext_in_record})"
        )

    ua_public_key = load_p256_public_from_uncompressed(ua_public_uncompressed)
    as_public_uncompressed = uncompressed_public_bytes(application_server_private)

    ikm = derive_web_push_ikm(application_server_private, ua_public_key, ua_auth_secret)
    cek, nonce = derive_content_encryption_key_and_nonce(ikm, salt)

    plaintext_padded = pad_last_record(plaintext, pad_to_length)
    ciphertext_with_tag = AESGCM(cek).encrypt(nonce, plaintext_padded, associated_data=None)

    header = (
        salt
        + struct.pack(">I", record_size)
        + bytes([UNCOMPRESSED_P256_POINT_BYTES])
        + as_public_uncompressed
    )
    return header + ciphertext_with_tag


# --- VAPID JWT (RFC 8292) — ES256 --------------------------------------------

def _ecdsa_der_to_jose_p256(der_signature: bytes) -> bytes:
    """Convert an ECDSA DER-encoded signature to JOSE's fixed-length 64-byte form.

    JOSE ES256 signatures are 64 bytes: R (32) || S (32), both zero-padded to
    32 bytes. `cryptography`'s ECDSA signer returns DER; we translate.
    """
    r, s = decode_dss_signature(der_signature)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _jose_p256_to_ecdsa_der(jose_signature: bytes) -> bytes:
    """Inverse of `_ecdsa_der_to_jose_p256` for verification code paths.

    Used by tests / verifiers that want to hand the signature back to
    `cryptography`'s verify path.
    """
    if len(jose_signature) != 64:
        raise ValueError(f"JOSE ES256 signature must be 64 bytes, got {len(jose_signature)}")
    r = int.from_bytes(jose_signature[:32], "big")
    s = int.from_bytes(jose_signature[32:], "big")
    return encode_dss_signature(r, s)


def sign_vapid_jwt(
    aud: str,
    sub: str,
    exp_epoch: int,
    application_server_private: ec.EllipticCurvePrivateKey,
) -> str:
    """Build and ES256-sign a VAPID JWT.

    Returns the compact-serialization JWT (`header.payload.signature`, all
    base64url-no-pad). Suitable for the `Authorization: vapid t=<jwt>,k=<pub>`
    header on every push send.

    Args:
        aud: MUST be the scheme+host of the subscription endpoint (RFC 8292 §2).
        sub: contact URI or mailto: for the application server (RFC 8292 §2.1).
        exp_epoch: unix seconds. RFC 8292 caps this at 24h in the future.
        application_server_private: the VAPID private key (P-256 EC).
    """
    header = {"typ": "JWT", "alg": "ES256"}
    claims = {"aud": aud, "exp": int(exp_epoch), "sub": sub}
    header_b64 = b64url_encode_no_pad(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    claims_b64 = b64url_encode_no_pad(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    der_signature = application_server_private.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    jose_signature = _ecdsa_der_to_jose_p256(der_signature)
    return f"{header_b64}.{claims_b64}.{b64url_encode_no_pad(jose_signature)}"


def verify_vapid_jwt(
    jwt_compact: str,
    application_server_public: ec.EllipticCurvePublicKey,
) -> dict:
    """Verify an ES256 VAPID JWT signature and return the decoded claims dict.

    Raises:
        ValueError — malformed JWT or signature verification failure.

    Intended for tests / audits. Production senders don't verify their own
    signatures; the push service does that on the receiving end.
    """
    parts = jwt_compact.split(".")
    if len(parts) != 3:
        raise ValueError("JWT must have exactly three dot-separated segments")
    header_b64, claims_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    jose_signature = b64url_decode(sig_b64)
    der_signature = _jose_p256_to_ecdsa_der(jose_signature)
    application_server_public.verify(der_signature, signing_input, ec.ECDSA(hashes.SHA256()))
    header = json.loads(b64url_decode(header_b64))
    claims = json.loads(b64url_decode(claims_b64))
    if header.get("alg") != "ES256" or header.get("typ") != "JWT":
        raise ValueError(f"unexpected JWT header {header!r}")
    return claims


def build_vapid_authorization_header(jwt_compact: str, application_server_public_b64url: str) -> str:
    """Assemble the `Authorization: vapid t=<jwt>,k=<pub>` header value.

    The caller is responsible for keeping `application_server_public_b64url`
    matched to the private key that signed the JWT — mismatched values are
    rejected by every push service.
    """
    return f"vapid t={jwt_compact},k={application_server_public_b64url}"
