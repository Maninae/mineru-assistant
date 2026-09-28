"""Tests for `mineru browser tabs` verb (P3-06).

⚠️⚠️ SAFETY (READ TWICE) ⚠️⚠️

  NO test in this file EVER binds a network port. Every HTTP call is
  routed through `urllib.request.urlopen`; every test patches that
  symbol on `mineru_cli.verbs.browser` with a canned response, and
  every patch asserts the exact URL string the verb would have hit.

  NO test in this file EVER contacts the live browser server on port
  9471. The `MINERU_BROWSER_PORT` env override lets tests point the
  verb at an OS-picked free port, but even then no socket is opened —
  urlopen is patched. The `test_no_socket_opened_*` tests
  monkeypatch `socket.socket` to raise on construction, proving the
  verb never falls out of urllib.

  NO test in this file EVER touches the on-disk snapshot at
  `$MINERU_HOME/cache/browser-tabs.json`. Every test uses
  `BROWSER_TABS_SNAPSHOT_PATH` to redirect the reader to a tmp file.

Coverage (spec §4.3 P3-06 done-criteria):

  Help + registration:
    - `mineru browser --help` renders.
    - `mineru browser tabs --help` renders with copy-pasteable
      examples.
    - `browser` sub-app registers right AFTER `slack` per §7 ordering.

  --json path:
    - Patched urlopen returns a canned /tabs body; verb prints it
      verbatim. `/health` probe hit first; `/tabs` hit second; both
      to 127.0.0.1:<test-port>.

  Default (pretty table) path:
    - Table header + separator + one row per tab; URL trimmed to 60
      chars with `…`; title trimmed to 40; opened-age computed.

  --grep path:
    - Three-tab mock; only rows whose URL OR title contains the
      pattern (case-insensitive) survive.

  --watch path:
    - Patched urlopen returns a fake SSE stream (two `event: opened`
      frames + a `:heartbeat` comment + EOF). Verb prints two JSON
      envelopes and exits cleanly.
    - `--watch` combined with `--grep` is rejected (BadParameter).

  Fallback path:
    - Patched urlopen raises URLError on both /health and /tabs; verb
      reads the seeded snapshot at `tmp_path / "browser-tabs.json"`;
      output carries `stale: true`; renders both --json and pretty
      variants.
    - No snapshot AND no live server exits 2 with an actionable
      stderr message.

  Non-negotiable safety:
    - Test bind port is never 9471.
    - No real socket is ever opened (proved with a `socket.socket`
      spy that raises on unexpected construction).
    - urlopen is called with the expected URL (asserted per test).
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import browser as browser_verb


runner = CliRunner()


# ---------------------------------------------------------------------------
# Autouse fixture: redirect port + snapshot path so tests never touch prod.
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
def isolated_browser_verb_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect port + snapshot path to keep tests hermetic + off 9471.

    Also clears the custom-verb registry memo — every `runner.invoke`
    of a browser verb still runs through the root `CustomVerbTyperGroup`
    (which calls `_load_registry_best_effort`), so a cached result from
    a sibling test suite's registry write could otherwise leak into
    this test's dispatch. Explicit reset makes the browser tests
    invariant against sibling ordering.

    Also isolates `MINERU_HOME` to `tmp_path` so the verb's
    bearer-token file resolver (added Sep 4 2026) does NOT accidentally
    read the developer's live `~/.mineru/cache/browser-server.token`.
    Without this override, running the suite on a machine where the
    live browser server is up would flip the verb into "wrap URLs in a
    Request with an Authorization header" mode, and the tests that
    assert `recorded[0][0] == "http://…/health"` (raw URL string) would
    fail. Tests that DO want to exercise the token-attach path seed
    the file themselves under this tmp_path.
    """
    port = _pick_free_port()
    assert port != 9471, "test setup grabbed the production port"
    monkeypatch.setenv("MINERU_BROWSER_PORT", str(port))
    # Snapshot path: default to a nonexistent file inside tmp so a
    # test that forgot to seed the file sees the "no snapshot" branch,
    # not a stale hit from a prior test.
    monkeypatch.setenv(
        "BROWSER_TABS_SNAPSHOT_PATH",
        str(tmp_path / "browser-tabs.json"),
    )
    # Point MINERU_HOME at tmp so `_resolved_token_path()` resolves
    # inside the test's tmp tree, NOT the operator's live workspace.
    monkeypatch.setenv("MINERU_HOME", str(tmp_path))
    # Isolation: drop any cached registry snapshot from a prior test.
    # `reset_warnings` clears both the shadow-warning gate and the
    # `_REGISTRY_CACHE` memo (they always reset together per
    # `mineru_cli.verbs.custom`).
    from mineru_cli.verbs import custom as custom_verbs

    custom_verbs.reset_warnings()


