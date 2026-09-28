#!/usr/bin/env python3
"""HTTP-level security-boundary tests against the real dispatcher.

Spins the REAL `ThreadingHttpServer` on a 127.0.0.1 ephemeral port in gate-OFF
mode (Keychain stubbed to return None so the startup probe reports "disabled"),
points the config at a SyntheticWorkspace tempdir, and hits it with stdlib
`http.client`. This closes the gap the unit-level suite couldn't reach cleanly:
the checks that live in `server.py::dispatch` and `enforce_write_guards`.

Coverage:

- **Host allowlist:** a forged `Host: evil.com` (and other non-allowlisted
  hosts) → 421 misdirected; the CLI-port-derived `127.0.0.1:<port>` and
  `localhost:<port>` → 200.
- **CSRF write-guard on `POST /api/seen`:** `Content-Type: text/plain` → 415;
  `application/json` with a bad `Origin` → 403; correct same-origin
  `application/json` with a real synthetic brief in the tempdir → 200;
  `OPTIONS /api/seen` → 405 (CORS preflight always fails).
- **Path traversal at the HTTP layer:** `/api/library/reports/../landline.json`,
  percent-encoded `..%2f`, `/api/brief/<feed>/<300-char name>`, and a
  NUL-byte path → 404 (never 500).
- **Security headers on a normal GET:** CSP, X-Content-Type-Options: nosniff,
  Referrer-Policy: no-referrer, X-Frame-Options: DENY, and a bland `Server:`
  header (no Python version leak).

The Keychain is never touched. Wall-clock is never depended on. Real state
under `$MINERU_HOME/` is never read or written.

Run: python3 -m pytest tests/test_http_security.py -q
     python3 tests/test_http_security.py
"""

import http.client
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace   # noqa: E402

import server as server_module   # noqa: E402
import unlock_gate   # noqa: E402


# --- HTTP server fixture -----------------------------------------------------

class HttpServerFixture:
    """Spins the real ThreadingHttpServer in gate-OFF mode on a random port.

    Usage:
        fx = HttpServerFixture()
        fx.start()
        # ... make requests ...
        fx.stop()
    """

    def __init__(self):
        self.workspace = None
        self.httpd = None
        self.thread = None
        self.port = None
        self._original_keychain_read = None
        self._original_allowed_hosts = None
        self._original_allowed_origins = None

    def start(self) -> None:
        # 1. Point config at a synthetic tempdir — never touches real state.
        self.workspace = SyntheticWorkspace()
        self.workspace.setup()

        # 2. Force gate OFF: Keychain read returns None, refresh cached state.
        #    This mirrors option A behavior — no test hash needed, no Keychain
        #    subprocess ever runs, the app dispatcher never sees the passphrase-
        #    gate branch.
        self._original_keychain_read = unlock_gate.read_passphrase_hash_from_keychain
        unlock_gate.read_passphrase_hash_from_keychain = lambda: None
        unlock_gate.GATE_ENABLED_CACHE = None
        unlock_gate.refresh_gate_state()
        assert not unlock_gate.is_gate_enabled(), \
            "expected gate OFF for these tests"

        # 3. Bind on an ephemeral port. Port 0 → OS assigns; server_address[1]
        #    gives back the actual bound port so allowed_hosts can be built
        #    correctly.
        self.httpd = server_module.ThreadingHttpServer(
            ("127.0.0.1", 0), server_module.MineruRequestHandler,
        )
        self.port = self.httpd.server_address[1]

        # 4. Snapshot the current allowlists and set the port-scoped ones so
        #    the SAME test process can restart the fixture without leaking
        #    values across cases.
        self._original_allowed_hosts = server_module.MineruRequestHandler.allowed_hosts
        self._original_allowed_origins = server_module.MineruRequestHandler.allowed_origins
        server_module.MineruRequestHandler.allowed_hosts = {
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
        }
        server_module.MineruRequestHandler.allowed_origins = {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        }

        # 5. Serve in a background thread; daemon=True means an assertion
        #    failure that skips teardown won't leave a stuck server behind.
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        # Restore handler-class attrs to whatever they were before.
        if self._original_allowed_hosts is not None:
            server_module.MineruRequestHandler.allowed_hosts = self._original_allowed_hosts
        if self._original_allowed_origins is not None:
            server_module.MineruRequestHandler.allowed_origins = self._original_allowed_origins
        # Restore Keychain read + gate cache.
        if self._original_keychain_read is not None:
            unlock_gate.read_passphrase_hash_from_keychain = self._original_keychain_read
        unlock_gate.GATE_ENABLED_CACHE = None
        if self.workspace is not None:
            self.workspace.teardown()

    # -- HTTP helper ---------------------------------------------------------

    def request(self, method: str, path: str, *, host=None, body=None,
                extra_headers=None):
        """Return (status, headers_dict, body_bytes) for one request.

        `host` overrides the Host header (needed for forged-Host tests). By
        default we send the canonical `127.0.0.1:<port>` which the allowlist
        accepts.
        """
        headers = {"Host": host if host is not None else f"127.0.0.1:{self.port}"}
        if extra_headers:
            headers.update(extra_headers)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k: v for k, v in resp.getheaders()}
            resp_body = resp.read()
        finally:
            conn.close()
        return status, resp_headers, resp_body


