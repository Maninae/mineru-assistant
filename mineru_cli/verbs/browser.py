"""`mineru browser` sub-app.

Phase 3 status (P3-06):

  - `tabs` (only wired verb in this task): READS the browser server's
    enriched `/tabs` endpoint (P3-05, spec §4.3), with three optional
    modes:

      * default:  pretty-print a compact ASCII table (targetId, opened
        age, URL trimmed to 60 chars, title trimmed to 40).
      * --json:   emit the raw response body verbatim.
      * --grep:   client-side case-insensitive substring filter on
        URL or title.
      * --watch:  streaming GET of /tabs/stream (SSE); one JSON line
        printed per event, exits when the server closes the stream.

    Fallback path: if the initial /health probe fails (connection
    refused, non-200, or timeout inside 500 ms), the verb reads the
    on-disk snapshot at `$MINERU_HOME/cache/browser-tabs.json` and adds
    a top-level `stale: true` flag. The snapshot path is env-
    overridable (`BROWSER_TABS_SNAPSHOT_PATH`) so tests can redirect
    it to a `tmp_path`; the port is env-overridable
    (`MINERU_BROWSER_PORT`) for the same reason. A test bind to 9471
    is impossible in this file — every socket call goes through
    urllib.request.urlopen, which the tests patch.

Wire-up rules (mirror memory.py / slack.py):

  - The urllib.request module is the ONLY outbound call site in this
    verb file. Tests mock urllib.request.urlopen (and, for the
    fallback branch, patch it to raise URLError). No test in this
    project may bind port 9471 or actually connect to the live
    server.
  - The port + snapshot path are resolved at CALL time (not import
    time) so tests can monkeypatch the env vars per test without a
    module reload dance.
  - Exit code discipline: 0 on success, 2 for a fallback-and-fail
    (both live and snapshot unavailable), 1 for a JSON decode error
    on the live response.

Verb -> engine map:

  Reads (wired):
    mineru browser tabs                       -> GET /tabs (pretty table)
    mineru browser tabs --json                -> GET /tabs (raw JSON body)
    mineru browser tabs --grep <pat>          -> GET /tabs + client filter
    mineru browser tabs --watch               -> GET /tabs/stream (SSE)

  Fallback (both wired verbs):
    file:  $MINERU_HOME/cache/browser-tabs.json  (with {stale: true} added)
"""

from __future__ import annotations

import datetime
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import typer


# ---------------------------------------------------------------------------
# Module constants — env knob names + defaults live here so a test can
# monkeypatch either the env or the resolver without reaching into private
# state. The values are resolved LAZILY at call time (see `_resolved_port`
# / `_resolved_snapshot_path`), not at import time, so a per-test
# `monkeypatch.setenv(...)` takes effect immediately.
# ---------------------------------------------------------------------------

# The live browser-server port. Default matches `browser/config.py::PORT`
# (which is `MINERU_BROWSER_PORT` env, default 9471). Kept in sync as a
# module constant here so the verb file is understandable in isolation;
# tests set `MINERU_BROWSER_PORT` to an OS-picked free port before
# invoking, and we never actually bind — we shell URLs into `urlopen`,
# which the tests patch.
BROWSER_PORT_ENV = "MINERU_BROWSER_PORT"
DEFAULT_BROWSER_PORT = 9471

# The on-disk snapshot path the crash-recovery fallback reads. The env
# knob is namespaced `BROWSER_TABS_SNAPSHOT_PATH` per the task's
# explicit contract (the browser server itself uses the longer
# `MINERU_BROWSER_TABS_SNAPSHOT_PATH` for its writer; this shorter form
# is the verb-side reader knob so a test can redirect ONLY the CLI
# without disturbing the server's writer).
#
# The default path composes off `$MINERU_HOME` and is resolved LAZILY
# at call time (see `_default_snapshot_path()` below) so multi-profile
# isolation works: `get_profile()` exports MINERU_HOME per profile
# BEFORE the verb runs, and the fallback correctly reads THIS profile's
# snapshot instead of leaking into the owner's `~/.mineru/cache/`
# tree. A module-level `Path.home() / ".mineru" / ...` constant would
# freeze the owner path at import time and defeat that isolation.
BROWSER_SNAPSHOT_PATH_ENV = "BROWSER_TABS_SNAPSHOT_PATH"

