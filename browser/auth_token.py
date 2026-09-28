"""Per-boot bearer-token auth for the local browser automation server.

The browser server binds `127.0.0.1:9471` and today accepts any local
POST /action — meaning any process running as the current user can call
`cookies export` (dumping live Gmail / LinkedIn / Slack / MyGreenhouse
session cookies to disk) or `evaluate document.cookie` (which returns
cookies straight back over HTTP). 127.0.0.1 is reachable by every
process on the machine, so port binding alone is not a security
boundary — the C1 finding from the Sep 4 2026 security audit.

This module implements the fix: the server generates a random per-boot
bearer token at startup, writes it to a 0600 file that only the owner
can read, and requires every mutating request to present it in the
`Authorization: Bearer <token>` header. The token file is overwritten
on every boot so a stolen token from a previous session is worthless.

Compared token strings use `hmac.compare_digest` so the reject/accept
decision does not leak timing information about how many prefix chars
matched.

Path shape:

    $MINERU_HOME/cache/browser-server.token       # 0600, owner-only
"""

from __future__ import annotations

import errno
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

from browser.config import MINERU_HOME, logger


# ---------------------------------------------------------------------------
# File path derivation. Kept in sync with `browser.config.MINERU_HOME` so a
# test that redirects the workspace via `MINERU_HOME=<tmp>` sees the token
# in its own tmp tree, never the operator's live `~/.mineru/cache/`.
# ---------------------------------------------------------------------------


def token_path() -> Path:
    """Return the per-boot bearer-token file path under `$MINERU_HOME/cache/`.

    Resolved at call time (not import time) so a per-test env override on
    `MINERU_HOME` takes effect. Production servers read the operator's
    real `$MINERU_HOME`; tests point it at `tmp_path`.
    """
    home = Path(os.environ.get("MINERU_HOME", str(MINERU_HOME)))
    return home / "cache" / "browser-server.token"


# ---------------------------------------------------------------------------
# Token generation / persistence (server side).
# ---------------------------------------------------------------------------


def generate_and_write_token() -> str:
    """Mint a fresh 32-byte URL-safe token; overwrite the 0600 file; return the token.

    - The parent directory is created with mode 0700 if missing (mirrors the
      cloakbrowser-profile permissions) so the token file cannot be world-
      or group-readable via directory traversal.
    - The token file is opened with `O_CREAT | O_TRUNC` and mode `0600` so
      the file is truncated on every boot: a leaked/stale token from a
      previous run cannot be reused against the new server.
    - `secrets.token_urlsafe(32)` returns ~43 URL-safe chars derived from
      32 random bytes — well above the 128-bit threshold for a bearer
      token that only lives for the process lifetime.
    """
    tok = secrets.token_urlsafe(32)
    path = token_path()
    _ensure_parent_dir(path)
    # O_TRUNC + mode 0600 via os.open so we set permissions atomically
    # (a plain `open("w")` + chmod is a window where the file is
    # world-readable). O_CLOEXEC prevents leaking the fd across an exec.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, tok.encode("utf-8"))
    finally:
        os.close(fd)
    # Belt-and-suspenders: if the umask changed the mode, force 0600.
    try:
        os.chmod(str(path), 0o600)
    except OSError as exc:
        logger.warning("browser auth: chmod 0600 on %s failed: %s", path, exc)
    logger.info("browser auth: wrote per-boot bearer token to %s", path)
    return tok


def _ensure_parent_dir(path: Path) -> None:
    """Create the token file's parent dir with mode 0700 if it doesn't exist."""
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "browser auth: cannot create parent dir %s: %s" % (parent, exc)
        ) from exc
    # Tighten mode iff we just created it (a pre-existing dir is left
    # alone so we don't clobber a shared `cache/` mode set on purpose).
    try:
        os.chmod(str(parent), 0o700)
    except OSError:
        # Non-fatal — the token file itself is 0600 regardless.
        pass


# ---------------------------------------------------------------------------
# Token read + constant-time verify (server + client side).
# ---------------------------------------------------------------------------


def read_token_or_none() -> Optional[str]:
    """Return the current bearer token, or None if the file is absent/unreadable.

    Used by the server on every request (cheap: a single small file
    read; token files are ~43 bytes). Never raises — a missing file or
    permission error is treated the same as an absent token, and the
    caller must reject the request.
    """
    path = token_path()
    try:
        with open(str(path), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("browser auth: could not read token file %s: %s", path, exc)
        return None
    tok = data.decode("utf-8", errors="replace").strip()
    if not tok:
        return None
    return tok


def read_token_or_raise() -> str:
    """Client-side read: return the token or raise a clear error.

    Used by `bin/browser` before firing a POST. If the token file is
    absent the server is either not up or is stale (pre-auth-hardening
    binary); the caller cannot proceed, so we raise with a message that
    tells the operator exactly which file we expected and why.
    """
    tok = read_token_or_none()
    if tok is None:
        path = token_path()
        raise RuntimeError(
            "browser auth: no bearer token at %s. The browser server "
            "generates this file on startup with mode 0600; a missing "
            "file usually means the server is not running (start it "
            "with `mineru browser` or `python3 %s/browser/server.py`) "
            "or that the running server pre-dates the auth-hardening "
            "change. Restart the server to mint a fresh token." % (
                path, os.environ.get("MINERU_HOME", str(MINERU_HOME))
            )
        )
    return tok


def verify_bearer(header_value: Optional[str], expected_token: Optional[str]) -> bool:
    """Constant-time compare of a `Authorization: Bearer …` header to the token.

    Returns True iff the header is present, well-formed (`Bearer <tok>`),
    and matches `expected_token` byte-for-byte. Uses `hmac.compare_digest`
    so a mismatch on the first byte and a mismatch on the last byte take
    the same wall-clock time (defeats naive timing side channels).

    Both a missing header AND a missing expected_token return False — the
    server rejects a request when either side is unavailable, so an
    accidentally-deleted token file cannot become a "no auth required"
    fallback.
    """
    if not header_value or not expected_token:
        return False
    # RFC 7235 §2.1: "Bearer <token>" (case-insensitive scheme).
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2:
        return False
    scheme, presented = parts[0], parts[1].strip()
    if scheme.lower() != "bearer":
        return False
    # `hmac.compare_digest` requires both operands to be str-or-both-bytes.
    return hmac.compare_digest(presented, expected_token)
