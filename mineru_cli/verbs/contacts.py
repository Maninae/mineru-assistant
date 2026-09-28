"""`mineru contacts` sub-app.

Phase 2 status (P2-05, contacts quarter): every Google Contacts verb the task
lists is wired end-to-end to the live `gog-firewall` engine at
`$MINERU_HOME/bin/gog-firewall` through `mineru_cli.wrappers.gog_firewall`.
Reads (`lookup`, `search`, `list`, `get`, `directory`, `other`) and writes
(`create`, `update`, `delete`) both route through the SAME firewalled
wrapper; write verbs are only executed live when the operator explicitly invokes
them, but the wrapper is the same code path so the firewall-preservation
invariant is uniform.

`contacts lookup <handle>` is a mineru-only convenience alias that routes
to `gog-firewall contacts search <handle>`: the raw gog binary doesn't
have a `lookup` subcommand, but the operator's workspace has a long-standing
`contacts-lookup` mental model of "resolve a phone / email / name string
to a contact record", which maps naturally to a Google Contacts search
query. The alias is called out in the verb docstring so a maintainer
reading the code doesn't hunt for a missing gog subverb.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  All Contacts verbs in this codebase route through
  `$MINERU_HOME/bin/gog-firewall` (or the path set on
  `MINERU_GOG_FIREWALL_BIN`). The raw `gog` binary at
  `/opt/homebrew/bin/gog` is NEVER called by any verb. External contact
  content (names, emails, phones, notes) is always screened for
  prompt-injection before it reaches the LLM context on read paths, and
  write paths route through the same firewalled tool so no verb ever
  escapes to the unwrapped binary.

  The single argv[0] source of truth is
  `mineru_cli.wrappers.gog_firewall.build_gog_firewall_argv`. The
  existing `test_argv0_resolves_to_gog_firewall_default` invariant test
  pins argv[0] to `gog-firewall`; a regression that swapped the binary
  for bare `gog` would fail the test suite immediately.

  Firewall exit codes propagate unchanged: 0 delivered, 77 all units
  blocked (prompt-injection detected everywhere), 78 firewall itself
  errored. Stderr redaction notices (`redacted N of M units`) flow
  through untouched — this file never captures, filters, or reformats
  them.

  Explicitly out of scope: `--raw`, `--unsafe-strip-invisible`, or any
  other firewall-bypassing flag. Those are workspace-level escape
  hatches, not CLI-level knobs. The grep-based tests block them from
  ever landing here as string literals.

Wire-up rules (mirror the P2-01/03/04 verb files exactly):

  - The wrapper is the ONLY subprocess call site. Each verb picks the
    sub-verb, folds root-level flags into the extras list, translates
    the small handful of mineru→gog argument shapes that differ (see
    below), and hands the argv to `run_gog_firewall`.
  - Extras (everything Typer didn't consume) pass straight through to
    gog-firewall, so engine flags like `--json` / `--max N` / `--page` /
    `--no-input` / `--given` / `--family` all work with zero wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json contacts list` behaves
    the same as `mineru contacts list --json`. Idempotent: if the user
    also passed the flag as a trailing extra we don't duplicate it.
  - Exit code from gog-firewall propagates unchanged via `typer.Exit`.

mineru → gog-firewall argument-shape translations (kept small and local):

  - `contacts lookup <handle>` → `contacts search <handle>` (mineru alias;
    gog has no `lookup` subverb, but a lookup by phone/email is exactly
    what `contacts search` does).
  - `contacts create --name "First Last"` → gog's create takes `--given`
    and `--family` separately. We accept `--name` as the mineru surface
    (per task spec) and split on the FIRST space: everything before is
    the given name, everything after is the family name. One-word names
    emit only `--given`. Power users can bypass by omitting `--name` and
    passing `--given` / `--family` directly via extras.
  - `--email` / `--phone` pass through unchanged (same flag names on gog).

Verb → gog-firewall subverb map:

  Reads (safe to run live; all pass through the injection firewall):
    mineru contacts lookup <handle>       → gog-firewall contacts search <handle>
    mineru contacts search <query>        → gog-firewall contacts search <query>
    mineru contacts list                  → gog-firewall contacts list
    mineru contacts get <resourceName>    → gog-firewall contacts get <resourceName>
    mineru contacts directory             → gog-firewall contacts directory list
    mineru contacts other                 → gog-firewall contacts other list

  Writes (never executed live during dev/test; tests patch
  run_gog_firewall and assert argv only):
    mineru contacts create --name / --email / --phone
                                          → gog-firewall contacts create \\
                                              [--given X --family Y] \\
                                              [--email E] [--phone P]
    mineru contacts update <resourceName> [fields]
                                          → gog-firewall contacts update <resourceName> [fields]
    mineru contacts delete <resourceName> → gog-firewall contacts delete <resourceName>
                                              (DESTRUCTIVE; NEVER run live in dev/test)
"""