# ---------------------------------------------------------------------------
# Fakes for urlopen: canned HTTP responses / SSE streams.
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal `HTTPResponse`-shaped double for a completed GET.

    Supports the small slice the verb reads: `.status`, `.getcode()`,
    `.read()`. Also implements context-manager entry/exit so `with
    urlopen(...) as resp:` works verbatim.
    """

    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        self.status = status
        self._body = body
        self._closed = False

    def getcode(self) -> int:
        return self.status

    def read(self, n: int = -1) -> bytes:
        if self._closed:
            return b""
        if n < 0 or n >= len(self._body):
            data, self._body = self._body, b""
        else:
            data, self._body = self._body[:n], self._body[n:]
        return data

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class FakeStream(FakeResponse):
    """A FakeResponse that also acts as a byte iterator for SSE-style reads.

    The verb reads from the stream in 1024-byte chunks until EOF. The
    canned bytes buffer is drained by successive `.read(1024)` calls,
    then EOF ends the SSE iterator.
    """


def _install_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[str, float], FakeResponse],
) -> List[Tuple[str, float]]:
    """Patch `mineru_cli.verbs.browser.urllib.request.urlopen`.

    Records every (url, timeout) tuple the verb called with. Returns
    the recorder list so tests can assert on order + count.
    """
    recorded: List[Tuple[str, float]] = []

    def fake_urlopen(url: str, timeout: float = 0.0):  # type: ignore[no-untyped-def]
        recorded.append((str(url), float(timeout)))
        return handler(str(url), float(timeout))

    monkeypatch.setattr(
        browser_verb.urllib.request, "urlopen", fake_urlopen
    )
    return recorded


def _forbid_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a real socket. Belt-and-suspenders."""

    real_socket = socket.socket

    def guarded_socket(*args: Any, **kwargs: Any):
        # Allow the autouse fixture to still pick a free port BEFORE
        # this guard is installed. Fail if invoked mid-test.
        raise AssertionError(
            "test opened a raw socket — verb should route only through "
            f"urllib.request.urlopen (args={args})"
        )

    monkeypatch.setattr(socket, "socket", guarded_socket)


# ---------------------------------------------------------------------------
# Help + registration
# ---------------------------------------------------------------------------


def test_browser_help_renders() -> None:
    result = runner.invoke(app, ["browser", "--help"])
    assert result.exit_code == 0, result.output
    assert "tabs" in result.output


def test_browser_tabs_help_renders_examples() -> None:
    result = runner.invoke(app, ["browser", "tabs", "--help"])
    assert result.exit_code == 0, result.output
    # Copy-pasteable examples surfaced in the help panel:
    assert "mineru browser tabs" in result.output
    assert "--watch" in result.output
    assert "--json" in result.output
    assert "--grep" in result.output


