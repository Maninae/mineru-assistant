"""`mineru memory` sub-app.

Foundation status: `search`, `tags`, and `query` wrap the live msearch
engine at `$MINERU_HOME/bin/msearch` through
`mineru_cli.wrappers.msearch`. The maintenance half (`warm-resume`,
`tree`, `reindex`, `consolidate`, `backup`) is real, implemented in
`mineru_cli.memory_ops`, and operates against the active profile's
`memory_root` / `workspace_absolute`.

Wire-up rules (task F4):

  - The msearch wrapper is the ONLY subprocess call site for msearch.
    This file picks the sub-verb, folds root-level flags into extras,
    and hands the argv down.
  - Extras (everything after the required positional) pass straight
    through to msearch, so engine flags like `--pretty` / `--json` /
    `--count` / `--top N` / `--no-cache` / `--timing` / `--workspace X`
    work with zero wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --pretty memory tags` behaves
    the same as `mineru memory tags --pretty`. Idempotent.
  - Exit code from msearch propagates unchanged via typer.Exit.

Maintenance-verb rules:

  - Every verb reads roots off the active `Profile` (memory_root,
    workspace_absolute, timezone). No hardcoded `.mineru` paths.
  - The verbs never mutate the source tree in an unrecoverable way:
    `backup` writes elsewhere, `consolidate` writes a `.raw.md`
    sibling and the distilled `<YYYY-MM-DD>.md`. `consolidate`'s
    default distiller shells out to headless Claude Code; if the CLI
    is missing or the run fails, the verb exits non-zero and NO
    consolidated `.md` is written, so a failure never corrupts the
    output file.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List, Optional

import typer

from mineru_cli.memory_ops import (
    DEFAULT_BACKUP_SUBDIR,
    DistillerError,
    build_memory_tree,
    build_warm_resume,
    consolidate_daily_fragments,
    create_backup,
    find_dates_missing_consolidation,
)
from mineru_cli.profile import get_profile
from mineru_cli.wrappers.msearch import run_msearch
from mineru_cli.verbs._helpers import propagate_global_flags

memory_app = typer.Typer(
    name="memory",
    help=(
        "Memory & search: msearch (tags + fulltext) plus warm-resume, tree, "
        "reindex, consolidate, backup."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# Thin alias for the shared helper so the call sites keep their local name.
_propagate_global_flags = propagate_global_flags


def _profile_workspace_args(ctx: typer.Context) -> List[str]:
    """Return `["--workspace", <profile.workspace_absolute>]`, or [].

    Isolation rule (Phase 1 security fix): every `mineru memory` engine
    call must run against the ACTIVE profile's workspace, not the live
    default `$MINERU_HOME`. Otherwise `mineru --profile alice memory search foo`
    silently returns hits from the operator's memory tree — a cross-profile leak
    in a multi-tenant model.

    The `msearch` engine already accepts `--workspace <root>` and
    computes its `memory/`, `reports/`, and tag-index paths from there,
    so we forward the profile's workspace root ONCE, immediately after
    the sub-command verb and BEFORE any user-supplied extras.

    Returns `[]` when no profile is on the context — for unit tests
    that build a Typer context by hand without any profile hydration.
    """
    obj = ctx.obj or {}
    profile = obj.get("profile_obj")
    if profile is None:
        return []
    workspace_root = getattr(profile, "workspace_absolute", None)
    if workspace_root is None:
        return []
    return ["--workspace", str(workspace_root)]


@memory_app.command(
    "search",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def search(
    ctx: typer.Context,
    term: str = typer.Argument(..., help="Keyword to search for across memory tags + fulltext."),
) -> None:
    """Keyword search across memory tags + fulltext (wraps `msearch keyword`)."""
    get_profile(ctx)  # hydrate + fail loud on bogus profile before msearch
    extras = _propagate_global_flags(ctx, list(ctx.args))
    workspace = _profile_workspace_args(ctx)
    rc = run_msearch(["keyword", term, *workspace, *extras])
    raise typer.Exit(code=rc)


@memory_app.command(
    "tags",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tags(ctx: typer.Context) -> None:
    """List / browse all tags across the memory tree (wraps `msearch tags`)."""
    get_profile(ctx)  # hydrate + fail loud on bogus profile before msearch
    extras = _propagate_global_flags(ctx, list(ctx.args))
    workspace = _profile_workspace_args(ctx)
    rc = run_msearch(["tags", *workspace, *extras])
    raise typer.Exit(code=rc)


@memory_app.command(
    "query",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def query(
    ctx: typer.Context,
    question: str = typer.Argument(..., help="Natural-language question."),
) -> None:
    """Natural-language to tag suggestions via local Ollama (wraps `msearch query`)."""
    get_profile(ctx)  # hydrate + fail loud on bogus profile before msearch
    extras = _propagate_global_flags(ctx, list(ctx.args))
    workspace = _profile_workspace_args(ctx)
    rc = run_msearch(["query", question, *workspace, *extras])
    raise typer.Exit(code=rc)


# --------------------------------------------------------------------
# Maintenance verbs (implemented by `mineru_cli.memory_ops`).
# --------------------------------------------------------------------


@memory_app.command("warm-resume")
def warm_resume(ctx: typer.Context) -> None:
    """Emit the session-start context bundle (recent daily memories + today).

    Reads from the active profile's `memory_root/daily/` and prints an
    XML-tagged bundle intended to be dropped straight into a fresh
    agent session's system prompt. Never mutates the tree.
    """
    profile = get_profile(ctx)
    bundle = build_warm_resume(
        Path(profile.memory_root),
        timezone=profile.timezone,
    )
    typer.echo(bundle, nl=False)


@memory_app.command("tree")
def tree(
    ctx: typer.Context,
    include_all: bool = typer.Option(
        False,
        "--include-all",
        help=(
            "Also list files under `daily/` and `monthly/` instead of "
            "collapsing them to a single summary line each."
        ),
    ),
) -> None:
    """Print the annotated memory tree (dir tree + per-file descriptions)."""
    profile = get_profile(ctx)
    summarized = () if include_all else None
    rendered = build_memory_tree(
        Path(profile.memory_root),
        summarized_dirs=summarized,
    )
    typer.echo(rendered, nl=False)


@memory_app.command(
    "reindex",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def reindex(ctx: typer.Context) -> None:
    """Force rebuild the msearch tag index for the active profile.

    Thin wrap over `msearch tags --no-cache`: the msearch engine
    rebuilds its per-workspace index on any command that carries
    `--no-cache`, so pointing it at `tags` for the active profile's
    workspace is enough to refresh a stale index.
    """
    get_profile(ctx)
    extras = _propagate_global_flags(ctx, list(ctx.args))
    workspace = _profile_workspace_args(ctx)
    argv = ["tags", *workspace, "--no-cache", *extras]
    rc = run_msearch(argv)
    raise typer.Exit(code=rc)


@memory_app.command("consolidate")
def consolidate(
    ctx: typer.Context,
    day: Optional[str] = typer.Option(
        None,
        "--date",
        metavar="YYYY-MM-DD",
        help=(
            "Date to (re)consolidate. Defaults to today in the profile's "
            "timezone. Use to REPLAY a specific day after improving the "
            "distiller prompt, or to BACKFILL a day the recurring cron "
            "missed."
        ),
    ),
    missing: bool = typer.Option(
        False,
        "--missing",
        help=(
            "Backfill every day under `daily/` that has session fragments "
            "but no consolidated `<date>.md` yet, oldest first. Mutually "
            "exclusive with --date. Ideal after an outage or fresh clone."
        ),
    ),
) -> None:
    """(Re)consolidate one day, or backfill every missing day.

    A day's session fragments under `<memory_root>/daily/` get
    concatenated into `<YYYY-MM-DD>.raw.md`, then handed to the
    default distiller (headless `claude -p`) to produce the final
    consolidated `<YYYY-MM-DD>.md`.

    Primary role: BACKFILL / REPLAY. The nightly recurring cron in a
    live deployment already consolidates each day as it ends, so this
    CLI is the tool you reach for when:

      - you want to redo a day after improving the distiller prompt
        (`mineru memory consolidate --date 2026-09-10`), or
      - the recurring cron missed a stretch (machine off, auth
        outage) and you need to catch up
        (`mineru memory consolidate --missing`).

    With no flags, "today" is the default target — handy for a manual
    on-demand run before end-of-day.

    Exit codes:

      - 0: at least one consolidated file was written successfully
        (or `--missing` ran with no missing days to process).
      - 1: --date given but the target had no session fragments.
      - 2: bad `--date` value, or `--date` and `--missing` combined.
      - 3: distiller failed on at least one day (`claude` missing on
        PATH, non-zero exit, or timeout). Any earlier successful days
        in a `--missing` run are already written; the failing day's
        raw bundle is preserved but its consolidated `.md` is NOT.
    """
    profile = get_profile(ctx)

    if missing and day is not None:
        typer.echo(
            "mineru memory consolidate: --date and --missing are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(code=2)

    memory_root = Path(profile.memory_root)

    if missing:
        targets = find_dates_missing_consolidation(memory_root)
        if not targets:
            typer.echo(
                "mineru memory consolidate: no days missing consolidation "
                f"under {profile.memory_root}/daily/."
            )
            raise typer.Exit(code=0)
        typer.echo(
            f"Backfilling {len(targets)} missing day(s): "
            f"{targets[0].isoformat()} .. {targets[-1].isoformat()}."
        )
    else:
        if day is None:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            try:
                zone = ZoneInfo(profile.timezone)
            except Exception:
                zone = ZoneInfo("UTC")
            targets = [datetime.now(zone).date()]
        else:
            try:
                targets = [date.fromisoformat(day)]
            except ValueError:
                typer.echo(
                    f"mineru memory consolidate: bad date '{day}' (want YYYY-MM-DD).",
                    err=True,
                )
                raise typer.Exit(code=2)

    any_written = False
    for target in targets:
        try:
            result = consolidate_daily_fragments(memory_root, target)
        except DistillerError as exc:
            typer.echo(
                f"mineru memory consolidate: distiller failed for "
                f"{target.isoformat()}: {exc}",
                err=True,
            )
            raise typer.Exit(code=3)

        if result.fragments_found == 0:
            # In --missing mode, a mid-run empty day is impossible
            # (the helper only lists days that have fragments); treat
            # it as a hard error there. In single-date mode, it's the
            # documented "nothing to do" exit code.
            typer.echo(
                f"mineru memory consolidate: no session fragments for "
                f"{target.isoformat()} under {profile.memory_root}/daily/.",
                err=True,
            )
            raise typer.Exit(code=1)

        typer.echo(
            f"[{target.isoformat()}] wrote raw bundle: {result.raw_bundle_path} "
            f"({result.fragments_found} fragments)."
        )
        typer.echo(f"[{target.isoformat()}] consolidated: {result.consolidated_path}")
        any_written = True

    if not any_written:
        # Defensive: the loop guards above always exit or set the flag,
        # so this is only reachable if `targets` was empty — and the
        # --missing / single-date branches above both handle that.
        raise typer.Exit(code=0)


@memory_app.command("backup")
def backup(
    ctx: typer.Context,
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        help=(
            "Directory to write the archive into. Defaults to "
            "`<workspace>/backups/`."
        ),
    ),
) -> None:
    """Snapshot the profile's memory tree to a timestamped `.tar.gz`.

    Useful as a safety net before any destructive memory op (mass
    dedup, schema migration). Never touches the source tree.
    """
    profile = get_profile(ctx)
    if out is None:
        out = Path(profile.workspace_absolute) / DEFAULT_BACKUP_SUBDIR

    try:
        result = create_backup(
            Path(profile.memory_root),
            backup_dir=out,
            profile_name=profile.name,
        )
    except FileNotFoundError as exc:
        typer.echo(f"mineru memory backup: {exc}", err=True)
        raise typer.Exit(code=1)
    except NotADirectoryError as exc:
        typer.echo(f"mineru memory backup: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Wrote {result.archive_path} ({result.byte_count} bytes, "
        f"{result.file_count} members from {result.source_root})."
    )
