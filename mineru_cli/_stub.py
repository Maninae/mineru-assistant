"""Shared stub helper.

Every verb the foundation increment does NOT wire to a real engine still needs
a Typer command so that `mineru <noun> --help` renders the discoverable
sub-verb listing (per the F1 task done-criteria).

`not_yet_implemented(verb)` prints a uniform notice to stderr and raises
`typer.Exit(code=STUB_EXIT_CODE)` (2) so scripted callers, cron jobs and
shell pipelines checking `$?` see a non-zero result. Otherwise a launchd job
would treat an un-implemented verb as a successful no-op and silently move
on. Exit code 2 matches every other fail-loud message shape in the CLI
(profile errors, missing required args). It is called from the body of every
stubbed command function.
"""

from __future__ import annotations

import typer

# Non-zero, POSIX-conventional "misuse of shell builtins / invocation
# failed" code. Distinct from success (0) so scripted callers detect the
# missing wire-up, and distinct from the wrapper-missing 127 so operators
# can differentiate "engine binary absent" from "verb not implemented".
STUB_EXIT_CODE = 2


def not_yet_implemented(verb: str) -> None:
    """Print the standard 'not yet implemented' notice to stderr and exit 2.

    Used by every stubbed verb body. Foundation is scope-locked to the tree
    skeleton plus a small set of live wire-ups; all other verbs land here so
    `--help` still discovers them.

    Fails loud (exit code 2) rather than returning normally so scripted
    callers (launchd cron jobs, shell pipelines) never treat an
    un-implemented verb as a successful no-op. The notice goes to stderr so
    stdout stays clean for any downstream `| jq` or `| grep` pipeline.
    """
    typer.echo(
        f"mineru {verb}: not yet implemented in foundation increment. "
        "See reports/2026-07-25-mineru-capability-spec.md §7 for the full verb tree.",
        err=True,
    )
    raise typer.Exit(code=STUB_EXIT_CODE)
