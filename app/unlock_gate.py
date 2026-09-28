"""Passphrase gate for the Mineru web app (defense-in-depth over tailnet).

The gate is ON iff macOS Keychain holds a `mineru-webapp-passphrase-hash`
entry (account `mineru`). Absent → gate OFF, byte-identical to today's open-
on-tailnet behavior (option A). Present → every request outside a small
exempt set requires a valid `mineru_unlock` bearer-token cookie.

Everything in this module is fail-closed: any Keychain read error, any
token-store read error, any cookie-parse ambiguity while the gate is
enabled → deny. The one Keychain access site is `read_passphrase_hash_from_keychain`;
that fixed-service, fixed-account subprocess call is the app's ENTIRE
Keychain surface.

Rate-limit / lockout for `POST /api/unlock`:
  - Global (single passphrase, single user) — not per-IP. Tailnet IPs are
    trusted-ish and easily spoofed; per-IP throttling here is theatre.
  - Persisted to disk (state/unlock-lockout.json, 0600) so a restart does
    not reset an active brute-force window.
  - OWASP-style exponential backoff after a small grace period, capped ~1h.
  - Every failed attempt (including malformed body / wrong shape) counts.

Python 3.9-compatible (system /usr/bin/python3 is 3.9.6). Stdlib only.
"""

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import stat
import string
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from config import (
    STATE_DIR,
    TAILNET_HOSTNAME,
    UNLOCK_COOKIE_MAX_AGE_SECONDS,
    UNLOCK_COOKIE_NAME,
    UNLOCK_LOCKOUT_PATH,
    UNLOCK_TOKENS_PATH,
)


logger = logging.getLogger(__name__)


# --- Keychain wiring (single fixed service; do not add other lookups) --------

# Service/account default to the framework namespace; a downstream install can
# point at its own Keychain items via PASSPHRASE_KEYCHAIN_{SERVICE,ACCOUNT}.
# Still exactly ONE Keychain lookup — the env only renames it, never widens it.
PASSPHRASE_KEYCHAIN_SERVICE = os.environ.get("PASSPHRASE_KEYCHAIN_SERVICE", "mineru-webapp-passphrase-hash")
PASSPHRASE_KEYCHAIN_ACCOUNT = os.environ.get("PASSPHRASE_KEYCHAIN_ACCOUNT", "mineru")
SECURITY_BINARY = "/usr/bin/security"
KEYCHAIN_READ_TIMEOUT_SECONDS = 5

# --- Lockout parameters (OWASP-style exponential backoff, capped ~1h) --------

# The first N failures cost nothing, so a fat-finger doesn't lock the operator
# out of their own house. After that, the window grows 5s → 10s → 20s → … up to 1h.
LOCKOUT_FAILURES_BEFORE_BACKOFF = 3
LOCKOUT_BASE_SECONDS = 5
LOCKOUT_MAX_SECONDS = 60 * 60

# --- On-disk file mode: owner rw only ----------------------------------------

FILE_MODE_0600 = stat.S_IRUSR | stat.S_IWUSR

# --- Passphrase / token sanity caps ------------------------------------------

# A submitted passphrase longer than this is refused up front — long inputs
# only slow the SHA-256 pass down; nothing legitimate is 4KB.
MAX_PASSPHRASE_LENGTH = 512

# Timing-uniformity fillers. Every /api/unlock rejection path (bad JSON,
# wrong shape, missing/oversized passphrase, torn-out Keychain) collapses
# to these dummies so the hash pipeline runs to completion in every case —
# wrong-passphrase and bad-shape then take the same path/time and no timing
# oracle leaks "reached hash compare" vs "rejected at parse". The dummies
# never match a real hash (SHA-256("\x00"*32) is not the sixty-four-zero
# hex string), so a validity check on the outcome still gates access.
DUMMY_PASSPHRASE_FOR_TIMING_UNIFORMITY = "\x00" * 32
DUMMY_HASH_FOR_TIMING_UNIFORMITY = "0" * 64
# Random tokens via `secrets.token_urlsafe(32)` are 43 chars of [A-Za-z0-9_-].
# Cap at 128 to accept future rotations to a wider token while rejecting a
# pathological cookie-jam.
MAX_TOKEN_LENGTH = 128
TOKEN_ALLOWED_CHARS = frozenset(string.ascii_letters + string.digits + "-_")
HEX_CHARS = frozenset(string.hexdigits.lower())
SHA256_HEX_LENGTH = 64

# --- Startup cache -----------------------------------------------------------