class _HttpTestCaseBase(unittest.TestCase):
    """Shared setUp/tearDown wiring; the fixture is per-test so no state
    leaks between cases and a failing test can't wedge the next one."""

    def setUp(self):
        self.fx = HttpServerFixture()
        try:
            self.fx.start()
        except OSError as bind_error:
            # Sandboxed environments sometimes deny loopback binds; skip
            # cleanly rather than hang or false-fail.
            self.skipTest(f"cannot bind ephemeral port: {bind_error}")

    def tearDown(self):
        self.fx.stop()


# --- Host allowlist ----------------------------------------------------------

class HostAllowlistTest(_HttpTestCaseBase):
    def test_forged_host_returns_421(self):
        status, _headers, body = self.fx.request(
            "GET", "/api/feeds", host="evil.com",
        )
        self.assertEqual(status, 421, f"forged Host: evil.com must 421, got {status}")
        self.assertIn(b"misdirected", body)

    def test_other_non_allowlisted_hosts_return_421(self):
        # A neighbor loopback address, a random ts.net that isn't ours,
        # an empty Host — all must lose.
        for bad in ("127.0.0.2:1", "some-other.tailnet-example.ts.net",
                    "attacker.example.com", ""):
            status, _headers, _body = self.fx.request(
                "GET", "/api/feeds", host=bad,
            )
            self.assertEqual(status, 421,
                             f"non-allowlisted Host={bad!r} must 421, got {status}")

    def test_loopback_ip_host_accepted(self):
        status, _headers, _body = self.fx.request(
            "GET", "/api/feeds", host=f"127.0.0.1:{self.fx.port}",
        )
        self.assertEqual(status, 200)

    def test_localhost_alias_accepted(self):
        status, _headers, _body = self.fx.request(
            "GET", "/api/feeds", host=f"localhost:{self.fx.port}",
        )
        self.assertEqual(status, 200)

    def test_host_check_is_case_insensitive(self):
        # `dispatch()` lowercases the host header before comparison.
        status, _headers, _body = self.fx.request(
            "GET", "/api/feeds", host=f"LOCALHOST:{self.fx.port}",
        )
        self.assertEqual(status, 200)


# --- CSRF write-guards on POST /api/seen -------------------------------------

