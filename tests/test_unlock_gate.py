#!/usr/bin/env python3
"""Tests for `app/unlock_gate.py` — the passphrase gate + lockout tracker.

The critical invariants (from the security audit):

- Correct passphrase verifies; wrong doesn't; the success predicate NEVER
  passes on the dummy passphrase or the dummy hash (the timing-oracle filler).
- Lockout math: OWASP-style exponential backoff after
  `LOCKOUT_FAILURES_BEFORE_BACKOFF=3` grace failures, base 5s, cap 3600s.
- A correct passphrase during an active lockout window is REFUSED with 429 —
  the lockout gate runs before any hash work.
- `coerce_finite_float` rejects None/NaN/inf/non-numeric → safe default (never
  raises out of the dispatcher, never leaves an "always locked" state).
- Corrupt / non-dict / non-UTF-8 token+lockout state → treated as empty/deny,
  never raises.
- Token store uses `secrets.token_urlsafe` (CSPRNG) and prunes expired entries
  on load.

**We NEVER touch the real Keychain in these tests.** The one Keychain-reading
function (`read_passphrase_hash_from_keychain`) is monkey-patched to return a
synthetic SHA-256 hash of a known passphrase.

Run: python3 -m pytest tests/test_unlock_gate.py -q
     python3 tests/test_unlock_gate.py
"""

import hashlib
import json
import os
import stat
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace   # noqa: E402

import unlock_gate   # noqa: E402
import handlers   # noqa: E402


TEST_PASSPHRASE = "correct-horse-battery-staple"
TEST_HASH = hashlib.sha256(TEST_PASSPHRASE.encode("utf-8")).hexdigest()


class KeychainStub:
    """Context-managed monkey-patch of read_passphrase_hash_from_keychain.

    Controls what the gate "sees" in Keychain without ever running `security`.
    """
    def __init__(self, hash_hex_or_none):
        self.hash_hex_or_none = hash_hex_or_none
        self.original = None

    def __enter__(self):
        self.original = unlock_gate.read_passphrase_hash_from_keychain
        unlock_gate.read_passphrase_hash_from_keychain = lambda: self.hash_hex_or_none
        # Force a re-probe so the cached gate flag is fresh.
        unlock_gate.GATE_ENABLED_CACHE = None
        unlock_gate.refresh_gate_state()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        unlock_gate.read_passphrase_hash_from_keychain = self.original
        unlock_gate.GATE_ENABLED_CACHE = None


class VerifyPassphraseTest(unittest.TestCase):
    """`verify_passphrase` is the constant-time compare underlying every unlock."""

    def test_correct_passphrase_matches(self):
        self.assertTrue(unlock_gate.verify_passphrase(TEST_PASSPHRASE, TEST_HASH))

    def test_wrong_passphrase_does_not_match(self):
        self.assertFalse(unlock_gate.verify_passphrase("wrong", TEST_HASH))

    def test_dummy_passphrase_never_matches_dummy_hash(self):
        # This is the property that keeps the timing-uniformity fillers safe:
        # even though every rejection path collapses to (dummy, dummy), the
        # compare must NOT return True on that pair — else the "was_valid_shape"
        # gate is the only wall between fail and pass.
        self.assertFalse(unlock_gate.verify_passphrase(
            unlock_gate.DUMMY_PASSPHRASE_FOR_TIMING_UNIFORMITY,
            unlock_gate.DUMMY_HASH_FOR_TIMING_UNIFORMITY,
        ))

    def test_non_string_inputs_return_false_not_raise(self):
        self.assertFalse(unlock_gate.verify_passphrase(None, TEST_HASH))
        self.assertFalse(unlock_gate.verify_passphrase(TEST_PASSPHRASE, None))
        self.assertFalse(unlock_gate.verify_passphrase(42, TEST_HASH))


