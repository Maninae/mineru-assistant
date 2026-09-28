"""`mineru directory` sub-app.

Phase 2 status (P2-05, directory quarter): every Google People verb the task
lists is wired end-to-end to the live `gog-firewall` engine at
`$MINERU_HOME/bin/gog-firewall` through `mineru_cli.wrappers.gog_firewall`.
`directory` is a pure read surface today (`me` / `get` / `search` / `relations`);
there are no write verbs at this layer, so every command is safe to run live.

RENAMED 2026-09-16 (audit §2B): this sub-app used to be `mineru people`.
Renamed to `directory` so the `people` name is freed for the machine-level
human registry (the audit's §2A rename `humans` -> `people` chains after this
one). The name `directory` reads more accurately anyway — the underlying gog
subverb is `gog people`, which itself wraps the Google Workspace *directory*
API (org-directory profiles, not the personal address book — that is
`contacts`). The old `mineru people ...` spelling remains a HIDDEN alias via
`people_alias_app` for the standard 90-day compat window; every invocation
under the old name emits a one-line DEPRECATED notice on stderr through the
shared `_deprecation.emit_rename_notice` helper.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  All directory verbs in this codebase route through
  `$MINERU_HOME/bin/gog-firewall` (or the path set on
  `MINERU_GOG_FIREWALL_BIN`). The raw `gog` binary at
  `/opt/homebrew/bin/gog` is NEVER called by any verb. External profile
  content (display names, org info, notes) is always screened for
  prompt-injection before it reaches the LLM context.

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
  other firewall-bypassing flag.

Wire-up rules (mirror the P2-01/03/04 verb files exactly):

  - The wrapper is the ONLY subprocess call site. Each verb picks the
    sub-verb, folds root-level flags into the extras list, and hands
    the argv to `run_gog_firewall`.
  - Extras (everything Typer didn't consume) pass straight through to
    gog-firewall, so engine flags like `--json` / `--max N` /
    `--fields` all work with zero wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json directory me` behaves the
    same as `mineru directory me --json`. Idempotent: if the user also
    passed the flag as a trailing extra we don't duplicate it.
  - Exit code from gog-firewall propagates unchanged via `typer.Exit`.

  NB: the underlying gog subverb is still `gog people ...` — the gog CLI
  did not rename its own surface. Only the operator-facing `mineru`
  spelling changed; the argv we hand to gog-firewall keeps saying
  `["people", "me", ...]`, `["people", "search", ...]`, etc.

Verb -> gog-firewall subverb map:

  Reads (safe to run live; all pass through the injection firewall):
    mineru directory me            -> gog-firewall people me
    mineru directory get <userId>  -> gog-firewall people get <userId>
    mineru directory search <q>    -> gog-firewall people search <q>
    mineru directory relations [<userId>]
                                   -> gog-firewall people relations [<userId>]
"""

import typer

