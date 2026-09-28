"""`mineru imessage` sub-app.

Phase 2 status (P2-06): every user-facing iMessage verb the task lists is
wired end-to-end. Reads (chats / history / group / search / watch /
whois / nickname / status) route through the injection firewall at
`$MINERU_HOME/bin/imsg-firewall` via `mineru_cli.wrappers.imsg_firewall`.
Writes (send / react / edit / unsend / delete / mark-read / typing /
notify / chat lifecycle / launch / rpc) route through the raw imsg
binary at `/opt/homebrew/bin/imsg` via `mineru_cli.wrappers.imsg`.

Full spelled-out name `imessage` (never `imsg`) per §0 of the capability
spec - `imsg` is the workspace CLI name only.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  Every iMessage READ verb in this file routes through
  `$MINERU_HOME/bin/imsg-firewall` (or the path set on
  `MINERU_IMSG_FIREWALL_BIN`). The raw `imsg` binary is NEVER called
  by any read verb. The `imsg-named` contact-resolving wrapper is
  EXPLICITLY FORBIDDEN for reads per TOOLS.md - the symlink is kept
  only for the operator's occasional manual override; agents must never
  invoke it. External message content (bodies, sender info, chat
  names) is always screened for prompt-injection before it reaches
  the LLM context.

  Every iMessage WRITE verb routes through `/opt/homebrew/bin/imsg`
  (or the path set on `MINERU_IMSG_BIN`). Outbound has no injection
  risk per SECURITY.md ("Never Trust External Content" applies to
  inbound; outbound is bytes the LLM already produced with the operator's
  approval flowing OUT to Messages).

  The single argv[0] source of truth for READS is
  `mineru_cli.wrappers.imsg_firewall.build_imsg_firewall_argv`, and
  for WRITES is `mineru_cli.wrappers.imsg.build_imsg_argv`. Tests
  assert:
    * READ argv[0] basename == `imsg-firewall` (never bare `imsg`,
      never `imsg-named`).
    * WRITE argv[0] basename == `imsg` (never `imsg-firewall`).
    * The verb file itself contains no `imsg-named` string literal
      (grep-invariant against the forbidden read path).

  Firewall exit codes on reads propagate unchanged: 0 delivered, 77
  all units blocked, 78 firewall itself errored. Stderr redaction
  notices (`redacted N of M units`) flow through untouched.

Wire-up rules (mirror the P2-01 Gmail file):

  - The wrappers are the ONLY subprocess call sites. Each verb picks
    the sub-verb, translates the small handful of mineru->engine
    argument shapes (e.g. `history <chat_id>` -> `--chat-id <chat_id>`,
    `delete` -> `delete-message`), folds root-level flags into the
    extras list, and hands the argv to the appropriate wrapper.
  - Extras (everything Typer didn't consume) pass straight through to
    the engine, so flags like `--limit N` / `--json` / `--attachments`
    / `--participants` / `--service` all work with zero wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json imessage chats`
    behaves the same as `mineru imessage chats --json`. Idempotent:
    if the user also passed the flag as a trailing extra we don't
    duplicate it. `imsg` uses `--json` (with aliases `-j`,
    `--json-output`, `--jsonOutput`) - we propagate the canonical
    `--json` form; power users can pass any alias explicitly.
  - Exit code from the engine propagates unchanged via `typer.Exit`.

mineru -> engine argument-shape translations (kept small and local,
same style as `contacts create` splitting `--name` into `--given`
`--family` for gog):

  - `history <chat_id>` -> `history --chat-id <chat_id>`
  - `group <chat_id>` -> `group --chat-id <chat_id>`
  - `search <query>` -> `search --query <query>`
  - `delete` -> `delete-message`  (the imsg subverb name for
    deleting a single message; `chat delete` is separate and maps to
    `chat-delete` for full-chat removal)
  - `mark-read` -> `read`  (imsg calls the "mark as read" verb `read`;
    we surface the clearer name to avoid confusion with `history`)
  - `notify` -> `notify-anyways`
  - `chat create` -> `chat-create`
  - `chat rename` -> `chat-name`
  - `chat photo` -> `chat-photo`
  - `chat add` -> `chat-add-member`
  - `chat remove` -> `chat-remove-member`
  - `chat leave` -> `chat-leave`
  - `chat delete` -> `chat-delete`

Verb -> engine subverb map:

  Reads (safe to run live; all pass through the injection firewall):
    mineru imessage chats [--limit N]         -> imsg-firewall chats [--limit N]
    mineru imessage history <chat_id> [...]   -> imsg-firewall history --chat-id <chat_id> [...]
    mineru imessage group <chat_id>           -> imsg-firewall group --chat-id <chat_id>
    mineru imessage search <query>            -> imsg-firewall search --query <query>
    mineru imessage watch                     -> imsg-firewall watch (streams)
    mineru imessage whois --address <handle>  -> imsg-firewall whois --address <handle>
    mineru imessage nickname --address <h>    -> imsg-firewall nickname --address <h>
    mineru imessage status                    -> imsg-firewall status

  Writes (MOCKED in tests; NEVER executed live during dev/test):
    mineru imessage send --to X --text Y ...  -> imsg send --to X --text Y ...
    mineru imessage react ...                 -> imsg react ...
    mineru imessage edit ...                  -> imsg edit ...
    mineru imessage unsend ...                -> imsg unsend ...
    mineru imessage delete ...                -> imsg delete-message ...
    mineru imessage mark-read ...             -> imsg read ...
    mineru imessage typing ...                -> imsg typing ...
    mineru imessage notify ...                -> imsg notify-anyways ...
    mineru imessage chat create ...           -> imsg chat-create ...
    mineru imessage chat rename ...           -> imsg chat-name ...
    mineru imessage chat photo ...            -> imsg chat-photo ...
    mineru imessage chat add ...              -> imsg chat-add-member ...
    mineru imessage chat remove ...           -> imsg chat-remove-member ...
    mineru imessage chat leave ...            -> imsg chat-leave ...
    mineru imessage chat delete ...           -> imsg chat-delete ...
    mineru imessage launch ...                -> imsg launch ... (starts IMCore bridge)
    mineru imessage rpc                       -> imsg rpc (interactive JSON-RPC loop)
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import typer

from mineru_cli.profile import Profile, get_profile
from mineru_cli.wrappers.imsg import run_imsg
from mineru_cli.wrappers.imsg_firewall import run_imsg_firewall


# ---------------------------------------------------------------------------
# Per-profile iMessage enable gate (C1 in the multi-tenant runtime-isolation
# spec).
#
# WHY: iMessage reads the ONE shared `~/Library/Messages/chat.db` — macOS
# ties iMessage to a single Apple ID per user account, and every process
# on this macOS user sees the same message store. A second Mineru profile
# on the same Mac invoking `mineru imessage chats` would silently read the
# primary profile's message history — a real privacy leak. There is no
# per-profile Messages database to route to; the isolation MUST be a
# CLI-level gate.
#
# Default `Profile.imessage_enabled = True` keeps the seed / owner profile
# working exactly as before. `mineru profile init` sets it to `False` for
# newly-created NON-owner profiles so a fresh non-owner install can't
# accidentally reach into the owner's messages. An operator who genuinely
# owns the Apple ID on this Mac and wants the 2nd profile to read too can
# flip the field back to `true` in that profile's profile.yaml.
# ---------------------------------------------------------------------------


def _require_imessage_profile(ctx: typer.Context) -> Profile:
    """Hydrate the active profile and enforce the per-profile iMessage gate.

    Every `mineru imessage <verb>` (read AND write) calls this at its
    point of use so a bogus `--profile` fails loud, and an
    `imessage_enabled: false` profile fails loud BEFORE any subprocess
    spawn. The message names the field, the profile, and the exact fix
    so the operator can either flip the field on or intentionally run
    the verb under the owning profile.
    """
    profile = get_profile(ctx)
    if not profile.imessage_enabled:
        raise typer.BadParameter(
            f"profile {profile.name!r} has iMessage disabled "
            "(`imessage_enabled: false`). iMessage reads the shared "
            "macOS chat.db for this user account, so leaving it enabled "
            "on a NON-owner profile would leak the owner's message "
            "history. To use iMessage under this profile, either run the "
            "verb under the owning profile (`--profile <owner>`) or set "
            f"`imessage_enabled: true` in {profile.profile_yaml_path}.",
            param_hint="--profile",
        )
    return profile

# ---------------------------------------------------------------------------
# imsg accepts multiple aliases for --json and --pretty (documented in the
# module docstring above). The shared `propagate_global_flags` helper only
# checks for the literal `--json` / `--pretty` — if an operator passed
# `--jsonOutput` (or `-j`) as a trailing extra AND the root callback set
# `ctx.obj["json"] = True`, the shared helper would append a duplicate
# `--json` and imsg would end up with two conflicting json-shape flags on
# the wire. We keep the shared helper strict-literal for callers where no
# alias exists, and route imessage through a local alias-aware wrapper.
# ---------------------------------------------------------------------------

_IMSG_JSON_ALIASES = ("--json", "-j", "--json-output", "--jsonOutput")
_IMSG_PRETTY_ALIASES = ("--pretty",)

# ---------------------------------------------------------------------------
# Top-level `mineru imessage` app.
# ---------------------------------------------------------------------------

imessage_app = typer.Typer(
    name="imessage",
    help=(
        "iMessage: firewall-preserving reads via imsg-firewall; direct outbound "
        "sends via imsg. Every read passes through the injection firewall; the "
        "raw imsg binary and the FORBIDDEN imsg-named path are never used for reads."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _propagate_global_flags(
    ctx: typer.Context, extras: Sequence[str]
) -> List[str]:
    """Alias-aware forwarder of `--json` / `--pretty` for the imsg wrappers.

    Behaves like `mineru_cli.verbs._helpers.propagate_global_flags` but
    treats every documented imsg alias (`-j`, `--json-output`,
    `--jsonOutput`) as "the json flag is already present, do not
    append a duplicate". Same for `--pretty` aliases (currently just
    the literal — kept as a set so a new alias landing later needs a
    one-line change here).

    Symmetric-diff-safe with the shared helper for the common case
    where no alias appears: the extras come back with `--json` or
    `--pretty` appended just like the shared helper would produce.
    """
    opts = ctx.obj or {}
    forwarded = list(extras)
    if opts.get("pretty") and not any(a in forwarded for a in _IMSG_PRETTY_ALIASES):
        forwarded.append("--pretty")
    if opts.get("json") and not any(a in forwarded for a in _IMSG_JSON_ALIASES):
        forwarded.append("--json")
    return forwarded


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@imessage_app.command(
    "chats",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chats(ctx: typer.Context) -> None:
    """List recent chats (wraps `imsg-firewall chats`).

    Firewall-preserving. Extras pass through, so engine flags like
    `--limit N` / `--json` / `--db <path>` work unchanged. Both
    `--limit` and `--max` are accepted (imsg-firewall translates
    `--max` -> `--limit` on the `chats` subcommand for gog parity).
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg_firewall(["chats", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "history",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def history(
    ctx: typer.Context,
    chat_id: str = typer.Argument(
        ..., help="Chat rowid (from `imessage chats`). Emitted as `--chat-id`."
    ),
) -> None:
    """Show recent messages in a chat (wraps `imsg-firewall history --chat-id`).

    Firewall-preserving. Extras pass through, so engine flags like
    `--limit N` / `--attachments` / `--convert-attachments` /
    `--participants` / `--start` / `--end` / `--json` all work.
    Both `--limit` and `--max` are accepted (imsg-firewall
    translates `--max` -> `--limit` on the `history` subcommand for
    gog parity).
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg_firewall(["history", "--chat-id", chat_id, *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "group",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def group(
    ctx: typer.Context,
    chat_id: str = typer.Argument(
        ..., help="Chat rowid (from `imessage chats`). Emitted as `--chat-id`."
    ),
) -> None:
    """Show chat identity and participants (wraps `imsg-firewall group --chat-id`).

    Firewall-preserving. Works for direct chats too; prints chat
    identifier, guid, display name, service, group flag, and
    participants. Extras pass through so `--json` / `--db` work.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg_firewall(["group", "--chat-id", chat_id, *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "search",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def search(
    ctx: typer.Context,
    query: str = typer.Argument(
        ..., help="Search query. Emitted as `--query`."
    ),
) -> None:
    """Search local Messages history (wraps `imsg-firewall search --query`).

    Firewall-preserving. Searches the local chat.db, not the injected
    bridge. Extras pass through so `--match exact|contains` /
    `--limit N` / `--json` all work unchanged. Default match is
    `contains`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg_firewall(["search", "--query", query, *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "watch",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def watch(ctx: typer.Context) -> None:
    """Stream incoming messages (wraps `imsg-firewall watch`).

    Firewall-preserving READ - each streamed message unit is screened
    for prompt-injection before it reaches the LLM caller. Long-lived
    subprocess; extras pass through so `--chat-id N` / `--debounce
    250ms` / `--since-rowid N` / `--participants +1...` / `--start
    ISO` / `--end ISO` / `--attachments` / `--convert-attachments` /
    `--reactions` / `--bb-events` / `--json` all work unchanged.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg_firewall(["watch", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "whois",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def whois(
    ctx: typer.Context,
    address: Optional[str] = typer.Option(
        None,
        "--address",
        metavar="HANDLE",
        help=(
            "Phone or email to check. Passed through as `--address`. "
            "Omit to let the engine surface its own missing-argument error."
        ),
    ),
) -> None:
    """Check whether a handle is reachable on iMessage (wraps `imsg-firewall whois`).

    Firewall-preserving READ. Extras pass through so `--type phone` /
    `--type email` / `--local` / `--json` all work unchanged.
    `--local` infers service from local chat.db without SIP / bridge.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: List[str] = ["whois"]
    if address is not None:
        argv.extend(["--address", address])
    argv.extend(extras)
    rc = run_imsg_firewall(argv)
    raise typer.Exit(code=rc)


@imessage_app.command(
    "nickname",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def nickname(
    ctx: typer.Context,
    address: Optional[str] = typer.Option(
        None,
        "--address",
        metavar="HANDLE",
        help="Phone or email to look up. Passed through as `--address`.",
    ),
) -> None:
    """Show contact-card / nickname info for a handle (wraps `imsg-firewall nickname`).

    Firewall-preserving READ. Default mode reads the contact-card
    nickname the correspondent shared over iMessage via the IMCore
    bridge (requires `imessage launch`, SIP disabled). `--local`
    returns YOUR local AddressBook contact name for the handle -
    different datum, no SIP required. Extras pass through so
    `--local` / `--json` all work unchanged.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: List[str] = ["nickname"]
    if address is not None:
        argv.extend(["--address", address])
    argv.extend(extras)
    rc = run_imsg_firewall(argv)
    raise typer.Exit(code=rc)


@imessage_app.command(
    "status",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def status(ctx: typer.Context) -> None:
    """Check availability of imsg advanced features (wraps `imsg-firewall status`).

    Firewall-preserving READ. Reports whether the IMCore bridge,
    dylib injection, and other advanced features are available and
    provides setup instructions if needed. Extras pass through so
    `--json` works.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg_firewall(["status", *extras])
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# WRITE verbs (must never be executed live during dev/test - the tests patch
# run_imsg and assert argv only). the operator invokes them explicitly.
# ---------------------------------------------------------------------------


@imessage_app.command(
    "send",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def send(
    ctx: typer.Context,
    to: Optional[str] = typer.Option(
        None,
        "--to",
        metavar="HANDLE",
        help=(
            "Recipient handle (phone number or email). Passed through as `--to`. "
            "Omit if using `--chat-id`/`--chat-identifier`/`--chat-guid` in extras."
        ),
    ),
    text: Optional[str] = typer.Option(
        None,
        "--text",
        metavar="MESSAGE",
        help="Message body. Passed through as `--text`.",
    ),
    service: Optional[str] = typer.Option(
        None,
        "--service",
        metavar="imessage|sms|auto",
        help="Service to use. Passed through as `--service`.",
    ),
) -> None:
    """Send a text (WRITE, OUTBOUND) - wraps `imsg send`.

    ⚠️ SAFETY: When testing sends, ALWAYS self-text (the operator's
    own phone number). NEVER send to a partner, family member, or
    friend — an unexpected AI text can alarm the recipient. When in
    doubt, ask which number. The rule is enforced socially, not by
    the CLI: this verb accepts any recipient - it's on the operator
    (or the drafting agent) to keep test sends self-directed.

    Default outbound pattern: draft the message, show the operator,
    wait for their "go ahead" (see AGENTS.md "External Comms"), THEN
    invoke this verb. Never compose and send in one step without
    confirmation.

    Every flag passes through opaquely - `--to`, `--text`, `--file`
    (attachment path), `--service imessage|sms|auto`, `--region`,
    `--chat-id`, `--chat-identifier`, `--chat-guid`,
    `--no-sms-fallback`, `--json`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: List[str] = ["send"]
    if to is not None:
        argv.extend(["--to", to])
    if text is not None:
        argv.extend(["--text", text])
    if service is not None:
        argv.extend(["--service", service])
    argv.extend(extras)
    rc = run_imsg(argv)
    raise typer.Exit(code=rc)


@imessage_app.command(
    "react",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def react(ctx: typer.Context) -> None:
    """Send a tapback reaction to the most recent message (WRITE) - wraps `imsg react`.

    Every flag passes through opaquely - `--chat-id N`, `--reaction
    love|like|dislike|laugh|emphasis|question` (or `-r`), `--db`,
    `--json`. Reacts to the MOST RECENT incoming message; requires
    Messages.app running (uses UI automation).
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["react", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "edit",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def edit(ctx: typer.Context) -> None:
    """Edit a sent message (WRITE, macOS 13+) - wraps `imsg edit`.

    Every flag passes through opaquely - `--chat <guid>`, `--message
    <guid>`, `--new-text <s>`, `--bc-text <s>`, `--part N`, `--json`.
    Selector-probed at startup; requires macOS 13+.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["edit", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "unsend",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def unsend(ctx: typer.Context) -> None:
    """Retract a sent message (WRITE, macOS 13+) - wraps `imsg unsend`.

    Every flag passes through opaquely - `--chat <guid>`, `--message
    <guid>`, `--part N`, `--json`. Selector-probed at startup;
    requires macOS 13+.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["unsend", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def delete(ctx: typer.Context) -> None:
    """Delete a single message from a chat locally (WRITE, DESTRUCTIVE) - wraps `imsg delete-message`.

    Deletes the message from Messages.app on this Mac; NEVER
    executed live during dev/test. Confirm every delete with the operator
    before invoking (per AGENTS.md External Comms). Every flag
    passes through opaquely - `--chat <guid>`, `--message <guid>`,
    `--json`.

    Note: this is the per-message delete. For chat-level removal
    (deleting an entire conversation), use `mineru imessage chat
    delete`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["delete-message", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "mark-read",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def mark_read(ctx: typer.Context) -> None:
    """Mark messages as read for a chat (WRITE) - wraps `imsg read`.

    We surface the clearer name `mark-read` here because `imsg`'s
    subverb is literally `read`, which reads ambiguously against
    `history`. Every flag passes through opaquely - `--to <handle>` /
    `--handle <handle>`, `--chat-id N`, `--chat-identifier <s>`,
    `--chat-guid <s>`, `--json`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["read", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "typing",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def typing(ctx: typer.Context) -> None:
    """Send a typing indicator to a chat (WRITE) - wraps `imsg typing`.

    Every flag passes through opaquely - `--to <handle>`, `--chat-id
    N`, `--chat-identifier <s>`, `--chat-guid <s>`, `--duration
    5s|3000ms`, `--stop true`, `--service imessage|sms|auto`,
    `--json`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["typing", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "notify",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def notify(ctx: typer.Context) -> None:
    """Force a notification for a filtered / suppressed message (WRITE) - wraps `imsg notify-anyways`.

    Every flag passes through opaquely - `--chat <guid>`, `--message
    <guid>`, `--json`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["notify-anyways", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "launch",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def launch(ctx: typer.Context) -> None:
    """Launch the IMCore bridge (WRITE, dylib injection) - wraps `imsg launch`.

    Starts Messages.app with the imsg-bridge-helper.dylib injected so
    the SIP-disabled advanced features (rich sends, chat creation,
    nickname bridge) work. Every flag passes through opaquely -
    `--dylib <path>`, `--kill-only`, `--json`. Requires SIP disabled;
    NEVER executed live during dev/test.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["launch", *extras])
    raise typer.Exit(code=rc)


@imessage_app.command(
    "rpc",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def rpc(ctx: typer.Context) -> None:
    """Run JSON-RPC over stdin/stdout against the IMCore bridge (WRITE, interactive) - wraps `imsg rpc`.

    Long-lived interactive session; extras pass through so `--db
    <path>` works. Requires the bridge to be launched (`mineru
    imessage launch`) first. NEVER executed live during dev/test -
    even the argv assertion tests patch run_imsg.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["rpc", *extras])
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# `mineru imessage chat` sub-app: create / rename / photo / add / remove /
# leave / delete. All WRITE verbs; all mock-only during dev/test.
# ---------------------------------------------------------------------------

chat_app = typer.Typer(
    name="chat",
    help=(
        "Chat lifecycle (WRITE): create / rename / photo / add / remove / "
        "leave / delete. Routes to imsg's chat-* subverbs; NEVER executed live "
        "during dev/test."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@chat_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chat_create(ctx: typer.Context) -> None:
    """Create a new chat, 1:1 or group (WRITE) - wraps `imsg chat-create`.

    Requires `mineru imessage launch` first (SIP-disabled, dylib
    injected). Every flag passes through opaquely - `--addresses
    "+1...,+1..."`, `--name "Crew"`, `--text "gm"` (initial message),
    `--service imessage`, `--json`. Chat creation is iMessage-only;
    for SMS sends use `mineru imessage send --service sms`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["chat-create", *extras])
    raise typer.Exit(code=rc)


@chat_app.command(
    "rename",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chat_rename(ctx: typer.Context) -> None:
    """Set a chat's display name (WRITE) - wraps `imsg chat-name`.

    Every flag passes through opaquely - `--chat <guid>`, `--name
    "New Name"`, `--json`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["chat-name", *extras])
    raise typer.Exit(code=rc)


@chat_app.command(
    "photo",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chat_photo(ctx: typer.Context) -> None:
    """Set or clear a group chat photo (WRITE) - wraps `imsg chat-photo`.

    Omit `--file` to clear the existing photo. Every flag passes
    through opaquely - `--chat <guid>`, `--file <path>`, `--json`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["chat-photo", *extras])
    raise typer.Exit(code=rc)


@chat_app.command(
    "add",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chat_add(ctx: typer.Context) -> None:
    """Add a participant to a group chat (WRITE) - wraps `imsg chat-add-member`.

    Every flag passes through opaquely - `--chat <guid>`, `--address
    <phone|email>`, `--json`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["chat-add-member", *extras])
    raise typer.Exit(code=rc)


@chat_app.command(
    "remove",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chat_remove(ctx: typer.Context) -> None:
    """Remove a participant from a group chat (WRITE) - wraps `imsg chat-remove-member`.

    Every flag passes through opaquely - `--chat <guid>`, `--address
    <phone|email>`, `--json`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["chat-remove-member", *extras])
    raise typer.Exit(code=rc)


@chat_app.command(
    "leave",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chat_leave(ctx: typer.Context) -> None:
    """Leave a group chat (WRITE) - wraps `imsg chat-leave`.

    Every flag passes through opaquely - `--chat <guid>`, `--json`.
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["chat-leave", *extras])
    raise typer.Exit(code=rc)


@chat_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chat_delete(ctx: typer.Context) -> None:
    """Delete a chat from Messages.app (WRITE, DESTRUCTIVE) - wraps `imsg chat-delete`.

    Removes the entire conversation from Messages.app on this Mac.
    Confirm every delete with the operator before invoking (per AGENTS.md
    External Comms). Every flag passes through opaquely - `--chat
    <guid>`, `--json`.

    Note: this is the whole-chat delete. For per-message removal
    use `mineru imessage delete` (which routes to
    `imsg delete-message`).
    """
    _require_imessage_profile(ctx)  # hydrate + fail loud on bogus profile OR disabled iMessage
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_imsg(["chat-delete", *extras])
    raise typer.Exit(code=rc)


imessage_app.add_typer(chat_app, name="chat")
