"""`mineru slack` sub-app.

READ-ONLY OBSERVER (§2.3 of the capability spec + SECURITY.md's Slack
observer policy). The operator's connected Slack workspace
is observer-mode by policy: passive read-only, never a participant.
This sub-app EXPOSES NO write verb - no `send`, `post`, `react`,
`schedule`, `create-canvas`, `update-canvas`, nothing that mutates
workspace state. That's not a "not yet wired" gap; it is the intended
surface. Adding a write verb here requires an explicit spec-level
change (§0 decision + SECURITY.md carve-out for the workspace in
question) - do NOT smuggle one in as a "quick capability."

The `mineru_slack` skill in `~/.claude/skills/` also codifies this rule:
"NEVER send text messages to any channel or DM - not even 'test'
messages. NEVER post in threads." The CLI mirrors that policy at the
surface level so an agent (or an operator) cannot accidentally send
by typing a plausible-looking command name.

Enforcement stack (defense in depth):

  1. Surface: no send/post/write verb is defined in this file. Typer
     will reject `mineru slack send ...` at parse time with
     "No such command 'send'".
  2. Grep invariant: `tests/test_slack_verbs.py` reads the source of
     this file and asserts none of the tokens `slack_send`,
     `send_message`, `post`, or `write` appear as command names or
     function names. A regression that added a write verb would fail
     the test suite immediately.
  3. Backing tools: neither `$MINERU_HOME/bin/slack-read` nor
     `$MINERU_HOME/bin/slack-refresh-users` mutates Slack state; both are
     `curl`-only reads. There is no write binary to wrap.

Phase 2 status (P2-07):

  - `read` and `users refresh` route to their live shell scripts via
    `mineru_cli.wrappers.slack_read.run_slack_read` and
    `mineru_cli.wrappers.slack_refresh_users.run_slack_refresh_users`.
    Both are pure READ Slack API calls (conversations.history +
    users.list); safe to run live against the connected Slack
    workspace.
  - `thread`, `channels`, and `search public` are ALSO wired to live
    shell scripts (`slack-thread` -> conversations.replies,
    `slack-channels` -> conversations.list, `slack-search-public` ->
    search.messages). All three are strict Slack READ endpoints; the
    observer policy holds. Wired 2026-09-16 per
    `reports/2026-09-16-mineru-stub-verbs-triage.md` §C, the BUILD
    slice of the stub-triage.
  - `profile`, `file`, `search channels`, `search public-and-private`
    remain hidden stubs, discoverable via `--help` on each verb but
    not surfaced in the parent listing. They map to Slack READ
    endpoints (users.profile.get, files.info, search.messages
    scoped variants) that don't have a live-tool backing yet and
    are left for a later increment that adds sibling shell scripts +
    wrappers. Stubs keep the verb tree discoverable without a
    surprise write path. Two niche stubs (`canvas` — Slack Canvas
    isn't relevant to the church-observer role, and `search users` —
    user IDs are cheaper to grab from a channel listing) were dropped
    in the 2026-09-16 stub-drop pass.

Wire-up rules (mirror memory.py / gmail.py):

  - The wrappers are the ONLY subprocess call sites. Each wired verb
    picks the argv shape, folds root-level flags into an extras list,
    and hands the argv to the appropriate wrapper.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json slack read C0...`
    behaves the same as `mineru slack read C0... --json`. Idempotent:
    if the user also passed the flag as a trailing extra we don't
    duplicate it. slack-read itself is a jq pipeline that always
    emits JSON; propagating `--json` is a no-op today but keeps the
    per-verb behavior symmetric with the rest of the CLI so a future
    extension of the shell script that toggles behavior on `--json`
    (or `--pretty`) works with zero verb-file changes.
  - Exit code from the engine propagates unchanged via `typer.Exit`.

Verb -> engine map:

  Reads (wired, safe to run live):
    mineru slack read <channel> [--limit N]        -> slack-read <channel> [N]
    mineru slack users refresh                     -> slack-refresh-users
    mineru slack thread <channel> <ts>             -> slack-thread <channel> <ts>
    mineru slack channels [--include-private]      -> slack-channels [--include-private]
    mineru slack search public <query>             -> slack-search-public <query>

  Reads (discoverable stubs; no live backing yet):
    mineru slack profile <userId>                  -> not_yet_implemented
    mineru slack file <fileId> --out <path>        -> not_yet_implemented
    mineru slack search channels <query>           -> not_yet_implemented
    mineru slack search public-and-private <query> -> not_yet_implemented
"""

from __future__ import annotations

from typing import List, Optional

import typer