import typer

from mineru_cli.profile import get_profile
from mineru_cli.wrappers.gog_firewall import run_gog_firewall
from mineru_cli.verbs._helpers import propagate_global_flags as _propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru contacts` app.
# ---------------------------------------------------------------------------

contacts_app = typer.Typer(
    name="contacts",
    help=(
        "Google Contacts: firewall-preserving reads and writes via gog-firewall. "
        "Every verb routes through the injection firewall; raw `gog` is never called. "
        "`lookup <handle>` is a mineru alias for `search <handle>`."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _split_name_into_given_and_family(name: str) -> list[str]:
    """Split a display name into gog's `--given` / `--family` flag pair.

    Rule: split on the first space. Everything before goes to `--given`,
    everything after (trimmed) goes to `--family`. A single-token name
    emits only `--given`. An empty or whitespace-only name is rejected as
    a `typer.BadParameter`, because emitting `--given ""` either errors at
    gog three layers down or (worse) creates a nameless contact that the operator
    has to hunt down by resourceName.

    Kept as its own helper so the split logic is easy to test and the
    create verb stays a straight-line argv builder.
    """
    stripped = name.strip()
    if not stripped:
        raise typer.BadParameter(
            "--name must not be empty or whitespace-only"
        )
    parts = stripped.split(" ", 1)
    if len(parts) == 1 or not parts[1].strip():
        return ["--given", parts[0]]
    given, family = parts[0], parts[1].strip()
    return ["--given", given, "--family", family]


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@contacts_app.command(
    "lookup",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def lookup(
    ctx: typer.Context,
    handle: str = typer.Argument(
        ...,
        help=(
            "A handle to resolve to a contact: phone number, email address, or "
            "partial name. Routes to `gog-firewall contacts search <handle>`."
        ),
    ),
) -> None:
    """Resolve a handle (phone / email / name) to a contact (mineru alias).

    `gog-firewall contacts` has no `lookup` subverb; this mineru command
    is a convenience alias that routes to `contacts search <handle>`
    because a handle-to-contact lookup is exactly what search performs
    over the People API. Firewall-preserving. Extras pass through so
    `--max N` / `--json` / `--page TOKEN` all work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["contacts", "search", handle, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@contacts_app.command(
    "search",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def search(
    ctx: typer.Context,
    query: str = typer.Argument(
        ..., help="Contacts search query (matches name / email / phone)."
    ),
) -> None:
    """Search contacts (wraps `gog-firewall contacts search`).

    Firewall-preserving. Extras pass through so `--max N` / `--json` /
    `--page TOKEN` all work unchanged. Additional positional query tokens
    are also forwarded, matching gog's variadic `<query> ...`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["contacts", "search", query, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@contacts_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def list_(ctx: typer.Context) -> None:
    """List contacts (wraps `gog-firewall contacts list`).

    Firewall-preserving. Extras pass through so `--max N` / `--page TOKEN` /
    `--json` all work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["contacts", "list", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@contacts_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def get(
    ctx: typer.Context,
    resource_name: str = typer.Argument(
        ...,
        metavar="RESOURCE_NAME",
        help="Contact resourceName (e.g. `people/c1234567890`).",
    ),
) -> None:
    """Get a single contact (wraps `gog-firewall contacts get`).

    Firewall-preserving. Extras pass through so `--json` works unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["contacts", "get", resource_name, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@contacts_app.command(
    "directory",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def directory(ctx: typer.Context) -> None:
    """List people in the Workspace directory (wraps `gog-firewall contacts directory list`).

    gog exposes `directory` as a sub-group with `list` and `search`
    verbs; the default here is `list`. To search the directory, use
    `contacts search` (it also covers the directory) or drop into the
    raw gog interface with `contacts directory search <query>` via extras.
    Firewall-preserving.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["contacts", "directory", "list", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@contacts_app.command(
    "other",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def other(ctx: typer.Context) -> None:
    """List "other contacts" (wraps `gog-firewall contacts other list`).

    Other contacts are auto-collected by Google (people you've emailed
    but not saved). gog exposes `other` as a sub-group with `list`,
    `search`, and `delete`; the default here is `list`. Firewall-preserving.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["contacts", "other", "list", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# WRITE verbs (must never be executed live during dev/test — the tests patch
# run_gog_firewall and assert argv only). the operator invokes them explicitly.
# ---------------------------------------------------------------------------


@contacts_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def create(
    ctx: typer.Context,
    name: str | None = typer.Option(
        None,
        "--name",
        metavar="DISPLAY_NAME",
        help=(
            "Display name (mineru convenience). Splits on the first space "
            "into `--given` + `--family` for gog. One-word names emit only "
            "`--given`. Power users can omit `--name` and pass `--given` / "
            "`--family` directly via extras."
        ),
    ),
    email: str | None = typer.Option(
        None,
        "--email",
        metavar="EMAIL",
        help="Contact email address (passes through as gog `--email`).",
    ),
    phone: str | None = typer.Option(
        None,
        "--phone",
        metavar="PHONE",
        help="Contact phone number (passes through as gog `--phone`).",
    ),
) -> None:
    """Create a new contact (WRITE) — wraps `gog-firewall contacts create`.

    `--name "First Last"` splits into gog's `--given First --family Last`.
    `--email` and `--phone` pass through unchanged. Every remaining engine
    flag also passes through opaquely (`--given`, `--family`, `--notes`,
    `--no-input`, `--json`).

    Example:
      mineru contacts create --name "Alice Smith" \\
        --email alice@example.com --phone +14155551234 --no-input
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["contacts", "create"]
    if name is not None:
        argv.extend(_split_name_into_given_and_family(name))
    if email is not None:
        argv.extend(["--email", email])
    if phone is not None:
        argv.extend(["--phone", phone])
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@contacts_app.command(
    "update",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def update(
    ctx: typer.Context,
    resource_name: str = typer.Argument(
        ...,
        metavar="RESOURCE_NAME",
        help="Contact resourceName to update (e.g. `people/c1234567890`).",
    ),
) -> None:
    """Update an existing contact (WRITE) — wraps `gog-firewall contacts update`.

    Every field-mutation flag passes through opaquely — `--given`,
    `--family`, `--email`, `--phone`, `--notes`, `--no-input`. gog
    treats an empty-string value (`--email ""`) as a clear. Unlike
    `create`, we don't accept `--name`: gog's update surface is
    field-level, so the caller should either pass `--given` / `--family`
    explicitly or use `contacts get` + `contacts create` to re-shape a
    contact.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["contacts", "update", resource_name, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@contacts_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def delete(
    ctx: typer.Context,
    resource_name: str = typer.Argument(
        ...,
        metavar="RESOURCE_NAME",
        help="Contact resourceName to delete (e.g. `people/c1234567890`).",
    ),
) -> None:
    """Delete a contact (WRITE, DESTRUCTIVE) — wraps `gog-firewall contacts delete`.

    NEVER executed live during dev/test (tests patch run_gog_firewall
    and assert argv only). Confirm every delete with the operator before
    invoking (per AGENTS.md External Comms). Extras pass through
    opaquely so `--force` / `--no-input` / `--json` work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["contacts", "delete", resource_name, *extras], ctx=ctx)
    raise typer.Exit(code=rc)