from mineru_cli._deprecation import emit_rename_notice
from mineru_cli.wrappers.gog_firewall import run_gog_firewall
from mineru_cli.verbs._helpers import propagate_global_flags as _propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru directory` app.
# ---------------------------------------------------------------------------

directory_app = typer.Typer(
    name="directory",
    help=(
        "Google People (Workspace directory profiles): firewall-preserving reads "
        "via gog-firewall. Every verb routes through the injection firewall; "
        "raw `gog` is never called. Renamed from `people` on 2026-09-16 to free "
        "the `people` name for the machine-level human registry."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@directory_app.command(
    "me",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def me(ctx: typer.Context) -> None:
    """Show the current profile (wraps `gog-firewall people me`).

    Firewall-preserving. Extras pass through so `--json` and other
    engine flags (`--fields`) work unchanged.
    """
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["people", "me", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@directory_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def get(
    ctx: typer.Context,
    user_id: str = typer.Argument(
        ...,
        metavar="USER_ID",
        help="Google People userId (e.g. `people/123` or a Workspace user email).",
    ),
) -> None:
    """Get a user profile by ID (wraps `gog-firewall people get`).

    Firewall-preserving. Extras pass through so `--json` and `--fields`
    work unchanged.
    """
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["people", "get", user_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@directory_app.command(
    "search",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def search(
    ctx: typer.Context,
    query: str = typer.Argument(
        ..., help="Search query (matches directory display names / emails)."
    ),
) -> None:
    """Search the Workspace directory (wraps `gog-firewall people search`).

    Firewall-preserving. Extras pass through so `--max N` / `--json` /
    additional positional query tokens all work unchanged.
    """
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["people", "search", query, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@directory_app.command(
    "relations",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def relations(
    ctx: typer.Context,
    user_id: str | None = typer.Argument(
        None,
        metavar="USER_ID",
        help=(
            "Optional user to fetch relations for. Omit for the current caller "
            "(gog defaults to `people/me` in that case)."
        ),
    ),
) -> None:
    """Get user relations (wraps `gog-firewall people relations`).

    Firewall-preserving. If `user_id` is omitted, gog defaults to the
    caller's own relations. Extras pass through so `--json` and
    `--fields` work unchanged.
    """
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["people", "relations"]
    if user_id is not None:
        argv.append(user_id)
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# HIDDEN DEPRECATED alias — `mineru people ...` (pre-2026-09-16 spelling).
# ---------------------------------------------------------------------------
#
# Standard 90-day compat window (see `mineru_cli/_deprecation.py`). Every
# verb under the old name fires a one-line DEPRECATED notice on stderr and
# then forwards to the same `run_gog_firewall` argv the canonical verb
# would have used. The alias sub-app is registered under `name="people"`
# with `hidden=True` in `mineru_cli/app.py` so it never renders in
# `mineru --help` yet remains callable for muscle-memory continuity.
#
# NOTE ABOUT COMMIT-2 COLLISION: the audit's next rename in this chain
# (`humans` -> `people` for the machine-level human registry) will TAKE
# the `people` name. When that lands, this hidden alias is DROPPED (a
# Typer sub-app cannot be registered twice under the same name, and the
# audit is explicit that the canonical `people` post-chain must be the
# human registry). Callers who need the Workspace-directory read surface
# after that will use the canonical `mineru directory` spelling. See the
# `_DEPRECATED_OLD_NAME_*` sentinels below for the exact strings the
# notice emits, kept as module constants so grep-based audits and tests
# can pin them.

_DEPRECATED_OLD_NAME_ME = "mineru people me"
_DEPRECATED_OLD_NAME_GET = "mineru people get"
_DEPRECATED_OLD_NAME_SEARCH = "mineru people search"
_DEPRECATED_OLD_NAME_RELATIONS = "mineru people relations"
_NEW_NAME_ME = "mineru directory me"
_NEW_NAME_GET = "mineru directory get"
_NEW_NAME_SEARCH = "mineru directory search"
_NEW_NAME_RELATIONS = "mineru directory relations"

people_alias_app = typer.Typer(
    name="people",
    help=(
        "DEPRECATED alias for `mineru directory` (Google Workspace directory "
        "reads). Renamed on 2026-09-16; the old name works for ~90 days."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@people_alias_app.command(
    "me",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def me_alias(ctx: typer.Context) -> None:
    """DEPRECATED alias for `mineru directory me`. Kept for ~90 days."""
    emit_rename_notice(_DEPRECATED_OLD_NAME_ME, _NEW_NAME_ME)
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["people", "me", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@people_alias_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def get_alias(
    ctx: typer.Context,
    user_id: str = typer.Argument(
        ...,
        metavar="USER_ID",
        help="Google People userId (e.g. `people/123` or a Workspace user email).",
    ),
) -> None:
    """DEPRECATED alias for `mineru directory get`. Kept for ~90 days."""
    emit_rename_notice(_DEPRECATED_OLD_NAME_GET, _NEW_NAME_GET)
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["people", "get", user_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@people_alias_app.command(
    "search",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def search_alias(
    ctx: typer.Context,
    query: str = typer.Argument(
        ..., help="Search query (matches directory display names / emails)."
    ),
) -> None:
    """DEPRECATED alias for `mineru directory search`. Kept for ~90 days."""
    emit_rename_notice(_DEPRECATED_OLD_NAME_SEARCH, _NEW_NAME_SEARCH)
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["people", "search", query, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@people_alias_app.command(
    "relations",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def relations_alias(
    ctx: typer.Context,
    user_id: str | None = typer.Argument(
        None,
        metavar="USER_ID",
        help=(
            "Optional user to fetch relations for. Omit for the current caller "
            "(gog defaults to `people/me` in that case)."
        ),
    ),
) -> None:
    """DEPRECATED alias for `mineru directory relations`. Kept for ~90 days."""
    emit_rename_notice(_DEPRECATED_OLD_NAME_RELATIONS, _NEW_NAME_RELATIONS)
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["people", "relations"]
    if user_id is not None:
        argv.append(user_id)
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)
