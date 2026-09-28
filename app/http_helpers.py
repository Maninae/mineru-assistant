"""HTTP response helpers, security headers, and shared file-serving choke points.

Kept separate from server.py so route handlers can import these without
pulling in the routing table. Everything security-sensitive lives here:
  - SECURITY_HEADERS + build_security_headers (applied to every response)
  - is_path_inside_allowlist (the read-allowlist enforcement)
  - safe_serve_file (extension allowlist + HTML CSP)
  - json_response / error_response / bytes_response

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

from config import (
    ALLOWED_EXTENSIONS,
    HTML_EXTENSIONS,
    MIME_TYPES,
    READ_ALLOWLIST,
)


logger = logging.getLogger(__name__)


MAX_JSON_BODY_BYTES = 4096
DEFAULT_TEXT_ENCODING = "utf-8"

# Hard cap on any file served through safe_serve_file. Anything larger 413s
# instead of being read into RAM in one shot. 25 MB comfortably covers every
# report/creation we ship today; oversized files are almost always accidents
# (a bloated PDF export, a stray dataset) and are better refused loudly than
# silently spiking process memory.
MAX_SERVE_BYTES = 25 * 1024 * 1024


# The default CSP applied to every non-sandbox response. Blocks inline JS,
# blocks cross-origin fetch/XHR/WebSocket, blocks framing, blocks base-href
# hijacks, blocks forms. Style needs `'unsafe-inline'` because the CSS custom-
# property theming assigns styles via element.style / inline `style="..."`.
DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "frame-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'none'"
)


# Fixed headers added to every response by every route.
BASE_SECURITY_HEADERS: Dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": DEFAULT_CSP,
}


# CSP for the sandboxed HTML iframe endpoint (e.g. anthology.html).
#
# `script-src 'unsafe-inline'` is deliberately allowed here (and ONLY here)
# so author-trusted creations pages that ship inline `<script>` — the
# encrypted anthology reader, self-contained data viz — actually work. Safe
# because the shell renders these responses inside `<iframe sandbox="allow-scripts">`
# (no `allow-same-origin`), so the frame is null-origin: no access to the
# app's cookies/localStorage/DOM, no fetch/XHR back to /api/*, no postMessage
# reach into the shell. `connect-src` / `frame-src` are still blocked by
# `default-src 'none'`, so even if a bug widened the CSP nothing here can
# call home. NEVER add 'unsafe-eval', NEVER apply these headers to the main
# app shell (the shell keeps DEFAULT_CSP with its strict `script-src 'self'`),
# and NEVER widen script-src beyond 'unsafe-inline'.
SANDBOX_HTML_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "img-src data: blob:; "
    "style-src 'unsafe-inline'; "
    "font-src data:; "
    "media-src data: blob:; "
    "frame-ancestors 'self'"
)


SANDBOX_HTML_HEADERS: Dict[str, str] = {
    "Content-Security-Policy": SANDBOX_HTML_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "SAMEORIGIN",
}


def apply_security_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Return a new headers dict with the base security headers added.

    Never lets a route silently omit them; explicit overrides in `headers`
    win only for headers the route deliberately set (e.g. Content-Length,
    Cache-Control). Security-critical headers are re-imposed here.
    """
    merged = dict(headers)
    for key, value in BASE_SECURITY_HEADERS.items():
        merged[key] = value
    return merged


def json_response(payload: Dict, status: int = 200, cache_seconds: int = 5) -> Tuple[int, Dict[str, str], bytes]:
    """Serialize a dict to a small-cache JSON response."""
    body = json.dumps(payload, ensure_ascii=False).encode(DEFAULT_TEXT_ENCODING)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Cache-Control": f"private, max-age={cache_seconds}",
    }
    return status, apply_security_headers(headers), body


def error_response(status: int, message: str) -> Tuple[int, Dict[str, str], bytes]:
    """Compact JSON error body. Never echoes internal paths."""
    return json_response({"error": message}, status=status, cache_seconds=0)


