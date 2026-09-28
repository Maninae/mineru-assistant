"""Tests for the per-boot bearer-token auth on the browser server (Sep 4 2026).

Covers the fix for the C1 finding in the Sep 4 2026 security audit:

  1. Server generates a random 32-byte URL-safe token on startup and
     writes it to `$MINERU_HOME/cache/browser-server.token` with mode
     0600 (parent dir 0700), overwriting any previous value.
  2. `do_POST` rejects requests without a valid
     `Authorization: Bearer <token>` header with a 401 + empty body,
     and takes no side-effect action (no cookie file written, no
     evaluate result computed).
  3. `do_POST` with a matching bearer token flows through to the
     normal 200 response path.
  4. `cookies export` restricts writes to under `$MINERU_HOME` only
     (`/tmp/…` is refused), refuses a symlink at the target path, and
     opens the file with `O_NOFOLLOW` set so an intermediate-race
     symlink cannot redirect the write.
  5. `cookies export` and `evaluate` calls are audit-logged with a
     summary (operation / path / expression tail) so an operator can
     spot a suspicious call in `logs/browser-server/server.log`.
  6. The CLI verb (`mineru_cli.verbs.browser`) attaches an
     `Authorization: Bearer <token>` header on every outbound request
     when the token file is readable.

⚠️⚠️ SAFETY (READ TWICE) ⚠️⚠️

  * NO test in this file EVER launches real Playwright / Chromium /
    CloakBrowser. Where a POST would need a real browser, tests
    monkeypatch `manager._ensure_browser` to no-op and stub the
    `_context` attribute so the action returns immediately.
  * NO test in this file EVER binds port 9471. Every socket is
    picked from the OS's ephemeral range and asserted != 9471.
  * NO test in this file EVER touches the operator's real
    `~/.mineru/cache/`. `MINERU_HOME` is redirected to `tmp_path` on
    every test via the autouse fixture.
"""

from __future__ import annotations

import http.client
import importlib
import io
import json
import os
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
from typer.testing import CliRunner


# ---------------------------------------------------------------------------
# Safe-port fixture (same pattern as test_browser_lifecycle / test_browser_sse).
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """OS-picked ephemeral port; never returns 9471."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    if port == 9471:
        return _pick_free_port()
    return port


@pytest.fixture(autouse=True)
def isolated_auth_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect port, PID file, snapshot path, and MINERU_HOME to tmp.

    Reloading `browser.config`, `browser.auth_token`, and `browser.server`
    guarantees each test sees the env's tmp paths — the constants those
    modules capture at import time (e.g. `browser.config.MINERU_HOME`)
    would otherwise pin to the developer's live `~/.mineru`.
    """
    tmp_pid = tmp_path / "test-server.pid"
    tmp_snapshot = tmp_path / "browser-tabs.json"
    port = _pick_free_port()
    assert port != 9471, "port fixture grabbed the production port"

    monkeypatch.setenv("MINERU_BROWSER_PORT", str(port))
    monkeypatch.setenv("MINERU_BROWSER_PID_FILE", str(tmp_pid))
    monkeypatch.setenv("MINERU_BROWSER_USE_CLOAK", "false")
    monkeypatch.setenv("MINERU_BROWSER_TABS_SNAPSHOT_PATH", str(tmp_snapshot))
    monkeypatch.setenv("MINERU_BROWSER_TABS_DEBOUNCE_MS", "0")
    monkeypatch.setenv("MINERU_HOME", str(tmp_path))

    import browser.config as config_mod
    import browser.auth_token as auth_mod
    import browser.server as server_mod

    importlib.reload(config_mod)
    importlib.reload(auth_mod)
    importlib.reload(server_mod)

    assert config_mod.PORT != 9471, "port rebind didn't take"
    # Also confirm the token path resolver picks the tmp override.
    assert auth_mod.token_path() == tmp_path / "cache" / "browser-server.token"


# ---------------------------------------------------------------------------
# Token generation + on-disk shape
# ---------------------------------------------------------------------------


