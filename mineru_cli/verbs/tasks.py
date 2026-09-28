"""`mineru tasks` sub-app.

Phase 2 status (P2-05, tasks quarter): every Google Tasks verb the task
lists is wired end-to-end to the live `gog-firewall` engine at
`$MINERU_HOME/bin/gog-firewall` through `mineru_cli.wrappers.gog_firewall`.
Reads (`lists`, `items`, `get`) and writes (`add`, `update`, `done`,
`undo`, `delete`, `clear`) both route through the SAME firewalled
wrapper; write verbs are only executed live when the operator explicitly
invokes them, but the wrapper is the same code path so the firewall-
preservation invariant is uniform.

Naming note (mineru surface vs gog surface):

  gog's Google Tasks top-level exposes a subverb `list <tasklistId>` for
  "list tasks IN a given list", plus a sub-group `lists` (plural) whose
  own default is "list task lists". mineru's surface used to mirror
  those two names verbatim (`lists` vs `list`), but the trailing-`s`
  collision was the canonical CLI foot-gun the 2026-09-16 audit §2C
  called out: singular vs plural of the same word for two totally
  different operations. Renamed on 2026-09-16 to `tasks items` for
  the "tasks IN one list" verb, matching Google's own API type
  (`Task` items OF a `TaskList`):

    mineru tasks lists           → gog-firewall tasks lists list
                                   (LIST the caller's task LISTS)
    mineru tasks items [<listId>] → gog-firewall tasks list [<listId>]
                                   (list TASKS in one list; omit listId
                                   for the caller's default list)

  The old `tasks list` spelling still works as a hidden Typer alias
  for ~90 days, with a one-line stderr deprecation notice on use.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  All Tasks verbs in this codebase route through
  `$MINERU_HOME/bin/gog-firewall` (or the path set on
  `MINERU_GOG_FIREWALL_BIN`). The raw `gog` binary at
  `/opt/homebrew/bin/gog` is NEVER called by any verb. External task
  content (titles, notes, due dates) is always screened for
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
  other firewall-bypassing flag.

Wire-up rules (mirror the P2-01/03/04 verb files exactly):

  - The wrapper is the ONLY subprocess call site. Each verb picks the
    sub-verb, folds root-level flags into the extras list, and hands
    the argv to `run_gog_firewall`.
  - Extras (everything Typer didn't consume) pass straight through to
    gog-firewall, so engine flags like `--json` / `--max N` / `--page` /
    `--title` / `--notes` / `--due` / `--status` / `--parent` /
    `--previous` / `--repeat` / `--show-completed` / `--no-input` all
    work with zero wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json tasks lists` behaves
    the same as `mineru tasks lists --json`. Idempotent: if the user
    also passed the flag as a trailing extra we don't duplicate it.
  - Exit code from gog-firewall propagates unchanged via `typer.Exit`.

mineru → gog-firewall argument-shape translations:

  - `tasks lists` → `tasks lists list` (mineru omits the trailing "list"
    subverb because "tasks lists" is the natural English reading and
    the gog sub-group's default operation).
  - `tasks items` (new canonical) and its hidden `tasks list` alias
    both map to gog's `tasks list [<listId>]`.
  - `tasks add <listId> --title <t>` accepts `--title` at the mineru
    surface for readability. gog's `tasks add` also takes `--title`
    natively (it's the required flag), so we just pass it through as
    the flag rather than translating to a positional.

Verb → gog-firewall subverb map:

  Reads (safe to run live; all pass through the injection firewall):
    mineru tasks lists                     → gog-firewall tasks lists list
    mineru tasks items [<listId>]          → gog-firewall tasks list [<listId>]
    mineru tasks list  [<listId>]          → DEPRECATED alias for `items`
    mineru tasks get <listId> <taskId>     → gog-firewall tasks get <listId> <taskId>

  Writes (never executed live during dev/test; tests patch
  run_gog_firewall and assert argv only):
    mineru tasks add <listId> --title T    → gog-firewall tasks add <listId> --title T
    mineru tasks update <listId> <taskId> [fields]
                                           → gog-firewall tasks update <listId> <taskId> [fields]
    mineru tasks done <listId> <taskId>    → gog-firewall tasks done <listId> <taskId>
    mineru tasks undo <listId> <taskId>    → gog-firewall tasks undo <listId> <taskId>
    mineru tasks delete <listId> <taskId>  → gog-firewall tasks delete <listId> <taskId>
                                              (DESTRUCTIVE; NEVER run live in dev/test)
    mineru tasks clear <listId>            → gog-firewall tasks clear <listId>
                                              (DESTRUCTIVE at the "clear completed" level)
"""

