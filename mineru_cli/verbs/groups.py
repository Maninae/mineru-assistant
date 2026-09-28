"""`mineru groups` sub-app.

Phase 2 status (P2-05, groups quarter): both Google Groups verbs the task
lists is wired end-to-end to the live `gog-firewall` engine at
`$MINERU_HOME/bin/gog-firewall` through `mineru_cli.wrappers.gog_firewall`.
`groups` is a pure read surface (`list` / `members`); there are no write
verbs at this layer, so every command is safe to run live.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  All Groups verbs in this codebase route through
  `$MINERU_HOME/bin/gog-firewall` (or the path set on
  `MINERU_GOG_FIREWALL_BIN`). The raw `gog` binary at
  `/opt/homebrew/bin/gog` is NEVER called by any verb. External group
  content (names, member lists, descriptions) is always screened for
  prompt-injection before it reaches the LLM context.

  The single argv[0] source of truth is
  `mineru_cli.wrappers.gog_firewall.build_gog_firewall_argv`. The
  existing `test_argv0_resolves_to_gog_firewall_default` invariant test
  pins argv[0] to `gog-firewall`; a regression that swapped the binary
  for bare `gog` would fail the test suite immediately.

  Firewall exit codes propagate unchanged: 0 delivered, 77 all units
  blocked, 78 firewall itself errored. Stderr redaction notices flow
  through untouched — this file never captures, filters, or reformats
  them.

  Explicitly out of scope: `--raw`, `--unsafe-strip-invisible`, or any
  other firewall-bypassing flag.

Naming note (mineru surface vs gog surface):

  Task spec calls the argument `<groupId>` while gog names it
  `<groupEmail>`. In Google Workspace, a group's identifier IS its
  email address; we keep `group_id` at the mineru surface for
  consistency with the task spec but call out in the help text that
  this is the group's email.

Verb → gog-firewall subverb map:

  Reads (safe to run live; all pass through the injection firewall):
    mineru groups list                → gog-firewall groups list
    mineru groups members <groupId>   → gog-firewall groups members <groupId>
"""

import typer

from mineru_cli.wrappers.gog_firewall import run_gog_firewall
from mineru_cli.verbs._helpers import propagate_global_flags as _propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru groups` app.
# ---------------------------------------------------------------------------

groups_app = typer.Typer(
    name="groups",
    help=(
        "Google Groups: firewall-preserving reads via gog-firewall. "
        "Every verb routes through the injection firewall; raw `gog` is never called."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@groups_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def list_(ctx: typer.Context) -> None:
    """List Google Groups the caller belongs to (wraps `gog-firewall groups list`).

    Firewall-preserving. Extras pass through so `--json` / `--max N`
    work unchanged.
    """
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["groups", "list", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@groups_app.command(
    "members",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def members(
    ctx: typer.Context,
    group_id: str = typer.Argument(
        ...,
        metavar="GROUP_ID",
        help=(
            "Group identifier (in Google Workspace this is the group's email "
            "address, e.g. `team@example.com`)."
        ),
    ),
) -> None:
    """List members of a Google Group (wraps `gog-firewall groups members`).

    Firewall-preserving. Extras pass through so `--json` / `--max N` /
    `--page TOKEN` all work unchanged.
    """
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["groups", "members", group_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)