def is_path_inside_allowlist(candidate: Path) -> bool:
    """True iff candidate resolves under any registered read root.

    Uses Path.resolve() so a symlink that escapes the root fails the
    relative_to check and returns False.
    """
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    for allowed_root in READ_ALLOWLIST:
        try:
            resolved.relative_to(allowed_root)
            return True
        except ValueError:
            continue
    return False


# --- Safe filesystem probes ---------------------------------------------------
#
# Path components longer than 255 bytes (macOS NAME_MAX) make every FS syscall
# raise OSError [Errno 63] "File name too long", including .exists() and
# .is_file(). Untrusted URL segments come in longer than that all the time.
# Wrapping the probe in a small helper keeps request handlers from having to
# repeat the try/except at every site, and it turns the class of "path is
# structurally impossible" errors into a clean False so callers 404 rather
# than 500 with a stack trace in the log.

def safe_resolve(candidate: Path) -> Optional[Path]:
    """Path.resolve(), or None if the OS refuses (ENAMETOOLONG, ENOENT loop, etc)."""
    try:
        return candidate.resolve()
    except OSError:
        return None


def safe_exists(candidate: Path) -> bool:
    """Path.exists() that treats "impossible path" the same as "missing"."""
    try:
        return candidate.exists()
    except OSError:
        return False


def safe_is_file(candidate: Path) -> bool:
    """Path.is_file() that treats "impossible path" the same as "not a file"."""
    try:
        return candidate.is_file()
    except OSError:
        return False


def safe_is_dir(candidate: Path) -> bool:
    """Path.is_dir() that treats "impossible path" the same as "not a dir"."""
    try:
        return candidate.is_dir()
    except OSError:
        return False


def bytes_response(
    body: bytes,
    content_type: str,
    filename: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, str], bytes]:
    """Wrap raw bytes into a 200 response with sensible defaults.

    Security headers are re-applied last so a per-route `extra_headers`
    cannot weaken CSP/nosniff/etc.
    """
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Cache-Control": "private, max-age=30",
    }
    if filename:
        headers["Content-Disposition"] = f'inline; filename="{filename}"'
    if extra_headers:
        headers.update(extra_headers)
    return 200, apply_security_headers(headers), body


def safe_serve_file(target_path: Path) -> Tuple[int, Dict[str, str], bytes]:
    """Serve one file from anywhere under READ_ALLOWLIST.

    Enforces:
      - extension allowlist (404 otherwise)
      - path resolved under an allowed root (404 otherwise)
      - HTML files: the sandbox-specific CSP overrides the default so a
        stored page can't reach back into the app API even if the client
        forgets to add sandbox=""
    """
    if not is_path_inside_allowlist(target_path):
        return error_response(404, "not found")
    if not safe_is_file(target_path):
        return error_response(404, "not found")

    extension = target_path.suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return error_response(404, "not found")

    try:
        file_size = target_path.stat().st_size
    except OSError as stat_error:
        logger.warning("safe_serve_file: stat refused (errno %s)", stat_error.errno)
        return error_response(404, "not found")
    if file_size > MAX_SERVE_BYTES:
        logger.warning("safe_serve_file: refusing oversized file (%d bytes)", file_size)
        return error_response(413, "file too large")

    try:
        body = target_path.read_bytes()
    except OSError as read_error:
        # Terse warning only; the raw error's message includes the offending
        # path segment which we do NOT want smeared across the log.
        logger.warning("safe_serve_file: read refused (errno %s)", read_error.errno)
        return error_response(404, "not found")

    content_type = MIME_TYPES.get(extension, "application/octet-stream")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Cache-Control": "private, max-age=30",
        "Content-Disposition": f'inline; filename="{target_path.name}"',
    }
    if extension in HTML_EXTENSIONS:
        # Only path where the strict-sandbox CSP takes effect. Everything
        # else uses the default CSP + BASE_SECURITY_HEADERS.
        headers.update(SANDBOX_HTML_HEADERS)
        return 200, headers, body
    return 200, apply_security_headers(headers), body
