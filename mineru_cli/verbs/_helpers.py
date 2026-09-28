"""Shared helpers for the `mineru_cli.verbs.*` sub-apps.

Small utilities that every verb file needs. Kept in one place so a change
(new global flag, new propagation rule) lands in one edit instead of the
12 near-identical copies the verb files used to carry.
"""

from __future__ import annotations

from typing import List, Sequence

import typer


def propagate_global_flags(
    ctx: typer.Context, extras: Sequence[str]
) -> List[str]:
    """Fold root-level `--json` / `--pretty` into the trailing extras list.

    The root callback (`mineru_cli.app.root`) records `--json` and `--pretty`
    on `ctx.obj`. Sub-commands that shell out to a live engine forward them
    so `mineru --json <noun> <verb>` behaves the same as
    `mineru <noun> <verb> --json`. Idempotent: if the user also passed the
    flag as a trailing extra we don't duplicate it.

    Args:
        ctx: current Typer context. `ctx.obj` may be None (very early
             failure paths); we tolerate that and treat it as no flags set.
        extras: `list(ctx.args)` from the caller, i.e. everything after the
             verb's declared positional args. Passed as a sequence to make
             it clear we only read it; a fresh list is returned.

    Returns:
        A new list containing every extra plus, at the end, `--pretty` and
        `--json` if the corresponding ctx.obj key is truthy and the flag
        isn't already present.
    """
    opts = ctx.obj or {}
    forwarded = list(extras)
    if opts.get("pretty") and "--pretty" not in forwarded:
        forwarded.append("--pretty")
    if opts.get("json") and "--json" not in forwarded:
        forwarded.append("--json")
    return forwarded