class WriteGuardTest(_HttpTestCaseBase):
    def _seed_brief(self, filename="test-brief.md"):
        """Write a real brief the seen-post handler will accept."""
        self.fx.workspace.write_brief(
            "alpha", filename, "# Test\n\nTLDR: sample\n", mtime=100.0,
        )

    def _post_json(self, body: bytes, extra_headers=None):
        merged = {"Content-Type": "application/json"}
        if extra_headers:
            merged.update(extra_headers)
        return self.fx.request(
            "POST", "/api/seen", body=body, extra_headers=merged,
        )

    def test_text_plain_content_type_returns_415(self):
        status, _headers, body = self.fx.request(
            "POST", "/api/seen",
            body=b'{"feed_id":"alpha","filename":"x.md"}',
            extra_headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertIn(b"content-type", body.lower())

    def test_missing_content_type_returns_415(self):
        status, _headers, _body = self.fx.request(
            "POST", "/api/seen", body=b'{}',
        )
        self.assertEqual(status, 415)

    def test_form_urlencoded_content_type_returns_415(self):
        # Another "simple" cross-origin POST shape.
        status, _headers, _body = self.fx.request(
            "POST", "/api/seen",
            body=b'feed_id=alpha&filename=x.md',
            extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 415)

    def test_bad_origin_returns_403(self):
        self._seed_brief()
        status, _headers, _body = self._post_json(
            b'{"feed_id":"alpha","filename":"test-brief.md"}',
            extra_headers={"Origin": "https://evil.com"},
        )
        self.assertEqual(status, 403)

    def test_correct_same_origin_json_returns_200(self):
        self._seed_brief()
        status, _headers, body = self._post_json(
            b'{"feed_id":"alpha","filename":"test-brief.md"}',
            extra_headers={"Origin": f"http://127.0.0.1:{self.fx.port}"},
        )
        self.assertEqual(status, 200, f"expected 200, got {status} body={body!r}")
        self.assertIn(b'"ok": true', body)

    def test_no_origin_json_still_reaches_handler(self):
        # A non-browser client (curl, tests) with the right Content-Type but
        # no Origin should still get through — the write-guard only rejects
        # PRESENT-but-wrong origins, per the docstring.
        self._seed_brief()
        status, _headers, body = self._post_json(
            b'{"feed_id":"alpha","filename":"test-brief.md"}',
        )
        self.assertEqual(status, 200, f"expected 200, got {status} body={body!r}")

    def test_options_returns_405(self):
        status, _headers, _body = self.fx.request("OPTIONS", "/api/seen")
        self.assertEqual(status, 405,
                         "CORS preflight must never succeed against /api/seen")

    def test_options_returns_405_on_any_path(self):
        # OPTIONS is refused globally, not just for /api/seen.
        status, _headers, _body = self.fx.request("OPTIONS", "/api/feeds")
        self.assertEqual(status, 405)


# --- Path traversal at the HTTP layer ----------------------------------------

class PathTraversalHttpTest(_HttpTestCaseBase):
    def test_library_dotdot_returns_404_not_500(self):
        # Even with real config directories, a `..` in the path resolves out
        # of the source root; the resolver raises ValueError → 404, never 500.
        status, _headers, _body = self.fx.request(
            "GET", "/api/library/reports/../landline.json",
        )
        self.assertIn(status, (404,),
                      f"traversal must 404, got {status}")

    def test_library_percent_encoded_dotdot_returns_404(self):
        status, _headers, _body = self.fx.request(
            "GET", "/api/library/reports/..%2flandline.json",
        )
        self.assertIn(status, (404,),
                      f"percent-encoded traversal must 404, got {status}")

    def test_raw_library_traversal_returns_404(self):
        status, _headers, _body = self.fx.request(
            "GET", "/raw/library/reports/..%2f..%2f..%2fetc%2fpasswd",
        )
        self.assertEqual(status, 404)

    def test_brief_with_300_char_name_returns_404(self):
        # macOS NAME_MAX = 255; a 300-char segment must degrade to 404, not
        # bubble ENAMETOOLONG as a 500.
        oversized = "a" * 300 + ".md"
        status, _headers, _body = self.fx.request(
            "GET", f"/api/brief/alpha/{oversized}",
        )
        self.assertEqual(status, 404)

    def test_brief_with_nul_byte_returns_404(self):
        # A raw NUL byte in a URL path is rejected by BaseHTTPRequestHandler
        # before it ever reaches dispatch. Percent-encoded %00 does reach the
        # handler, and the resolver's ValueError trap turns it into a 404.
        status, _headers, _body = self.fx.request(
            "GET", "/api/brief/alpha/x%00.md",
        )
        self.assertEqual(status, 404)

    def test_static_traversal_returns_404(self):
        # /static/../server.py must not reach outside app/static/.
        status, _headers, _body = self.fx.request(
            "GET", "/static/..%2fserver.py",
        )
        self.assertEqual(status, 404)


# --- Security headers on a normal GET ----------------------------------------

class SecurityHeadersTest(_HttpTestCaseBase):
    def test_all_required_security_headers_present_on_api_feeds(self):
        status, headers, _body = self.fx.request("GET", "/api/feeds")
        self.assertEqual(status, 200)
        # Case-preserved dict from getheaders(); http.client returns them as
        # the server sent them.
        header_names = {name.lower() for name in headers}
        self.assertIn("content-security-policy", header_names)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_csp_blocks_inline_and_cross_origin(self):
        _status, headers, _body = self.fx.request("GET", "/api/feeds")
        csp = headers.get("Content-Security-Policy", "")
        # Load-bearing directives: no inline JS (script-src 'self' only),
        # no cross-origin fetch, no framing, no forms, no `<base>` hijack.
        self.assertIn("script-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertIn("form-action 'none'", csp)

    def test_server_header_is_bland(self):
        # `Server: Mineru` — no Python or BaseHTTP version leak.
        _status, headers, _body = self.fx.request("GET", "/api/feeds")
        server_hdr = headers.get("Server", "")
        self.assertEqual(server_hdr, "Mineru",
                         f"Server header must be bland 'Mineru', got {server_hdr!r}")
        self.assertNotIn("Python", server_hdr)
        self.assertNotIn("BaseHTTP", server_hdr)


if __name__ == "__main__":
    unittest.main()
