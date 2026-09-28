"""`mineru telegram` sub-app.

⚠️ SAFETY RULE — READ THIS TWICE ⚠️

  **During dev/test NO real Telegram delivery ever fires.** The write /
  outbound verbs here (`send`, `deliver`) route through
  `mineru_cli.wrappers.deliver_output.run_deliver_output`; every test in
  `tests/test_telegram_verbs.py` PATCHES that wrapper with a recorder
  and asserts the argv that WOULD be sent to the underlying script.
  `subprocess.run` is never actually invoked against the live
  deliver-output.py during dev/test.

  If a maintainer runs a wired outbound verb by hand (e.g. `mineru
  telegram send "test"` from a real shell), that IS a real send to
  the operator's Telegram chat via the live bot token that deliver-output.py
  loads from macOS Keychain. The verb layer does no gating. Confirm
  with the operator before any live outbound invocation.

  The underlying deliver-output.py script enforces its own allowlist
  (fail-closed on empty) so a misconfigured chat id can't smuggle a
  delivery to the wrong destination — but the "will this actually
  deliver" gate lives one layer down, in the live tool, not here.

Phase 2 status (P2-08):

  - `send <text>`: wraps `deliver-output.py --raw <text>` — WRITE, OUTBOUND.
  - `deliver <path>`: wraps `deliver-output.py <path>` — WRITE, OUTBOUND.
  - `inject <label> <content>`: WRITE to the local inject-queue only
    (drops a JSON file under `$MINERU_HOME/cache/inject-queue/` for the
    daemon to pick up as an ACK). Does NOT send to Telegram. Mirrors
    deliver-output.py's `enqueue_for_session` byte-for-byte
    (`{ts}-{safe_label}.json` filename shape, `{"label", "content"}`
    payload, `[^A-Za-z0-9_-]` -> `_` label sanitization). Tests use
    `MINERU_INJECT_QUEUE_DIR` to redirect the write to a tmp path.
  - `allowlist`: READ-ONLY view of the current chat-id allowlist. Reads
    macOS Keychain by default (`security find-generic-password -a mineru
    -s telegram-allowed-chat-ids -w`) so the source of truth matches
    what `deliver-output.py` and the Landline daemon consult. Tests
    redirect the read via `MINERU_TELEGRAM_ALLOWLIST_FILE` pointing at
    a tmp file with comma-separated chat ids.
  - `photo <path>`: WRITE, OUTBOUND — sends an image via the
    self-contained sendPhoto transport in
    `mineru_cli.wrappers.telegram_photo`. Reads the file bytes,
    computes sha256, stages the bytes file in the retention cache
    (`mineru_cli.wrappers.telegram_image_cache`), calls the transport,
    then commits the sidecar record on success. With `--dedup` the
    verb short-circuits when a cached record for the same sha256 has
    a live `telegram_file_id`: it POSTs `photo=<file_id>` (cached-file
    fast path) instead of re-uploading, without opening the source
    bytes a second time. Caption is truncated to
    `TELEGRAM_CAPTION_MAX_CHARS` with a `…` ellipsis (the verb owns
    that truncation so the sidecar records the exact caption sent).
    Tests PATCH `mineru_cli.wrappers.telegram_photo.send_photo` so no
    real HTTP call ever fires; `--dedup` tests also assert
    `is_cached_id=True` and that the source file's bytes are NOT
    re-read on the cached path.
  - `photos`: READ-ONLY ledger sub-app over
    `$MINERU_HOME/cache/telegram_sent_images/` (mode 0700, files 0600).
    Sub-commands `list [--since ..] [--chat ..] [--json]`,
    `search "<substring>" [--json]`, `show <id-or-prefix>`,
    `prune [--dry-run]`. `list` and `search` render either a JSON
    array of the raw sidecar dicts or a compact ASCII bullet table
    (never a Markdown table, per the operator's Telegram rule — CLI stdout is
    fine for plain columns). All ledger verbs are local reads /
    trash-based writes; no HTTP.

Wire-up rules (mirror the memory.py / gmail.py verb files):

  - `send` and `deliver` route through the single subprocess call site
    in `mineru_cli.wrappers.deliver_output.run_deliver_output`. Neither
    verb file constructs its own subprocess.
  - `inject` writes directly to a Python-managed local queue directory;
    it does not shell out. The task explicitly scopes inject to "WRITE
    to inject-queue" and the live deliver-output.py has no inject-only
    mode we could wrap without rewriting it. The queue-file shape is
    frozen to match the daemon consumer at
    `landline/config.py::INJECT_TIMESTAMP_FORMAT` so a future change to
    that format needs a coordinated update here.
  - `allowlist` reads the SAME Keychain slot deliver-output.py and the
    Landline guard read (`telegram-allowed-chat-ids`), so what the CLI
    prints is what an actual delivery would consult. The
    `MINERU_TELEGRAM_ALLOWLIST_FILE` env override exists strictly so
    hermetic tests can point at a tmp file — do NOT rely on it in prod.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional

import typer

from mineru_cli.profile import get_profile
from mineru_cli.secrets import SecretsConfig, build_resolver
from mineru_cli.wrappers import telegram_photo as photo_transport
from mineru_cli.wrappers.deliver_output import run_deliver_output
from mineru_cli.wrappers.telegram_image_cache import (
    DEFAULT_RETENTION_DAYS,
    RETENTION_FOREVER,
    PendingCacheEntry,
    PruneReport,
    SentImageCacheError,
    SentImageRecord,
    cache_binary,
    cache_dir_for_workspace,
    commit_record,
    iter_records,
    lookup_by_sha256,
    prune_expired,
    search_records,
)
from mineru_cli.wrappers.telegram_photo import (
    TELEGRAM_CAPTION_MAX_CHARS,
    send_photo,
)

# ---------------------------------------------------------------------------
# Constants shared across the file (kept module-level, not deep in a function,
# so tests can import + monkeypatch them without touching function internals).
# ---------------------------------------------------------------------------

# The canonical Keychain slot the deliver-output.py script and the Landline
# guard both consult. Kept as a string literal here (no cross-import from
# deliver-output.py or landline.*) so an unrelated syntax error in either
# consumer can't take down `mineru telegram allowlist`.
TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE = "telegram-allowed-chat-ids"

# Default Keychain account. Matches the profile's `keychain_account` (which
# defaults to "mineru") and the hardcoded `-a mineru` calls in
# deliver-output.py. When the active profile carries a different
# keychain_account we use that instead (see `_keychain_account_for_ctx`).
DEFAULT_KEYCHAIN_ACCOUNT = "mineru"

# The macOS Keychain CLI. Absolute path so PATH shenanigans can't redirect
# the read to a shim. Absent when running on non-macOS or under a stripped
# PATH; we handle that as "no Keychain available" (falls through to the
# file override or fails loud, per _read_allowlist logic).
SECURITY_BINARY = "/usr/bin/security"

# Test-only env override: when set, `allowlist` reads the allowlist string
# from the file at this path instead of touching Keychain. Documented as
# test-only in the verb docstring; NOT a supported production mechanism.
ALLOWLIST_FILE_ENV = "MINERU_TELEGRAM_ALLOWLIST_FILE"

# Inject queue location. Env override wins for tests; otherwise we derive
# from the ACTIVE profile's workspace (`<workspace_absolute>/cache/
# inject-queue`) so two co-existing profiles never write ACKs into each
# other's queue. The pre-Phase-1 hardcoded `$MINERU_HOME/cache/inject-queue`
# is intentionally gone — that leaked profile B's ACKs into profile A's
# workspace.
#
# ⚠️ LANDLINE DAEMON HANDSHAKE (cutover-only): the Landline daemon in
# `~/Developer/claude-landline` (`landline/inject_reader.py`) reads from
# `$MINERU_HOME/cache/inject-queue` today. When the daemon is scoped to a
# specific profile, its inject-queue reader MUST be pointed at the SAME
# per-profile path this CLI writes to, or ACKs will silently disappear.
# Daemon-side coordination is deferred; the CLI write path is
# isolation-correct NOW so a future daemon cutover only has to change the
# reader.
INJECT_QUEUE_DIR_ENV = "MINERU_INJECT_QUEUE_DIR"

# Canonical inject-queue timestamp format. Kept as a string literal here on
# purpose: the daemon-side consumer parses this via
# landline.config.INJECT_TIMESTAMP_FORMAT in ~/Developer/claude-landline; if
# that format ever changes, update this constant in lockstep. The CLI stays
# free of any landline.* import so an unrelated daemon import-time error
# can never break the mineru CLI.
INJECT_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"

# Label sanitizer: everything outside [A-Za-z0-9_-] becomes `_`. Matches
# deliver-output.py:175 exactly (`re.sub(r"[^A-Za-z0-9_-]", "_", label)`).
_LABEL_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")


# ---------------------------------------------------------------------------
# Top-level `mineru telegram` app.
# ---------------------------------------------------------------------------

telegram_app = typer.Typer(
    name="telegram",
    help=(
        "Telegram: Landline delivery + interactive daemon surface. "
        "SAFETY: send / deliver / inject are WRITE operations; tests MOCK "
        "the wrapper, never actually contacting Telegram."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _keychain_account_for_ctx(ctx: typer.Context) -> str:
    """Return the Keychain account name for the active profile.

    Lazy hydration (2026-08-28 rev): `get_profile(ctx)` loads + validates
    the active profile on first call and caches it. The returned
    `Profile.keychain_account` field defaults to `mineru`, matching every
    hardcoded `-a mineru` call site the audit inventoried.

    Fallback to `DEFAULT_KEYCHAIN_ACCOUNT` is only for callsites that
    construct a Typer context without going through the root callback
    (certain unit tests) — in normal CLI use the profile is always
    hydrated by the time this runs.
    """
    profile_obj = get_profile(ctx)
    return getattr(profile_obj, "keychain_account", DEFAULT_KEYCHAIN_ACCOUNT)


def _read_allowlist(ctx: typer.Context) -> str:
    """Return the raw allowlist string (comma-separated chat ids).

    Precedence:
      1. `MINERU_TELEGRAM_ALLOWLIST_FILE` env override (test-only): read
         and return the file's stripped contents. If the env var is set
         but the file is missing we exit 2 with an actionable message —
         a misconfigured test should surface immediately.
      2. macOS Keychain via `security find-generic-password -a <account>
         -s telegram-allowed-chat-ids -w`. Returns the trimmed value on
         hit. Any non-zero exit code (missing item, locked keychain,
         `security` binary absent) is treated as "no allowlist" and we
         exit 2 with a hint pointing at the fix. This matches
         deliver-output.py's fail-closed default.

    The raw string is returned verbatim (no splitting, no dedup) so the
    caller can render it however it likes. The upstream consumers
    (deliver-output.py, landline.runtime.guard) all treat empty /
    whitespace as "empty allowlist -> block everyone".
    """
    override_path = os.environ.get(ALLOWLIST_FILE_ENV)
    if override_path:
        p = Path(override_path)
        # `is_file()` (not `exists()`) catches both missing paths AND
        # paths that resolve to a directory / socket / device — any
        # non-file target would otherwise blow up with a raw
        # `IsADirectoryError` traceback out of `read_text` instead of
        # the actionable "points at a missing/non-file target" message.
        if not p.is_file():
            typer.echo(
                f"mineru telegram allowlist: {ALLOWLIST_FILE_ENV}={override_path!r} "
                "does not point at a regular file "
                "(missing, or is a directory / device / socket).",
                err=True,
            )
            raise typer.Exit(code=2)
        return p.read_text(encoding="utf-8").strip()

    account = _keychain_account_for_ctx(ctx)
    try:
        completed = subprocess.run(
            [
                SECURITY_BINARY,
                "find-generic-password",
                "-a",
                account,
                "-s",
                TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        # Non-macOS host, or `security` at a different path. Nothing
        # to read from; surface the exact fix so the operator knows
        # whether to set the env override (tests) or install/expose
        # the `security` binary (real macOS).
        typer.echo(
            f"mineru telegram allowlist: {SECURITY_BINARY!r} not available "
            f"(non-macOS host?). Set {ALLOWLIST_FILE_ENV} to a file with "
            "comma-separated chat ids to read from a file instead.",
            err=True,
        )
        raise typer.Exit(code=2)

    if completed.returncode != 0:
        # macOS `security` exit codes:
        #   44 -> SecKeychainItemNotFound (allowlist never configured)
        #   36 -> SecAuthFailed (keychain locked; unlock login keychain)
        # We collapse both to a single actionable message; the operator
        # who saw a locked keychain will know from the OS behavior.
        typer.echo(
            f"mineru telegram allowlist: could not read Keychain slot "
            f"{TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE!r} (account={account!r}). "
            "Store the allowlist first: `security add-generic-password "
            f"-a {account} -s {TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE} -w "
            "'<comma-separated-chat-ids>' -U`.",
            err=True,
        )
        raise typer.Exit(code=2)

    # security -w emits the value on stdout with a trailing newline;
    # .strip() drops any surrounding whitespace so downstream renderings
    # see a clean comma-list.
    return completed.stdout.strip()


def _resolved_inject_queue_dir(ctx: typer.Context) -> Path:
    """Return the inject-queue directory for the active profile.

    Precedence:
      1. `MINERU_INJECT_QUEUE_DIR` env override — but ONLY when the env
         value was set by the operator (test override), NOT by the
         framework's own `_export_profile_env` echo. Distinguished via
         `is_env_framework_managed`.
      2. Active profile's `<workspace_absolute>/cache/inject-queue`
         (hydrated via `get_profile(ctx)`). This is the isolation-
         correct default: profile B's ACKs land under profile B's
         workspace, never under the operator's.
      3. `MINERU_INJECT_QUEUE_DIR` env override (framework-set) — used
         only when the active profile has no `workspace_absolute`
         (defensive path for a duck-typed profile stub in a unit test).
      4. `get_profile(ctx)` raises `typer.BadParameter` when no active
         profile is selectable — we deliberately do NOT fall back to
         `$MINERU_HOME/cache/inject-queue`; that pre-Phase-1 hardcoded
         default leaked cross-profile writes into the operator's workspace.

    Ordering note (2026-09-04 step-5 audit, Finding 7): `get_profile()`
    now exports `MINERU_INJECT_QUEUE_DIR` into `os.environ` on first
    call so downstream SUBPROCESSES inherit the profile's inject-queue
    path (deliver-output, cc-job-lib, ...). But in-process consumers
    (this function) must ignore that framework echo, or a second
    `runner.invoke()` on a different profile would still see the FIRST
    profile's queue path in env. `is_env_framework_managed` filters the
    echo so an operator's explicit `monkeypatch.setenv(...)` still wins.
    """
    from mineru_cli.profile.loader import is_env_framework_managed

    override = os.environ.get(INJECT_QUEUE_DIR_ENV)
    if override and not is_env_framework_managed(INJECT_QUEUE_DIR_ENV):
        return Path(override)
    profile = get_profile(ctx)
    workspace_root = getattr(profile, "workspace_absolute", None)
    if workspace_root is not None:
        return Path(workspace_root) / "cache" / "inject-queue"
    if override:
        return Path(override)
    raise typer.BadParameter(
        "mineru telegram inject: cannot resolve inject-queue path — active "
        f"profile has no `workspace_absolute` field and {INJECT_QUEUE_DIR_ENV} "
        "is not set. Fix the profile.yaml or set the env override.",
        param_hint=None,
    )


def _write_inject(ctx: typer.Context, label: str, content: str) -> Path:
    """Drop a JSON file into the inject queue for the daemon to consume.

    Mirrors deliver-output.py's `enqueue_for_session`:
      - filename: `{ts}-{safe_label}.json` where ts is
        `datetime.now().strftime("%Y%m%dT%H%M%S")` and safe_label
        substitutes any character outside `[A-Za-z0-9_-]` with `_`.
      - payload: `{"label": <original label>, "content": <content>}`
        with ensure_ascii=False so non-ASCII content lands verbatim.

    Collision safety: the second-precision timestamp + label sanitizer
    can collapse two distinct invocations to the same filename inside
    the same wall-clock second (e.g. `foo bar` and `foo-bar` both
    sanitize to `foo-bar`). Naively `write_text` would silently
    overwrite the first write, dropping an ACK the daemon never saw.
    We open with `O_CREAT|O_EXCL` and on `FileExistsError` add a
    numeric suffix (`-2`, `-3`, ...) before the `.json` extension so
    the timestamp prefix the daemon parses stays intact and every
    write survives.

    Returns the path of the newly-written file so tests / callers can
    verify the exact write.
    """
    queue_dir = _resolved_inject_queue_dir(ctx)
    queue_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime(INJECT_TIMESTAMP_FORMAT)
    safe_label = _LABEL_UNSAFE_CHARS.sub("_", label)
    payload = json.dumps({"label": label, "content": content}, ensure_ascii=False)
    payload_bytes = payload.encode("utf-8")
    base = f"{ts}-{safe_label}"
    # First attempt: `<ts>-<safe_label>.json`; subsequent collisions add
    # `-2`, `-3`, ... The suffix is BEFORE `.json` (never after `<ts>`)
    # so the daemon-side timestamp parser still reads the right prefix.
    for suffix_index in range(1, 10_000):
        if suffix_index == 1:
            candidate = queue_dir / f"{base}.json"
        else:
            candidate = queue_dir / f"{base}-{suffix_index}.json"
        try:
            fd = os.open(
                str(candidate),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload_bytes)
        except Exception:
            # If the write itself fails after the exclusive create, we
            # must not leave a phantom half-written queue file the
            # daemon might read. Best-effort cleanup, then re-raise.
            try:
                candidate.unlink()
            except OSError:
                pass
            raise
        return candidate
    raise RuntimeError(
        f"inject: could not allocate a collision-free queue filename for "
        f"base={base!r} after 10000 attempts — the queue dir is stuck."
    )


# ---------------------------------------------------------------------------
# WRITE / OUTBOUND verbs (mock-only in tests).
# ---------------------------------------------------------------------------


@telegram_app.command("send")
def send(
    ctx: typer.Context,
    text: str = typer.Argument(
        ...,
        help=(
            "Raw text to send to the daemon's Telegram chat. Passed through as "
            "`--raw <text>` to deliver-output.py; the underlying script "
            "handles markdown->HTML conversion and chunking to Telegram's 4096-"
            "char limit."
        ),
    ),
) -> None:
    """Send a raw text to the operator's Telegram chat (WRITE, OUTBOUND).

    Wraps `deliver-output.py --raw <text>`. The live script:
      1. Reads the bot token + chat id from macOS Keychain
         (`telegram-bot-token` / `telegram-chat-id`).
      2. Checks the chat id against the allowlist slot
         (`telegram-allowed-chat-ids`); fail-closed on empty.
      3. Converts markdown to Telegram HTML and posts via the Bot API,
         chunking at paragraph / line / word boundaries.
      4. On success, drops an inject-queue entry (label `"raw"`) so
         the Landline daemon acknowledges the delivery.

    Fail-loud on a bogus `--profile`: `get_profile(ctx)` runs first so
    an unresolvable / malformed profile stops the send before any
    subprocess spawn (2026-08-28 rev, F3 hardening).

    ⚠️ SAFETY: during dev/test the wrapper is patched. A live shell
    invocation IS a real send. Confirm with the operator before invoking
    outbound.
    """
    get_profile(ctx)
    rc = run_deliver_output(["--raw", text], ctx=ctx)
    raise typer.Exit(code=rc)


@telegram_app.command("deliver")
def deliver(
    ctx: typer.Context,
    path: str = typer.Argument(
        ...,
        help=(
            "Path to a brief file to deliver (e.g. `briefs_morning/"
            "morning-2026-07-27.md`). File content is markdown-converted "
            "and delivered by deliver-output.py."
        ),
    ),
) -> None:
    """Deliver a brief file to the operator's Telegram chat (WRITE, OUTBOUND).

    Wraps `deliver-output.py <path>`. The live script reads the file's
    contents (label defaults to the file stem so the inject-queue entry
    is meaningfully named) and does the same load/allowlist/chunk/send
    dance as `send`. Empty files exit 1 with "Empty content, skipping
    delivery" — that surfaces verbatim on stderr.

    Fail-loud on a bogus `--profile`: `get_profile(ctx)` runs first so
    an unresolvable / malformed profile stops the deliver before any
    subprocess spawn (2026-08-28 rev, F3 hardening).

    ⚠️ SAFETY: during dev/test the wrapper is patched. A live shell
    invocation IS a real send. Confirm with the operator before invoking
    outbound.
    """
    get_profile(ctx)
    rc = run_deliver_output([path], ctx=ctx)
    raise typer.Exit(code=rc)


@telegram_app.command("inject")
def inject(
    ctx: typer.Context,
    label: str = typer.Argument(
        ...,
        help=(
            "Inject-queue label (a-z0-9_-). Written into the queue "
            "filename after sanitization and into the payload verbatim."
        ),
    ),
    content: str = typer.Argument(
        ...,
        help="Content payload the daemon will inject as an ACK on pickup.",
    ),
) -> None:
    """Drop an inject-queue entry for the Landline daemon (WRITE, local file).

    Writes `<ts>-<safe_label>.json` into the inject-queue directory,
    resolved per profile: `<active_profile.workspace_absolute>/cache/
    inject-queue/`. Overrideable for tests via `MINERU_INJECT_QUEUE_DIR`.
    Byte-identical payload to `deliver-output.py`'s `enqueue_for_session`
    (`{"label": <label>, "content": <content>}`).

    ⚠️ Landline daemon coordination: at cutover, point the daemon's
    inject-queue reader at the SAME per-profile path this verb writes
    to. Mismatch = silent ACK loss.

    Does NOT send to Telegram. Use this when the daemon should
    acknowledge an out-of-band delivery (e.g. a cron report that
    reached the operator via another channel) without re-sending it.

    On success, prints the absolute path of the written file to stdout
    for the caller to log / verify.
    """
    written = _write_inject(ctx, label, content)
    typer.echo(str(written))


# ---------------------------------------------------------------------------
# READ verb.
# ---------------------------------------------------------------------------


@telegram_app.command("allowlist")
def allowlist(ctx: typer.Context) -> None:
    """Show the current Telegram chat-id allowlist (READ-ONLY).

    Reads the SAME source of truth deliver-output.py and the Landline
    guard consult: the macOS Keychain slot `telegram-allowed-chat-ids`
    on account `mineru` (or the active profile's `keychain_account`).
    Prints the raw comma-separated string to stdout.

    Test-only override: set `MINERU_TELEGRAM_ALLOWLIST_FILE` to the path
    of a file containing the comma-separated allowlist string. The verb
    then reads from that file instead of Keychain. NOT a supported
    production mechanism — production always goes through Keychain so
    the CLI-visible value matches what actually gates a delivery.

    Empty output means the allowlist is unset, which per the fail-
    closed policy means the delivery script will refuse every send.
    """
    raw = _read_allowlist(ctx)
    typer.echo(raw)


# ---------------------------------------------------------------------------
# Phase 3 (P3-03) — photo verb + photos ledger sub-app (§4.1 + §4.2).
# ---------------------------------------------------------------------------

# Retention windows advertised on the --retention help. The transport /
# cache layer accept any positive int + the 'forever' sentinel; these are
# the four preset windows §4.1 documents, with `forever` for keepers.
RETENTION_CHOICES = ("30", "60", "90", "365", "forever")

# The parse_mode surface the verb accepts (lowercase for typing ergonomics).
# We forward the Bot-API-shaped uppercase form on the wire, per Telegram docs.
PARSE_MODE_CHOICES = ("html", "markdown")
PARSE_MODE_TO_WIRE = {"html": "HTML", "markdown": "MarkdownV2"}

# Caption ellipsis — one character so the truncation math is trivial and
# the sidecar records the exact bytes that hit Telegram.
CAPTION_ELLIPSIS = "…"

# --since 30d / 12h / 7 shorthand parser. Kept local to this verb rather
# than promoted to a shared helper: `mineru gmail search` uses Gmail's
# native `newer_than:1d` operator (no local parse), so a shared helper
# wouldn't have a second caller today.
_SINCE_PATTERN = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_SINCE_UNIT_TO_SECONDS = {
    "": 86400,   # bare integer -> days
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
}


def _cache_dir_for_ctx(ctx: typer.Context) -> Optional[Path]:
    """Return the per-profile sent-image cache dir, or None for env-override handoff.

    Precedence:
      1. `MINERU_TELEGRAM_SENT_IMAGE_DIR` env override — deferred to the
         wrapper's own `resolve_cache_dir()`, so returning None here
         lets the wrapper honor the env. Every test sets that env, so
         the wrapper still sees the tmp path.
      2. Active profile's `<workspace_absolute>/cache/telegram_sent_images/`
         (hydrated via `get_profile(ctx)`). This is the isolation-correct
         default: profile B's send history never leaks into profile A's
         workspace.
      3. `get_profile(ctx)` raises `typer.BadParameter` when no active
         profile is selectable — the legacy `$MINERU_HOME/...` fallback
         is retired (F2 completion, 2026-08-28 rev).

    Returning None (instead of computing a per-profile path) when the
    env override is set keeps the wrapper's env-first resolution
    authoritative, so a test that patches the env can still control
    the cache location without also having to inject a profile.
    """
    if os.environ.get("MINERU_TELEGRAM_SENT_IMAGE_DIR"):
        return None
    profile = get_profile(ctx)
    workspace_root = getattr(profile, "workspace_absolute", None)
    if workspace_root is not None:
        return cache_dir_for_workspace(Path(workspace_root))
    return None


def _resolver_for_ctx(ctx: typer.Context):
    """Return a `SecretsResolver` hydrated from the active profile.

    Mirrors `mineru_cli.verbs.secrets._resolver_for_ctx`: calls
    `get_profile(ctx)` to load + validate the active profile on first
    call (fails loud on bogus profile), which populates
    `ctx.obj["secrets_config"]` with the profile-derived backend chain
    (env then keychain, both scoped to the profile's `keychain_account`).
    We read that back and hand it to `build_resolver`.

    Fallback to `SecretsConfig()` is only for tests that build a Typer
    context without going through the root callback.
    """
    get_profile(ctx)
    obj = ctx.obj or {}
    config = obj.get("secrets_config") or SecretsConfig()
    return build_resolver(config)


def _truncate_caption(caption: Optional[str]) -> Optional[str]:
    """Trim to TELEGRAM_CAPTION_MAX_CHARS with a one-char ellipsis.

    Character-count truncation (not byte-count) — Telegram's caption
    limit is defined in code-points, so a multi-byte character is one
    unit against the cap.

    Returns None on None (the transport treats None as "field not set");
    an empty string passes through as an empty caption.
    """
    if caption is None:
        return None
    if len(caption) <= TELEGRAM_CAPTION_MAX_CHARS:
        return caption
    # Leave room for the single-char ellipsis so the final string is
    # exactly at the cap.
    keep = TELEGRAM_CAPTION_MAX_CHARS - len(CAPTION_ELLIPSIS)
    return caption[:keep] + CAPTION_ELLIPSIS


def _normalize_retention(raw: str) -> "int | str":
    """Convert the CLI `--retention` string to the cache-layer shape.

    'forever' -> the module sentinel (string); any digit -> int. Anything
    else raises typer.BadParameter so the CLI surfaces a clean usage
    frame instead of a stack trace out of the cache layer.
    """
    if raw.lower() == RETENTION_FOREVER:
        return RETENTION_FOREVER
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--retention must be an integer number of days or 'forever'; got {raw!r}."
        ) from exc
    if parsed <= 0:
        raise typer.BadParameter(
            f"--retention must be a positive integer; got {parsed}."
        )
    return parsed


def _parse_since(raw: str) -> datetime.datetime:
    """Parse `--since 30d` / `--since 12h` / `--since 7` (bare = days).

    Returns an aware `datetime` at now-<delta>. Raises
    `typer.BadParameter` on malformed input.

    Design note: the shape mirrors `age`-style shorthands (Amazon CLI,
    Gmail `newer_than:1d`) so operators reach for it without a doc lookup.
    """
    m = _SINCE_PATTERN.match(raw)
    if not m:
        raise typer.BadParameter(
            f"--since must look like '30d' / '12h' / '90' (bare == days); got {raw!r}."
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    seconds = n * _SINCE_UNIT_TO_SECONDS[unit]
    return datetime.datetime.now().astimezone() - datetime.timedelta(seconds=seconds)


def _render_records_json(records: List[SentImageRecord]) -> str:
    """Return a JSON array of the raw sidecar dicts (stable key order).

    We return the sidecar `.raw` dicts verbatim rather than a re-derived
    view, so an operator scripting against `--json` sees the exact bytes
    on disk and doesn't have to reconcile two shapes.
    """
    return json.dumps([r.raw for r in records], indent=2, sort_keys=True, ensure_ascii=False)


def _render_records_plain(records: List[SentImageRecord]) -> str:
    """Return a compact ASCII bullet listing.

    Never a Markdown table — MEMORY.md's "no tables in Telegram" rule
    doesn't strictly apply here (this is CLI stdout, not Telegram), but
    we still keep it bullet-shaped so a copy-paste into Telegram
    renders cleanly.

    Shape per record:

      - <stem>  chat=<chat_id>  sent=<sent_at>  expires=<expires_at|forever>
        label=<label>  caption=<one-line caption slice>
    """
    if not records:
        return "(no records)"
    lines: List[str] = []
    for r in records:
        expires_frag = "forever" if r.expires_at is None else r.expires_at
        # Caption preview: single line, trimmed, so a multi-line caption
        # can't blow up the bullet.
        preview = (r.caption or "").replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:79] + CAPTION_ELLIPSIS
        lines.append(
            f"- {r.stem}  chat={r.chat_id}  sent={r.sent_at}  "
            f"expires={expires_frag}\n"
            f"    label={r.label}  caption={preview!r}"
        )
    return "\n".join(lines)


def _find_record_by_prefix(
    prefix: str, *, cache_dir: Optional[Path] = None
) -> Optional[SentImageRecord]:
    """Return the record whose stem starts with `prefix`, or None.

    Ambiguous prefixes (>=2 matches) raise `typer.BadParameter` so the
    operator sees the ambiguity and can extend the prefix. Empty
    prefix is treated as no-match, not "everything", to avoid a
    surprising accidental hit on a bare `mineru telegram photos show`.
    """
    if not prefix:
        return None
    matches = [r for r in iter_records(cache_dir=cache_dir) if r.stem.startswith(prefix)]
    if not matches:
        return None
    if len(matches) > 1:
        stems = ", ".join(sorted(r.stem for r in matches))
        raise typer.BadParameter(
            f"prefix {prefix!r} matches {len(matches)} records: {stems}. "
            "Provide more of the stem."
        )
    return matches[0]


# ---------------------------------------------------------------------------
# `mineru telegram photo` — WRITE, OUTBOUND.
# ---------------------------------------------------------------------------


@telegram_app.command(
    "photo",
    help=(
        "Send a photo via the self-contained sendPhoto transport (WRITE, OUTBOUND).\n\n"
        "Examples:\n"
        "  mineru telegram photo /tmp/juno.jpg --caption 'Juno smile'\n"
        "  mineru telegram photo /tmp/juno.jpg --dedup --retention 90\n"
        "  mineru telegram photo /tmp/keeper.jpg --retention forever --label 'anthology'\n"
        "  mineru telegram photo /tmp/reply.jpg --reply-to 12345 --parse-mode html\n\n"
        "⚠️ SAFETY: during dev/test the transport is patched. A live shell "
        "invocation IS a real send. Confirm with the operator before invoking outbound."
    ),
)
def photo(
    ctx: typer.Context,
    path: str = typer.Argument(
        ...,
        help=(
            "Absolute path to the image file. Reads the file bytes on the "
            "fresh-upload path; on --dedup the source file is opened only "
            "when no cached telegram_file_id is available for its sha256."
        ),
    ),
    caption: Optional[str] = typer.Option(
        None,
        "--caption",
        help=(
            "Optional caption text. Truncated to "
            f"{TELEGRAM_CAPTION_MAX_CHARS} chars with a '{CAPTION_ELLIPSIS}' "
            "ellipsis before send (so the sidecar records exactly what hit Telegram)."
        ),
    ),
    parse_mode: Optional[str] = typer.Option(
        None,
        "--parse-mode",
        case_sensitive=False,
        help=(
            "Caption parser: html or markdown. Forwarded on the wire as "
            "'HTML' or 'MarkdownV2' per Bot API docs."
        ),
    ),
    retention: str = typer.Option(
        str(DEFAULT_RETENTION_DAYS),
        "--retention",
        help=(
            "Days to keep in the sent-image cache: 30 / 60 / 90 / 365 / forever. "
            "Defaults to 60 (seed profile). 'forever' skips prune."
        ),
    ),
    dedup: bool = typer.Option(
        False,
        "--dedup",
        help=(
            "SHA256 the source bytes; if a cached record has a live "
            "telegram_file_id for that hash, POST photo=<file_id> instead of "
            "re-uploading. Bandwidth saver; confirms Telegram still has the file."
        ),
    ),
    label: Optional[str] = typer.Option(
        None,
        "--label",
        help=(
            "Ledger label (defaults to the filename stem). Recorded on the "
            "sidecar for search / show; not sent to Telegram."
        ),
    ),
    reply_to: Optional[int] = typer.Option(
        None,
        "--reply-to",
        help="Optional reply threading target (Telegram messageId).",
    ),
    chat_id: Optional[str] = typer.Option(
        None,
        "--chat-id",
        help=(
            "Override the resolved default chat id. Defaults to the "
            "'telegram-chat-id' secret via the profile's backend chain."
        ),
    ),
) -> None:
    """Send a photo to Telegram, cache the sidecar, return a machine-readable summary.

    Flow:
      1. Read source bytes; compute sha256.
      2. Resolve the effective chat_id (--chat-id > secret).
      3. If --dedup, look up the sha256 in the retention cache; on hit,
         call the transport with `is_cached_id=True` (no fresh upload,
         no second read of the source file).
      4. Otherwise stage the bytes in the cache (0600), call the
         transport with the on-disk path (fresh multipart), and commit
         the sidecar with the returned telegram_file_id + message_id.

    On success prints a compact JSON summary to stdout
    (`{stem, message_id, telegram_file_id, dedup, expires_at}`) so a
    caller (skill, cron job) can log / route it. On failure exits with
    a non-zero code and a stderr line naming the transport error label.
    """
    # Truncate caption BEFORE both the transport call and the sidecar
    # write so both surfaces record the exact bytes that hit Telegram.
    trimmed_caption = _truncate_caption(caption)
    retention_value = _normalize_retention(retention)
    # Validate `--parse-mode` against the accepted set BEFORE the wire
    # lookup — a bare dict read on an unrecognized value would surface
    # as a `KeyError` traceback (exit 1, empty stderr) instead of the
    # clean Typer usage frame + exit 2 the CLI promises.
    wire_parse_mode: Optional[str] = None
    if parse_mode is not None:
        normalized_parse_mode = parse_mode.lower()
        if normalized_parse_mode not in PARSE_MODE_TO_WIRE:
            raise typer.BadParameter(
                f"--parse-mode must be one of {PARSE_MODE_CHOICES}; "
                f"got {parse_mode!r}.",
                param_hint="--parse-mode",
            )
        wire_parse_mode = PARSE_MODE_TO_WIRE[normalized_parse_mode]

    resolver = _resolver_for_ctx(ctx)
    # We resolve the chat id BEFORE the send so the sidecar records it
    # even on the cached-file fast path (the transport also resolves it
    # for the actual HTTP call; both paths agree).
    resolved_chat_id: str = photo_transport._resolve_chat_id(resolver, chat_id)

    src = Path(path).expanduser()
    if not src.exists() or not src.is_file():
        typer.echo(
            f"mineru telegram photo: source photo {src} does not exist or is not a file.",
            err=True,
        )
        raise typer.Exit(code=2)

    # Per-profile cache root: threaded into every cache call below so
    # the send-photo history is isolated to the active profile.
    ctx_cache_dir = _cache_dir_for_ctx(ctx)

    dedup_hit: Optional[SentImageRecord] = None
    if dedup:
        # sha256 for the dedup lookup — cheap enough (few MB image, single
        # SHA pass) to skip the "delay to see if we need it" branch.
        sha256_hex = hashlib.sha256(src.read_bytes()).hexdigest()
        dedup_hit = lookup_by_sha256(sha256_hex, cache_dir=ctx_cache_dir)

    if dedup_hit is not None:
        # CACHED FAST PATH. Do NOT re-open the source file; the transport
        # takes the opaque file_id string and POSTs form-urlencoded.
        result = send_photo(
            dedup_hit.telegram_file_id,
            chat_id=resolved_chat_id,
            caption=trimmed_caption,
            parse_mode=wire_parse_mode,
            reply_to_message_id=reply_to,
            is_cached_id=True,
            secrets_resolver=resolver,
        )
        summary = {
            "stem": dedup_hit.stem,
            "message_id": result.get("message_id"),
            "telegram_file_id": result.get("telegram_file_id") or dedup_hit.telegram_file_id,
            "dedup": True,
            "expires_at": dedup_hit.expires_at,
            "ok": bool(result.get("ok")),
        }
        _finish_photo(result, summary)
        return

    # FRESH UPLOAD PATH. Stage in cache first so the bytes live at 0600
    # from the moment they hit disk; then send; then commit sidecar.
    try:
        handle: PendingCacheEntry = cache_binary(
            src,
            retention_days=retention_value,
            label=label,
            caption=trimmed_caption,
            chat_id=resolved_chat_id,
            cache_dir=ctx_cache_dir,
        )
    except SentImageCacheError as exc:
        typer.echo(f"mineru telegram photo: {exc}", err=True)
        raise typer.Exit(code=2)

    result = send_photo(
        str(handle.bytes_path),
        chat_id=resolved_chat_id,
        caption=trimmed_caption,
        parse_mode=wire_parse_mode,
        reply_to_message_id=reply_to,
        is_cached_id=False,
        secrets_resolver=resolver,
    )

    if not result.get("ok"):
        # Do NOT commit a sidecar on failure — the ledger must never
        # carry a phantom entry a later --dedup lookup would trust.
        summary = {
            "stem": handle.stem,
            "message_id": None,
            "telegram_file_id": None,
            "dedup": False,
            "expires_at": handle.expires_at,
            "ok": False,
            "error": result.get("error"),
            "retry_after": result.get("retry_after"),
        }
        _finish_photo(result, summary)
        return

    committed = commit_record(
        handle,
        telegram_file_id=result.get("telegram_file_id"),
        message_id=result.get("message_id"),
    )
    summary = {
        "stem": committed.stem,
        "message_id": committed.message_id,
        "telegram_file_id": committed.telegram_file_id,
        "dedup": False,
        "expires_at": committed.expires_at,
        "ok": True,
    }
    _finish_photo(result, summary)


def _finish_photo(result: dict, summary: dict) -> None:
    """Print the summary and exit with the appropriate code.

    Success -> stdout JSON summary, exit 0.
    Failure -> stderr error label + stdout summary (so a scripted caller
    still gets the machine-readable shape), exit 1 (or 3 for rate-limit
    so the caller can distinguish retry-worthy from hard-failure).
    """
    typer.echo(json.dumps(summary, sort_keys=True, ensure_ascii=False))
    if summary.get("ok"):
        raise typer.Exit(code=0)
    err_label = result.get("error") or "unknown"
    typer.echo(
        f"mineru telegram photo: transport failed ({err_label}); "
        "sidecar NOT committed. See summary above.",
        err=True,
    )
    if err_label == "rate_limited":
        # Distinct exit code so a caller loop knows to sleep the
        # retry_after seconds and re-invoke.
        raise typer.Exit(code=3)
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# `mineru telegram photos` — READ-ONLY (plus a trash-based prune).
# ---------------------------------------------------------------------------


photos_app = typer.Typer(
    name="photos",
    help=(
        "Sent-image retention cache: list / search / show / prune. "
        "Reads the local ledger at "
        "<active_profile.workspace_absolute>/cache/telegram_sent_images/. "
        "No network I/O; prune uses `trash` (never `rm`)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@photos_app.command(
    "list",
    help=(
        "List cached sent-image records, newest last.\n\n"
        "Examples:\n"
        "  mineru telegram photos list\n"
        "  mineru telegram photos list --since 30d --json\n"
        "  mineru telegram photos list --chat 12345"
    ),
)
def photos_list(
    ctx: typer.Context,
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="Only include records sent after `<N>d|h|m|s|w` (bare int = days).",
    ),
    chat: Optional[str] = typer.Option(
        None,
        "--chat",
        help="Filter by chat_id (string-equal; int or str both accepted).",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the raw sidecar dicts as a JSON array."
    ),
) -> None:
    """List cached sent-image records with optional filters."""
    since_dt = _parse_since(since) if since else None
    ctx_cache_dir = _cache_dir_for_ctx(ctx)
    records = list(iter_records(since=since_dt, chat_id=chat, cache_dir=ctx_cache_dir))
    if json_out:
        typer.echo(_render_records_json(records))
    else:
        typer.echo(_render_records_plain(records))


@photos_app.command(
    "search",
    help=(
        "Case-insensitive substring search across caption AND label.\n\n"
        "Examples:\n"
        "  mineru telegram photos search river\n"
        "  mineru telegram photos search 'first smile' --json"
    ),
)
def photos_search(
    ctx: typer.Context,
    substring: str = typer.Argument(
        ..., help="Substring to match against caption OR label (case-insensitive)."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the raw sidecar dicts as a JSON array."
    ),
) -> None:
    """Search cached records by caption / label substring."""
    ctx_cache_dir = _cache_dir_for_ctx(ctx)
    records = search_records(substring, cache_dir=ctx_cache_dir)
    if json_out:
        typer.echo(_render_records_json(records))
    else:
        typer.echo(_render_records_plain(records))


@photos_app.command(
    "show",
    help=(
        "Print the sidecar JSON for a single record.\n\n"
        "Examples:\n"
        "  mineru telegram photos show 20260725-153042-a1b2c3d4\n"
        "  mineru telegram photos show 20260725"
    ),
)
def photos_show(
    ctx: typer.Context,
    id_or_prefix: str = typer.Argument(
        ...,
        help=(
            "Full stem or unique prefix (e.g. `20260725-153042-a1b2c3d4` or "
            "`20260725-153042`). Ambiguous prefixes are rejected."
        ),
    ),
) -> None:
    """Show one record's full sidecar JSON, exit 2 on miss / ambiguity."""
    ctx_cache_dir = _cache_dir_for_ctx(ctx)
    record = _find_record_by_prefix(id_or_prefix, cache_dir=ctx_cache_dir)
    if record is None:
        typer.echo(
            f"mineru telegram photos show: no record matched {id_or_prefix!r}.",
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(json.dumps(record.raw, indent=2, sort_keys=True, ensure_ascii=False))


@photos_app.command(
    "prune",
    help=(
        "Trash cache entries past their `expires_at`. Uses `trash` (never `rm`).\n\n"
        "Examples:\n"
        "  mineru telegram photos prune --dry-run\n"
        "  mineru telegram photos prune"
    ),
)
def photos_prune(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "List candidates without touching the filesystem. "
            "Mirrors the `--dry-run` flag on cleanup-retention.sh."
        ),
    ),
) -> None:
    """Manual prune of expired entries; `forever` entries are preserved."""
    ctx_cache_dir = _cache_dir_for_ctx(ctx)
    try:
        report: PruneReport = prune_expired(dry_run=dry_run, cache_dir=ctx_cache_dir)
    except SentImageCacheError as exc:
        typer.echo(f"mineru telegram photos prune: {exc}", err=True)
        raise typer.Exit(code=2)

    verb = "would trash" if dry_run else "trashed"
    typer.echo(
        f"{verb} {len(report.expired_records)} expired record(s); "
        f"kept {report.kept_forever} forever, {report.kept_unexpired} unexpired."
    )
    for record in report.expired_records:
        typer.echo(f"  - {record.stem}  expires_at={record.expires_at}")

    # Surface per-record trash failures loudly so a partially-failed
    # prune doesn't silently look like a success. Exit non-zero (2) so
    # a cron / shell caller can gate downstream steps on the real
    # outcome.
    if report.failed_records:
        typer.echo(
            f"FAILED to trash {len(report.failed_records)} record(s):",
            err=True,
        )
        for record, err in report.failed_records:
            typer.echo(f"  - {record.stem}: {err}", err=True)
        raise typer.Exit(code=2)


telegram_app.add_typer(photos_app, name="photos")