def test_generate_and_write_token_creates_0600_file(tmp_path: Path) -> None:
    """The token file lands under $MINERU_HOME/cache/ with mode 0600."""
    from browser import auth_token

    tok = auth_token.generate_and_write_token()
    assert isinstance(tok, str) and len(tok) >= 32, tok

    path = auth_token.token_path()
    assert path == tmp_path / "cache" / "browser-server.token"
    assert path.exists()
    mode = stat.S_IMODE(os.stat(str(path)).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # Parent dir also tightened.
    parent_mode = stat.S_IMODE(os.stat(str(path.parent)).st_mode)
    assert parent_mode == 0o700, f"expected parent dir 0o700, got {oct(parent_mode)}"


def test_generate_and_write_token_overwrites_previous(tmp_path: Path) -> None:
    """A second boot mints a fresh token and clobbers the old one."""
    from browser import auth_token

    tok1 = auth_token.generate_and_write_token()
    tok2 = auth_token.generate_and_write_token()
    assert tok1 != tok2, "two boots minted the same token — RNG failure"
    # The on-disk value is the latest one.
    on_disk = auth_token.token_path().read_text(encoding="utf-8").strip()
    assert on_disk == tok2


def test_read_token_or_none_returns_none_when_absent(tmp_path: Path) -> None:
    from browser import auth_token

    # No file yet under this tmp $MINERU_HOME.
    assert auth_token.read_token_or_none() is None


def test_read_token_or_none_returns_token_when_present(tmp_path: Path) -> None:
    from browser import auth_token

    tok = auth_token.generate_and_write_token()
    assert auth_token.read_token_or_none() == tok


def test_read_token_or_raise_raises_when_absent(tmp_path: Path) -> None:
    from browser import auth_token

    with pytest.raises(RuntimeError, match="no bearer token"):
        auth_token.read_token_or_raise()


# ---------------------------------------------------------------------------
# Constant-time verify
# ---------------------------------------------------------------------------


def test_verify_bearer_accepts_matching_token() -> None:
    from browser import auth_token

    assert auth_token.verify_bearer("Bearer abcXYZ", "abcXYZ") is True
    # RFC 7235: scheme is case-insensitive.
    assert auth_token.verify_bearer("bearer abcXYZ", "abcXYZ") is True
    assert auth_token.verify_bearer("BEARER abcXYZ", "abcXYZ") is True


def test_verify_bearer_rejects_missing_header() -> None:
    from browser import auth_token

    assert auth_token.verify_bearer(None, "abcXYZ") is False
    assert auth_token.verify_bearer("", "abcXYZ") is False


def test_verify_bearer_rejects_missing_token_state() -> None:
    """A missing server-side token is NEVER 'no auth required'."""
    from browser import auth_token

    assert auth_token.verify_bearer("Bearer abcXYZ", None) is False
    assert auth_token.verify_bearer("Bearer abcXYZ", "") is False


def test_verify_bearer_rejects_wrong_token() -> None:
    from browser import auth_token

    assert auth_token.verify_bearer("Bearer wrong", "expected") is False


def test_verify_bearer_rejects_wrong_scheme() -> None:
    from browser import auth_token

    assert auth_token.verify_bearer("Basic YWJjOnh5eg==", "abc") is False
    assert auth_token.verify_bearer("Token abc", "abc") is False


def test_verify_bearer_rejects_malformed_header() -> None:
    from browser import auth_token

    # No scheme + token separator.
    assert auth_token.verify_bearer("Bearer", "abc") is False
    assert auth_token.verify_bearer("just-a-blob", "abc") is False


# ---------------------------------------------------------------------------
# Cookie export path lockdown
# ---------------------------------------------------------------------------


def _fresh_manager() -> Any:
    """Return a freshly-loaded BrowserManager (no browser context)."""
    import browser.server as server_mod

    importlib.reload(server_mod)
    return server_mod.BrowserManager()


def test_validate_cookie_path_accepts_mineru_home_child(tmp_path: Path) -> None:
    """A path under $MINERU_HOME/… is accepted."""
    mgr = _fresh_manager()
    target = tmp_path / "cache" / "mycookies.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved = mgr._validate_cookie_path(str(target))
    assert resolved == str(target.resolve())


def test_validate_cookie_path_rejects_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The historical /tmp allowlist is gone (Sep 4 2026 audit fix)."""
    mgr = _fresh_manager()
    with pytest.raises(ValueError, match="must resolve under"):
        mgr._validate_cookie_path("/tmp/leaked-cookies.json")


def test_validate_cookie_path_rejects_etc_passwd(tmp_path: Path) -> None:
    """An obvious out-of-tree path is refused."""
    mgr = _fresh_manager()
    with pytest.raises(ValueError, match="must resolve under"):
        mgr._validate_cookie_path("/etc/passwd")


def test_validate_cookie_path_rejects_symlink(tmp_path: Path) -> None:
    """A symlink at the target path is refused pre-resolution."""
    mgr = _fresh_manager()
    real = tmp_path / "cache" / "real.json"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("[]", encoding="utf-8")
    link = tmp_path / "cache" / "sym.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        mgr._validate_cookie_path(str(link))


def test_validate_cookie_path_rejects_mineru_home_root(tmp_path: Path) -> None:
    """Cannot write a cookie FILE at the $MINERU_HOME directory path."""
    mgr = _fresh_manager()
    with pytest.raises(ValueError):
        mgr._validate_cookie_path(str(tmp_path))


def test_cookies_export_uses_o_nofollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The write-open call carries O_NOFOLLOW so a race-swapped symlink can't redirect."""
    import browser.server as server_mod

    mgr = _fresh_manager()

    # Stub browser context so _ensure_browser / .cookies() don't try to
    # launch Chromium.
    class _FakeCtx:
        pages = []

        def cookies(self) -> list:
            return [{"name": "session", "value": "s3cret", "domain": "gmail.com"}]

    mgr._context = _FakeCtx()
    monkeypatch.setattr(mgr, "_ensure_browser", lambda: None)

    seen_flags: List[int] = []
    real_open = os.open

    def spy_open(path, flags, mode=0o666, **kwargs):  # type: ignore[no-untyped-def]
        seen_flags.append(int(flags))
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(server_mod.os, "open", spy_open)

    target = tmp_path / "cache" / "cookies.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    result = mgr.cookies("export", path=str(target))
    assert result["count"] == 1
    # At least one os.open call from our export path carried O_NOFOLLOW.
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    assert o_nofollow != 0, "platform without O_NOFOLLOW — expected on macOS/Linux"
    matched = [f for f in seen_flags if f & o_nofollow]
    assert matched, f"no os.open call carried O_NOFOLLOW; saw flags={seen_flags}"


# ---------------------------------------------------------------------------
# Audit logging on cookies + evaluate
# ---------------------------------------------------------------------------


def test_cookies_export_is_audit_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every cookies op fires an INFO audit log line before dispatch."""
    import browser.server as server_mod

    mgr = _fresh_manager()

    class _FakeCtx:
        def cookies(self) -> list:
            return []

    mgr._context = _FakeCtx()
    monkeypatch.setattr(mgr, "_ensure_browser", lambda: None)

    target = tmp_path / "cache" / "audited.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with caplog.at_level("INFO", logger="browser-server"):
        mgr.cookies("export", path=str(target))
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "browser audit: cookies" in joined, joined
    assert "export" in joined
    assert str(target) in joined


def test_evaluate_is_audit_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Every evaluate call fires an INFO audit line with a truncated expression summary."""
    mgr = _fresh_manager()

    class _FakePage:
        def evaluate(self, expr: str, arg: Any = None) -> str:
            return "ok"

    mgr._tabs["tab_x"] = _FakePage()

    with caplog.at_level("INFO", logger="browser-server"):
        result = mgr.evaluate("tab_x", "document.cookie")
    assert result["result"] == "ok"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "browser audit: evaluate" in joined, joined
    assert "document.cookie" in joined
    assert "tab_x" in joined


def test_evaluate_truncates_long_expression_in_log(caplog: pytest.LogCaptureFixture) -> None:
    """Long expressions are trimmed in the audit log so it stays scannable."""
    mgr = _fresh_manager()

    class _FakePage:
        def evaluate(self, expr: str, arg: Any = None) -> str:
            return "x"

    mgr._tabs["tab_x"] = _FakePage()

    long_expr = "a" * 500
    with caplog.at_level("INFO", logger="browser-server"):
        mgr.evaluate("tab_x", long_expr)
    audit_lines = [
        r.getMessage() for r in caplog.records
        if "browser audit: evaluate" in r.getMessage()
    ]
    assert audit_lines
    # The full 500-char expression must not appear verbatim; the
    # ellipsis-truncated form is what gets logged.
    assert long_expr not in audit_lines[-1]
    assert "…" in audit_lines[-1]


# ---------------------------------------------------------------------------
# do_POST auth (integration): real ThreadingHTTPServer + http.client
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_server(tmp_path: Path) -> Tuple[ThreadingHTTPServer, int, Any, str]:
    """Spin up an in-process ThreadingHTTPServer with the auth token seeded.

    Yields (server, port, manager, token). The manager has a stubbed
    `_context` and a no-op `_ensure_browser` so `action=status` /
    `action=cookies` calls flow without launching Chromium.
    """
    import browser.server as server_mod
    from browser import auth_token
    from browser.tab_event_bus import TabEventBus
    from browser.tab_snapshot_writer import SnapshotWriter

    # Seed the token file so the server accepts POSTs.
    token = auth_token.generate_and_write_token()

    bus = TabEventBus()
    snapshot_path = tmp_path / "browser-tabs.json"
    mgr = server_mod.BrowserManager(event_bus=bus, snapshot_writer=None)
    writer = SnapshotWriter(
        snapshot_path,
        snapshot_fn=lambda: mgr.list_tabs(),
        debounce_ms=0,
    )
    mgr._snapshot_writer = writer

    # No-op browser: status returns "no_browser" without a launch.
    mgr._ensure_browser = lambda: None  # type: ignore[assignment]

    class _FakeCtx:
        pages = []  # for status probe

        def cookies(self) -> list:
            return [{"name": "s", "value": "v", "domain": "example.com"}]

    mgr._context = _FakeCtx()

    server_mod.manager = mgr
    server_mod._tab_event_bus = bus
    server_mod._snapshot_writer = writer

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9471, "test picked prod port"

    server = ThreadingHTTPServer(("127.0.0.1", port), server_mod.BrowserHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield server, port, mgr, token
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)


def _http_post_json(
    port: int,
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 3.0,
) -> Tuple[int, bytes]:
    """POST /action with a JSON body; return (status, body_bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        payload = json.dumps(body).encode("utf-8")
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        conn.request("POST", "/action", body=payload, headers=req_headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, raw
    finally:
        conn.close()


def test_post_without_auth_returns_401(
    auth_server: Tuple[ThreadingHTTPServer, int, Any, str],
) -> None:
    """A POST with no Authorization header is rejected with 401 + empty body."""
    _server, port, _mgr, _token = auth_server
    status, body = _http_post_json(port, {"action": "status"})
    assert status == 401, (status, body)
    # Empty body — no JSON hint, no error text.
    assert body == b""


def test_post_with_wrong_token_returns_401(
    auth_server: Tuple[ThreadingHTTPServer, int, Any, str],
) -> None:
    """A POST with a bearer token that doesn't match returns 401."""
    _server, port, _mgr, _token = auth_server
    status, body = _http_post_json(
        port,
        {"action": "status"},
        headers={"Authorization": "Bearer NOT-THE-REAL-TOKEN"},
    )
    assert status == 401, (status, body)
    assert body == b""


def test_post_with_correct_token_returns_200(
    auth_server: Tuple[ThreadingHTTPServer, int, Any, str],
) -> None:
    """A POST with the matching bearer token flows through to the normal 200 path."""
    _server, port, _mgr, token = auth_server
    status, body = _http_post_json(
        port,
        {"action": "status"},
        headers={"Authorization": "Bearer %s" % token},
    )
    assert status == 200, (status, body)
    parsed = json.loads(body.decode("utf-8"))
    assert "status" in parsed
    assert "port" in parsed


def test_unauth_cookies_export_writes_no_file(
    auth_server: Tuple[ThreadingHTTPServer, int, Any, str],
    tmp_path: Path,
) -> None:
    """A no-auth `cookies export` MUST NOT create the target file — the auth
    check fires before the body is even parsed, so no side effect can happen."""
    _server, port, _mgr, _token = auth_server
    target = tmp_path / "cache" / "unauth-cookies.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    status, _body = _http_post_json(
        port,
        {"action": "cookies", "operation": "export", "path": str(target)},
    )
    assert status == 401
    assert not target.exists(), "unauth request created a cookie file — auth bypassed!"


