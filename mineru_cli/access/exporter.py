"""Pure join + Keychain-export helpers for the per-profile allowlist.

This module is the load-bearing engine side of the Arc 2 step-4 daemon
handshake: it converts the authoritative pair of YAML files
(`access.yaml` + `humans.yaml`) into the single, opaque comma-string that
the Landline daemon reads from macOS Keychain. The daemon never imports
engine code — the seam is one-way, and this module writes the slot the
daemon consumes.

Keychain contract (must match `landline.runtime.guard._parse_int_set`):

  - SERVICE  = "telegram-allowed-chat-ids"   (fixed; never varies)
  - ACCOUNT  = <Profile.keychain_account>    (per-profile; default "mineru",
                                              e.g. "landline" / "mineru" /
                                              "gemini-scout" on multi-agent
                                              installs)
  - FORMAT   = a single string whose content is a comma-separated list of
               integer Telegram USER ids (`from.id` on each inbound
               message; in a 1:1 owner-bot chat this equals `chat.id`,
               which is why existing single-value slots keep working).
               Whitespace around commas is tolerated by the parser. A
               non-integer token is silently skipped. An EMPTY string
               (or a string with only junk tokens) parses to an empty
               set, which is FAIL-CLOSED — the daemon then rejects
               every sender.

Why pure-join first, `security` shell-out second:

  `resolve_allowlist_ids` is a small deterministic function with strict
  validation (owner present, no duplicates, no zero/negative ids). It
  has no I/O and is exhaustively testable. The verb layer + the
  `sync`/`status` CLI commands compose it with a Keychain reader/writer
  that shells out to `/usr/bin/security` (so the wire format is exactly
  what the daemon reads via its own `security find-generic-password`
  call). Tests never write the live Keychain: the reader/writer helpers
  take an injectable subprocess runner + binary path, and every unit
  test patches those.

Never import from `landline.*` here. The seam stays one-way: the CLI
refreshes the Keychain slot the daemon reads. A cross-repo import from
this side would silently couple the daemon's release cycle to the
engine's.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from mineru_cli.access.schema import AccessConfig, AccessTier
from mineru_cli.humans.schema import HumansRegistry


logger = logging.getLogger(__name__)


# --- Constants (must match the Landline guard byte-for-byte) --------------

# Fixed Keychain service name the daemon reads.
# `landline.runtime.guard` and `deliver-output.py` both read this exact
# string; a rename here silently unwires every existing install.
TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE = "telegram-allowed-chat-ids"

# Fallback Keychain account for callers that build a resolver without a
# fully-hydrated profile (mostly tests). Matches the seed profile default
# and every hardcoded `-a mineru` call site the audit inventoried.
DEFAULT_KEYCHAIN_ACCOUNT = "mineru"

# Absolute path to the macOS `security` binary. Absolute on purpose so
# a PATH shim can't redirect the read/write to something else.
SECURITY_BINARY = "/usr/bin/security"

# Bounded wait for the `security` subprocess. The binary can wedge
# indefinitely on a locked login keychain waiting on GUI unlock; cap
# it so the CLI can never hang.
KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS = 5.0

# Exit codes we branch on. Documented empirically for `security -w`:
#   0  -> hit
#   44 -> SecKeychainItemNotFound (never configured)
#   36 -> SecAuthFailed (locked keychain)
KEYCHAIN_EXIT_HIT = 0
KEYCHAIN_EXIT_ITEM_NOT_FOUND = 44
KEYCHAIN_EXIT_LOCKED = 36


# Type alias for the injectable subprocess runner. Callers (and tests)
# hand in something with `subprocess.run`-compatible signature so we
# never touch the real Keychain in unit tests.
SubprocessRunner = Callable[..., subprocess.CompletedProcess]


class AllowlistExportError(RuntimeError):
    """Raised on any failure inside `resolve_allowlist_ids` /
    `format_allowlist_value` / the Keychain read+write helpers.

    The `__str__` always names the offending handle, field, or Keychain
    identifier so a Typer handler can render it verbatim to stderr.
    """


# --- Pure join ------------------------------------------------------------


def resolve_allowlist_ids(
    access_config: AccessConfig,
    humans_registry: HumansRegistry,
) -> List[int]:
    """Resolve the per-profile access allowlist to Telegram user ids.

    Joins `access.yaml` (owner + `authorized` list) against `humans.yaml`
    (handle -> Telegram identity).

    Cat-B critical (2026-09-04 step-5 audit, Finding 11): the daemon
    guard has NO tier concept — every id it reads from the Keychain slot
    is admitted with full owner-equivalent access. Guest-tier scoping
    (restricting the toolset a GUEST can reach) is UNIMPLEMENTED. Until
    that lands, a guest in the exported allowlist would silently grant
    owner-equivalent access. So this resolver EXCLUDES every non-owner
    (GUEST-tier) entry from the exported ids, and emits a loud stderr
    warning per skipped handle so the operator sees exactly which humans
    are NOT being admitted and why. Owner-tier entries are unaffected.

    Returns the resolved ids in a deterministic order: the owner first,
    then every OWNER-tier authorized entry in declaration order. This
    ordering is a stability property (diffs are boring across syncs) —
    the daemon-side parser is order-insensitive.

    Validation (fail-loud, all raise `AllowlistExportError`):
      - the owner MUST be present in the resolved list.
      - no duplicate Telegram ids (two humans pointing at the same
        `telegram_id` is a humans.yaml misconfiguration the operator
        needs to see, not a silently deduped slot).
      - every id MUST be a positive integer (Telegram user ids are
        positive; a `0` or negative slips through would either match
        nothing at the daemon or shadow a legitimate id).
      - every OWNER-tier authorized handle MUST resolve to a `humans.yaml`
        entry (the `access.yaml` loader already cross-checks this, but the
        exporter re-checks so a caller who hands in a hand-built
        `AccessConfig` still fails loud). GUEST-tier handles are skipped
        BEFORE this check — they never enter the resolved list, so a
        stale/broken guest handle cannot block a sync.
    """
    if not isinstance(access_config, AccessConfig):
        raise AllowlistExportError(
            f"resolve_allowlist_ids: access_config must be AccessConfig, "
            f"got {type(access_config).__name__}."
        )
    if not isinstance(humans_registry, HumansRegistry):
        raise AllowlistExportError(
            f"resolve_allowlist_ids: humans_registry must be HumansRegistry, "
            f"got {type(humans_registry).__name__}."
        )

    owner_handle = access_config.owner
    if owner_handle not in humans_registry:
        raise AllowlistExportError(
            f"profile {access_config.profile_name!r}: owner handle "
            f"{owner_handle!r} is not present in humans.yaml. Add the "
            "human before syncing the Keychain allowlist."
        )

    # Deterministic order: owner FIRST, then every OWNER-tier authorized
    # entry in the order they appear in access.yaml. `access.authorized`
    # always includes the owner (the loader materializes it even if the
    # operator omitted them), so we skip re-adding the owner mid-loop.
    # GUEST-tier entries are EXCLUDED and each triggers a loud warning
    # (see Finding 11 note above).
    ordered_handles: List[str] = [owner_handle]
    seen_handles = {owner_handle}
    skipped_guests: List[str] = []
    for entry in access_config.authorized:
        if entry.tier != AccessTier.OWNER:
            # Skip guest-tier (and any future non-owner tier that lands
            # before real per-tier enforcement wires in). Owner entry
            # itself always has tier=OWNER, so this branch never drops it.
            if entry.human != owner_handle:
                skipped_guests.append(entry.human)
            continue
        if entry.human in seen_handles:
            continue
        if entry.human not in humans_registry:
            raise AllowlistExportError(
                f"profile {access_config.profile_name!r}: authorized handle "
                f"{entry.human!r} (tier={entry.tier.value}) is not present "
                "in humans.yaml. Reject the sync and fix the registry."
            )
        ordered_handles.append(entry.human)
        seen_handles.add(entry.human)

    if skipped_guests:
        # One warning line per skipped guest so grepping logs picks up
        # each handle by name. The header explains WHY they were skipped
        # so a future maintainer does not think the exclusion is a bug.
        joined = ", ".join(repr(h) for h in skipped_guests)
        header = (
            f"[access] profile {access_config.profile_name!r}: "
            f"NOT admitting guest-tier handles to the Keychain allowlist "
            f"({len(skipped_guests)} skipped: {joined}). Guest-tier scoping "
            "is not yet enforced by the daemon — admitting them would grant "
            "OWNER-equivalent access. Remove these entries from access.yaml "
            "or wait for the real per-tier enforcement to land."
        )
        logger.warning(header)
        # Also mirror to stderr so a `mineru access sync --apply` operator
        # sees the warning even when logging is not configured. Import here
        # to keep the module import surface tiny and match the "sys" usage
        # of the surrounding CLI/verb layer.
        import sys as _sys
        print(header, file=_sys.stderr)

    ids: List[int] = []
    seen_ids: dict[int, str] = {}
    for handle in ordered_handles:
        human = humans_registry.get(handle)
        tg_id = human.telegram_id
        # `bool` subclasses `int` in Python; a stray `true`/`false` in
        # humans.yaml would otherwise pass an isinstance(int) check and
        # get written to Keychain as "1" / "0", either of which is
        # meaningless as a Telegram user id.
        if isinstance(tg_id, bool) or not isinstance(tg_id, int):
            raise AllowlistExportError(
                f"profile {access_config.profile_name!r}: human {handle!r} "
                f"has non-integer telegram_id {tg_id!r} in humans.yaml. "
                "Fix the registry before syncing."
            )
        if tg_id <= 0:
            raise AllowlistExportError(
                f"profile {access_config.profile_name!r}: human {handle!r} "
                f"has non-positive telegram_id {tg_id} in humans.yaml. "
                "Telegram user ids are positive integers."
            )
        if tg_id in seen_ids:
            other = seen_ids[tg_id]
            raise AllowlistExportError(
                f"profile {access_config.profile_name!r}: humans {other!r} "
                f"and {handle!r} both resolve to telegram_id {tg_id}. "
                "Deduplicate humans.yaml before syncing."
            )
        seen_ids[tg_id] = handle
        ids.append(tg_id)

    return ids


def format_allowlist_value(ids: Sequence[int]) -> str:
    """Serialize resolved ids to the Keychain wire format.

    A comma-separated string of decimal integers, no surrounding
    whitespace, no trailing comma. Matches the daemon parser's tolerant
    input format (`" 111 , 222 "` → {111, 222}) but emits the strict
    canonical form so re-syncs produce identical bytes when nothing has
    changed (the `status` diff stays clean).

    An empty sequence serializes to the empty string, which the daemon
    parses as an empty set — fail-closed. This is a deliberate choice:
    an operator who somehow ends up with zero resolved ids should see
    the daemon block every sender rather than silently accept anyone.
    """
    return ",".join(str(int(i)) for i in ids)


# --- Keychain read (diff) -------------------------------------------------


@dataclass(frozen=True)
class KeychainReadResult:
    """Outcome of reading the Keychain allowlist slot.

    Attributes:
        raw: the exact string stored in the slot (never `None`; a real
            "item not found" surfaces as `present=False` with `raw=""`).
        present: True iff the `security` call returned success (exit 0).
            False on `SecKeychainItemNotFound` (44), a wedged/locked
            keychain, `security` missing, or any other non-zero exit.
        rc: the raw exit code from `security` (0 on hit; 44 on miss;
            36 on locked; negative on subprocess errors we translated).
        stderr_snippet: a short, safe stderr snippet for diagnostic
            logging. Never contains the stored value.
    """

    raw: str
    present: bool
    rc: int
    stderr_snippet: str = ""


def read_keychain_allowlist(
    keychain_account: str,
    *,
    service: str = TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE,
    binary: str = SECURITY_BINARY,
    runner: Optional[SubprocessRunner] = None,
    timeout_seconds: float = KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS,
) -> KeychainReadResult:
    """Read the Keychain allowlist slot for `keychain_account`.

    Args:
        keychain_account: the `-a` account namespace. Should match the
            profile's `keychain_account` (default "mineru").
        service: the `-s` service name. Fixed to
            `TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE` in production; the
            override slot exists for tests only.
        binary: the `security` binary path. Same override rationale.
        runner: injection point for tests. Omit for production; a mock
            that returns a `subprocess.CompletedProcess`-shaped object
            is what unit tests hand in.
        timeout_seconds: bounded wait on the subprocess.

    Returns a `KeychainReadResult`. Never raises — an absent slot, a
    locked keychain, or a missing binary all resolve to `present=False`
    with an actionable `stderr_snippet` for logging.
    """
    if not isinstance(keychain_account, str) or not keychain_account:
        raise AllowlistExportError(
            "read_keychain_allowlist: keychain_account must be a non-empty "
            f"string; got {keychain_account!r}."
        )
    argv = [
        binary,
        "find-generic-password",
        "-a",
        keychain_account,
        "-s",
        service,
        "-w",
    ]
    run = runner or _default_runner
    try:
        completed = run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return KeychainReadResult(
            raw="",
            present=False,
            rc=-1,
            stderr_snippet=f"{binary!r} not available (non-macOS host?).",
        )
    except subprocess.TimeoutExpired:
        return KeychainReadResult(
            raw="",
            present=False,
            rc=-2,
            stderr_snippet=(
                f"security find-generic-password timed out after "
                f"{timeout_seconds:.1f}s (is the login keychain locked?)."
            ),
        )

    stderr_snippet = _safe_stderr(getattr(completed, "stderr", "") or "")
    rc = int(completed.returncode)
    if rc == KEYCHAIN_EXIT_HIT:
        raw = (completed.stdout or "")
        if raw.endswith("\n"):
            raw = raw[:-1]
        return KeychainReadResult(raw=raw, present=True, rc=rc)
    # Every non-hit path collapses to present=False so the caller shows
    # a clean drift line ("keychain empty / not set") without needing to
    # branch on the exact rc.
    return KeychainReadResult(
        raw="", present=False, rc=rc, stderr_snippet=stderr_snippet
    )


# --- Keychain write (apply) -----------------------------------------------


@dataclass(frozen=True)
class KeychainWriteResult:
    """Outcome of writing the Keychain allowlist slot.

    Attributes:
        ok: True iff `security add-generic-password -U` returned 0.
        rc: the raw exit code.
        stderr_snippet: short safe stderr for diagnostic logging.
        argv_shape: the argv shape actually invoked, WITHOUT the value
            (which stays out of any log). Handy in tests to assert
            "service and account went through the CLI cleanly."
    """

    ok: bool
    rc: int
    stderr_snippet: str
    argv_shape: List[str]


def write_keychain_allowlist(
    keychain_account: str,
    value: str,
    *,
    service: str = TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE,
    binary: str = SECURITY_BINARY,
    runner: Optional[SubprocessRunner] = None,
    timeout_seconds: float = KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS,
) -> KeychainWriteResult:
    """Write the Keychain allowlist slot atomically via `-U`.

    Uses `security add-generic-password -U -a <account> -s <service>
    -w <value>` — the `-U` flag updates an existing item in place OR
    creates it fresh, so the same command works on both a first sync
    and every subsequent one.

    Security invariants:
      - `value` is a comma-separated list of decimal integers. It IS an
        argv element — this is unavoidable with the `security` CLI —
        but every character in a well-formed value is ASCII-safe (0-9
        and `,`), and the value is validated by
        `format_allowlist_value` before it reaches here. A caller who
        hands in a value with embedded shell metacharacters is a bug in
        our own code, not a hostile input.
      - We NEVER log `value`. The stderr snippet may contain the
        service/account but never the payload.
      - `stdin` is detached (DEVNULL) so a prompt from `security` (e.g.
        keychain unlock) can never block waiting on input.
    """
    if not isinstance(keychain_account, str) or not keychain_account:
        raise AllowlistExportError(
            "write_keychain_allowlist: keychain_account must be a "
            f"non-empty string; got {keychain_account!r}."
        )
    if not isinstance(value, str):
        raise AllowlistExportError(
            "write_keychain_allowlist: value must be a string; got "
            f"{type(value).__name__}."
        )
    argv = [
        binary,
        "add-generic-password",
        "-U",
        "-a",
        keychain_account,
        "-s",
        service,
        "-w",
        value,
    ]
    # Argv shape without the payload — for logging + test assertions.
    argv_shape = [
        binary, "add-generic-password", "-U",
        "-a", keychain_account, "-s", service, "-w", "<REDACTED>",
    ]
    run = runner or _default_runner
    try:
        completed = run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return KeychainWriteResult(
            ok=False,
            rc=-1,
            stderr_snippet=f"{binary!r} not available (non-macOS host?).",
            argv_shape=argv_shape,
        )
    except subprocess.TimeoutExpired:
        return KeychainWriteResult(
            ok=False,
            rc=-2,
            stderr_snippet=(
                f"security add-generic-password timed out after "
                f"{timeout_seconds:.1f}s (is the login keychain locked?)."
            ),
            argv_shape=argv_shape,
        )

    rc = int(completed.returncode)
    stderr_snippet = _safe_stderr(getattr(completed, "stderr", "") or "")
    return KeychainWriteResult(
        ok=(rc == 0),
        rc=rc,
        stderr_snippet=stderr_snippet,
        argv_shape=argv_shape,
    )


# --- Diff (status) --------------------------------------------------------


@dataclass(frozen=True)
class AllowlistDiff:
    """Structured drift between resolved YAML and Keychain slot.

    Attributes:
        resolved_ids: the ordered list `resolve_allowlist_ids` returned
            for the current YAML pair.
        keychain_ids: the set of ids parsed out of the Keychain slot
            (see `parse_keychain_allowlist`); may be empty when the
            slot is unset OR when its content is all junk tokens.
        missing_from_keychain: resolved ids the daemon does NOT see.
            The exact fix a `sync --apply` would restore.
        extra_in_keychain: ids the daemon DOES see but that no longer
            resolve from `access.yaml`. Usually a revoked human or a
            stale legacy slot.
        in_sync: True iff both sides are the same set AND the raw
            Keychain string is the canonical form for `resolved_ids`.
            (A slot with `"111, 222"` parses to the same set as
            `"111,222"` but is NOT canonical, so `in_sync` is False
            and `sync --apply` would rewrite it to the canonical form.)
        canonical_value: the exact string `sync --apply` would write.
    """

    resolved_ids: List[int]
    keychain_ids: List[int]
    missing_from_keychain: List[int]
    extra_in_keychain: List[int]
    in_sync: bool
    canonical_value: str


def parse_keychain_allowlist(raw: str) -> List[int]:
    """Parse the Keychain slot's raw string using the daemon's semantics.

    Mirrors `landline.runtime.guard._parse_int_set`:
      - split on commas.
      - strip whitespace around every token.
      - skip empty tokens (from `"111,,222"`) and non-integer tokens
        (silent skip; a fully-junk string parses to an empty list).
      - preserve declaration order, deduped.

    Returned as a list (not a set) so the `status` verb can render the
    daemon-side view in the same order the operator would eyeball in
    the Keychain slot. Callers that need set semantics build a `set(...)`
    from the return value.
    """
    if not isinstance(raw, str):
        raise AllowlistExportError(
            f"parse_keychain_allowlist: expected str, got {type(raw).__name__}."
        )
    seen: dict[int, None] = {}
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            # Silent skip — same as the daemon parser. A slot that
            # parses to zero valid ids will surface as "keychain
            # empty" in the diff, which is the fail-closed signal.
            continue
        if value in seen:
            continue
        seen[value] = None
    return list(seen.keys())


def diff_allowlist(
    resolved_ids: Sequence[int], keychain_read: KeychainReadResult
) -> AllowlistDiff:
    """Compute a structured drift between resolved YAML and Keychain.

    See `AllowlistDiff` for the shape. The `in_sync` flag is a stricter
    check than set equality: a Keychain slot with tolerated non-canonical
    formatting (extra whitespace, non-integer noise, wrong order) is NOT
    in sync even when its resolved integer set matches, because a re-run
    of `sync --apply` would rewrite it. That's the honest signal.
    """
    canonical = format_allowlist_value(resolved_ids)
    kc_ids = parse_keychain_allowlist(keychain_read.raw) if keychain_read.present else []
    resolved_set = set(int(i) for i in resolved_ids)
    kc_set = set(kc_ids)
    missing = [i for i in resolved_ids if i not in kc_set]
    extra = [i for i in kc_ids if i not in resolved_set]
    in_sync = (
        keychain_read.present
        and resolved_set == kc_set
        and keychain_read.raw == canonical
    )
    return AllowlistDiff(
        resolved_ids=list(resolved_ids),
        keychain_ids=kc_ids,
        missing_from_keychain=missing,
        extra_in_keychain=extra,
        in_sync=in_sync,
        canonical_value=canonical,
    )


# --- Internals ------------------------------------------------------------


def _default_runner(*args, **kwargs) -> subprocess.CompletedProcess:
    """Default subprocess runner (thin wrapper around `subprocess.run`).

    Extracted so tests that need to substitute a runner don't have to
    monkeypatch `subprocess.run` on the whole module — they pass their
    own callable via `runner=`. In production, this hits the real
    `security` binary.
    """
    return subprocess.run(*args, **kwargs)


def _safe_stderr(stderr: str) -> str:
    """Return a short, log-safe snippet of `security` stderr.

    Trims to a single line and clamps length so a giant stderr can't
    fill a log. Never contains the stored payload — `security -w`
    stderr talks about the item metadata, not the value.
    """
    if not stderr:
        return ""
    first = stderr.strip().splitlines()[0] if stderr.strip() else ""
    if len(first) > 200:
        first = first[:197] + "..."
    return first


__all__ = [
    "TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE",
    "DEFAULT_KEYCHAIN_ACCOUNT",
    "SECURITY_BINARY",
    "AllowlistDiff",
    "AllowlistExportError",
    "KeychainReadResult",
    "KeychainWriteResult",
    "diff_allowlist",
    "format_allowlist_value",
    "parse_keychain_allowlist",
    "read_keychain_allowlist",
    "resolve_allowlist_ids",
    "write_keychain_allowlist",
]
