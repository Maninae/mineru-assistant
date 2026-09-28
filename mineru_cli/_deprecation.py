"""Shared stderr deprecation-notice helper for renamed CLI names.

Every rename in the 2026-09-16 naming-consolidation pass (see
`reports/2026-09-16-mineru-cli-naming-consolidation-audit.md`) keeps the
OLD name as a HIDDEN Typer alias for ~90 days so existing operators,
launchd plists, cron scripts, and shell aliases keep working while the
canonical name settles. Every call to an old name emits this notice on
stderr; the alias itself still dispatches to the new implementation.

Uniform message shape so operators can grep across every rename with
one pattern (`grep DEPRECATED:`). The message names the OLD spelling,
the NEW canonical form, and the removal window; downstream tests can
also grep for the exact substring.

Applies to verb renames (`profile hydrate` -> `profile install`),
flag renames (`--no-dry-run` -> `--apply`), and future group renames
(`humans` -> `people`, `people` -> `directory`) the audit sequences
after this batch.
"""

from __future__ import annotations

import typer

# Advertised removal window. Kept as a module constant so bumping it
# (or dropping the aliases at the end of the deprecation cycle) is a
# one-line change and every notice picks it up together.
DEPRECATION_WINDOW_DAYS = 90


def emit_rename_notice(old: str, new: str) -> None:
    """Print a one-line 'renamed' notice on stderr.

    `old` is the deprecated spelling (verb subcommand, flag, or group)
    the operator typed; `new` is the canonical replacement. Emit exactly
    one line, no color codes, to stderr so the notice survives
    `2>err.log` and never contaminates a stdout `| jq` pipeline.
    """
    typer.echo(
        f"DEPRECATED: `{old}` was renamed to `{new}`. The old name still "
        f"works but will be removed in ~{DEPRECATION_WINDOW_DAYS} days. "
        "Update your scripts.",
        err=True,
    )