# `is_gate_enabled()` reads this after the one-time startup probe. Re-probable
# by calling `refresh_gate_state()` (useful for tests; production doesn't need
# it since /api/unlock re-reads the hash from Keychain fresh on every call).
GATE_LOCK = threading.RLock()
GATE_ENABLED_CACHE: Optional[bool] = None


# --- Errors ------------------------------------------------------------------

class GateReadError(Exception):
    """Keychain read failed in a way we can't distinguish from tampering.

    Callers must fail-closed: for the startup probe, treat as gate ON (so we
    do not accidentally serve open on a Keychain outage). For POST /api/unlock,
    treat as an authentication failure (deny) — never leak the error text.
    """


# --- Keychain access ---------------------------------------------------------

def read_passphrase_hash_from_keychain() -> Optional[str]:
    """Return the SHA-256 hex hash from Keychain, or None if the item is absent.

    Returns:
        - str (lowercased 64-char hex) — passphrase hash exists, gate is ON.
        - None — item authoritatively not found in Keychain, gate is OFF.

    Raises:
        GateReadError — Keychain call failed in an ambiguous way (system error,
        malformed hash, unexpected exit). Callers must fail-closed on this.

    - This is the app's ONLY Keychain access. Do not add other service names.
    - Hardcoded service/account guards against arg-injection or scope creep.
    - The subprocess never receives user input; there is nothing to escape.
    """
    try:
        result = subprocess.run(
            [
                SECURITY_BINARY,
                "find-generic-password",
                "-a", PASSPHRASE_KEYCHAIN_ACCOUNT,
                "-s", PASSPHRASE_KEYCHAIN_SERVICE,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as subprocess_error:
        raise GateReadError("keychain subprocess failed") from subprocess_error

    if result.returncode == 0:
        candidate = result.stdout.strip().lower()
        if len(candidate) != SHA256_HEX_LENGTH:
            raise GateReadError("hash wrong length")
        if any(char not in HEX_CHARS for char in candidate):
            raise GateReadError("hash not hex")
        return candidate

    # Any non-zero exit that names "could not be found" is authoritatively
    # absent (item does not exist). Every other non-zero exit is ambiguous —
    # keychain locked, permission denied, malformed argv — and must fail loud.
    stderr_lower = (result.stderr or "").lower()
    if "could not be found" in stderr_lower:
        return None
    raise GateReadError(f"security exit {result.returncode}")


# --- Gate on/off cache -------------------------------------------------------

def probe_gate_at_startup() -> bool:
    """Read the Keychain once and cache whether the gate is on.

    Called from server startup. Fail-closed: if Keychain reads are broken we
    turn the gate ON, so we don't accidentally serve open on a keychain-locked
    boot. (POST /api/unlock will also deny in that state — user has to fix
    Keychain before the app is usable, which is the right posture for this.)
    """
    global GATE_ENABLED_CACHE
    with GATE_LOCK:
        try:
            hash_hex = read_passphrase_hash_from_keychain()
        except GateReadError as gate_error:
            logger.error(
                "passphrase-gate: keychain read failed at startup (%s); failing closed (gate ON, unlocks will fail)",
                gate_error,
            )
            GATE_ENABLED_CACHE = True
            return True
        enabled = hash_hex is not None
        GATE_ENABLED_CACHE = enabled
        logger.info("passphrase-gate: %s", "ENABLED" if enabled else "disabled (option A)")
        return enabled


def is_gate_enabled() -> bool:
    """Fast-path read of the cached gate state.

    Safe to call on every request. If the startup probe hasn't run yet
    (imports before main()), fail-closed and probe on the spot.
    """
    with GATE_LOCK:
        if GATE_ENABLED_CACHE is None:
            return probe_gate_at_startup()
        return GATE_ENABLED_CACHE


def refresh_gate_state() -> bool:
    """Re-probe the Keychain; return the new gate state.

    Useful for tests and for a future "reload without restart" hook. Not
    called from the request path — production re-reads the hash freshly
    inside the unlock handler, not here.
    """
    global GATE_ENABLED_CACHE
    with GATE_LOCK:
        GATE_ENABLED_CACHE = None
        return probe_gate_at_startup()


# --- Atomic writer (shared shape with seen_ledger.save_ledger) ---------------

def atomic_write_json_600(target: Path, data) -> None:
    """Write `data` as JSON to `target` atomically, then chmod 600.

    Same-directory NamedTemporaryFile → fsync → os.replace. Any writer crash
    mid-write leaves the previous good file intact.
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
        json.dump(data, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.chmod(tmp.name, FILE_MODE_0600)
    os.replace(tmp.name, target)


# --- Token store (in-memory read cache is not used; disk is the source) ------

def load_token_store() -> Dict[str, float]:
    """Load {token: expiry_epoch}. Expired entries are pruned as we load.

    Fail-closed: on read/parse error, return an empty dict. Missing file is
    normal (first boot; no one has unlocked yet).
    """
    with GATE_LOCK:
        if not UNLOCK_TOKENS_PATH.exists():
            return {}
        try:
            with open(UNLOCK_TOKENS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as read_error:
            logger.warning("unlock-tokens.json unreadable, treating as empty: %s", read_error)
            return {}
        if not isinstance(data, dict):
            logger.warning("unlock-tokens.json wrong shape; discarding")
            return {}
        now = time.time()
        pruned: Dict[str, float] = {}
        for token, expiry in data.items():
            if not isinstance(token, str) or not isinstance(expiry, (int, float)):
                continue
            if len(token) > MAX_TOKEN_LENGTH:
                continue
            if any(char not in TOKEN_ALLOWED_CHARS for char in token):
                continue
            if float(expiry) > now:
                pruned[token] = float(expiry)
        return pruned


def save_token_store(store: Dict[str, float]) -> None:
    """Atomic 0600 write of the pruned token store."""
    atomic_write_json_600(UNLOCK_TOKENS_PATH, store)


def mint_and_store_token() -> str:
    """Generate a fresh 256-bit token, store {token: expiry}, return the token."""
    token = secrets.token_urlsafe(32)
    expiry_epoch = time.time() + UNLOCK_COOKIE_MAX_AGE_SECONDS
    with GATE_LOCK:
        store = load_token_store()
        store[token] = expiry_epoch
        save_token_store(store)
    return token


def invalidate_token(token: str) -> bool:
    """Drop `token` from the store. Returns True if it was present."""
    if not isinstance(token, str) or not token:
        return False
    with GATE_LOCK:
        store = load_token_store()
        if token not in store:
            return False
        del store[token]
        save_token_store(store)
        return True


def is_token_valid(token: str) -> bool:
    """Token exists in the store and hasn't expired.

    Loading the store here also prunes expired tokens — cheap, and keeps a
    revoked/expired token from lingering across a race.
    """
    if not isinstance(token, str) or not token:
        return False
    if len(token) > MAX_TOKEN_LENGTH:
        return False
    if any(char not in TOKEN_ALLOWED_CHARS for char in token):
        return False
    with GATE_LOCK:
        return token in load_token_store()


# --- Cookie parsing ----------------------------------------------------------

def extract_unlock_cookie_value(cookie_header: Optional[str]) -> Optional[str]:
    """Pull the `mineru_unlock` value out of a raw Cookie header.

    Manual scan (not `http.cookies.SimpleCookie`) because SimpleCookie is
    lenient about attributes we don't care about and can raise on odd input
    from other cookies on the same host. We know our cookie name and shape;
    reject anything else outright.
    """
    if not cookie_header:
        return None
    for pair in cookie_header.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        equals = pair.find("=")
        if equals < 0:
            continue
        name = pair[:equals].strip()
        if name != UNLOCK_COOKIE_NAME:
            continue
        value = pair[equals + 1:].strip()
        # Some clients wrap cookie values in double quotes.
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        if not value or len(value) > MAX_TOKEN_LENGTH:
            return None
        if any(char not in TOKEN_ALLOWED_CHARS for char in value):
            return None
        return value
    return None


def request_has_valid_unlock_cookie(headers) -> bool:
    """True iff the request's Cookie header carries a live, unexpired token."""
    cookie_header = headers.get("Cookie") if headers is not None else None
    token = extract_unlock_cookie_value(cookie_header)
    if token is None:
        return False
    return is_token_valid(token)


# --- Loopback detection for the Secure cookie attribute ---------------------
#
# Browsers refuse to store a `Secure` cookie over plain HTTP, so if we always
# set Secure the unlock loop 200s while the cookie is silently dropped and the
# next request re-locks. Over the tailnet the app is exposed via
# `tailscale serve` (HTTPS terminated at the proxy → HTTP into 127.0.0.1:PORT),
# so the transport IS secure and Secure must stay set. Over local dev/QA the
# request comes in on `http://127.0.0.1:<port>` (or `localhost`, `[::1]`) with
# no TLS in front, and we must OMIT Secure so the cookie actually sticks.
#
# Loopback traffic never leaves the machine, so a non-Secure cookie there is
# safe. The Host header is validated by the Host-allowlist BEFORE this check,
# so `host` is already one of the allowlisted values — we just strip the port
# and ask "is the hostname a loopback name?".

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Self-enforcing invariant: the tailnet host must never be loopback-shaped, or
# cookie_secure_for_host would drop Secure for the real HTTPS deployment. If a
# future tailnet rename lands on a loopback name, fail loudly at import, not at
# the first cookie mint. (Security-audit hardening, 2026-08-19.)
assert TAILNET_HOSTNAME not in LOOPBACK_HOSTNAMES, (
    f"TAILNET_HOSTNAME {TAILNET_HOSTNAME!r} must not be a loopback name"
)


def cookie_secure_for_host(host: Optional[str]) -> bool:
    """True iff a Set-Cookie for this request Host should carry `Secure`.

    - Loopback (`127.0.0.1`, `localhost`, `[::1]`, `::1`, optionally with a
      trailing `:port`) → False. Traffic is not network-observable, and
      Secure would otherwise trap plain-HTTP loopback in an unlock loop.
    - Anything else (the tailnet name served over HTTPS by `tailscale serve`)
      → True.
    - Empty / None / unrecognized → True (fail-safe: keep Secure on).

    Assumes `host` has already passed the Host-allowlist check upstream, so
    the only inputs seen in production are `127.0.0.1:<port>`,
    `localhost:<port>`, or the configured `TAILNET_HOSTNAME`.
    """
    if not host:
        return True
    normalized = host.strip().lower()
    if not normalized:
        return True
    # Bracketed IPv6 literal (e.g. `[::1]:5195`) — keep the brackets in the
    # hostname so `[::1]` matches the allowlist entry cleanly, and split the
    # optional `:port` after the closing bracket.
    if normalized.startswith("["):
        bracket_end = normalized.find("]")
        if bracket_end < 0:
            return True   # malformed; fail-safe.
        hostname = normalized[: bracket_end + 1]
    else:
        # Bare IPv4 / hostname: strip everything from the first colon onward.
        hostname = normalized.split(":", 1)[0]
    return hostname not in LOOPBACK_HOSTNAMES


def build_unlock_set_cookie_header(token: str, host: Optional[str] = None) -> str:
    """The Set-Cookie value for a successful unlock.

    Always HttpOnly (XSS can't read), SameSite=Strict (CSRF-resistant),
    Path=/, Max-Age=7d. The `Secure` attribute is CONDITIONAL on `host`
    via `cookie_secure_for_host`: on for the tailnet (HTTPS), off for
    loopback (plain HTTP dev/QA — browsers otherwise drop Secure cookies).
    Default `host=None` keeps Secure on, so an accidental omission
    fails safe.
    """
    return _build_cookie_header(token=token, max_age=UNLOCK_COOKIE_MAX_AGE_SECONDS, host=host)


def build_unlock_clear_cookie_header(host: Optional[str] = None) -> str:
    """Set-Cookie value that clears the client's cookie (used by /api/lock).

    Mirrors `build_unlock_set_cookie_header`'s Secure decision so a clear
    over loopback (dev/QA) isn't silently discarded by the browser.
    """
    return _build_cookie_header(token="", max_age=0, host=host)


def _build_cookie_header(token: str, max_age: int, host: Optional[str]) -> str:
    """Shared Set-Cookie assembly for set and clear (Secure toggles on host)."""
    secure_attr = "; Secure" if cookie_secure_for_host(host) else ""
    return (
        f"{UNLOCK_COOKIE_NAME}={token}"
        f"; Path=/"
        f"; Max-Age={max_age}"
        "; HttpOnly"
        f"{secure_attr}"
        "; SameSite=Strict"
    )


# --- Constant-time passphrase compare ----------------------------------------

def verify_passphrase(submitted_passphrase: str, expected_hash_hex: str) -> bool:
    """Constant-time compare of SHA-256(submitted) against expected hash.

    - SHA-256 is fine for a high-entropy passphrase; the real brute-force
      defense is the persistent rate-limit/lockout above.
    - `hmac.compare_digest` avoids leaking prefix-match information through
      response timing (the string-compare shortcut).
    """
    if not isinstance(submitted_passphrase, str) or not isinstance(expected_hash_hex, str):
        return False
    submitted_hash = hashlib.sha256(submitted_passphrase.encode("utf-8")).hexdigest()
    return hmac.compare_digest(
        submitted_hash.encode("ascii"),
        expected_hash_hex.lower().encode("ascii"),
    )


# --- Lockout tracker (persistent, global) ------------------------------------

def load_lockout_state() -> Dict[str, float]:
    """Return {fail_count, locked_until_epoch}. Missing/corrupt file → both zero.

    Every coercion is guarded so a hand-edited (or intentionally poisoned)
    state file — non-numeric strings, `Infinity`, NaN — resets that field to
    zero instead of raising through the dispatcher as a 500. This mirrors
    the per-item skip pattern used in `load_token_store`.
    """
    with GATE_LOCK:
        if not UNLOCK_LOCKOUT_PATH.exists():
            return {"fail_count": 0.0, "locked_until": 0.0}
        try:
            with open(UNLOCK_LOCKOUT_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as read_error:
            logger.warning("unlock-lockout.json unreadable, resetting: %s", read_error)
            return {"fail_count": 0.0, "locked_until": 0.0}
        if not isinstance(data, dict):
            return {"fail_count": 0.0, "locked_until": 0.0}
        return {
            "fail_count": coerce_finite_float(data.get("fail_count"), 0.0),
            "locked_until": coerce_finite_float(data.get("locked_until"), 0.0),
        }


def coerce_finite_float(value, default: float) -> float:
    """Best-effort float coercion that keeps the caller on the happy path.

    Returns `default` on any of:
      - value is None
      - float(value) raises (ValueError, TypeError)
      - the result is NaN or ±inf (comparisons against `now` would be
        undefined and can create a permanent "always locked" state)
    """
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def save_lockout_state(state: Dict[str, float]) -> None:
    """Atomic 0600 write of the lockout state."""
    atomic_write_json_600(UNLOCK_LOCKOUT_PATH, state)


def compute_lockout_seconds(fail_count: int) -> int:
    """OWASP-style exponential backoff:  no lockout for the first
    LOCKOUT_FAILURES_BEFORE_BACKOFF failures; then LOCKOUT_BASE_SECONDS * 2^k.
    Capped at LOCKOUT_MAX_SECONDS (~1h) so a very stubborn attacker can't
    inflate a single window past that.
    """
    if fail_count <= LOCKOUT_FAILURES_BEFORE_BACKOFF:
        return 0
    exponent = fail_count - LOCKOUT_FAILURES_BEFORE_BACKOFF - 1
    if exponent > 30:  # 2**30 seconds is already huge; avoid overflow.
        return LOCKOUT_MAX_SECONDS
    seconds = LOCKOUT_BASE_SECONDS * (2 ** exponent)
    return min(seconds, LOCKOUT_MAX_SECONDS)


def current_lockout_retry_after() -> Optional[int]:
    """If a lockout is active, return Retry-After seconds; else None.

    Called FIRST inside /api/unlock, before any hash work, so a locked-out
    caller doesn't get to keep spending compute (or timing-leak the compare).

    Defense in depth: `load_lockout_state` already sanitizes NaN/inf via
    `coerce_finite_float`, but recheck here so a future refactor that swaps
    the loader can't quietly reintroduce an "always locked" state.
    """
    with GATE_LOCK:
        state = load_lockout_state()
        locked_until = state.get("locked_until", 0.0)
        if not isinstance(locked_until, (int, float)) or not math.isfinite(locked_until):
            return None
        now = time.time()
        if locked_until > now:
            # +1 so the client doesn't wake up microseconds early and retry
            # into the tail end of the window; keeps responses "round" too.
            return int(locked_until - now) + 1
        return None


def record_failed_unlock() -> Tuple[int, int]:
    """Bump the failure counter, apply the new lockout window if we cross it.

    Returns (new_fail_count, applied_lockout_seconds). `applied_lockout_seconds`
    is 0 during the grace period (attempts 1..N), positive once backoff kicks in.
    """
    with GATE_LOCK:
        state = load_lockout_state()
        new_count = int(state.get("fail_count", 0)) + 1
        state["fail_count"] = float(new_count)
        lockout_seconds = compute_lockout_seconds(new_count)
        if lockout_seconds > 0:
            state["locked_until"] = time.time() + lockout_seconds
        save_lockout_state(state)
        return new_count, lockout_seconds


def reset_lockout() -> None:
    """Clear failure count + lockout window. Called on a successful unlock."""
    with GATE_LOCK:
        save_lockout_state({"fail_count": 0.0, "locked_until": 0.0})
