"""`mineru calendar` sub-app.

Phase 2 status (P2-02): the eight core Google Calendar verbs the task requires
are wired end-to-end through the live gog-firewall engine at
`$MINERU_HOME/bin/gog-firewall` via `mineru_cli.wrappers.gog_firewall`. Reads
(list / get / search / calendars) route through the injection firewall on
the same code path as writes (create / update / delete / respond), keeping
firewall-preservation uniform across the surface. Two additional sub-verbs
(freebusy / conflicts) stay as discoverable stubs; they'll be wired in a
later increment. The niche Google Calendar admin surface (Workspace ACL,
color reference, server time, users/team, focus-time / OOO /
working-location event kinds, propose-time URLs, and the redundant `suggest`
already covered by MCP `suggest_time`) was deliberately dropped from the
verb tree in the 2026-09-16 stub-drop pass — see
`reports/2026-09-16-mineru-stub-verbs-triage.md` §C.

MCP-vs-CLI CONTEXT (LOAD-BEARING NOTE, do not remove):

  Per `prompts/GCALENDAR.md`, the AGENT-preferred path for Google Calendar
  is the claude.ai MCP Calendar connector (`mcp__claude_ai_Google_Calendar__*`).
  MCP has full read AND write scopes and is what an agent should reach for
  during interactive sessions. The `gog-firewall calendar` CLI is the
  documented fallback used only when the MCP connector's OAuth token has
  lapsed and returns an auth error.

  This Python CLI CANNOT invoke MCP tools — MCP is an agent-runtime
  protocol, not a shell binary. So the mineru CLI uses the `gog-firewall
  calendar` fallback for every verb. Future maintainers: if you're
  wondering why we don't call MCP here, this is the reason. The MCP
  reads-are-firewall-exempt tradeoff (§3.1 of the capability spec) does
  NOT apply to this file — every CLI verb routes through gog-firewall,
  which screens external event content for prompt-injection like every
  other read path in the CLI.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  Every calendar verb in this codebase routes through
  `$MINERU_HOME/bin/gog-firewall` (or the path set on
  `MINERU_GOG_FIREWALL_BIN`). The raw `gog` binary at
  `/opt/homebrew/bin/gog` is NEVER called by any verb here. External
  event content (summaries, descriptions, attendee-supplied fields) is
  always screened for prompt-injection before it reaches the LLM context
  on read paths, and write paths route through the same firewalled tool
  so no verb ever escapes to the unwrapped binary.

  The single argv[0] source of truth is
  `mineru_cli.wrappers.gog_firewall.build_gog_firewall_argv`, which the
  existing `test_argv0_resolves_to_gog_firewall_default` invariant test
  pins to `gog-firewall`. A regression that swapped the binary for bare
  `gog` would fail that test AND the new calendar routing tests
  immediately.

  Firewall exit codes propagate unchanged: 0 delivered, 77 all units
  blocked (prompt-injection detected everywhere), 78 firewall itself
  errored. Stderr redaction notices (`redacted N of M units`) flow
  through untouched — this file never captures, filters, or reformats
  them.

Wire-up rules (mirror the P2-01 Gmail file exactly):

  - `run_gog_firewall` is the ONLY subprocess call site. Each verb picks
    the sub-verb, resolves the `--calendar` alias, folds root-level
    flags into the extras list, and hands the argv to the wrapper.
  - Extras (everything Typer didn't consume) pass straight through to
    gog-firewall, so engine flags like `--json` / `--from ISO` /
    `--to ISO` / `--max N` / `--all` / `--send-updates none|externalOnly|all`
    / `--attendees` / `--summary` / `--location` / `--no-input` all work
    with zero wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json calendar list` behaves
    the same as `mineru calendar list --json`. Idempotent: if the user
    also passed the flag as a trailing extra we don't duplicate it.
  - Exit code from gog-firewall propagates unchanged via `typer.Exit`.

Calendar-alias resolution (`--calendar <alias>`):

  A mineru-CLI convenience layer on top of the raw gog-firewall
  positional. If the active profile carries
  `extras.connectors.google.calendars` (a `{alias: full_id}` dict), the
  value passed to `--calendar` is looked up there first; unmatched
  values pass through as-is, so raw Google calendar IDs, `primary`, and
  email-style shared-calendar IDs all still work with no config.
  Aliases like `personal` / `church` / `family` are wired per install
  into `profiles/<name>/profile.yaml` under `extras.connectors.google.calendars`.

  When `--calendar` is omitted:
    - `list` / `search` / `calendars`: no calendarId positional emitted;
      gog-firewall applies its own default (primary for list; search
      and calendars don't take a calendarId at all).
    - `get` / `create` / `update` / `delete` / `respond`: gog-firewall
      REQUIRES the calendarId positional, so we default to `primary`.
      GCALENDAR.md warns this is usually the wrong calendar for personal
      events — the docstring on each verb calls that out and the operator
      should pass `--calendar <alias>` explicitly.

Verb → gog-firewall subverb map (kept close to what an operator would
type by hand against gog-firewall directly):

  Reads (safe to run live; all pass through the injection firewall):
    mineru calendar list [--calendar A]     → gog-firewall calendar events [<calId>] ...
    mineru calendar get <eventId>           → gog-firewall calendar event <calId> <eventId>
    mineru calendar search <query>          → gog-firewall calendar search <query>
    mineru calendar calendars               → gog-firewall calendar calendars

  Writes (never executed live during dev/test; the tests patch
  run_gog_firewall and assert argv only):
    mineru calendar create ...              → gog-firewall calendar create <calId> ...
    mineru calendar update <eventId> ...    → gog-firewall calendar update <calId> <eventId> ...
    mineru calendar delete <eventId>        → gog-firewall calendar delete <calId> <eventId>
    mineru calendar respond <eventId> ...   → gog-firewall calendar respond <calId> <eventId> ...
"""