def test_unauth_evaluate_returns_no_result(
    auth_server: Tuple[ThreadingHTTPServer, int, Any, str],
) -> None:
    """A no-auth `evaluate` must return 401 with no result field."""
    _server, port, _mgr, _token = auth_server
    status, body = _http_post_json(
        port,
        {"action": "evaluate", "targetId": "tab_x", "expression": "document.cookie"},
    )
    assert status == 401
    assert body == b""


def test_get_endpoints_stay_unauth(
    auth_server: Tuple[ThreadingHTTPServer, int, Any, str],
) -> None:
    """`GET /health` and `GET /tabs` are read-only and remain unauth'd.

    The audit's scope is mutating requests — the SSE stream + /health
    probe must stay unauth'd so a monitoring loop or a --watch CLI
    doesn't need to plumb the token file.
    """
    _server, port, _mgr, _token = auth_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 200, resp.status
        body = json.loads(resp.read().decode("utf-8"))
        assert "status" in body
    finally:
        conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
    try:
        conn.request("GET", "/tabs")
        resp = conn.getresponse()
        assert resp.status == 200, resp.status
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI verb attaches Authorization: Bearer <token>
# ---------------------------------------------------------------------------


runner = CliRunner()


def _seed_snapshot(path: Path, tabs: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generatedAt": "2026-09-04T09:00:00-07:00", "tabs": tabs}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_verb_attaches_bearer_header_when_token_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mineru browser tabs` attaches Authorization: Bearer <tok> on every request."""
    from browser import auth_token
    from mineru_cli.app import app
    from mineru_cli.verbs import browser as browser_verb

    # Seed the per-boot token under $MINERU_HOME (= tmp_path).
    token = auth_token.generate_and_write_token()

    recorded_headers: List[Dict[str, str]] = []
    recorded_urls: List[str] = []

    class _FakeResp:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self._body = body
            self.status = status

        def read(self, n: int = -1) -> bytes:
            if n < 0 or n >= len(self._body):
                data, self._body = self._body, b""
            else:
                data, self._body = self._body[:n], self._body[n:]
            return data

        def getcode(self) -> int:
            return self.status

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def close(self) -> None:
            pass

    def fake_urlopen(url_or_req, timeout: float = 0.0):  # type: ignore[no-untyped-def]
        # url_or_req is either a str (no auth) or urllib.request.Request (auth).
        if isinstance(url_or_req, urllib.request.Request):
            recorded_urls.append(url_or_req.full_url)
            # Capture headers as a lowercase-key dict — urllib normalizes
            # keys to title-case internally, but `.headers` is case-
            # sensitive so we index defensively.
            headers = {k.lower(): v for k, v in url_or_req.header_items()}
            recorded_headers.append(headers)
        else:
            recorded_urls.append(str(url_or_req))
            recorded_headers.append({})
        # Canned bodies for /health then /tabs.
        if recorded_urls[-1].endswith("/health"):
            return _FakeResp(b'{"status":"ok"}')
        return _FakeResp(b'{"tabs": []}')

    monkeypatch.setattr(browser_verb.urllib.request, "urlopen", fake_urlopen)

    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 0, result.output

    # Both /health and /tabs were called; each carried the header.
    assert len(recorded_headers) >= 2
    for headers in recorded_headers:
        assert "authorization" in headers, (
            f"missing Authorization header; got {headers}"
        )
        auth_value = headers["authorization"]
        assert auth_value == "Bearer %s" % token, auth_value


def test_verb_omits_header_when_token_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no token file exists (server not up), the verb still runs GETs unauthed.

    This preserves the CLI's fallback-to-snapshot flow: a dead server
    has no token file, and the verb must still be able to read the
    on-disk cache without a mandatory auth header.
    """
    from mineru_cli.app import app
    from mineru_cli.verbs import browser as browser_verb

    # Do NOT seed a token — token_path() resolves to a nonexistent file.
    recorded: List[Any] = []

    def fake_urlopen(url_or_req, timeout: float = 0.0):  # type: ignore[no-untyped-def]
        recorded.append(url_or_req)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(browser_verb.urllib.request, "urlopen", fake_urlopen)

    # Seed a snapshot so the fallback finishes cleanly.
    snapshot = tmp_path / "cache" / "browser-tabs.json"
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(snapshot))
    _seed_snapshot(snapshot, [{"targetId": "tab_1", "url": "https://x",
                                "title": "X", "openedAt": "2026-09-04T09:00:00-07:00"}])

    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 0, result.output

    # The /health call happened as a raw URL string (no Request wrapper).
    assert recorded, "verb didn't reach urlopen"
    assert isinstance(recorded[0], str), (
        f"expected plain URL string when token absent; got {type(recorded[0])}"
    )


def test_verb_resolved_token_path_honors_mineru_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_resolved_token_path()` picks up per-test MINERU_HOME override."""
    from mineru_cli.verbs import browser as browser_verb

    monkeypatch.setenv("MINERU_HOME", str(tmp_path))
    assert browser_verb._resolved_token_path() == (
        tmp_path / "cache" / "browser-server.token"
    )


# ---------------------------------------------------------------------------
# Non-negotiable safety
# ---------------------------------------------------------------------------


def test_auth_server_port_is_not_9471(
    auth_server: Tuple[ThreadingHTTPServer, int, Any, str],
) -> None:
    _server, port, _mgr, _token = auth_server
    assert port != 9471


def test_no_real_playwright_imported() -> None:
    pw = sys.modules.get("playwright.sync_api")
    if pw is None:
        return
    assert getattr(pw, "__file__", None) in (None, ""), (
        f"real playwright was imported: {pw.__file__!r}"
    )