def test_browser_registered_in_connectors_band_after_slack() -> None:
    """Layered ordering (2026-09-16 audit §D): browser sits in the Connectors
    band, after slack. The exact registration slot moved: browser now anchors
    the Band-2 tail (right before `brevity`, which is a bare command), with
    `finance` + `amazon` slotting between slack and browser per the audit.
    The old strict "right-after-slack" contract is no longer the shape.
    """
    names = [g.name for g in app.registered_groups]
    assert "slack" in names, names
    assert "browser" in names, names
    slack_idx = names.index("slack")
    browser_idx = names.index("browser")
    assert browser_idx > slack_idx, (
        f"browser must still register after slack in the Connectors band; "
        f"got {names}"
    )


# ---------------------------------------------------------------------------
# --json snapshot path
# ---------------------------------------------------------------------------


def test_tabs_json_emits_body_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mineru browser tabs --json` prints the /tabs body byte-for-byte."""
    canned_body = json.dumps(
        {
            "tabs": [
                {
                    "targetId": "tab_9cfdc834",
                    "url": "https://example.com/",
                    "title": "Example",
                    "openedAt": "2026-07-27T10:00:00-07:00",
                    "openedBy": "user",
                }
            ]
        }
    ).encode("utf-8")

    def _handler(url: str, timeout: float) -> FakeResponse:
        if url.endswith("/health"):
            return FakeResponse(body=b'{"status":"ok"}', status=200)
        if url.endswith("/tabs"):
            return FakeResponse(body=canned_body, status=200)
        raise AssertionError(f"unexpected url {url}")

    recorded = _install_urlopen(monkeypatch, _handler)

    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 0, result.output
    # Verbatim: our canned body appears in the CLI output.
    assert canned_body.decode("utf-8") in result.output

    # Both endpoints hit, in the documented order, and both against
    # 127.0.0.1 on the test-picked port.
    port = int(browser_verb._resolved_port())
    assert port != 9471
    assert len(recorded) == 2
    assert recorded[0][0] == f"http://127.0.0.1:{port}/health"
    assert recorded[1][0] == f"http://127.0.0.1:{port}/tabs"
    # Health probe uses the short deadline.
    assert recorded[0][1] == browser_verb.HEALTH_PROBE_TIMEOUT_SECONDS


def test_tabs_json_and_grep_reshapes_but_still_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--json --grep` filters the tabs list but keeps a JSON envelope."""
    canned_body = json.dumps(
        {
            "tabs": [
                {"targetId": "tab_1", "url": "https://github.com/x", "title": "GH"},
                {"targetId": "tab_2", "url": "https://gmail.com/", "title": "Mail"},
            ]
        }
    ).encode("utf-8")

    def _handler(url: str, timeout: float) -> FakeResponse:
        if url.endswith("/health"):
            return FakeResponse(body=b'{"status":"ok"}', status=200)
        return FakeResponse(body=canned_body, status=200)

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(
        app, ["browser", "tabs", "--json", "--grep", "gmail"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout.strip().splitlines()[-1])
    assert isinstance(parsed, dict)
    assert len(parsed["tabs"]) == 1
    assert parsed["tabs"][0]["targetId"] == "tab_2"


# ---------------------------------------------------------------------------
# Default (pretty table) path
# ---------------------------------------------------------------------------


def test_tabs_default_pretty_table_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    canned_body = json.dumps(
        {
            "tabs": [
                {
                    "targetId": "tab_deadbeef",
                    "url": "https://example.com/",
                    "title": "Example",
                    "openedAt": "2026-07-27T10:00:00-07:00",
                }
            ]
        }
    ).encode("utf-8")

    def _handler(url: str, timeout: float) -> FakeResponse:
        if url.endswith("/health"):
            return FakeResponse(body=b'{"status":"ok"}', status=200)
        return FakeResponse(body=canned_body, status=200)

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs"])
    assert result.exit_code == 0, result.output
    # Header, separator, and target id show up in the table.
    assert "TARGET_ID" in result.output
    assert "OPENED" in result.output
    assert "URL" in result.output
    assert "TITLE" in result.output
    assert "tab_deadbeef" in result.output
    assert "https://example.com/" in result.output
    assert "Example" in result.output


def test_tabs_default_trims_long_url_and_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_url = "https://very.long.example.com/" + "x" * 200
    long_title = "T" * 200
    canned_body = json.dumps(
        {
            "tabs": [
                {
                    "targetId": "tab_1",
                    "url": long_url,
                    "title": long_title,
                    "openedAt": "2026-07-27T10:00:00-07:00",
                }
            ]
        }
    ).encode("utf-8")

    def _handler(url: str, timeout: float) -> FakeResponse:
        if url.endswith("/health"):
            return FakeResponse(body=b'{"status":"ok"}', status=200)
        return FakeResponse(body=canned_body, status=200)

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs"])
    assert result.exit_code == 0, result.output
    # The trimmed URL cell contains the ellipsis and does NOT contain
    # the tail of the raw URL (the last 100 xs).
    assert "…" in result.output
    # Untrimmed URL never surfaces at length.
    assert long_url not in result.output
    assert long_title not in result.output


# ---------------------------------------------------------------------------
# --grep path
# ---------------------------------------------------------------------------


def test_tabs_grep_filters_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canned_body = json.dumps(
        {
            "tabs": [
                {"targetId": "tab_1", "url": "https://github.com/", "title": "GitHub"},
                {"targetId": "tab_2", "url": "https://gmail.com/", "title": "Mail"},
                {"targetId": "tab_3", "url": "https://forum.com/", "title": "Forum GITHUB"},
            ]
        }
    ).encode("utf-8")

    def _handler(url: str, timeout: float) -> FakeResponse:
        if url.endswith("/health"):
            return FakeResponse(body=b'{"status":"ok"}', status=200)
        return FakeResponse(body=canned_body, status=200)

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--grep", "gitHub"])
    assert result.exit_code == 0, result.output
    # tab_1 (URL match) and tab_3 (title match, case-insensitive) survive;
    # tab_2 (mail) is filtered out.
    assert "tab_1" in result.output
    assert "tab_3" in result.output
    assert "tab_2" not in result.output


def test_tabs_grep_no_match_renders_empty_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canned_body = json.dumps(
        {
            "tabs": [
                {"targetId": "tab_1", "url": "https://a", "title": "A"},
            ]
        }
    ).encode("utf-8")

    def _handler(url: str, timeout: float) -> FakeResponse:
        if url.endswith("/health"):
            return FakeResponse(body=b'{"status":"ok"}', status=200)
        return FakeResponse(body=canned_body, status=200)

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--grep", "nomatch"])
    assert result.exit_code == 0, result.output
    assert "(no tabs open)" in result.output


# ---------------------------------------------------------------------------
# --watch path (SSE stream)
# ---------------------------------------------------------------------------


def test_tabs_watch_streams_two_events_then_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fake SSE stream: two `event: opened` frames + `:heartbeat`, then EOF."""
    # Compose a byte-level SSE stream. Blank line terminates each frame.
    frame_1 = (
        b"event: opened\n"
        b'data: {"type":"opened","tab":{"targetId":"tab_1","url":"https://one"}}\n'
        b"\n"
    )
    frame_2 = (
        b"event: opened\n"
        b'data: {"type":"opened","tab":{"targetId":"tab_2","url":"https://two"}}\n'
        b"\n"
    )
    heartbeat = b":heartbeat\n\n"
    stream_bytes = frame_1 + heartbeat + frame_2  # ends without trailing EOL burst

    def _handler(url: str, timeout: float) -> FakeResponse:
        assert url.endswith("/tabs/stream"), url
        return FakeStream(body=stream_bytes, status=200)

    recorded = _install_urlopen(monkeypatch, _handler)

    result = runner.invoke(app, ["browser", "tabs", "--watch"])
    # Exit code 0 on natural stream close (EOF).
    assert result.exit_code == 0, result.output

    # Only the /tabs/stream URL was fetched; no /health probe on --watch.
    assert len(recorded) == 1
    port = int(browser_verb._resolved_port())
    assert port != 9471
    assert recorded[0][0] == f"http://127.0.0.1:{port}/tabs/stream"

    # Two JSON envelopes printed, one per event; heartbeat is silent.
    # Use .stdout (not .output) so any stderr warning from an unrelated
    # side channel doesn't get mistakenly parsed as an SSE envelope.
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    parsed_lines = [json.loads(ln) for ln in lines]
    assert len(parsed_lines) == 2
    for envelope, expected_target in zip(parsed_lines, ("tab_1", "tab_2")):
        assert envelope["event"] == "opened"
        assert envelope["data"]["tab"]["targetId"] == expected_target