import typer

from mineru_cli._deprecation import emit_rename_notice
from mineru_cli.profile import get_profile
from mineru_cli.wrappers.gog_firewall import run_gog_firewall
from mineru_cli.verbs._helpers import propagate_global_flags as _propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru tasks` app.
# ---------------------------------------------------------------------------

tasks_app = typer.Typer(
    name="tasks",
    help=(
        "Google Tasks: firewall-preserving reads and writes via gog-firewall. "
        "Every verb routes through the injection firewall; raw `gog` is never called. "
        "`lists` (plural) = enumerate task LISTS; `items` = enumerate TASKS "
        "in one list. (The old `tasks list` still works as a hidden alias "
        "of `items` for ~90 days.)"
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@tasks_app.command(
    "lists",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def lists(ctx: typer.Context) -> None:
    """List the caller's task LISTS (wraps `gog-firewall tasks lists list`).

    "lists" (plural) is intentional: this returns the collection of
    task lists the caller owns / has access to. To list tasks WITHIN a
    single list, use `tasks list <listId>` (singular) instead.
    Firewall-preserving; extras pass through so `--json` and `--max N`
    work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["tasks", "lists", "list", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@tasks_app.command(
    "items",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def items(
    ctx: typer.Context,
    list_id: str | None = typer.Argument(
        None,
        metavar="LIST_ID",
        help=(
            "Task list ID to enumerate. Omit to let gog default to the "
            "caller's default task list (see `mineru tasks lists` to enumerate)."
        ),
    ),
) -> None:
    """List tasks in ONE task list (wraps `gog-firewall tasks list`).

    `items` resolves the audit's `tasks list` / `tasks lists` collision:
    `lists` (plural) enumerates task LISTS; `items` enumerates TASKS
    inside one list. Matches Google's own API type (`Task` items OF a
    `TaskList`). Firewall-preserving; extras pass through so
    `--show-completed` / `--show-deleted` / `--due-min ISO` /
    `--due-max ISO` / `--max N` / `--page TOKEN` / `--json` all work
    unchanged. The old `tasks list` spelling still works as a hidden
    alias for ~90 days.
    """
    _run_tasks_items(ctx, list_id)


@tasks_app.command(
    "list",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def list_alias(
    ctx: typer.Context,
    list_id: str | None = typer.Argument(
        None,
        metavar="LIST_ID",
        help="See `tasks items --help`.",
    ),
) -> None:
    """DEPRECATED alias for `tasks items`. Kept for ~90 days."""
    emit_rename_notice("mineru tasks list", "mineru tasks items")
    _run_tasks_items(ctx, list_id)


def _run_tasks_items(ctx: typer.Context, list_id: str | None) -> None:
    """Shared body for `tasks items` and its hidden `tasks list` alias.

    Both verbs pipe to `gog-firewall tasks list [<listId>]` (gog's own
    singular verb — the rename lives in the mineru surface only, not
    in the wrapped tool).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["tasks", "list"]
    if list_id is not None:
        argv.append(list_id)
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@tasks_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def get(
    ctx: typer.Context,
    list_id: str = typer.Argument(..., metavar="LIST_ID", help="Task list ID."),
    task_id: str = typer.Argument(..., metavar="TASK_ID", help="Task ID."),
) -> None:
    """Get a single task (wraps `gog-firewall tasks get`).

    Firewall-preserving. Extras pass through so `--json` works unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["tasks", "get", list_id, task_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# WRITE verbs (must never be executed live during dev/test — the tests patch
# run_gog_firewall and assert argv only). the operator invokes them explicitly.
# ---------------------------------------------------------------------------


@tasks_app.command(
    "add",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def add(
    ctx: typer.Context,
    list_id: str = typer.Argument(
        ..., metavar="LIST_ID", help="Task list ID to add the task to."
    ),
    title: str = typer.Option(
        ...,
        "--title",
        metavar="TITLE",
        help="New task title (required by gog).",
    ),
) -> None:
    """Add a new task (WRITE) — wraps `gog-firewall tasks add`.

    `--title` is required. Every other engine flag passes through
    opaquely: `--notes`, `--due <RFC3339|YYYY-MM-DD>`, `--parent` (for
    subtasks), `--previous` (sibling ordering), `--repeat daily|weekly|
    monthly|yearly`, `--repeat-count N`, `--repeat-until <date>`,
    `--no-input`, `--json`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(
        ["tasks", "add", list_id, "--title", title, *extras], ctx=ctx
    )
    raise typer.Exit(code=rc)


@tasks_app.command(
    "update",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def update(
    ctx: typer.Context,
    list_id: str = typer.Argument(..., metavar="LIST_ID", help="Task list ID."),
    task_id: str = typer.Argument(..., metavar="TASK_ID", help="Task ID to update."),
) -> None:
    """Update an existing task (WRITE, partial) — wraps `gog-firewall tasks update`.

    Every field-mutation flag passes through opaquely: `--title`,
    `--notes`, `--due <RFC3339|YYYY-MM-DD>`, `--status
    needsAction|completed`, `--no-input`. An empty-string value
    (`--notes ""`) clears the field.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["tasks", "update", list_id, task_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@tasks_app.command(
    "done",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def done(
    ctx: typer.Context,
    list_id: str = typer.Argument(..., metavar="LIST_ID", help="Task list ID."),
    task_id: str = typer.Argument(
        ..., metavar="TASK_ID", help="Task ID to mark completed."
    ),
) -> None:
    """Mark a task completed (WRITE) — wraps `gog-firewall tasks done`.

    Extras pass through opaquely (`--no-input`, `--json`).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["tasks", "done", list_id, task_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@tasks_app.command(
    "undo",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def undo(
    ctx: typer.Context,
    list_id: str = typer.Argument(..., metavar="LIST_ID", help="Task list ID."),
    task_id: str = typer.Argument(
        ..., metavar="TASK_ID", help="Task ID to mark needs-action (undo completed)."
    ),
) -> None:
    """Mark a task as needs-action (WRITE, undo `done`) — wraps `gog-firewall tasks undo`.

    Extras pass through opaquely (`--no-input`, `--json`).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["tasks", "undo", list_id, task_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@tasks_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def delete(
    ctx: typer.Context,
    list_id: str = typer.Argument(..., metavar="LIST_ID", help="Task list ID."),
    task_id: str = typer.Argument(
        ..., metavar="TASK_ID", help="Task ID to delete."
    ),
) -> None:
    """Delete a task (WRITE, DESTRUCTIVE) — wraps `gog-firewall tasks delete`.

    NEVER executed live during dev/test (tests patch run_gog_firewall
    and assert argv only). Confirm every delete with the operator before
    invoking (per AGENTS.md External Comms). Extras pass through
    opaquely so `--force` / `--no-input` work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["tasks", "delete", list_id, task_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@tasks_app.command(
    "clear",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def clear(
    ctx: typer.Context,
    list_id: str = typer.Argument(
        ..., metavar="LIST_ID", help="Task list ID to clear completed tasks from."
    ),
) -> None:
    """Clear completed tasks from a list (WRITE, DESTRUCTIVE) — wraps `gog-firewall tasks clear`.

    Removes every task in `list_id` whose status is `completed`.
    NEVER executed live during dev/test. Confirm with the operator before
    invoking. Extras pass through opaquely (`--force`, `--no-input`).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["tasks", "clear", list_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)