# The initial `/health` probe deadline. Kept short (500 ms) so a
# `mineru browser tabs` call against a dead server falls back to the
# on-disk snapshot within one blink instead of hanging on a slow
# handshake. Tests patch urlopen so this deadline never actually elapses
# in the suite; it's the prod contract.
HEALTH_PROBE_TIMEOUT_SECONDS = 0.5

# The /tabs read deadline. Longer than the health probe because the
# response can be non-trivial once the server has 20+ tabs open; the
# server itself is 127.0.0.1-only and cached in memory, so a 2 s
# ceiling is generous.
TABS_READ_TIMEOUT_SECONDS = 2.0

# The /tabs/stream connect deadline. `urlopen(timeout=…)` bounds the
# TCP handshake + first byte; once the stream is open we read frames
# until EOF (no per-frame timeout — the server sends `:heartbeat`
# comments so a stalled connection surfaces as a socket error).
STREAM_CONNECT_TIMEOUT_SECONDS = 3.0

# Per-boot bearer token — see `browser/auth_token.py` for the server-side
# generator that writes this file at `$MINERU_HOME/cache/browser-server.token`
# with mode 0600 on every startup. The verb reads the file at request
# time (never caches) so a server restart between CLI invocations
# transparently rotates the token in the client too.
#
# Read semantics:
#   * File present + readable  -> attach `Authorization: Bearer <tok>`
#   * File absent / unreadable -> proceed without the header (the verb's
#     GET surface is currently unauth'd; a missing token file means the
#     server isn't up, in which case the /health probe fails anyway and
#     the on-disk snapshot fallback path fires).
# Mutating POSTs (none in this verb file today) MUST surface a clear
# error on a missing token — see `bin/browser` for that path.
BROWSER_TOKEN_FILE_ENV = "MINERU_HOME"

# Column widths for the compact ASCII table (spec §4.3 example). The
# 60/40 URL/title trims are the numbers the task's done-criteria call
# out verbatim.
URL_COLUMN_WIDTH = 60
TITLE_COLUMN_WIDTH = 40

# Ellipsis appended when a value is trimmed. One codepoint so the
# trim math is len(value) <= width; if len > width we take the
# first (width - 1) chars and append the ellipsis.
ELLIPSIS = "…"


# ---------------------------------------------------------------------------
# Top-level `mineru browser` sub-app.
# ---------------------------------------------------------------------------