def test_tabs_watch_forbids_grep() -> None:
    """--grep + --watch is a user error (rejected at the CLI surface)."""
    result = runner.invoke(app, ["browser", "tabs", "--watch", "--grep", "foo"])
    assert result.exit_code != 0, result.output
    assert "grep" in result.output.lower()


def test_tabs_watch_dead_server_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--watch"])
    assert result.exit_code == 2, result.output
    assert "browser server" in result.output.lower() or "tabs/stream" in result.output.lower()


# ---------------------------------------------------------------------------
# Fallback path
# ---------------------------------------------------------------------------


def _seed_snapshot(path: Path, tabs: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generatedAt": "2026-07-27T09:00:00-07:00", "tabs": tabs}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_tabs_fallback_reads_snapshot_with_stale_true_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "custom-tabs.json"
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(snapshot))
    _seed_snapshot(
        snapshot,
        [{"targetId": "tab_fallback", "url": "https://x", "title": "X",
          "openedAt": "2026-07-27T09:00:00-07:00"}],
    )

    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    recorded = _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 0, result.output

    # The /health probe attempt was made against the test port (never 9471).
    port = int(browser_verb._resolved_port())
    assert port != 9471
    assert len(recorded) >= 1
    assert recorded[0][0] == f"http://127.0.0.1:{port}/health"

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload.get("stale") is True
    assert payload["tabs"][0]["targetId"] == "tab_fallback"


