"""`mineru brevity` bare verb.

Phase 2 task P2-10 wire-up. The single command here wraps the live
`brevity` CLI at `$MINERU_HOME/bin/brevity` through
`mineru_cli.wrappers.brevity`. `brevity` is a Smart Brevity summarizer
that reads a URL or local file (article, PDF, YouTube video, etc.) and
emits an Axios-style summary on stdout.

Shape choice -- BARE COMMAND, not a group:

  Per §7 of the 2026-07-25 capability spec, brevity is a bare verb
  on the root `mineru` app (`├── brevity <url> [--extended]`),
  parallel to `track`, `grocery`, `trend`, etc. A Typer sub-app
  (group) is deliberately NOT used here because a Click group
  always tries to route the first non-option positional as a
  subcommand name, so `mineru brevity <url> --extended` (the
  natural human-typed order) would fail with "No such command
  '--extended'". A bare `@app.command()` registration takes the
  positional and the `--extended` option cleanly with no ordering
  quirks. The amazon verb file DOES use the sub-app group pattern
  because it has multiple sub-verbs (history / order / invoice /
  ...); the two files are shaped by the domain, not by a uniform
  wrapping.

READ-ONLY (safe to run live):

  brevity is entirely read-only against external URLs and local
  files -- it fetches content (via the `summarize` CLI, which in
  turn drives Firecrawl / yt-dlp / etc.) and writes an Axios-style
  Smart Brevity summary to stdout. It never posts, sends, emails,
  or writes to any external service. Safe to invoke live during
  dev / test.

DEAD SIBLINGS -- DO NOT ADD VERBS FOR THEM:

  Per §7 of the 2026-07-25 capability spec, `$MINERU_HOME/bin/
  artifact-detect` and `$MINERU_HOME/bin/artifact-remove` are DEAD
  (scheduled for removal). This verb file MUST NOT create verbs
  for them; leaving them unwrapped is intentional. The wrapper
  companion (`wrappers/brevity.py`) carries the same note.

Wire-up rules:

  - The wrapper is the ONLY subprocess call site.
  - The positional `<url-or-file>` is a required Typer argument;
    the `--extended` flag is a Typer option that maps 1:1 to the
    engine's own `--extended`.
  - Extras (anything Typer didn't consume) pass straight through to
    brevity, so a future engine flag flows through with zero wrapper
    changes.
  - Root-level `--pretty` / `--json` (recorded on `ctx.obj` by the
    app callback) are DELIBERATELY NOT forwarded into brevity argv.
    The brevity script has no `--json` / `--pretty` flag and its
    `case` block on any unknown option calls `error()` -> `exit 1`
    ("Unknown option: --json"). Same posture as the finance /
    amazon wrappers: swallow the root-level flags at the CLI
    boundary.
  - Exit code from brevity propagates unchanged via `typer.Exit`.
"""

from __future__ import annotations

from typing import List

import typer

from mineru_cli.wrappers.brevity import run_brevity


def register_brevity(app: typer.Typer) -> None:
    """Register the bare `brevity` command on the root Typer app.

    Called once from `app.py` at import time (in place of `add_typer`,
    since brevity is a bare verb not a group -- see the module
    docstring's Shape choice note).
    """

    @app.command(
        "brevity",
        help=(
            "Smart Brevity summarizer (via brevity CLI): read a URL or local file "
            "(article, PDF, YouTube, ...) and emit an Axios-style summary. "
            "READ-ONLY -- fetches external content but never posts / sends / writes."
        ),
        # Panel matches the Band-2 (connectors) grouping in `app.py` so the
        # root `--help` clusters `brevity` with `gmail`, `drive`, ... at the
        # tail (2026-09-16 audit §D).
        rich_help_panel="Connectors",
        context_settings={
            "allow_extra_args": True,
            "ignore_unknown_options": True,
            "help_option_names": ["-h", "--help"],
        },
    )
    def brevity(
        ctx: typer.Context,
        url_or_file: str = typer.Argument(
            ...,
            metavar="URL_OR_FILE",
            help="URL, local file path (article / PDF / video) to summarize.",
        ),
        extended: bool = typer.Option(
            False,
            "--extended",
            help="Detailed ~3-5-min-read summary (default is a ~1-min brief).",
        ),
    ) -> None:
        """Summarize a URL or file in Smart Brevity form (READ) -- wraps `brevity`.

        The default output is a ~1-min-read brief; `--extended` gives the
        ~3-5-min-read detailed form. The underlying `brevity` script
        delegates to the `summarize` CLI with a Smart Brevity prompt
        template. Extras pass through opaquely.
        """
        extras: List[str] = list(ctx.args)
        argv: List[str] = [url_or_file]
        if extended:
            argv.append("--extended")
        argv += extras
        rc = run_brevity(argv)
        raise typer.Exit(code=rc)


__all__ = ["register_brevity"]
