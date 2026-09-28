"""HTTP dispatcher + entry point for the Mineru web app.

The route table is declared once here; each handler lives in handlers.py.
Path allowlist enforcement, extension checks, and JSON body caps live in
http_helpers.py so nothing gets re-implemented.

Binds 127.0.0.1:5195 by default (see config.HOST / config.PORT). Tailnet
exposure happens outside this process via `tailscale serve`; the server
never binds a wider interface.

Two boundary checks live here (not in per-route handlers), because they gate
every request equally:
  - Host-header allowlist: rejects DNS-rebinding attempts that trick a
    browser into POSTing to 127.0.0.1 with an attacker-controlled hostname.
  - Cross-origin write protection: POSTs must carry `Content-Type: application/json`
    (blocks the text/plain CSRF that skips preflight) and, when Origin is
    present, must originate from an allowed origin. OPTIONS returns 405 so
    no CORS preflight ever succeeds.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import http.server
import logging
import re
import socketserver
import sys
import urllib.parse
from typing import Callable, Dict, List, Optional, Set, Tuple

from config import (
    HOST,
    PORT,
    READ_ALLOWLIST,
    TAILNET_HOSTNAME,
    build_allowed_hosts,
    build_allowed_origins,
)
import handlers
import push_endpoints
import seen_ledger
import unlock_gate
from http_helpers import MAX_JSON_BODY_BYTES, error_response


logger = logging.getLogger("mineru_app")


# --- Route table --------------------------------------------------------------

# (method, compiled_pattern, handler, wants_body)
ROUTES: List[Tuple[str, re.Pattern, Callable, bool]] = [
    ("GET", re.compile(r"^/$"), handlers.handle_root, False),
    ("GET", re.compile(r"^/lock/?$"), handlers.handle_lock_screen, False),
    ("GET", re.compile(r"^/manifest\.webmanifest$"), handlers.handle_manifest, False),
    ("GET", re.compile(r"^/sw\.js$"), handlers.handle_service_worker, False),
    ("GET", re.compile(r"^/static/(?P<rel>.+)$"), handlers.handle_static, False),
    ("GET", re.compile(r"^/api/feeds/?$"), handlers.handle_feeds_index, False),
    ("GET", re.compile(r"^/api/feed/(?P<feed_id>[A-Za-z0-9_-]+)/?$"), handlers.handle_feed_page, False),
    ("GET", re.compile(r"^/api/brief/(?P<feed_id>[A-Za-z0-9_-]+)/(?P<filename>.+)$"), handlers.handle_brief_detail, False),
    ("GET", re.compile(r"^/api/library/(?P<source>[A-Za-z0-9_-]+)(?:/(?P<relpath>.+))?/?$"), handlers.handle_library, False),
    ("GET", re.compile(r"^/raw/library/(?P<source>[A-Za-z0-9_-]+)/(?P<relpath>.+)$"), handlers.handle_raw_library, False),
    ("GET", re.compile(r"^/sandbox/library/(?P<source>[A-Za-z0-9_-]+)/(?P<relpath>.+)$"), handlers.handle_sandbox_library, False),
    ("GET", re.compile(r"^/api/pulse/?$"), handlers.handle_pulse, False),
    ("GET", re.compile(r"^/api/today/?$"), handlers.handle_today, False),
    ("GET", re.compile(r"^/api/search/?$"), handlers.handle_search, False),
    ("POST", re.compile(r"^/api/seen/?$"), handlers.handle_seen_post, True),
    ("POST", re.compile(r"^/api/unlock/?$"), handlers.handle_unlock, True),
    ("GET", re.compile(r"^/api/push/vapid-key/?$"), push_endpoints.handle_push_vapid_key, False),
    ("POST", re.compile(r"^/api/push/subscribe/?$"), push_endpoints.handle_push_subscribe, True),
    ("POST", re.compile(r"^/api/push/unsubscribe/?$"), push_endpoints.handle_push_unsubscribe, True),
]


# --- Passphrase-gate exemption allowlist -------------------------------------
#
# When the gate is enabled (Keychain has the hash), every request outside this
# tiny set requires a valid `mineru_unlock` cookie. The set is intentionally
# path-literal — not a prefix match — so an attacker can't sneak a longer path
# (`/lock/../api/feeds`) or a `?query`-shaped extension past the check.
#
# Only the lock screen's OWN assets get in:
#   - POST /api/unlock: the unlock endpoint itself (must run write-guards too).
#   - GET  /lock:       the lock screen HTML.
#   - GET  /static/css/lock.css, /static/js/lock.js: its own tiny CSS + JS.
#   - GET  /static/css/tokens.css: shared theme tokens the lock screen reuses.
#   - GET  /static/icons/favicon.svg + apple-touch-icon-180.png: browser tab / iOS
#     home-screen icon so a locked tab still shows the Mineru mark.
#
# Notably NOT exempt (deliberately): /manifest.webmanifest, /sw.js, and the
# app's own bundle JS/CSS. A locked device must never load anything that could
# render app data or install a service worker over the app path.

EXEMPT_WHEN_LOCKED_POST_PATHS: Set[str] = {
    "/api/unlock",
    "/api/unlock/",
}

EXEMPT_WHEN_LOCKED_GET_PATHS: Set[str] = {
    "/lock",
    "/lock/",
    "/static/css/lock.css",
    "/static/css/tokens.css",
    "/static/js/lock.js",
    "/static/icons/favicon.svg",
    "/static/icons/apple-touch-icon-180.png",
}


def is_request_exempt_when_locked(method: str, path: str) -> bool:
    """True iff the (method, path) pair is one of the lock-screen assets."""
    if method == "POST":
        return path in EXEMPT_WHEN_LOCKED_POST_PATHS
    if method in ("GET", "HEAD"):
        return path in EXEMPT_WHEN_LOCKED_GET_PATHS
    return False


def request_accepts_html(headers) -> bool:
    """Very small "is this a browser navigation" heuristic.

    The Accept header on a navigational load includes `text/html`; API/fetch
    calls request `application/json` (or `*/*` from curl). We use this only to
    pick between "serve the lock screen (browser)" and "return 401 (API)"
    when a gated request comes in without a valid cookie.
    """
    accept = (headers.get("Accept") or "").lower()
    return "text/html" in accept


JSON_CONTENT_TYPE = "application/json"


# --- HTTP handler -------------------------------------------------------------

class MineruRequestHandler(http.server.BaseHTTPRequestHandler):
    """One-shot dispatcher over the ROUTES table.

    `allowed_hosts` and `allowed_origins` are populated in main() after the
    CLI port is resolved, so the same handler class works when the app is
    launched on a non-default port.
    """

    # Bland server identity — no framework or Python version disclosed.
    server_version = "Mineru"
    sys_version = ""

    # Populated at startup with the port-parameterized allowlists.
    allowed_hosts: Set[str] = set()
    allowed_origins: Set[str] = set()

    def version_string(self) -> str:
        # Overriding version_string is what actually silences Python 3.9's
        # default `Server: BaseHTTP/0.6 Python/3.9.6` disclosure.
        return "Mineru"

    def log_message(self, format: str, *args) -> None:
        # Paths and counts only, per SECURITY: no brief bodies land in logs.
        logger.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self) -> None:
        self.dispatch("GET")

    # HEAD is treated as a GET whose body is dropped by BaseHTTPRequestHandler
    # (Python's http.server takes care of not writing the body for HEAD only
    # if `command` is set; we still just run the same handler and let write_
    # response emit headers-only when command is HEAD).
    def do_HEAD(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_OPTIONS(self) -> None:
        """No-op OPTIONS handler — 405s every preflight.

        The write API only accepts same-origin POSTs (Content-Type=JSON), and
        we never want a CORS preflight to succeed here. Returning 405 for
        every OPTIONS keeps that door closed.
        """
        status, headers_out, body = error_response(405, "method not allowed")
        self.write_response(status, headers_out, body)

    def dispatch(self, method: str) -> None:
        # Host allowlist — first line of defense against DNS rebinding. A
        # missing Host header is HTTP/1.1-illegal; reject with 400 like a
        # forged one.
        host_header = (self.headers.get("Host") or "").strip().lower()
        if host_header not in self.allowed_hosts:
            logger.info("host rejected: %r", host_header)
            status, headers_out, body = error_response(421, "misdirected request")
            self.write_response(status, headers_out, body)
            return

        split = urllib.parse.urlsplit(self.path)

        # Passphrase gate. Runs BEFORE route matching so no data handler can
        # be reached before the cookie is validated. If the gate is disabled
        # (no Keychain hash, option A), this is a no-op and the app behaves
        # byte-identical to today. If the gate is enabled:
        #   - navigational GET without a valid cookie → serve the lock screen
        #   - anything else without a valid cookie → 401 {"error":"locked"}
        # The three-item exempt allowlist (unlock endpoint + lock screen +
        # its own static assets) is the ONLY thing that ever reaches the
        # ROUTES table while locked. `POST /api/unlock` is exempt from the
        # gate but still runs the dispatcher's Content-Type / Origin write-
        # guards (wants_body=True), so a CSRF or wrong-origin POST is 415/403
        # before it can spend hash-compare compute.
        if unlock_gate.is_gate_enabled():
            if not is_request_exempt_when_locked(method, split.path):
                if not unlock_gate.request_has_valid_unlock_cookie(self.headers):
                    if request_accepts_html(self.headers) and method in ("GET", "HEAD"):
                        status, headers_out, body = handlers.handle_lock_screen(None, None)
                    else:
                        status, headers_out, body = error_response(401, "locked")
                    self.write_response(status, headers_out, body)
                    return

        query = urllib.parse.parse_qs(split.query)
        for route_method, pattern, handler, wants_body in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(split.path)
            if not match:
                continue
            try:
                if wants_body:
                    guard_response = self.enforce_write_guards()
                    if guard_response is not None:
                        self.write_response(*guard_response)
                        return
                    length = int(self.headers.get("Content-Length") or "0")
                    if length < 0 or length > MAX_JSON_BODY_BYTES:
                        status, headers_out, body = error_response(413, "body too large")
                    else:
                        raw_body = self.rfile.read(length) if length else b""
                        # `host` threads the (already Host-allowlist-validated) request
                        # Host into wants_body handlers. handle_unlock uses it to decide
                        # whether the Set-Cookie carries `Secure` (on for the HTTPS
                        # tailnet, off for plain-HTTP loopback dev/QA). Other body
                        # handlers accept and ignore it.
                        status, headers_out, body = handler(match, query, raw_body, host=host_header)
                else:
                    status, headers_out, body = handler(match, query)
            except Exception as unexpected_error:
                logger.exception("handler crashed on %s %s: %s", method, split.path, unexpected_error)
                status, headers_out, body = error_response(500, "internal error")
            self.write_response(status, headers_out, body)
            return
        status, headers_out, body = error_response(404, "not found")
        self.write_response(status, headers_out, body)

    def enforce_write_guards(self) -> Optional[Tuple[int, Dict[str, str], bytes]]:
        """CSRF-hardening checks for state-changing requests.

        Returns an error triple to short-circuit dispatch, or None to proceed.

        - Require Content-Type: application/json. The text/plain CSRF variant
          that bypasses CORS preflight can't set this without triggering an
          OPTIONS preflight, which we 405.
        - If Origin is present, it must be in the allowlist. Same-origin
          browser POSTs always set Origin (fetch/XHR), so a present-but-wrong
          value is a cross-site attempt. A missing Origin is only produced by
          legitimate non-browser clients (curl, tests), and those are still
          blocked by the Content-Type check unless they set it explicitly.
        """
        raw_content_type = (self.headers.get("Content-Type") or "")
        content_type = raw_content_type.split(";", 1)[0].strip().lower()
        if content_type != JSON_CONTENT_TYPE:
            logger.info("write rejected: content-type=%r", raw_content_type)
            return error_response(415, "content-type must be application/json")

        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.allowed_origins:
            logger.info("write rejected: origin=%r", origin)
            return error_response(403, "origin not allowed")
        return None

    def write_response(self, status: int, headers_out: Dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for key, value in headers_out.items():
            self.send_header(key, value)
        self.end_headers()
        # HEAD returns headers only; the Content-Length header is preserved so
        # clients still see the payload size that GET would have delivered.
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Client hung up mid-write; nothing meaningful to log.
                pass


class ThreadingHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded so a slow file read doesn't wedge the sidebar."""
    daemon_threads = True
    allow_reuse_address = True