def test_tabs_fallback_pretty_renders_stale_banner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "fallback.json"
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(snapshot))
    _seed_snapshot(
        snapshot,
        [{"targetId": "tab_stale", "url": "https://s", "title": "S",
          "openedAt": "2026-07-27T09:00:00-07:00"}],
    )

    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs"])
    assert result.exit_code == 0, result.output
    assert "stale=true" in result.output
    assert "tab_stale" in result.output


def test_tabs_no_server_no_snapshot_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Env fixture already points BROWSER_TABS_SNAPSHOT_PATH at a
    # nonexistent tmp path; no seeding here.
    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs"])
    assert result.exit_code == 2, result.output
    assert "no on-disk snapshot" in result.output.lower() or "unreachable" in result.output.lower()


def test_tabs_fallback_grep_narrows_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "fallback.json"
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(snapshot))
    _seed_snapshot(
        snapshot,
        [
            {"targetId": "tab_a", "url": "https://alpha.io", "title": "A",
             "openedAt": "2026-07-27T09:00:00-07:00"},
            {"targetId": "tab_b", "url": "https://beta.io", "title": "B",
             "openedAt": "2026-07-27T09:00:00-07:00"},
        ],
    )

    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--json", "--grep", "beta"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload.get("stale") is True
    ids = [t["targetId"] for t in payload["tabs"]]
    assert ids == ["tab_b"]


# ---------------------------------------------------------------------------
# Non-negotiable safety: no real socket ever opened by the verb code path.
# ---------------------------------------------------------------------------


