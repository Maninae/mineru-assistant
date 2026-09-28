#!/usr/bin/env python3
"""Known-answer test against the RFC 8291 §5 Web Push message-encryption vector.

RFC 8291 publishes fixed inputs (UA keys, auth secret, AS keys, salt, plaintext)
that MUST encrypt to a fixed ciphertext record. Reproducing the exact record
byte-for-byte is the proof that our aes128gcm/HKDF/ECDH pipeline agrees with
every other Web Push sender in the world. If this test regresses, real pushes
will be rejected as "corrupted" by Apple / FCM before ever hitting the phone.

Everything runs offline. No network, no Keychain, no state files touched.

Also spot-checks VAPID JWT signing / verification (ES256, DER↔JOSE conversion),
which is orthogonal to the aes128gcm pipeline but shares the same module.

Run: python3 -m pytest tests/test_webpush_rfc8291.py -q
     python3 tests/test_webpush_rfc8291.py
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace  # noqa: E402, F401 (imported for side-effect: sys.path setup)

import webpush_crypto as webpush_crypto  # noqa: E402


# --- RFC 8291 §5 test vector (verbatim from the RFC) -------------------------

RFC_8291_PLAINTEXT = b"When I grow up, I want to be a watermelon"

RFC_8291_UA_PRIVATE_B64URL = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_8291_UA_PUBLIC_B64URL = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)

RFC_8291_AS_PRIVATE_B64URL = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_8291_AS_PUBLIC_B64URL = (
    "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
)

RFC_8291_AUTH_SECRET_B64URL = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_8291_SALT_B64URL = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_8291_RECORD_SIZE = 4096

# Expected intermediate derivations (RFC 8291 Appendix A / §5).
RFC_8291_IKM_B64URL = "S4lYMb_L0FxCeq0WhDx813KgSYqU26kOyzWUdsXYyrg"
RFC_8291_CEK_B64URL = "oIhVW04MRdy2XN9CiKLxTg"
RFC_8291_NONCE_B64URL = "4h_95klXJ5E_qnoN"

# The full encrypted record body the RFC publishes as the fixed output.
# 144 bytes = 16 salt + 4 rs + 1 idlen + 65 keyid + 58 ciphertext-with-tag
# (58 = 41-byte plaintext + 1-byte padding-delim + 16-byte AES-GCM tag).
RFC_8291_EXPECTED_RECORD_B64URL = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def b64url(text: str) -> bytes:
    """Shorthand for tests: base64url decode with padding restored."""
    return webpush_crypto.b64url_decode(text)


class Rfc8291IntermediatesTest(unittest.TestCase):
    """Reproduce the intermediate IKM / CEK / NONCE derivations from Appendix A.

    Cheap sanity checks — if these fail, the final-record test would fail too
    but with a much less obvious explanation. Keep them so a future refactor
    that breaks the KDF chain gets pinpointed here.
    """

    def setUp(self):
        self.ua_public = b64url(RFC_8291_UA_PUBLIC_B64URL)
        self.ua_auth = b64url(RFC_8291_AUTH_SECRET_B64URL)
        self.as_private = webpush_crypto.load_p256_private_from_scalar(
            b64url(RFC_8291_AS_PRIVATE_B64URL)
        )
        self.salt = b64url(RFC_8291_SALT_B64URL)

    def test_derived_ikm_matches_rfc(self):
        ua_public_key = webpush_crypto.load_p256_public_from_uncompressed(self.ua_public)
        ikm = webpush_crypto.derive_web_push_ikm(self.as_private, ua_public_key, self.ua_auth)
        self.assertEqual(webpush_crypto.b64url_encode_no_pad(ikm), RFC_8291_IKM_B64URL)

    def test_derived_cek_and_nonce_match_rfc(self):
        ua_public_key = webpush_crypto.load_p256_public_from_uncompressed(self.ua_public)
        ikm = webpush_crypto.derive_web_push_ikm(self.as_private, ua_public_key, self.ua_auth)
        cek, nonce = webpush_crypto.derive_content_encryption_key_and_nonce(ikm, self.salt)
        self.assertEqual(webpush_crypto.b64url_encode_no_pad(cek), RFC_8291_CEK_B64URL)
        self.assertEqual(webpush_crypto.b64url_encode_no_pad(nonce), RFC_8291_NONCE_B64URL)

    def test_key_info_shape(self):
        # 14 label + 65 ua_pub + 65 as_pub = 144 bytes; header bytes are exact.
        key_info = webpush_crypto.build_key_info(
            self.ua_public,
            webpush_crypto.uncompressed_public_bytes(self.as_private),
        )
        self.assertEqual(len(key_info), 14 + 65 + 65)
        self.assertTrue(key_info.startswith(b"WebPush: info\x00"))


class Rfc8291FinalRecordTest(unittest.TestCase):
    """The load-bearing test: encrypt the RFC's exact inputs, get its exact output.

    If this passes, the crypto agrees with every conformant push service. If it
    fails, do NOT ship — a real device push will be rejected with `403` or
    silently discarded.
    """

    def test_encrypt_produces_rfc_8291_expected_record(self):
        encrypted = webpush_crypto.encrypt_aes128gcm_web_push(
            plaintext=RFC_8291_PLAINTEXT,
            ua_public_uncompressed=b64url(RFC_8291_UA_PUBLIC_B64URL),
            ua_auth_secret=b64url(RFC_8291_AUTH_SECRET_B64URL),
            application_server_private=webpush_crypto.load_p256_private_from_scalar(
                b64url(RFC_8291_AS_PRIVATE_B64URL)
            ),
            salt=b64url(RFC_8291_SALT_B64URL),
            record_size=RFC_8291_RECORD_SIZE,
            # Minimal padding: 41-byte plaintext + 1-byte delimiter = 42 bytes
            # padded; matches the RFC vector exactly.
            pad_to_length=len(RFC_8291_PLAINTEXT) + 1,
        )
        expected = b64url(RFC_8291_EXPECTED_RECORD_B64URL)
        # First-fail must point at the exact byte, not "byte strings differ".
        self.assertEqual(
            webpush_crypto.b64url_encode_no_pad(encrypted),
            RFC_8291_EXPECTED_RECORD_B64URL,
            "aes128gcm record must reproduce RFC 8291 §5 byte-for-byte",
        )
        self.assertEqual(encrypted, expected)


class VapidJwtTest(unittest.TestCase):
    """VAPID JWT (ES256): sign, verify, round-trip, and check header shape."""

    def setUp(self):
        # Fresh key for each test — no shared state, no Keychain touch.
        from cryptography.hazmat.primitives.asymmetric import ec
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.public_key = self.private_key.public_key()

    def test_jwt_round_trip_verifies(self):
        exp = int(time.time()) + 60
        jwt = webpush_crypto.sign_vapid_jwt(
            aud="https://fcm.googleapis.com",
            sub="mailto:sam@example.com",
            exp_epoch=exp,
            application_server_private=self.private_key,
        )
        claims = webpush_crypto.verify_vapid_jwt(jwt, self.public_key)
        self.assertEqual(claims["aud"], "https://fcm.googleapis.com")
        self.assertEqual(claims["sub"], "mailto:sam@example.com")
        self.assertEqual(claims["exp"], exp)

    def test_jwt_has_three_segments_and_es256_header(self):
        jwt = webpush_crypto.sign_vapid_jwt(
            aud="https://push.example",
            sub="mailto:x@example.com",
            exp_epoch=int(time.time()) + 60,
            application_server_private=self.private_key,
        )
        segments = jwt.split(".")
        self.assertEqual(len(segments), 3)
        import json
        header = json.loads(webpush_crypto.b64url_decode(segments[0]))
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["typ"], "JWT")

    def test_tampered_signature_fails_verification(self):
        jwt = webpush_crypto.sign_vapid_jwt(
            aud="https://push.example",
            sub="mailto:x@example.com",
            exp_epoch=int(time.time()) + 60,
            application_server_private=self.private_key,
        )
        header, claims, sig = jwt.split(".")
        # Flip a char in the MIDDLE of the signature — the last b64url char
        # can encode partial-byte tail bits, so a change there doesn't always
        # perturb the decoded signature bytes. Middle-of-string change always
        # rolls at least one full byte of R/S, guaranteeing verify-fail.
        mid = len(sig) // 2
        different = "A" if sig[mid] != "A" else "B"
        broken_sig = sig[:mid] + different + sig[mid + 1:]
        with self.assertRaises(Exception):  # cryptography raises InvalidSignature
            webpush_crypto.verify_vapid_jwt(f"{header}.{claims}.{broken_sig}", self.public_key)

    def test_wrong_public_key_fails_verification(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        other_public = ec.generate_private_key(ec.SECP256R1()).public_key()
        jwt = webpush_crypto.sign_vapid_jwt(
            aud="https://push.example",
            sub="mailto:x@example.com",
            exp_epoch=int(time.time()) + 60,
            application_server_private=self.private_key,
        )
        with self.assertRaises(Exception):
            webpush_crypto.verify_vapid_jwt(jwt, other_public)


class Base64UrlHelpersTest(unittest.TestCase):
    """Tiny round-trip sanity for the pad-tolerant base64url helpers."""

    def test_encode_no_pad_strips_trailing_equals(self):
        # 1 byte input → 2 chars + 2 pad; encoder must drop the pad.
        self.assertEqual(webpush_crypto.b64url_encode_no_pad(b"\x00"), "AA")

    def test_decode_tolerates_missing_padding(self):
        # RFC 8291 test vectors are all published without padding; the decoder
        # MUST restore the missing `=` chars so the vector loads.
        self.assertEqual(webpush_crypto.b64url_decode("AA"), b"\x00")
        self.assertEqual(
            webpush_crypto.b64url_decode(RFC_8291_UA_PUBLIC_B64URL),
            b64url(RFC_8291_UA_PUBLIC_B64URL),
        )


if __name__ == "__main__":
    unittest.main()