import typer

from mineru_cli._stub import not_yet_implemented
from mineru_cli.profile import get_profile
from mineru_cli.wrappers.gog_firewall import run_gog_firewall
from mineru_cli.verbs._helpers import propagate_global_flags as _propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru calendar` app.
# ---------------------------------------------------------------------------

calendar_app = typer.Typer(
    name="calendar",
    help=(
        "Google Calendar: firewall-preserving reads and writes via the "
        "gog-firewall CLI fallback. MCP is the agent-preferred path per "
        "prompts/GCALENDAR.md, but a Python CLI cannot invoke MCP tools, "
        "so mineru uses the gog-firewall calendar fallback for every verb."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

# Default calendarId when the operator omits `--calendar` on a verb whose
# gog-firewall positional is REQUIRED (get / create / update / delete /
# respond). Matches gog-firewall's own default so behavior is uniform.
# GCALENDAR.md warns this is the wrong choice for personal events — the
# per-verb docstrings tell the user to pass --calendar explicitly.
DEFAULT_CALENDAR_ID = "primary"


# Google Calendar reserved shorthand IDs. These are documented Google
# short-form calendar identifiers that route without an alias mapping;
# anything else that looks like a bare word (no `@`, no `.`) must be
# declared in the profile's `connectors.google.calendars` dict, otherwise
# we fail loud at the CLI boundary instead of leaking an opaque "Invalid
# calendar ID" error up from gog three layers down.
RESERVED_CALENDAR_SHORTHANDS = frozenset({"primary"})


def _looks_like_raw_calendar_id(value: str) -> bool:
    """True when a `--calendar` value already looks like a real calendar identifier.

    Real identifiers are: full Google Calendar IDs
    (`...@group.calendar.google.com`), user email addresses (`x@y.z`), and
    the documented reserved shorthand `primary`. Everything else looks
    like an alias the profile is supposed to expand.
    """
    if value in RESERVED_CALENDAR_SHORTHANDS:
        return True
    return "@" in value or "." in value


def _resolve_calendar_id(ctx: typer.Context, alias_or_id: str | None) -> str | None:
    """Translate a `--calendar <alias>` value to its full calendar ID.

    Aliases live on the active profile under
    `extras.connectors.google.calendars` as a `{alias: full_id}` dict.
    Matched aliases expand to their configured value. Raw Google calendar
    IDs (`...@group.calendar.google.com`), user emails, and the reserved
    shorthand `primary` pass through unchanged.

    A bare-word value that is NOT reserved and NOT in the profile's alias
    dict is a user error: gog-firewall would reject it 3-4 layers down
    with an opaque "Invalid calendar ID" error. We surface a clean
    `typer.BadParameter` at the CLI boundary instead, telling the user
    exactly where to configure the alias.

    Backwards-compat carve-out: when `ctx.obj` is missing entirely (very
    early failure paths or unit tests that stub the ctx), we pass the
    value through unchanged so callers that don't hydrate a profile still
    work.

    `None` in, `None` out: the verb decides whether to substitute the
    module default (`primary`) for the required-positional case.
    """
    if alias_or_id is None:
        return None
    if not ctx.obj:
        return alias_or_id
    profile_obj = ctx.obj.get("profile_obj")
    if profile_obj is None:
        return alias_or_id
    aliases = (
        getattr(profile_obj, "extras", {})
        .get("connectors", {})
        .get("google", {})
        .get("calendars", {})
    )
    if alias_or_id in aliases:
        return aliases[alias_or_id]
    if _looks_like_raw_calendar_id(alias_or_id):
        return alias_or_id
    raise typer.BadParameter(
        f"Unknown --calendar alias {alias_or_id!r}. Configure it under "
        f"profiles/<name>/profile.yaml `connectors.google.calendars` "
        f"(alias -> full calendar ID) or pass a raw calendar ID / email / "
        f"`primary` directly."
    )


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@calendar_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def list_events(
    ctx: typer.Context,
    calendar: str | None = typer.Option(
        None,
        "--calendar",
        metavar="ALIAS_OR_ID",
        help=(
            "Calendar alias (resolved via profile) or raw calendar ID. "
            "Omit to let gog-firewall default to `primary`. GCALENDAR.md "
            "warns `primary` is usually the wrong calendar for the operator's "
            "personal events — pass `--calendar personal` for those."
        ),
    ),
) -> None:
    """List events on a calendar (wraps `gog-firewall calendar events`).

    Firewall-preserving. Extras pass through, so engine flags like
    `--from 2026-07-27` / `--to 2026-08-03` / `--today` / `--tomorrow` /
    `--week` / `--days N` / `--max N` / `--query "..."` / `--all` /
    `--json` work unchanged.

    See also `prompts/GCALENDAR.md` for the calendar-ID reference and
    the reason MCP is preferred in agent runs.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["calendar", "events"]
    resolved = _resolve_calendar_id(ctx, calendar)
    if resolved is not None:
        argv.append(resolved)
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@calendar_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def get(
    ctx: typer.Context,
    event_id: str = typer.Argument(..., help="Event ID (from `calendar list`)."),
    calendar: str | None = typer.Option(
        None,
        "--calendar",
        metavar="ALIAS_OR_ID",
        help=(
            "Calendar alias or raw calendar ID. Defaults to `primary` if "
            "omitted; GCALENDAR.md warns this is usually the wrong calendar "
            "for personal events — pass `--calendar personal` "
            "for those."
        ),
    ),
) -> None:
    """Get a single event (wraps `gog-firewall calendar event`).

    Firewall-preserving. Extras pass through, so `--json` and any other
    engine flags work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    resolved = _resolve_calendar_id(ctx, calendar) or DEFAULT_CALENDAR_ID
    rc = run_gog_firewall(["calendar", "event", resolved, event_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@calendar_app.command(
    "search",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Full-text search query."),
) -> None:
    """Full-text search across events (wraps `gog-firewall calendar search`).

    Firewall-preserving. Extras pass through, so engine flags like
    `--from` / `--to` / `--today` / `--tomorrow` / `--week` / `--days N` /
    `--all` / `--json` work unchanged.

    Note: gog-firewall's calendar search doesn't take a per-calendar
    filter positional — it searches across the calendars the current
    OAuth account can see. To filter by a specific calendar use
    `mineru calendar list --calendar <alias> --query "..."` instead.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["calendar", "search", query, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@calendar_app.command(
    "calendars",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def calendars(ctx: typer.Context) -> None:
    """List calendars accessible to the profile's Google account.

    Wraps `gog-firewall calendar calendars`. Firewall-preserving.
    Extras pass through so `--json` and other engine flags work.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["calendar", "calendars", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# WRITE verbs (must never be executed live during dev/test — the tests patch
# run_gog_firewall and assert argv only). the operator invokes them explicitly.
# ---------------------------------------------------------------------------


@calendar_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def create(
    ctx: typer.Context,
    calendar: str | None = typer.Option(
        None,
        "--calendar",
        metavar="ALIAS_OR_ID",
        help=(
            "Calendar alias or raw calendar ID. Defaults to `primary`. "
            "For personal events, pass `--calendar personal` "
            "(GCALENDAR.md's `location`-required rule still applies)."
        ),
    ),
) -> None:
    """Create an event (WRITE, OUTBOUND) — wraps `gog-firewall calendar create`.

    Every event flag passes through opaquely — `--summary`,
    `--from <RFC3339>` (start), `--to <RFC3339>` (end), `--location`,
    `--description`, `--attendees a@x,b@y`, `--all-day`, `--rrule ...`,
    `--reminder popup:30m`, `--visibility`, `--transparency`,
    `--send-updates none|externalOnly|all`, `--with-meet`, `--attachment`,
    `--source-url`, and so on. Add `--no-input` for scripted runs.

    GCALENDAR.md's hard rules still apply: always pass a real
    `--location` for personal events; use `--send-updates none` on
    scripted personal bookkeeping so attendees aren't emailed.

    Example (draft first, then invoke on the operator's approval):
      mineru calendar create --calendar personal \\
        --summary "Dinner with Alice" \\
        --from "2026-08-02T18:30:00-07:00" \\
        --to   "2026-08-02T20:30:00-07:00" \\
        --location "1 Ferry Building, San Francisco, CA 94111" \\
        --send-updates none --no-input
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    resolved = _resolve_calendar_id(ctx, calendar) or DEFAULT_CALENDAR_ID
    rc = run_gog_firewall(["calendar", "create", resolved, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@calendar_app.command(
    "update",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def update(
    ctx: typer.Context,
    event_id: str = typer.Argument(..., help="Event ID to update."),
    calendar: str | None = typer.Option(
        None,
        "--calendar",
        metavar="ALIAS_OR_ID",
        help=(
            "Calendar alias or raw calendar ID the event lives on. "
            "Defaults to `primary`; pass `--calendar personal` for "
            "personal events."
        ),
    ),
) -> None:
    """Update an event (partial update, WRITE) — wraps `gog-firewall calendar update`.

    Only the fields you pass change; every other field is preserved.
    Engine flags pass through opaquely: `--summary`, `--from`, `--to`,
    `--location`, `--description`, `--attendees` (replaces),
    `--add-attendee` (adds without clobbering), `--send-updates`,
    `--no-input`, etc.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    resolved = _resolve_calendar_id(ctx, calendar) or DEFAULT_CALENDAR_ID
    rc = run_gog_firewall(
        ["calendar", "update", resolved, event_id, *extras], ctx=ctx
    )
    raise typer.Exit(code=rc)


@calendar_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def delete(
    ctx: typer.Context,
    event_id: str = typer.Argument(..., help="Event ID to delete."),
    calendar: str | None = typer.Option(
        None,
        "--calendar",
        metavar="ALIAS_OR_ID",
        help=(
            "Calendar alias or raw calendar ID the event lives on. "
            "Defaults to `primary`; pass `--calendar personal` for "
            "personal events."
        ),
    ),
) -> None:
    """Delete an event (WRITE, DESTRUCTIVE) — wraps `gog-firewall calendar delete`.

    Confirm every delete with the operator before invoking (per AGENTS.md External
    Comms). Recurring-event scope flags pass through opaquely:
    `--scope single|future|all`, `--original-start`, `--no-input`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    resolved = _resolve_calendar_id(ctx, calendar) or DEFAULT_CALENDAR_ID
    rc = run_gog_firewall(
        ["calendar", "delete", resolved, event_id, *extras], ctx=ctx
    )
    raise typer.Exit(code=rc)


@calendar_app.command(
    "respond",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def respond(
    ctx: typer.Context,
    event_id: str = typer.Argument(..., help="Event ID to respond to."),
    calendar: str | None = typer.Option(
        None,
        "--calendar",
        metavar="ALIAS_OR_ID",
        help=(
            "Calendar alias or raw calendar ID the event lives on. "
            "Defaults to `primary`; pass `--calendar personal` for "
            "personal events."
        ),
    ),
) -> None:
    """Respond to an invite (WRITE, OUTBOUND) — wraps `gog-firewall calendar respond`.

    Engine flags pass through opaquely: `--status accepted|declined|tentative|needsAction`
    (required) and `--comment "..."` (optional note back to the organizer).

    Example:
      mineru calendar respond <eventId> --calendar personal \\
        --status accepted --no-input
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    resolved = _resolve_calendar_id(ctx, calendar) or DEFAULT_CALENDAR_ID
    rc = run_gog_firewall(
        ["calendar", "respond", resolved, event_id, *extras], ctx=ctx
    )
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# Discoverable stubs — deliberately left un-wired in this P2-02 increment.
# `freebusy` and `conflicts` compose usefully on top of `list_events` for
# cross-calendar scheduling but don't have a MCP equivalent (MCP's
# `suggest_time` is the covered path); a future increment wires both through
# gog-firewall calendar. Kept hidden from `--help` per the 2026-09-16 audit
# §3A so the parent listing stays focused on the wired verbs.
# ---------------------------------------------------------------------------


@calendar_app.command("freebusy", hidden=True)
def freebusy() -> None:
    """Free/busy query."""
    not_yet_implemented("calendar freebusy")


@calendar_app.command("conflicts", hidden=True)
def conflicts() -> None:
    """Conflict check across a window."""
    not_yet_implemented("calendar conflicts")