def test_urlopen_is_the_only_outbound_call_site(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sanity: verb code never bypasses urlopen to open its own socket.

    Installs a `socket.socket` spy AFTER the port fixture has run (so
    the fixture's own free-port grab happens first, then the spy
    guards the verb's runtime). A test failure here means the verb
    added a raw socket that would bypass the mock and could hit
    9471.
    """
    # Seed a snapshot so the fallback branch has data.
    snapshot = tmp_path / "socket-guard.json"
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(snapshot))
    _seed_snapshot(
        snapshot,
        [{"targetId": "tab_safe", "url": "https://a", "title": "A",
          "openedAt": "2026-07-27T09:00:00-07:00"}],
    )

    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    _forbid_sockets(monkeypatch)

    result = runner.invoke(app, ["browser", "tabs", "--json"])
    # If any code path in the verb opened a raw socket, the spy would
    # have raised AssertionError before we got here.
    assert result.exit_code == 0, result.output


def test_urlopen_never_targets_9471_in_tests() -> None:
    """The autouse port fixture picks a non-9471 port every time."""
    port = int(browser_verb._resolved_port())
    assert port != 9471, port


def test_resolved_snapshot_path_honors_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "elsewhere.json"
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(override))
    assert browser_verb._resolved_snapshot_path() == override


def test_resolved_snapshot_path_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no override AND no MINERU_HOME, the default lands under
    `~/.mineru/cache/browser-tabs.json` (the owner default). The
    per-profile case is covered by
    `test_default_snapshot_path_follows_mineru_home` below.
    """
    monkeypatch.delenv("BROWSER_TABS_SNAPSHOT_PATH", raising=False)
    monkeypatch.delenv("MINERU_HOME", raising=False)
    resolved = browser_verb._resolved_snapshot_path()
    assert str(resolved).endswith(".mineru/cache/browser-tabs.json")


def test_resolved_port_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINERU_BROWSER_PORT", raising=False)
    assert browser_verb._resolved_port() == 9471


def test_resolved_port_bad_value_raises_bad_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_BROWSER_PORT", "not-an-int")
    with pytest.raises(typer_exceptions()):
        browser_verb._resolved_port()


def typer_exceptions() -> type:
    """typer.BadParameter is a click.BadParameter; import lazily to keep
    the test file free of a hard click dep at collection time."""
    import typer
    return typer.BadParameter


# ---------------------------------------------------------------------------
# Security hardening: BROWSER_TABS_SNAPSHOT_PATH allowlist
# ---------------------------------------------------------------------------


def test_snapshot_override_rejects_path_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An override pointing outside `$MINERU_HOME/cache/` and the tmp dir is refused.

    `/etc/passwd` would let a hostile / mistaken env var trick the
    fallback reader into dumping arbitrary file contents. The CLI now
    validates the override and exits 2.
    """
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", "/etc/passwd")
    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 2, result.output
    combined = (result.output + (result.stderr or "")).lower()
    assert "allowlist" in combined or "outside" in combined or "refusing" in combined


def test_snapshot_override_rejects_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A symlink override is refused — mirrors `_validate_cookie_path`."""
    real_target = tmp_path / "real.json"
    _seed_snapshot(
        real_target,
        [{"targetId": "tab_x", "url": "https://x", "title": "X",
          "openedAt": "2026-07-27T09:00:00-07:00"}],
    )
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(real_target)
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(symlink))

    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 2, result.output
    combined = (result.output + (result.stderr or "")).lower()
    assert "symlink" in combined


def test_snapshot_override_accepts_pytest_tmp_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """pytest's tmp_path lives under gettempdir() — allowlist must accept it.

    Every fallback-path test in this file uses tmp_path via monkeypatch.
    A regression here would break the whole fallback-path suite.
    """
    snapshot = tmp_path / "allowlist-ok.json"
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(snapshot))
    _seed_snapshot(
        snapshot,
        [{"targetId": "tab_ok", "url": "https://ok", "title": "OK",
          "openedAt": "2026-07-27T09:00:00-07:00"}],
    )

    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["tabs"][0]["targetId"] == "tab_ok"


