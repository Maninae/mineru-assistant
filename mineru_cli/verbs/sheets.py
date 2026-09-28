"""`mineru sheets` sub-app.

Phase 2 status (P2-04, sheets half): every Google Sheets verb the task
lists is wired end-to-end to the live `gog-firewall` engine at
`$MINERU_HOME/bin/gog-firewall` through `mineru_cli.wrappers.gog_firewall`.
Reads (`get`, `metadata`) and writes (`update`, `append`, `clear`,
`format`) both route through the SAME firewalled wrapper; write verbs
are only executed live when the operator explicitly invokes them, but the wrapper
is the same code path so the firewall-preservation invariant is uniform.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  All Sheets verbs in this codebase route through
  `$MINERU_HOME/bin/gog-firewall` (or the path set on
  `MINERU_GOG_FIREWALL_BIN`). The raw `gog` binary at
  `/opt/homebrew/bin/gog` is NEVER called by any verb. External sheet
  content (cell values, formulas, sheet titles) is always screened for
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

Wire-up rules (mirror the P2-01 Gmail / P2-03 Drive / P2-04 Docs files
exactly):

  - The wrapper is the ONLY subprocess call site. Each verb picks the
    sub-verb, folds root-level flags into the extras list, translates
    the small handful of mineru→gog argument shapes that differ (see
    below), and hands the argv to `run_gog_firewall`.
  - Extras (everything Typer didn't consume) pass straight through to
    gog-firewall, so engine flags like `--json` / `--dimension` /
    `--render` / `--input` / `--insert` / `--values-json` /
    `--format-json` / `--format-fields` / `--no-input` all work with
    zero wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json sheets metadata <id>`
    behaves the same as `mineru sheets metadata <id> --json`.
    Idempotent: if the user also passed the flag as a trailing extra
    we don't duplicate it.
  - Exit code from gog-firewall propagates unchanged via `typer.Exit`.

mineru → gog-firewall argument-shape translations (kept small and local
so the raw gog interface stays discoverable):

  - `sheets get <sheetId> [--range <A1>]` → gog requires `<range>` as a
    positional. We accept `--range` for the mineru surface (matches the
    P2-04 task spec) and emit the positional when supplied. When
    `--range` is omitted, no range positional is emitted; gog rejects
    with its own clear "expected <range>" error — the same
    let-gog-report-what's-missing pattern as `drive copy` without
    `--name`.
  - `sheets update <sheetId> --range <A1> --value <v>` → gog's shape is
    `sheets update <sheetId> <range> [<values> ...]`. We emit
    `<sheetId> <A1> <v>`. Power users who need a 2-D value grid can
    still pass `--values-json '[["a","b"],["c","d"]]'` via extras and
    omit `--value`.
  - `sheets append <sheetId> --range <A1> --value <v>` → same shape as
    update: `sheets append <sheetId> <A1> <v>`.
  - `sheets clear <sheetId> --range <A1>` → `sheets clear <sheetId>
    <A1>`. `--range` is required at the mineru layer because gog
    accepts nothing else meaningful for clear; enforcing it at the
    mineru layer gives a nicer error than gog's "expected <range>".
  - `sheets format <sheetId> --range <A1> [--format-json / --format-fields
    via extras]` → `sheets format <sheetId> <A1> [extras]`. The two
    format flags (`--format-json` and `--format-fields`) are gog's own
    interface; we don't re-name them at the mineru layer, they simply
    pass through opaquely.

Verb → gog-firewall subverb map:

  Reads (safe to run live; all pass through the injection firewall):
    mineru sheets get <sheetId> [--range A1]  → gog-firewall sheets get <sheetId> [A1]
    mineru sheets metadata <sheetId>          → gog-firewall sheets metadata <sheetId>

  Writes (never executed live during dev/test; tests patch
  run_gog_firewall and assert argv only):
    mineru sheets update <sheetId> --range A --value v → gog-firewall sheets update <sheetId> A v
    mineru sheets append <sheetId> --range A --value v → gog-firewall sheets append <sheetId> A v
    mineru sheets clear  <sheetId> --range A           → gog-firewall sheets clear  <sheetId> A
    mineru sheets format <sheetId> --range A [opts]    → gog-firewall sheets format <sheetId> A [opts]
"""

import typer

