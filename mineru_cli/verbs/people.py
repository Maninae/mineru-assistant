"""`mineru people` sub-app (machine-level human registry).

Phase 1 multi-profile framework. Wired verbs today:
  - `list` — enumerate every human in the registry file at the workspace
             root. Table or JSON output.
  - `path` — print the absolute path where the registry file is expected
             to live (workspace-root-relative).

RENAMED 2026-09-16 (audit §2A F3): this sub-app used to be `mineru humans`.
Renamed to `people` — plain English, matches the audit's stated preference
(and, incidentally, mirrors Google's own People API terminology, which is
why the Google Workspace directory sub-app had to move to `directory` in
the preceding commit of this rename chain). The old `mineru humans`
spelling remains a HIDDEN alias via `humans_alias_app` for the standard
90-day compat window; every invocation under the old name emits a
one-line DEPRECATED notice on stderr through the shared
`_deprecation.emit_rename_notice` helper.

Internal package naming: the underlying Python package
`mineru_cli.humans` (loader, schema, registry types) intentionally keeps
the old name for now — that is the audit's F8 "internal name; users never
see it" pattern, sequenced as a follow-up. Renaming the module in the
same change would cascade into every access-exporter import site with no
UX win.

File-name naming (`humans.yaml` -> `people.yaml`): also a follow-up in
this rename chain (commit 3). Until that lands, the loader reads
`humans.yaml`; after it, the loader prefers `people.yaml` with a
`humans.yaml` fallback (mirrors the `active` / `current` symlink pattern
from commit `c318df6`).

Guest-tier enforcement is DEFERRED (Phase 1 is owner-only); no verb here
touches the Landline daemon.
"""

from __future__ import annotations

import json

import typer

from mineru_cli._deprecation import emit_rename_notice
from mineru_cli.humans import (
    HumansError,
    default_people_yaml_path,
    legacy_humans_yaml_path,
    load_humans_registry,
    resolve_registry_yaml_path,
)

people_app = typer.Typer(
    name="people",
    help=(
        "Machine-level human registry (thin Telegram identities in "
        "<workspace_root>/people.yaml, with a legacy humans.yaml "
        "fallback)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@people_app.command("list")
def list_people(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False, "--json", help="Emit the registry as JSON."
    ),
) -> None:
    """List every human in the registry file.

    Output shape (pretty, default):

        handle  telegram_id   display_name
        sam     123456789     Sam Rivera
        mira    987654321     Mira Rivera
        (2 humans)

    Or JSON with `--json`:

        {"humans": [{"handle": "sam", "telegram_id": 123456789, ...}, ...]}

    The JSON key stays `"humans"` for back-compat with pre-2026-09-16
    consumers (scripts and dashboards that parsed `mineru humans list
    --json` piped output). The verb name changed; the JSON payload
    shape did not.
    """
    obj = ctx.obj or {}
    use_json = bool(json_out or obj.get("json"))
    try:
        registry = load_humans_registry()
    except HumansError as exc:
        typer.echo(f"mineru people list: {exc}", err=True)
        raise typer.Exit(code=2)

    if use_json:
        payload = {
            "humans": [
                {
                    "handle": h.handle,
                    "telegram_id": h.telegram_id,
                    "display_name": h.display_name,
                }
                for h in registry
            ]
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=False))
        return

    if len(registry) == 0:
        typer.echo("(no humans registered)")
        return
    handle_w = max(len("handle"), max(len(h.handle) for h in registry))
    tg_w = max(len("telegram_id"), max(len(str(h.telegram_id)) for h in registry))
    typer.echo(
        f"  {'handle'.ljust(handle_w)}  {'telegram_id'.ljust(tg_w)}  display_name"
    )
    for h in registry:
        typer.echo(
            f"  {h.handle.ljust(handle_w)}  {str(h.telegram_id).ljust(tg_w)}  {h.display_name}"
        )
    typer.echo(f"  ({len(registry)} human{'s' if len(registry) != 1 else ''})")


@people_app.command("path")
def path() -> None:
    """Print the absolute path where the registry file is expected to live.

    Prints the CANONICAL `people.yaml` path (2026-09-16 audit §2A F3
    rename). If a pre-rename `humans.yaml` is present at the workspace
    root but `people.yaml` is not, a second stderr line names the
    legacy fallback path the loader would actually READ so the operator
    is never confused about which file is live.

    Handy for shell composition ("open the file the CLI is going to
    read") and for verifying the workspace-root resolution without
    invoking another verb.
    """
    canonical = default_people_yaml_path()
    typer.echo(str(canonical))
    # If the loader would actually walk the legacy fallback (the
    # canonical file doesn't exist but a pre-rename humans.yaml does),
    # tell the operator so they know their next `people list` reads
    # the legacy file, not the canonical one.
    resolved = resolve_registry_yaml_path()
    if resolved != canonical:
        legacy = legacy_humans_yaml_path()
        typer.echo(
            f"note: canonical people.yaml is absent; loader will read "
            f"the legacy {legacy}. Rename it (or re-run `mineru profile "
            "init`) to migrate.",
            err=True,
        )


# ---------------------------------------------------------------------------
# HIDDEN DEPRECATED alias — `mineru humans ...` (pre-2026-09-16 spelling).
# ---------------------------------------------------------------------------
#
# Standard 90-day compat window (see `mineru_cli/_deprecation.py`). Both
# verbs under the old name fire a one-line DEPRECATED notice on stderr and
# then forward to the same shared body the canonical verb would use
# (`list_people` / `path`). The alias sub-app is registered under
# `name="humans"` with `hidden=True` in `mineru_cli/app.py` so it never
# renders in `mineru --help` yet remains callable for muscle-memory
# continuity (launchd plists / operator shell aliases keep working).

_DEPRECATED_OLD_NAME_LIST = "mineru humans list"
_DEPRECATED_OLD_NAME_PATH = "mineru humans path"
_NEW_NAME_LIST = "mineru people list"
_NEW_NAME_PATH = "mineru people path"

humans_alias_app = typer.Typer(
    name="humans",
    help=(
        "DEPRECATED alias for `mineru people` (machine-level human registry). "
        "Renamed on 2026-09-16; the old name works for ~90 days."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@humans_alias_app.command("list")
def list_alias(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False, "--json", help="Emit the registry as JSON."
    ),
) -> None:
    """DEPRECATED alias for `mineru people list`. Kept for ~90 days."""
    emit_rename_notice(_DEPRECATED_OLD_NAME_LIST, _NEW_NAME_LIST)
    # Delegate through the canonical verb body. Typer commands are plain
    # callables, so re-invoking directly gives identical output shape,
    # exit-code plumbing, and error handling.
    list_people(ctx, json_out=json_out)


@humans_alias_app.command("path")
def path_alias() -> None:
    """DEPRECATED alias for `mineru people path`. Kept for ~90 days."""
    emit_rename_notice(_DEPRECATED_OLD_NAME_PATH, _NEW_NAME_PATH)
    path()