def test_snapshot_override_accepts_mineru_cache_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A `$MINERU_HOME/cache/…` override is on the allowlist (production path).

    Doesn't actually seed the file (we don't create `cache/` under
    tmp_path); we only validate the override RESOLVES to a permitted
    root — `$MINERU_HOME/cache/` derived at CALL time from the
    autouse fixture's `MINERU_HOME=tmp_path` — then the reader
    gracefully returns None because the file doesn't exist.

    Pre-fix regression this guards: the allowlist was hardcoded to
    `Path.home() / ".mineru" / "cache"`, so a per-profile MINERU_HOME
    override was REJECTED and a sibling profile had no in-band route
    to its own snapshot.
    """
    fake_cache_path = str(tmp_path / "cache" / "nonexistent-test.json")
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", fake_cache_path)

    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--json"])
    # Allowed by allowlist, but the file doesn't exist → exit 2 with
    # the "no snapshot" branch (NOT the "outside allowlist" branch).
    assert result.exit_code == 2, result.output
    combined = (result.output + (result.stderr or "")).lower()
    assert "no on-disk snapshot" in combined or "unreachable" in combined
    assert "allowlist" not in combined  # NOT the allowlist error


# ---------------------------------------------------------------------------
# Cross-profile snapshot isolation (audit re-run, step-5 P0 blocker)
#
# Pre-fix defect: `DEFAULT_BROWSER_SNAPSHOT_PATH` was a module-level
# `Path.home() / ".mineru" / "cache" / "browser-tabs.json"` frozen at
# import time, and `_snapshot_override_allowed_roots()` hardcoded the
# allowlist root to the SAME owner path. Concrete leak: alice runs
# `mineru --profile alice browser tabs` while her per-profile browser
# server is DOWN; the fallback read hit the OWNER cache and printed
# the owner's currently-open tab URLs + titles. Worse, the sibling-
# path validator REJECTED alice trying to point BROWSER_TABS_SNAPSHOT_PATH
# at her own `~/.mineru/profiles/alice/cache/`, so she had no in-band
# route to her own snapshot either.
#
# Fix: both the default path and the allowlist root are derived from
# `$MINERU_HOME` at CALL time (mirrors `_resolved_token_path()`).
# `get_profile()` already exports MINERU_HOME per profile before the
# verb runs, so the fallback now correctly reads THIS profile's cache
# and the allowlist correctly accepts THIS profile's overrides.
# ---------------------------------------------------------------------------


def test_default_snapshot_path_follows_mineru_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two distinct MINERU_HOME values → two DISTINCT per-profile paths.

    A module-level `Path.home()` constant would produce the SAME
    owner-cache path for both — this test guards that regression by
    driving `_default_snapshot_path()` under alice's env and then
    bob's env and asserting the paths differ AND each is scoped to
    its own profile root.
    """
    monkeypatch.delenv("BROWSER_TABS_SNAPSHOT_PATH", raising=False)

    alice_home = tmp_path / "alice"
    bob_home = tmp_path / "bob"
    alice_home.mkdir()
    bob_home.mkdir()

    monkeypatch.setenv("MINERU_HOME", str(alice_home))
    alice_default = browser_verb._default_snapshot_path()
    alice_resolved = browser_verb._resolved_snapshot_path()

    monkeypatch.setenv("MINERU_HOME", str(bob_home))
    bob_default = browser_verb._default_snapshot_path()
    bob_resolved = browser_verb._resolved_snapshot_path()

    # Distinct per-profile paths — not a single shared owner path.
    assert alice_default != bob_default
    assert alice_resolved != bob_resolved

    # Each scoped to its own MINERU_HOME.
    assert alice_default == alice_home / "cache" / "browser-tabs.json"
    assert bob_default == bob_home / "cache" / "browser-tabs.json"
    assert alice_resolved == alice_default
    assert bob_resolved == bob_default

    # And neither leaks into the owner cache.
    owner_cache = str(Path.home() / ".mineru" / "cache")
    assert owner_cache not in str(alice_default)
    assert owner_cache not in str(bob_default)