# --- Entry point --------------------------------------------------------------

def parse_cli_port(argv: List[str]) -> int:
    """Optional `--port N` override for dev/testing."""
    port = PORT
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
    return port


def main(argv: Optional[List[str]] = None) -> int:
    """Bind and serve forever."""
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        level=logging.INFO,
    )
    seen_ledger.ensure_state_dir()
    port = parse_cli_port(argv)

    # Probe the passphrase-gate state once at startup. This is the ONLY
    # Keychain access the app ever makes off the request path — the log
    # line either says "ENABLED" (option B) or "disabled (option A)" and
    # you can trust it for the rest of this process's lifetime.
    unlock_gate.probe_gate_at_startup()

    # Tailnet exposure is a security boundary: the served ts.net name must be in
    # the Host allowlist. Unset means loopback-only — legitimate for local dev,
    # but a misconfig if you meant to expose over the tailnet, so say so loudly.
    if not TAILNET_HOSTNAME:
        logger.warning(
            "MINERU_TAILNET_HOSTNAME is not set - serving loopback-only. To expose "
            "over the tailnet via `tailscale serve`, set it to this app's ts.net name."
        )

    # The Host and Origin allowlists depend on the CLI port. Set them on the
    # request handler class once so every thread sees the same values.
    MineruRequestHandler.allowed_hosts = build_allowed_hosts(port)
    MineruRequestHandler.allowed_origins = build_allowed_origins(port)

    server = ThreadingHttpServer((HOST, port), MineruRequestHandler)
    logger.info("Mineru web app listening on http://%s:%d/", HOST, port)
    logger.info("read allowlist: %d roots", len(READ_ALLOWLIST))
    for allowed_root in READ_ALLOWLIST:
        logger.info("  - %s", allowed_root)
    logger.info("allowed hosts: %s", sorted(MineruRequestHandler.allowed_hosts))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown requested")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