from mineru_cli._stub import not_yet_implemented
from mineru_cli.profile import get_profile
from mineru_cli.wrappers.slack_channels import run_slack_channels
from mineru_cli.wrappers.slack_read import run_slack_read
from mineru_cli.wrappers.slack_refresh_users import run_slack_refresh_users
from mineru_cli.wrappers.slack_search_public import run_slack_search_public
from mineru_cli.wrappers.slack_thread import run_slack_thread
from mineru_cli.verbs._helpers import propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru slack` app.
#
# The help text names the observer policy so `mineru slack --help` surfaces
# the read-only guarantee immediately (a defense-in-depth signal for anyone
# who arrives via tab-completion without reading this docstring).
# ---------------------------------------------------------------------------

slack_app = typer.Typer(
    name="slack",
    help=(
        "Slack (READ-ONLY observer per SECURITY.md policy): read a "
        "channel's history, list channels, read a thread, search public "
        "messages, refresh user cache. NO send/post/write verb exists; "
        "observer-mode is enforced at the CLI surface. A small residue "
        "of read verbs (profile / file / search channels / search "
        "public-and-private) remains as hidden stubs pending live-tool "
        "backing."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# Thin alias for the shared helper so the call sites keep their local name.
# The one canonical implementation lives in `mineru_cli.verbs._helpers` so
# a change to the global-flag set lands in one place, not twelve.
_propagate_global_flags = propagate_global_flags


# ---------------------------------------------------------------------------
# READ verbs (wired to live engines; safe to run live against the connected Slack workspace).
# ---------------------------------------------------------------------------


@slack_app.command(
    "read",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def read(
    ctx: typer.Context,
    channel: str = typer.Argument(
        ...,
        help=(
            "Slack channel id (e.g. `C02RAQRC10T`). Positional arg the shell "
            "script expects. Get channel ids from `mineru slack channels` once "
            "wired, or from Slack's UI (Channel Details -> ID)."
        ),
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-n",
        metavar="N",
        help=(
            "Max messages to fetch (positional second arg to slack-read; "
            "defaults to 10 if omitted)."
        ),
    ),
) -> None:
    """Read a channel's recent history (wraps `slack-read <channel> [limit]`).

    READ-ONLY per observer policy. Under the hood the shell script hits
    Slack's `conversations.history` endpoint and reshapes each message
    into `{ts, user, text, reactions, replies}` via jq using the local
    `$MINERU_HOME/cache/slack-users.json` cache to resolve raw ids to
    real names. Run `mineru slack users refresh` if a new member shows
    up as a raw id.

    Extras after `--limit` pass straight through in case the shell
    script grows options later; root-level `--pretty` / `--json` are
    also propagated (currently no-ops on the jq pipeline, but symmetric
    with the rest of the CLI).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    # slack-read's positional shape is `<channel_id> [limit]` where
    # limit is the SECOND positional. When --limit is set we emit it in
    # that position; otherwise let the shell script default (10).
    argv: List[str] = [channel]
    if limit is not None:
        argv.append(str(limit))
    argv.extend(extras)
    rc = run_slack_read(argv, ctx=ctx)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# `mineru slack users` sub-app: refresh (wraps slack-refresh-users).
# The verb sits under a `users` noun so a future `users list` (Slack Web API
# users.list read) lands next to it cleanly.
# ---------------------------------------------------------------------------

users_app = typer.Typer(
    name="users",
    help="Slack users: refresh local user cache (wraps `slack-refresh-users`).",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@users_app.command(
    "refresh",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def users_refresh(ctx: typer.Context) -> None:
    """Refresh local Slack user cache (wraps `slack-refresh-users`).

    READ-ONLY per observer policy. Calls Slack's `users.list` endpoint
    and rewrites `$MINERU_HOME/cache/slack-users.json` so `slack read`
    can resolve raw user-ids to `real_name` (or `name` fallback). Run
    this when a new church-member shows up in `slack read` as a raw
    id like `U08H3XXXXXX`.

    Extras pass through opaquely for forward-compatibility with any
    future flags the shell script grows.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_slack_refresh_users(list(extras), ctx=ctx)
    raise typer.Exit(code=rc)


slack_app.add_typer(users_app, name="users")


# ---------------------------------------------------------------------------
# Discoverable READ stubs - Slack Web API endpoints that don't have a live
# shell-script backing yet. Left as stubs (not silently dropped) so the verb
# tree stays discoverable via `--help` and a later increment can wire them by
# adding shell scripts + wrappers without changing the CLI surface.
#
# NONE of these are write verbs. The strict no-write invariant holds: every
# stub below maps to a Slack read endpoint (users.profile.get, files.info,
# search.messages scoped variants) if/when wired.
# ---------------------------------------------------------------------------


@slack_app.command(
    "thread",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def thread(
    ctx: typer.Context,
    channel: str = typer.Argument(
        ..., help="Slack channel id containing the thread (e.g. `C02RAQRC10T`)."
    ),
    ts: str = typer.Argument(
        ...,
        help=(
            "Parent message timestamp (thread ts). The `ts` field on the "
            "parent message in the channel history, e.g. `1723050000.123456`."
        ),
    ),
) -> None:
    """Read a thread's parent + replies (wraps `slack-thread <channel> <ts>`).

    READ-ONLY per observer policy. The shell script hits Slack's
    `conversations.replies` endpoint and reshapes each message into
    `{ts, user, text, reactions, thread_ts, parent_user_id}` via jq
    using the local `$MINERU_HOME/cache/slack-users.json` cache to
    resolve raw ids to real names. Run `mineru slack users refresh` if
    a new member shows up as a raw id.

    Extras pass through opaquely (e.g. a trailing `100` becomes the
    limit positional the shell script accepts). Root-level `--pretty`
    / `--json` are also propagated (no-op today; symmetric with the
    rest of the CLI for a future shell-script extension).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: List[str] = [channel, ts, *extras]
    rc = run_slack_thread(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@slack_app.command("profile", hidden=True)
def profile(
    user_id: str = typer.Argument(..., help="Slack user id (e.g. `U08H3ABCDEF`)."),
) -> None:
    """Read a user's profile (would map to `users.profile.get`)."""
    not_yet_implemented(f"slack profile {user_id}")


@slack_app.command(
    "channels",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def channels(
    ctx: typer.Context,
    include_private: bool = typer.Option(
        False,
        "--include-private",
        help=(
            "Include private channels the bot is a member of (Slack scopes "
            "private-channel visibility to membership). Default is public "
            "only, matching the church-observer role."
        ),
    ),
) -> None:
    """List channels the bot can see (wraps `slack-channels`).

    READ-ONLY per observer policy. Wraps Slack's `conversations.list`
    with `types=public_channel` by default, or
    `types=public_channel,private_channel` under `--include-private`.
    Each channel comes back as `{id, name, is_private, is_member,
    is_archived, num_members, topic, purpose}`. Use the returned `id`
    with `mineru slack read <id>` or `mineru slack thread <id> <ts>`.

    Extras and root-level `--pretty` / `--json` pass through the same
    way as the other Slack verbs.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: List[str] = []
    if include_private:
        argv.append("--include-private")
    argv.extend(extras)
    rc = run_slack_channels(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@slack_app.command("file", hidden=True)
def file(
    file_id: str = typer.Argument(..., help="Slack file id."),
    out: str = typer.Option(
        ...,
        "--out",
        metavar="PATH",
        help="Local path to write the downloaded file to.",
    ),
) -> None:
    """Download a Slack file to a local path (would map to `files.info` + url_private_download)."""
    not_yet_implemented(f"slack file {file_id} --out {out}")


# ---------------------------------------------------------------------------
# `mineru slack search` sub-app: search {channels|public|public-and-private}
# All are READ stubs. Distinct sub-verbs (rather than a single verb with an
# `--op` flag) so `--help` renders each search scope with its own docstring.
# ---------------------------------------------------------------------------

search_app = typer.Typer(
    name="search",
    help=(
        "Search Slack (READ-ONLY): channels / public / public-and-private. "
        "All scopes map to Slack's read-only search endpoints."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@search_app.command("channels", hidden=True)
def search_channels(
    query: str = typer.Argument(..., help="Search query."),
) -> None:
    """Search channels the bot can see (would map to `search.messages` scoped to channels)."""
    not_yet_implemented(f"slack search channels {query!r}")


@search_app.command(
    "public",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def search_public(
    ctx: typer.Context,
    query: str = typer.Argument(
        ...,
        help=(
            "Slack search query. Supports Slack's search operators "
            "(`from:@name`, `in:#channel`, `after:YYYY-MM-DD`, ...)."
        ),
    ),
) -> None:
    """Search public Slack messages (wraps `slack-search-public <query>`).

    READ-ONLY per observer policy. Wraps Slack's `search.messages`
    endpoint scoped to public channels and reshapes each match into
    `{ts, user, channel_id, channel_name, text, permalink}`.

    NOTE: `search.messages` requires a USER token (xoxp-), not a bot
    token (xoxb-). If the Keychain entry is a bot token, the response
    surfaces `ok: false, error: "not_allowed_token_type"` verbatim so
    the operator can see exactly what to fix. The wrapper does not
    diagnose token type on its own.

    Extras (`--count N`, `--sort timestamp|score`) and root-level
    `--pretty` / `--json` pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: List[str] = [query, *extras]
    rc = run_slack_search_public(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@search_app.command("public-and-private", hidden=True)
def search_public_and_private(
    query: str = typer.Argument(..., help="Search query."),
) -> None:
    """Search both public and (bot-visible) private channels (would map to `search.messages`)."""
    not_yet_implemented(f"slack search public-and-private {query!r}")


slack_app.add_typer(search_app, name="search")