browser_app = typer.Typer(
    name="browser",
    help=(
        "Browser automation server (127.0.0.1:9471) surface. Today: `tabs` "
        "(list open tabs with live streaming + on-disk fallback). READS "
        "only — no verb here mutates browser state; open/act/snapshot live "
        "in the raw `browser` HTTP surface until a wrapper is wired."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# Resolver helpers
# ---------------------------------------------------------------------------


def _resolved_port() -> int:
    """Return the server port: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics
    where `[ -z "$VAR" ]` is the same as unset). Non-int values raise
    typer.BadParameter so the CLI surfaces a clean usage frame instead
    of a stack trace out of urllib.
    """
    raw = os.environ.get(BROWSER_PORT_ENV)
    if not raw:
        return DEFAULT_BROWSER_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise typer.BadParameter(
            f"{BROWSER_PORT_ENV}={raw!r} is not an integer port."
        ) from exc


def _default_snapshot_path() -> Path:
    """Return the fallback-snapshot path derived from `$MINERU_HOME` at call time.

    Mirrors `_resolved_token_path()` — the value is composed on every
    call so a per-profile MINERU_HOME (exported by `get_profile()`
    before the verb runs) is honored transparently. A stale module-
    level constant would freeze the owner's `~/.mineru/cache/...`
    path at import time and leak the owner's live tab snapshot into
    a sibling profile (alice) whose per-profile browser server is
    down and falls back to the on-disk read.
    """
    home = os.environ.get("MINERU_HOME") or os.path.expanduser("~/.mineru")
    return Path(home) / "cache" / "browser-tabs.json"


def _resolved_snapshot_path() -> Path:
    """Return the fallback-snapshot path: env override or per-profile default.

    Tests set `BROWSER_TABS_SNAPSHOT_PATH` to a tmp path per test; the
    default derives from `$MINERU_HOME` (see `_default_snapshot_path`)
    so it matches the browser server's per-profile writer target.

    Security: the override is validated against an allowlist rooted at
    `$MINERU_HOME/cache/` (production, derived at call time so a per-
    profile MINERU_HOME correctly scopes the allowlist to THIS
    profile's cache) and the system temp dir (tests / ephemeral
    scratch), mirroring `_validate_cookie_path` in `browser/server.py`.
    A path outside that allowlist, or one that resolves through a
    symlink, prints a stderr warning and exits 2 — an override pointing
    at `/etc/passwd` (or a symlink to it) would otherwise be read
    verbatim and could leak arbitrary file contents when the operator
    inspects the parsed JSON. The default path is trusted verbatim
    (we own it).
    """
    override = os.environ.get(BROWSER_SNAPSHOT_PATH_ENV)
    if not override:
        return _default_snapshot_path()
    return _validate_snapshot_override(override)


def _snapshot_override_allowed_roots() -> List[Path]:
    """Return the allowed root directories for a BROWSER_TABS_SNAPSHOT_PATH override.

    Production: `$MINERU_HOME/cache/` ONLY (derived at CALL time from
    the process env, NOT a module-level `Path.home()` snapshot; that
    would freeze the owner cache path and reject a sibling profile
    trying to point the override at its own per-profile cache). Tests:
    the platform temp dir (`tempfile.gettempdir()`) is added lazily
    and ONLY when pytest is running in-process (detected via the
    `PYTEST_CURRENT_TEST` / `PYTEST_VERSION` env vars pytest itself
    sets, or an explicit `MINERU_ALLOW_TEMPDIR_SNAPSHOT` opt-in). This
    prevents a production `BROWSER_TABS_SNAPSHOT_PATH=/tmp/...`
    planted by any local user on a shared box from passing validation
    and being read as an authoritative snapshot.

    Rationale: previously `/tmp` and `gettempdir()` were unconditionally
    on the allowlist so pytest's tmp_path (which lives under gettempdir)
    would resolve. A test convenience should never be a production
    seam — this gate keeps the tests green without weakening the prod
    posture.
    """
    home = os.environ.get("MINERU_HOME") or os.path.expanduser("~/.mineru")
    roots: List[Path] = [
        (Path(home) / "cache").resolve(),
    ]
    if _tempdir_allowed_for_snapshot():
        try:
            roots.append(Path(tempfile.gettempdir()).resolve())
        except Exception:
            pass
    return roots


def _tempdir_allowed_for_snapshot() -> bool:
    """True when tempdir belongs on the snapshot allowlist for this process.

    Enabled only under pytest (detected via the `PYTEST_CURRENT_TEST` or
    `PYTEST_VERSION` env vars pytest sets automatically) or via an
    explicit `MINERU_ALLOW_TEMPDIR_SNAPSHOT=1` opt-in for ad-hoc
    developer scratch. Production processes see neither, so the
    tempdir is NOT on the allowlist for them.
    """
    if os.environ.get("MINERU_ALLOW_TEMPDIR_SNAPSHOT"):
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if os.environ.get("PYTEST_VERSION"):
        return True
    return False


def _validate_snapshot_override(raw: str) -> Path:
    """Resolve+allowlist the override; on rejection print stderr and exit 2.

    Rejects:
      - a path whose real resolution lands outside the allowlist
        (`$MINERU_HOME/cache/` or the system temp dir).
      - a symlink at the override location itself (mirrors
        `_validate_cookie_path` in `browser/server.py`: even a symlink
        that points into the allowlist is rejected, since it lets the
        override switch targets without touching the env var).

    Returns the ORIGINAL (unresolved) Path on success so a downstream
    `read_text` operates on the same string the operator asked about.
    """
    candidate = Path(raw)
    if candidate.is_symlink():
        typer.echo(
            f"mineru browser tabs: {BROWSER_SNAPSHOT_PATH_ENV}={raw!r} is a "
            "symlink; refusing (mirrors cookie-path policy).",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        typer.echo(
            f"mineru browser tabs: {BROWSER_SNAPSHOT_PATH_ENV}={raw!r} could "
            f"not be resolved ({exc}).",
            err=True,
        )
        raise typer.Exit(code=2)
    allowed_roots = _snapshot_override_allowed_roots()
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return candidate
        except ValueError:
            continue
    root_list = ", ".join(str(r) for r in allowed_roots)
    typer.echo(
        f"mineru browser tabs: {BROWSER_SNAPSHOT_PATH_ENV}={raw!r} resolves "
        f"to {resolved} which is outside the allowlist ({root_list}). "
        "Refusing.",
        err=True,
    )
    raise typer.Exit(code=2)


def _resolved_token_path() -> Path:
    """Return the bearer-token file path used by the running browser server.

    Mirrors `browser/auth_token.py::token_path` — kept as a small helper
    here so this verb file has no import-time dependency on the browser
    package (the verb ships in the CLI wheel, `browser/` is a separate
    server package). `MINERU_HOME` is resolved at call time so a per-
    test env override works.
    """
    home = os.environ.get(BROWSER_TOKEN_FILE_ENV) or os.path.expanduser("~/.mineru")
    return Path(home) / "cache" / "browser-server.token"


def _read_bearer_token_or_none() -> Optional[str]:
    """Return the current bearer token, or None if the file is absent/unreadable.

    Never raises: a missing file just means "no token to attach". The
    verb's GET surface still works against unauth'd endpoints; a POST
    would need a fatal-on-missing helper, which is not yet used here.
    """
    path = _resolved_token_path()
    try:
        with open(str(path), "rb") as f:
            data = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    tok = data.decode("utf-8", errors="replace").strip()
    return tok or None


def _authorized_urlopen(url: str, timeout: float) -> Any:
    """`urlopen` wrapper that attaches `Authorization: Bearer <token>` when available.

    - Token present -> wrap the URL in `urllib.request.Request` with the
      header set, so the outgoing request carries auth. This is the
      normal path once the server has generated its per-boot token.
    - Token absent  -> fall through to the plain-URL form (backward-
      compatible with the pre-auth tests and with a dead-server GET
      that will fail anyway).

    Tests: the `mineru_cli.verbs.browser.urllib.request.urlopen` symbol
    is what the suite patches — this helper does not bypass that.
    """
    token = _read_bearer_token_or_none()
    if token is None:
        return urllib.request.urlopen(url, timeout=timeout)
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer %s" % token},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _server_base_url() -> str:
    """Return `http://127.0.0.1:<port>` (localhost-only by policy).

    Never `0.0.0.0`; never a public hostname. The browser server itself
    binds only to `127.0.0.1` (see `browser/server.py::main`), so
    reaching for anything else would be a bug — the URL builder pins
    the host so a future refactor can't relax that.
    """
    return f"http://127.0.0.1:{_resolved_port()}"


# ---------------------------------------------------------------------------
# HTTP probes (all go through urllib.request — tests patch this)
# ---------------------------------------------------------------------------


def _health_ok() -> bool:
    """Return True iff `GET /health` returns 200 within the probe deadline.

    Any failure mode (URLError, timeout, non-200 status, exception out
    of urlopen) collapses to False so the caller falls back to the
    on-disk snapshot. We don't parse the body — the server's `/health`
    JSON is `{status, tabs, pid, port}`, and a 200 is enough evidence
    that a live read of `/tabs` will succeed.
    """
    url = f"{_server_base_url()}/health"
    try:
        with _authorized_urlopen(url, timeout=HEALTH_PROBE_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                # http.client.HTTPResponse pre-3.9 exposed `.code` instead
                # of `.status`; keep both paths working.
                status = resp.getcode()
            return status == 200
    except Exception:
        return False


def _get_tabs_body() -> str:
    """Return the raw response body of `GET /tabs` as text.

    Raises urllib.error.URLError / .HTTPError on failure; the caller
    is responsible for the fallback branch.
    """
    url = f"{_server_base_url()}/tabs"
    with _authorized_urlopen(url, timeout=TABS_READ_TIMEOUT_SECONDS) as resp:
        # `.read()` returns bytes; decode as UTF-8 (the server hard-
        # codes `application/json; charset=utf-8` semantics — the
        # `_send_json` helper serializes with `ensure_ascii=False`).
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _load_snapshot_or_none() -> Optional[Dict[str, Any]]:
    """Read the on-disk snapshot, tag it {stale: true}, or return None.

    Missing file / unreadable file / non-JSON contents all collapse to
    None so the caller can print a coherent stderr message. A
    successful read returns the parsed dict with a `stale=True` key
    forcibly injected (even if the file already had one — the CLI
    contract is that the fallback path ALWAYS tags stale).
    """
    path = _resolved_snapshot_path()
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        parsed = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        # A snapshot writer that emitted a bare list would break the
        # `{stale: true}` contract; treat as unreadable.
        return None
    parsed["stale"] = True
    return parsed


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


def _trim(value: str, width: int) -> str:
    """Trim `value` to at most `width` chars, appending the ellipsis on cut.

    The value is coerced to str first so a non-string field (unlikely
    from the server, but defensive) doesn't crash the table render.
    An `width <= 1` cap collapses to the ellipsis itself so callers
    can't produce empty cells.
    """
    text = str(value) if value is not None else ""
    if len(text) <= width:
        return text
    if width <= 1:
        return ELLIPSIS
    return text[: width - 1] + ELLIPSIS


def _opened_age(opened_at: Optional[str]) -> str:
    """Return a compact age string like `5s`, `12m`, `3h`, `2d`.

    `opened_at` is ISO 8601 with an offset (produced by
    BrowserManager._now_iso via `astimezone().isoformat(...)`). A
    parse failure / missing value collapses to `-` so the table
    always renders.

    Rounding: seconds < 60 -> `<n>s`; minutes < 60 -> `<n>m`; hours
    < 24 -> `<n>h`; else `<n>d`. Truncation, not rounding, so the age
    only ever advances (never appears to move backwards on refresh).
    """
    if not opened_at:
        return "-"
    try:
        parsed = datetime.datetime.fromisoformat(opened_at)
    except ValueError:
        return "-"
    now = datetime.datetime.now().astimezone()
    if parsed.tzinfo is None:
        # If the server ever emitted a naive stamp, treat it as local.
        parsed = parsed.replace(tzinfo=now.tzinfo)
    delta = now - parsed
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def _filter_tabs_by_grep(tabs: List[Dict[str, Any]], pattern: str) -> List[Dict[str, Any]]:
    """Case-insensitive substring match on `url` OR `title`.

    A tab missing either field is included when the OTHER field
    matches (defensive against a partial /tabs row). An empty
    pattern degenerates to "match everything" — Typer already
    disallows `--grep ""` at the surface (Optional[str] with
    non-empty default enforcement below), but we still handle it
    gracefully here.
    """
    if not pattern:
        return list(tabs)
    needle = pattern.lower()
    matches: List[Dict[str, Any]] = []
    for tab in tabs:
        url = str(tab.get("url", "")).lower()
        title = str(tab.get("title", "")).lower()
        if needle in url or needle in title:
            matches.append(tab)
    return matches


def _render_table(tabs: List[Dict[str, Any]]) -> str:
    """Return the compact ASCII table (spec §4.3 example shape).

    Columns:
      TARGET_ID     | OPENED | URL                                                          | TITLE
      tab_9cfdc834  | 5m     | https://…                                                    | …

    Empty tabs list renders a `(no tabs open)` sentinel so the caller
    can't be confused between "server has no tabs" and "we hit a
    parse error".
    """
    if not tabs:
        return "(no tabs open)"
    # Precompute rows so the column widths can adapt to the longest
    # observed targetId in this response (defensive against a future
    # id format that's longer than 12 chars).
    rows: List[Tuple[str, str, str, str]] = []
    for tab in tabs:
        target_id = str(tab.get("targetId", "?"))
        age = _opened_age(tab.get("openedAt"))
        url = _trim(tab.get("url", ""), URL_COLUMN_WIDTH)
        title = _trim(tab.get("title", ""), TITLE_COLUMN_WIDTH)
        rows.append((target_id, age, url, title))
    id_width = max(len("TARGET_ID"), max(len(r[0]) for r in rows))
    age_width = max(len("OPENED"), max(len(r[1]) for r in rows))
    url_width = max(len("URL"), URL_COLUMN_WIDTH)
    title_width = max(len("TITLE"), TITLE_COLUMN_WIDTH)

    def _fmt(row: Tuple[str, str, str, str]) -> str:
        tid, age, url, title = row
        return (
            f"{tid:<{id_width}}  {age:<{age_width}}  "
            f"{url:<{url_width}}  {title:<{title_width}}".rstrip()
        )

    header = _fmt(("TARGET_ID", "OPENED", "URL", "TITLE"))
    separator = "-" * len(header)
    body = "\n".join(_fmt(r) for r in rows)
    return f"{header}\n{separator}\n{body}"


# ---------------------------------------------------------------------------
# SSE parsing (one line of JSON per event)
# ---------------------------------------------------------------------------


def _iter_sse_events(stream: io.BufferedIOBase) -> "Any":
    """Yield `(event_type, data_str)` tuples from an SSE byte stream.

    Frames are delimited by a blank line (per the SSE spec). Within a
    frame, each line starts with a prefix such as `event: `,
    `data: `, or `:` (comment / heartbeat). We collect `event:` and
    `data:` values, ignoring the rest, and yield once the blank line
    terminates the frame.

    A frame with only comment lines yields nothing (heartbeats stay
    silent on stdout). EOF on the stream ends the iterator cleanly.
    """
    event_type = "message"
    data_parts: List[str] = []
    buffer = b""
    while True:
        chunk = stream.read(1024)
        if not chunk:
            # EOF. Flush any half-frame we accumulated.
            if data_parts:
                yield event_type, "\n".join(data_parts)
            return
        buffer += chunk
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            # SSE uses \n or \r\n; strip a trailing \r defensively.
            line = line_bytes.rstrip(b"\r").decode("utf-8", errors="replace")
            if line == "":
                # Frame terminator.
                if data_parts:
                    yield event_type, "\n".join(data_parts)
                event_type = "message"
                data_parts = []
                continue
            if line.startswith(":"):
                # Comment line (typically `:heartbeat`). Ignore.
                continue
            if line.startswith("event:"):
                event_type = line[len("event:"):].lstrip()
                continue
            if line.startswith("data:"):
                data_parts.append(line[len("data:"):].lstrip())
                continue
            # `id:` / `retry:` / any other SSE field: ignored (we
            # only ship event + data downstream).


# ---------------------------------------------------------------------------
# `mineru browser tabs` — READ-ONLY.
# ---------------------------------------------------------------------------


@browser_app.command(
    "tabs",
    help=(
        "List open browser tabs — snapshot / stream / filter.\n\n"
        "Examples:\n"
        "  mineru browser tabs                          # pretty ASCII table (default)\n"
        "  mineru browser tabs --json                   # raw JSON body from GET /tabs\n"
        "  mineru browser tabs --grep github            # client-side URL/title filter\n"
        "  mineru browser tabs --watch                  # live SSE stream, one JSON line per event\n\n"
        "Fallback: when the live server is unreachable, reads "
        "`$MINERU_HOME/cache/browser-tabs.json` and adds `stale: true`."
    ),
)
def tabs(
    watch: bool = typer.Option(
        False,
        "--watch",
        help=(
            "Follow live tab-state changes via GET /tabs/stream (SSE). "
            "Prints one JSON line per event (opened / closed / navigated / "
            "focused / snapshot). Exits when the server closes the stream."
        ),
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit the raw JSON body of `GET /tabs` verbatim (no pretty "
            "table). Mutually consistent with --watch (which is always "
            "JSON-per-line)."
        ),
    ),
    grep: Optional[str] = typer.Option(
        None,
        "--grep",
        metavar="PATTERN",
        help=(
            "Client-side case-insensitive substring filter on URL OR title. "
            "Applied AFTER the /tabs read, so the server sees an unfiltered "
            "request. Cannot be combined with --watch."
        ),
    ),
) -> None:
    """List currently-open browser tabs.

    Read path:
      1. Probe `GET /health` with a 500 ms deadline. On success, read
         `GET /tabs`; on failure, fall through to the fallback.
      2. Fallback: read `$MINERU_HOME/cache/browser-tabs.json`; add
         `stale: true`; render / emit.
      3. Both paths render the same shape — a top-level object with a
         `tabs` array. Grep filters the array client-side; --json
         emits the full object verbatim.

    --watch bypasses the fallback entirely (no meaningful stream to
    replay from a static snapshot). On a dead server it exits 2 with a
    stderr message instead of silently reading the file.
    """
    if watch:
        if grep is not None:
            raise typer.BadParameter(
                "--grep is not compatible with --watch (grep is snapshot-only).",
                param_hint="--grep",
            )
        _run_watch()
        return

    if _health_ok():
        _run_snapshot_live(json_out=json_out, grep=grep)
        return

    _run_snapshot_fallback(json_out=json_out, grep=grep)


def _run_snapshot_live(json_out: bool, grep: Optional[str]) -> None:
    """Live-server snapshot path: GET /tabs, filter, render."""
    try:
        body = _get_tabs_body()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        # Rare: /health said OK, but /tabs failed between the probes.
        # Fall back to the snapshot so the user still sees SOMETHING.
        typer.echo(
            f"mineru browser tabs: /tabs read failed after /health OK ({exc}); "
            "falling back to on-disk snapshot.",
            err=True,
        )
        _run_snapshot_fallback(json_out=json_out, grep=grep)
        return

    if json_out and grep is None:
        # Verbatim emit — no reshape. Preserves the server's ordering,
        # key set, and JSON formatting bytes-for-bytes so a scripted
        # caller sees exactly what /tabs returned.
        typer.echo(body)
        return

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        typer.echo(
            f"mineru browser tabs: /tabs returned non-JSON ({exc}); "
            f"raw body follows.\n{body}",
            err=True,
        )
        raise typer.Exit(code=1)

    tabs_list = parsed.get("tabs", []) if isinstance(parsed, dict) else []
    if grep:
        tabs_list = _filter_tabs_by_grep(tabs_list, grep)

    if json_out:
        # Re-emit filtered shape as JSON (keeps the top-level object
        # so downstream jq pipelines still find `.tabs[]`).
        payload = {"tabs": tabs_list}
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return

    typer.echo(_render_table(tabs_list))


def _run_snapshot_fallback(json_out: bool, grep: Optional[str]) -> None:
    """On-disk fallback path: read the cache file, tag stale, render."""
    parsed = _load_snapshot_or_none()
    if parsed is None:
        typer.echo(
            "mineru browser tabs: server unreachable and no on-disk snapshot at "
            f"{_resolved_snapshot_path()} (set {BROWSER_SNAPSHOT_PATH_ENV} to "
            "override, or start the browser server).",
            err=True,
        )
        raise typer.Exit(code=2)

    tabs_list = parsed.get("tabs", []) if isinstance(parsed, dict) else []
    if grep:
        tabs_list = _filter_tabs_by_grep(tabs_list, grep)
        # Rebuild the shape so the emitted object also carries the
        # filtered list (parsed still carries the full list otherwise).
        parsed = dict(parsed)
        parsed["tabs"] = tabs_list

    if json_out:
        typer.echo(json.dumps(parsed, ensure_ascii=False))
        return

    # Pretty mode: print a one-line stale banner then the table so
    # the operator sees which mode they're in without a --json parse.
    generated_at = parsed.get("generatedAt", "?")
    typer.echo(f"[stale=true  snapshot generatedAt={generated_at}]")
    typer.echo(_render_table(tabs_list))


def _run_watch() -> None:
    """Streaming path: open GET /tabs/stream, print one JSON line per event."""
    url = f"{_server_base_url()}/tabs/stream"
    try:
        resp = _authorized_urlopen(url, timeout=STREAM_CONNECT_TIMEOUT_SECONDS)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        typer.echo(
            f"mineru browser tabs --watch: could not open {url} ({exc}). "
            "Start the browser server first.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        for event_type, data in _iter_sse_events(resp):
            # Emit one line per event: prefer the parsed data JSON,
            # but attach the SSE event_type so a consumer knows which
            # verb (snapshot / opened / closed / navigated / focused)
            # fired. If data isn't JSON, wrap the raw string so we
            # never drop a byte.
            try:
                parsed_data = json.loads(data)
            except json.JSONDecodeError:
                parsed_data = {"raw": data}
            envelope = {"event": event_type, "data": parsed_data}
            typer.echo(json.dumps(envelope, ensure_ascii=False))
    finally:
        try:
            resp.close()
        except Exception:
            pass