class CoerceFiniteFloatTest(unittest.TestCase):
    def test_finite_number_passes_through(self):
        self.assertEqual(unlock_gate.coerce_finite_float(3.14, 0.0), 3.14)
        self.assertEqual(unlock_gate.coerce_finite_float(0, 99.0), 0.0)

    def test_none_returns_default(self):
        self.assertEqual(unlock_gate.coerce_finite_float(None, 42.0), 42.0)

    def test_non_numeric_returns_default(self):
        self.assertEqual(unlock_gate.coerce_finite_float("hi", 7.0), 7.0)
        self.assertEqual(unlock_gate.coerce_finite_float([1, 2], 7.0), 7.0)
        self.assertEqual(unlock_gate.coerce_finite_float({}, 7.0), 7.0)

    def test_nan_returns_default(self):
        self.assertEqual(unlock_gate.coerce_finite_float(float("nan"), 5.0), 5.0)

    def test_infinity_returns_default(self):
        # An "always locked" state is exactly what an inf lockout would create.
        self.assertEqual(unlock_gate.coerce_finite_float(float("inf"), 0.0), 0.0)
        self.assertEqual(unlock_gate.coerce_finite_float(float("-inf"), 0.0), 0.0)

    def test_string_infinity_returns_default(self):
        # `float("Infinity")` succeeds and then math.isfinite catches it.
        self.assertEqual(unlock_gate.coerce_finite_float("Infinity", 0.0), 0.0)


class LockoutMathTest(unittest.TestCase):
    """`compute_lockout_seconds` implements the OWASP-style backoff."""

    def test_grace_period_no_lockout(self):
        for k in range(1, unlock_gate.LOCKOUT_FAILURES_BEFORE_BACKOFF + 1):
            self.assertEqual(unlock_gate.compute_lockout_seconds(k), 0,
                             f"failure #{k} must still be inside the grace window")

    def test_first_backoff_is_base_seconds(self):
        first_after_grace = unlock_gate.LOCKOUT_FAILURES_BEFORE_BACKOFF + 1
        self.assertEqual(unlock_gate.compute_lockout_seconds(first_after_grace),
                         unlock_gate.LOCKOUT_BASE_SECONDS)

    def test_backoff_doubles_each_step(self):
        base = unlock_gate.LOCKOUT_BASE_SECONDS
        grace = unlock_gate.LOCKOUT_FAILURES_BEFORE_BACKOFF
        self.assertEqual(unlock_gate.compute_lockout_seconds(grace + 1), base)
        self.assertEqual(unlock_gate.compute_lockout_seconds(grace + 2), base * 2)
        self.assertEqual(unlock_gate.compute_lockout_seconds(grace + 3), base * 4)

    def test_cap_at_lockout_max_seconds(self):
        # Some large number well past the cap.
        big = unlock_gate.LOCKOUT_FAILURES_BEFORE_BACKOFF + 40
        self.assertEqual(unlock_gate.compute_lockout_seconds(big),
                         unlock_gate.LOCKOUT_MAX_SECONDS)

    def test_cap_exactly_3600(self):
        self.assertEqual(unlock_gate.LOCKOUT_MAX_SECONDS, 3600)