def test_server_down_fallback_reads_per_profile_snapshot_not_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With MINERU_HOME=<alice> and no override + server down, the
    fallback path is under <alice>/cache — NOT the owner
    `~/.mineru/cache`. This is the concrete leak the audit called out:
    alice's `mineru browser tabs` used to print the owner's live tabs.
    """
    monkeypatch.delenv("BROWSER_TABS_SNAPSHOT_PATH", raising=False)

    alice_home = tmp_path / "alice"
    (alice_home / "cache").mkdir(parents=True)
    monkeypatch.setenv("MINERU_HOME", str(alice_home))

    resolved = browser_verb._resolved_snapshot_path()

    # Alice-scoped …
    assert str(resolved).startswith(str(alice_home))
    assert resolved == alice_home / "cache" / "browser-tabs.json"
    # … and explicitly NOT the owner's live cache path.
    owner_cache = str(Path.home() / ".mineru" / "cache")
    assert not str(resolved).startswith(owner_cache)


def test_snapshot_override_accepts_per_profile_cache_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A profile can point BROWSER_TABS_SNAPSHOT_PATH at its OWN
    `$MINERU_HOME/cache/browser-tabs.json` and pass validation.

    Pre-fix regression this guards: the allowlist root was frozen to
    `Path.home() / ".mineru" / "cache"`, so alice's override at
    `~/.mineru/profiles/alice/cache/browser-tabs.json` was REJECTED
    with `outside the allowlist` and she had no in-band route to her
    own snapshot. After the fix, the allowlist derives the root from
    `$MINERU_HOME` at call time.
    """
    alice_home = tmp_path / "alice"
    (alice_home / "cache").mkdir(parents=True)
    monkeypatch.setenv("MINERU_HOME", str(alice_home))

    alice_snapshot = alice_home / "cache" / "browser-tabs.json"
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(alice_snapshot))
    _seed_snapshot(
        alice_snapshot,
        [{"targetId": "tab_alice", "url": "https://alice.local", "title": "A",
          "openedAt": "2026-09-04T09:00:00-07:00"}],
    )

    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload.get("stale") is True
    assert payload["tabs"][0]["targetId"] == "tab_alice"


def test_snapshot_override_outside_home_and_tempdir_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A path outside `$MINERU_HOME/cache` AND outside the tempdir
    tree is still rejected — the fix widens the allowlist to be
    per-profile, NOT to accept anything.

    Simulates a hostile / mistaken override at `/etc/passwd` under
    a profile-scoped MINERU_HOME. Both the outside-allowlist reject
    and the symlink reject remain in force.
    """
    alice_home = tmp_path / "alice"
    (alice_home / "cache").mkdir(parents=True)
    monkeypatch.setenv("MINERU_HOME", str(alice_home))

    # 1) outside-allowlist reject
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", "/etc/passwd")
    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 2, result.output
    combined = (result.output + (result.stderr or "")).lower()
    assert "allowlist" in combined or "outside" in combined or "refusing" in combined

    # 2) symlink-at-override reject, even when the target lives INSIDE
    #    this profile's cache (mirrors the cookie-path policy).
    real_target = alice_home / "cache" / "real-tabs.json"
    _seed_snapshot(
        real_target,
        [{"targetId": "tab_real", "url": "https://r", "title": "R",
          "openedAt": "2026-09-04T09:00:00-07:00"}],
    )
    sym = alice_home / "cache" / "sym-tabs.json"
    sym.symlink_to(real_target)
    monkeypatch.setenv("BROWSER_TABS_SNAPSHOT_PATH", str(sym))

    def _handler(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, _handler)
    result = runner.invoke(app, ["browser", "tabs", "--json"])
    assert result.exit_code == 2, result.output
    combined = (result.output + (result.stderr or "")).lower()
    assert "symlink" in combined