from mineru_cli.profile import get_profile
from mineru_cli.wrappers.gog_firewall import run_gog_firewall
from mineru_cli.verbs._helpers import propagate_global_flags as _propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru sheets` app.
# ---------------------------------------------------------------------------

sheets_app = typer.Typer(
    name="sheets",
    help=(
        "Google Sheets: firewall-preserving reads and writes via gog-firewall. "
        "Every verb routes through the injection firewall; raw `gog` is never called."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@sheets_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def get(
    ctx: typer.Context,
    sheet_id: str = typer.Argument(..., help="Google Sheets spreadsheetId."),
    range_a1: str | None = typer.Option(
        None,
        "--range",
        metavar="A1_RANGE",
        help=(
            "A1-notation range to read (e.g. `Sheet1!A1:B10`). Required by "
            "gog-firewall; when omitted here, gog rejects with a clear "
            "'expected <range>' error naming what's missing."
        ),
    ),
) -> None:
    """Get values from a range (wraps `gog-firewall sheets get`).

    Firewall-preserving. Extras pass through so gog's `--dimension
    ROWS|COLUMNS`, `--render FORMATTED_VALUE|UNFORMATTED_VALUE|FORMULA`,
    and `--json` all work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["sheets", "get", sheet_id]
    if range_a1 is not None:
        argv.append(range_a1)
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@sheets_app.command(
    "metadata",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def metadata(
    ctx: typer.Context,
    sheet_id: str = typer.Argument(..., help="Google Sheets spreadsheetId."),
) -> None:
    """Get spreadsheet metadata (wraps `gog-firewall sheets metadata`).

    Firewall-preserving. Extras pass through so `--json` works unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["sheets", "metadata", sheet_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# WRITE verbs (must never be executed live during dev/test — tests patch
# run_gog_firewall and assert argv only). the operator invokes them explicitly.
# ---------------------------------------------------------------------------


@sheets_app.command(
    "update",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def update(
    ctx: typer.Context,
    sheet_id: str = typer.Argument(..., help="Google Sheets spreadsheetId."),
    range_a1: str = typer.Option(
        ...,
        "--range",
        metavar="A1_RANGE",
        help="A1-notation range to update (e.g. `Sheet1!A1:B2`).",
    ),
    value: str | None = typer.Option(
        None,
        "--value",
        metavar="VALUE",
        help=(
            "Value to write. gog treats it as `[<values> ...]` — one string can "
            "encode a grid via commas (rows) and pipes (cells), e.g. `a,b|c,d`. "
            "Omit `--value` and pass `--values-json '[[...]]'` via extras for a "
            "structured 2-D grid."
        ),
    ),
) -> None:
    """Update values in a range (WRITE) — wraps `gog-firewall sheets update`.

    Every engine flag passes through opaquely: `--input RAW|USER_ENTERED`
    (default USER_ENTERED), `--values-json '[[...]]'` for a structured
    2-D grid (use in place of `--value`), `--copy-validation-from` to
    copy data-validation rules from another range, and `--no-input`
    for scripted runs.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["sheets", "update", sheet_id, range_a1]
    if value is not None:
        argv.append(value)
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@sheets_app.command(
    "append",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def append(
    ctx: typer.Context,
    sheet_id: str = typer.Argument(..., help="Google Sheets spreadsheetId."),
    range_a1: str = typer.Option(
        ...,
        "--range",
        metavar="A1_RANGE",
        help="A1-notation range to append to (e.g. `Sheet1!A:C`).",
    ),
    value: str | None = typer.Option(
        None,
        "--value",
        metavar="VALUE",
        help=(
            "Value(s) to append. gog treats it as `[<values> ...]` — one string "
            "can encode a grid via commas (rows) and pipes (cells), e.g. "
            "`a,b|c,d`. Omit `--value` and pass `--values-json '[[...]]'` via "
            "extras for a structured 2-D grid."
        ),
    ),
) -> None:
    """Append values to a range (WRITE) — wraps `gog-firewall sheets append`.

    Every engine flag passes through opaquely: `--input
    RAW|USER_ENTERED`, `--insert OVERWRITE|INSERT_ROWS` (append behavior
    when the target range already has values), `--values-json '[[...]]'`
    for a structured 2-D grid, `--copy-validation-from`, `--no-input`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["sheets", "append", sheet_id, range_a1]
    if value is not None:
        argv.append(value)
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@sheets_app.command(
    "clear",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def clear(
    ctx: typer.Context,
    sheet_id: str = typer.Argument(..., help="Google Sheets spreadsheetId."),
    range_a1: str = typer.Option(
        ...,
        "--range",
        metavar="A1_RANGE",
        help="A1-notation range to clear (e.g. `Sheet1!A1:B2`).",
    ),
) -> None:
    """Clear values in a range (WRITE, DESTRUCTIVE) — wraps `gog-firewall sheets clear`.

    Removes cell values in the target range; formatting is left intact.
    Confirm with the operator before invoking (per AGENTS.md External Comms).
    Every engine flag passes through opaquely (`--no-input`, etc.).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["sheets", "clear", sheet_id, range_a1, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@sheets_app.command(
    "format",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def format_(
    ctx: typer.Context,
    sheet_id: str = typer.Argument(..., help="Google Sheets spreadsheetId."),
    range_a1: str = typer.Option(
        ...,
        "--range",
        metavar="A1_RANGE",
        help="A1-notation range to format (e.g. `Sheet1!A1:B2`).",
    ),
) -> None:
    """Apply cell formatting to a range (WRITE) — wraps `gog-firewall sheets format`.

    The format description is supplied via gog's own flags, forwarded as
    extras: `--format-json <cellFormat-json>` (Sheets API CellFormat
    shape) and `--format-fields <mask>` (e.g.
    `userEnteredFormat.textFormat.bold`, or the shorter
    `textFormat.bold`). Extras pass through opaquely so `--no-input`
    also works.

    Example:
      mineru sheets format <sheetId> --range 'Sheet1!A1:B2' \\
        --format-fields 'textFormat.bold' \\
        --format-json '{"textFormat":{"bold":true}}' \\
        --no-input
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(
        ["sheets", "format", sheet_id, range_a1, *extras], ctx=ctx
    )
    raise typer.Exit(code=rc)