class LockoutStateFileRobustnessTest(unittest.TestCase):
    """Corrupt / non-dict / non-UTF-8 state files must not raise."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_missing_file_returns_zeros(self):
        state = unlock_gate.load_lockout_state()
        self.assertEqual(state, {"fail_count": 0.0, "locked_until": 0.0})

    def test_non_utf8_content_returns_zeros(self):
        unlock_gate.UNLOCK_LOCKOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        unlock_gate.UNLOCK_LOCKOUT_PATH.write_bytes(b"\xff\xfe\x00\x00garbage")
        state = unlock_gate.load_lockout_state()
        self.assertEqual(state, {"fail_count": 0.0, "locked_until": 0.0})

    def test_non_dict_content_returns_zeros(self):
        unlock_gate.UNLOCK_LOCKOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        unlock_gate.UNLOCK_LOCKOUT_PATH.write_text("[1, 2, 3]", encoding="utf-8")
        state = unlock_gate.load_lockout_state()
        self.assertEqual(state, {"fail_count": 0.0, "locked_until": 0.0})

    def test_poisoned_locked_until_never_creates_permanent_lock(self):
        # A hand-poisoned "locked_until": "Infinity" would create a permanent
        # lockout if not sanitized. `coerce_finite_float` catches it.
        unlock_gate.UNLOCK_LOCKOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        for poisoned in ("Infinity", "NaN", "not-a-number", None):
            unlock_gate.UNLOCK_LOCKOUT_PATH.write_text(
                json.dumps({"fail_count": 0, "locked_until": poisoned}),
                encoding="utf-8",
            )
            self.assertIsNone(unlock_gate.current_lockout_retry_after(),
                              f"poisoned locked_until={poisoned!r} must not lock")

    def test_current_lockout_returns_none_when_expired(self):
        # A locked_until in the past — no lockout should be reported.
        unlock_gate.save_lockout_state({"fail_count": 5.0,
                                        "locked_until": time.time() - 100.0})
        self.assertIsNone(unlock_gate.current_lockout_retry_after())

    def test_current_lockout_returns_positive_when_active(self):
        unlock_gate.save_lockout_state({"fail_count": 5.0,
                                        "locked_until": time.time() + 30.0})
        ra = unlock_gate.current_lockout_retry_after()
        self.assertIsNotNone(ra)
        self.assertGreater(ra, 0)
        self.assertLessEqual(ra, 32)   # 30s + 1s rounding + a hair of slack.


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_mint_returns_csprng_token(self):
        # Two mints must produce two distinct high-entropy tokens.
        t1 = unlock_gate.mint_and_store_token()
        t2 = unlock_gate.mint_and_store_token()
        self.assertNotEqual(t1, t2)
        # `secrets.token_urlsafe(32)` yields 43 base64-url chars (32*8/6 rounded).
        self.assertEqual(len(t1), 43)
        for token in (t1, t2):
            for ch in token:
                self.assertIn(ch, unlock_gate.TOKEN_ALLOWED_CHARS)

    def test_mint_persisted_with_0600(self):
        unlock_gate.mint_and_store_token()
        perms = stat.S_IMODE(os.stat(unlock_gate.UNLOCK_TOKENS_PATH).st_mode)
        self.assertEqual(perms, stat.S_IRUSR | stat.S_IWUSR)

    def test_load_prunes_expired(self):
        # Two tokens: one with expiry in the past, one in the future.
        expired = "expired_token_" + ("a" * 20)
        live = "live_token_____" + ("b" * 20)
        # Assert both fit the allowed-chars gate before the store cares.
        for token in (expired, live):
            self.assertTrue(
                all(c in unlock_gate.TOKEN_ALLOWED_CHARS for c in token),
                f"synthetic token {token!r} must satisfy the char gate",
            )
        now = time.time()
        unlock_gate.save_token_store({expired: now - 100.0,
                                      live: now + 100.0})
        pruned = unlock_gate.load_token_store()
        self.assertNotIn(expired, pruned)
        self.assertIn(live, pruned)

    def test_load_ignores_bad_shape(self):
        unlock_gate.UNLOCK_TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
        unlock_gate.UNLOCK_TOKENS_PATH.write_text("not-json{{", encoding="utf-8")
        self.assertEqual(unlock_gate.load_token_store(), {})

    def test_load_ignores_non_dict(self):
        unlock_gate.UNLOCK_TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
        unlock_gate.UNLOCK_TOKENS_PATH.write_text("[]", encoding="utf-8")
        self.assertEqual(unlock_gate.load_token_store(), {})

    def test_load_ignores_bad_entries_but_keeps_good_ones(self):
        # A mix: valid string+float, junk in the middle. Only the valid pair
        # survives the per-item filter.
        good = "good_token_____" + ("c" * 20)
        unlock_gate.UNLOCK_TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
        unlock_gate.UNLOCK_TOKENS_PATH.write_text(json.dumps({
            good: time.time() + 100.0,
            "!!!bad-chars!!!": time.time() + 100.0,
            "a" * (unlock_gate.MAX_TOKEN_LENGTH + 1): time.time() + 100.0,
            "int-expiry": "not-a-number",
        }), encoding="utf-8")
        pruned = unlock_gate.load_token_store()
        self.assertEqual(list(pruned.keys()), [good])

    def test_is_token_valid_rejects_non_string(self):
        self.assertFalse(unlock_gate.is_token_valid(None))
        self.assertFalse(unlock_gate.is_token_valid(42))
        self.assertFalse(unlock_gate.is_token_valid(""))

    def test_is_token_valid_rejects_bad_chars(self):
        self.assertFalse(unlock_gate.is_token_valid("has spaces here"))
        self.assertFalse(unlock_gate.is_token_valid("dots.and.dots"))


class ExtractUnlockCookieTest(unittest.TestCase):
    def test_extract_valid_cookie(self):
        raw = f"other=foo; {unlock_gate.UNLOCK_COOKIE_NAME}=abc_DEF-123; more=bar"
        self.assertEqual(unlock_gate.extract_unlock_cookie_value(raw), "abc_DEF-123")

    def test_missing_cookie_returns_none(self):
        self.assertIsNone(unlock_gate.extract_unlock_cookie_value(None))
        self.assertIsNone(unlock_gate.extract_unlock_cookie_value(""))
        self.assertIsNone(unlock_gate.extract_unlock_cookie_value("other=foo"))

    def test_bad_char_cookie_rejected(self):
        raw = f"{unlock_gate.UNLOCK_COOKIE_NAME}=has spaces"
        self.assertIsNone(unlock_gate.extract_unlock_cookie_value(raw))

    def test_oversized_cookie_rejected(self):
        raw = f"{unlock_gate.UNLOCK_COOKIE_NAME}=" + ("a" * (unlock_gate.MAX_TOKEN_LENGTH + 1))
        self.assertIsNone(unlock_gate.extract_unlock_cookie_value(raw))


class UnlockHandlerBehaviourTest(unittest.TestCase):
    """The `handle_unlock` HTTP flow, with a stubbed Keychain."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def _post(self, passphrase_or_body):
        if isinstance(passphrase_or_body, (bytes, bytearray)):
            body = bytes(passphrase_or_body)
        elif passphrase_or_body is None:
            body = b"{}"
        else:
            body = json.dumps({"passphrase": passphrase_or_body}).encode("utf-8")
        return handlers.handle_unlock(None, None, body)

    def test_correct_passphrase_returns_200_and_sets_cookie(self):
        with KeychainStub(TEST_HASH):
            status, headers, body = self._post(TEST_PASSPHRASE)
            self.assertEqual(status, 200)
            self.assertIn("Set-Cookie", headers)
            self.assertIn(unlock_gate.UNLOCK_COOKIE_NAME, headers["Set-Cookie"])
            self.assertIn(b'"ok": true', body)

    def test_wrong_passphrase_returns_401_uniform_body(self):
        with KeychainStub(TEST_HASH):
            status, headers, body = self._post("wrong-passphrase")
            self.assertEqual(status, 401)
            self.assertNotIn("Set-Cookie", headers)
            self.assertIn(b'"incorrect"', body)

    def test_bad_shape_body_returns_401_not_400(self):
        # The timing-uniformity fix: bad shapes must NOT short-circuit as 400.
        # They must produce the SAME 401 as a wrong passphrase.
        # Reset lockout between iterations so accumulated fails don't 429 us.
        with KeychainStub(TEST_HASH):
            for bad in (b"not-json{{",
                        b"[1,2,3]",
                        json.dumps({"password": TEST_PASSPHRASE}).encode(),
                        json.dumps({"passphrase": 42}).encode(),
                        json.dumps({"passphrase": ""}).encode(),
                        b""):
                unlock_gate.reset_lockout()
                status, _, body = handlers.handle_unlock(None, None, bad)
                self.assertEqual(status, 401,
                                 f"bad body {bad!r} must fold into a uniform 401")
                self.assertIn(b'"incorrect"', body)

    def test_gate_off_returns_404_for_unlock_and_lock_screen(self):
        with KeychainStub(None):   # Keychain returns None => gate OFF (option A)
            status, _, _ = self._post(TEST_PASSPHRASE)
            self.assertEqual(status, 404, "gate OFF must 404 the unlock endpoint")
            status2, _, _ = handlers.handle_lock_screen(None, None)
            self.assertEqual(status2, 404, "gate OFF must 404 the lock screen")

    def test_correct_passphrase_during_lockout_is_refused(self):
        # Preload a lockout window into the on-disk lockout file.
        unlock_gate.save_lockout_state({"fail_count": 10.0,
                                        "locked_until": time.time() + 60.0})
        with KeychainStub(TEST_HASH):
            status, headers, body = self._post(TEST_PASSPHRASE)
            self.assertEqual(status, 429,
                             "an active lockout must refuse even the correct passphrase")
            self.assertIn("Retry-After", headers)
            # And the correct passphrase during lockout must NOT mint a cookie.
            self.assertNotIn("Set-Cookie", headers)

    def test_success_predicate_never_passes_on_dummy_passphrase(self):
        # Even with the gate ON, `body_was_valid=False` (bad shape) must lose:
        # the outcome-composition line requires ALL of (valid shape, real hash,
        # matched compare). A dummy passphrase against the real hash must NOT
        # ever succeed, regardless of any other bit.
        with KeychainStub(TEST_HASH):
            status, _, body = self._post(b"")  # empty body -> bad shape -> dummy
            self.assertEqual(status, 401)
            # And no token got minted.
            self.assertEqual(unlock_gate.load_token_store(), {})

    def test_success_predicate_never_passes_when_keychain_gone(self):
        # Enable gate at test start with a real hash, then mid-flight tear it
        # out (Keychain read returns None). Compare still runs (against the
        # dummy hash), but success requires expected_hash is not None → deny.
        # Note: the startup probe fires under KeychainStub(TEST_HASH); the fresh
        # read inside handle_unlock uses whatever the stub returns at CALL time.
        original_read = unlock_gate.read_passphrase_hash_from_keychain
        try:
            # Turn gate ON with a valid hash.
            unlock_gate.read_passphrase_hash_from_keychain = lambda: TEST_HASH
            unlock_gate.refresh_gate_state()
            # Now flip the fresh read to return None (Keychain torn out).
            unlock_gate.read_passphrase_hash_from_keychain = lambda: None
            status, headers, body = self._post(TEST_PASSPHRASE)
            self.assertEqual(status, 401,
                             "torn-out Keychain must never let a passphrase in")
            self.assertNotIn("Set-Cookie", headers)
        finally:
            unlock_gate.read_passphrase_hash_from_keychain = original_read
            unlock_gate.GATE_ENABLED_CACHE = None

    def test_failed_attempt_records_lockout_state(self):
        with KeychainStub(TEST_HASH):
            self._post("wrong-1")
            self._post("wrong-2")
        state = unlock_gate.load_lockout_state()
        self.assertGreaterEqual(state["fail_count"], 2.0)

    def test_successful_unlock_resets_lockout(self):
        unlock_gate.save_lockout_state({"fail_count": 2.0, "locked_until": 0.0})
        with KeychainStub(TEST_HASH):
            status, _, _ = self._post(TEST_PASSPHRASE)
        self.assertEqual(status, 200)
        state = unlock_gate.load_lockout_state()
        self.assertEqual(state["fail_count"], 0.0)


if __name__ == "__main__":
    unittest.main()
