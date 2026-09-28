"""`mineru docs` sub-app.

Phase 2 status (P2-04, docs half): every Google Docs verb the task lists is
wired end-to-end to the live `gog-firewall` engine at
`$MINERU_HOME/bin/gog-firewall` through `mineru_cli.wrappers.gog_firewall`.
Reads (`export`, `info`, `cat`) and writes (`create`, `copy`, `from-html`)
both route through the SAME firewalled wrapper; write verbs are only
executed live when the operator explicitly invokes them, but the wrapper is the
same code path so the firewall-preservation invariant is uniform.

The one atypical verb is `from-html`, which needs a local `pandoc`
HTML→DOCX conversion before the Drive upload. Pandoc is a pure format
converter with no network side effects, so it is not a firewall concern;
the *upload* portion still routes through `run_gog_firewall(["drive",
"upload", ...])`. This mirrors the workspace's `mineru__google-docs-export`
skill workflow exactly.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  All Docs verbs in this codebase route through
  `$MINERU_HOME/bin/gog-firewall` (or the path set on
  `MINERU_GOG_FIREWALL_BIN`). The raw `gog` binary at
  `/opt/homebrew/bin/gog` is NEVER called by any verb. External document
  content (titles, descriptions, plain-text bodies) is always screened
  for prompt-injection before it reaches the LLM context on read paths,
  and write paths route through the same firewalled tool so no verb ever
  escapes to the unwrapped binary.

  The single argv[0] source of truth for gog-firewall subprocess calls
  is `mineru_cli.wrappers.gog_firewall.build_gog_firewall_argv`. The
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
  hatches (see prompts/GMAIL.md Gotcha 9 for the sibling Gmail
  discussion), not CLI-level knobs.

  Pandoc-subprocess carve-out: `from-html` uses `subprocess.run` to
  invoke `pandoc` for local HTML→DOCX conversion. Pandoc is a pure
  local converter; it does NOT reach out to any Google service, does
  not touch external content that needs firewall screening, and cannot
  be a covert path to bare `gog`. The follow-up Drive upload still
  routes through the firewall wrapper. Tests block any string-literal
  reference to `/opt/homebrew/bin/gog` and any firewall-bypass flag in
  this file to keep the carve-out narrow.

Wire-up rules (mirror the P2-01 Gmail / P2-03 Drive files exactly):

  - The wrapper is the ONLY subprocess call site for gog-firewall. Each
    verb picks the sub-verb, folds root-level flags into the extras
    list, translates the small handful of mineru→gog argument shapes
    that differ (see below), and hands the argv to `run_gog_firewall`.
  - Extras (everything Typer didn't consume) pass straight through to
    gog-firewall, so engine flags like `--json` / `--parent` / `--out` /
    `--format` / `--max-bytes` / `--no-input` all work with zero
    wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json docs info <id>` behaves
    the same as `mineru docs info <id> --json`. Idempotent: if the
    user also passed the flag as a trailing extra we don't duplicate it.
  - Exit code from gog-firewall propagates unchanged via `typer.Exit`.

mineru → gog-firewall argument-shape translations (kept small and local
so the raw gog interface stays discoverable):

  - `docs create --title <name>`  → gog takes `<title>` as a required
    positional; we accept `--title` for readability and emit the
    positional.
  - `docs copy <docId> --title <name>` → gog takes `<docId> <title>` as
    positionals; we accept `--title` for the new title and emit the
    positional.
  - `docs export <docId> --format pdf|docx|txt` → gog's `--format` flag
    passes through opaquely. `--out <path>` also passes through via
    extras when the operator wants to control the output path.
  - `docs from-html <path> --name <name>` → mineru-only convenience.
    Runs `pandoc <path> -o <tmp>.docx` locally (optionally with a
    `--reference-doc` for table borders when the operator supplies one
    via extras), then routes the upload through
    `gog-firewall drive upload <tmp>.docx --name <name>.docx`.

Verb → gog-firewall subverb map:

  Reads (safe to run live; all pass through the injection firewall):
    mineru docs export <docId>             → gog-firewall docs export <docId>
    mineru docs info <docId>               → gog-firewall docs info <docId>
    mineru docs cat <docId>                → gog-firewall docs cat <docId>

  Writes (never executed live during dev/test; tests patch
  run_gog_firewall + pandoc and assert argv only):
    mineru docs create --title T           → gog-firewall docs create T
    mineru docs copy <docId> --title T     → gog-firewall docs copy <docId> T
    mineru docs from-html <path> --name N  → pandoc <path> -o /tmp/<name>.docx
                                             then gog-firewall drive upload
                                             /tmp/<name>.docx --name N.docx
"""

import os
import subprocess
import tempfile

import typer

from mineru_cli.profile import get_profile
from mineru_cli.wrappers.gog_firewall import run_gog_firewall
from mineru_cli.verbs._helpers import propagate_global_flags as _propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru docs` app.
# ---------------------------------------------------------------------------

docs_app = typer.Typer(
    name="docs",
    help=(
        "Google Docs: firewall-preserving reads and writes via gog-firewall. "
        "Every verb routes through the injection firewall; raw `gog` is never called. "
        "`from-html` additionally invokes local pandoc for HTML→DOCX conversion "
        "before the Drive upload (upload still goes through the firewall)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

# Pandoc binary path used by `docs from-html`. Overridable via env so tests can
# point at a fake or a non-default install (`brew --prefix`, `nix profile`, …).
# Distinct env from the firewall bin so the two evolve independently.
DEFAULT_PANDOC_BIN = "/opt/homebrew/bin/pandoc"
PANDOC_BIN_ENV = "MINERU_PANDOC_BIN"

# Exit code returned when a required helper binary (pandoc) is missing.
# Matches the wrapper's convention for `gog-firewall` not-found (127 == POSIX
# "command not found") so shell pipelines can special-case both uniformly.
MISSING_BIN_EXIT_CODE = 127


def _resolve_pandoc_bin() -> str:
    """Return the pandoc binary path: env override or documented default.

    An empty-string env var is treated as unset (matches shell semantics for
    tools that check `[ -z "$VAR" ]`).
    """
    override = os.environ.get(PANDOC_BIN_ENV)
    if override:
        return override
    return DEFAULT_PANDOC_BIN


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@docs_app.command(
    "export",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def export(
    ctx: typer.Context,
    doc_id: str = typer.Argument(..., help="Google Docs docId to export."),
    export_format: str | None = typer.Option(
        None,
        "--format",
        metavar="FORMAT",
        help="Export format: pdf|docx|txt (defaults to gog's own default of pdf).",
    ),
) -> None:
    """Export a Google Doc (wraps `gog-firewall docs export`).

    Firewall-preserving. Extras pass through, so gog's `--out <path>` and
    other engine flags work unchanged. When `--format` is omitted, gog's
    own default (`pdf`) applies.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["docs", "export", doc_id]
    if export_format is not None:
        argv.extend(["--format", export_format])
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@docs_app.command(
    "info",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def info(
    ctx: typer.Context,
    doc_id: str = typer.Argument(..., help="Google Docs docId."),
) -> None:
    """Get Doc metadata (wraps `gog-firewall docs info`).

    Firewall-preserving. Extras pass through so `--json` works unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["docs", "info", doc_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@docs_app.command(
    "cat",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def cat(
    ctx: typer.Context,
    doc_id: str = typer.Argument(..., help="Google Docs docId."),
) -> None:
    """Print a Google Doc as plain text (wraps `gog-firewall docs cat`).

    Firewall-preserving. Extras pass through, so gog's `--max-bytes N`
    (0 = unlimited) and `--json` work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["docs", "cat", doc_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# WRITE verbs (must never be executed live during dev/test — the tests patch
# run_gog_firewall and assert argv only). the operator invokes them explicitly.
# ---------------------------------------------------------------------------


@docs_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def create(
    ctx: typer.Context,
    title: str = typer.Option(
        ...,
        "--title",
        metavar="TITLE",
        help=(
            "Title for the new Doc. gog-firewall takes this as a required "
            "positional; we accept `--title` for readability and emit the "
            "positional."
        ),
    ),
) -> None:
    """Create a new Google Doc (WRITE, OUTBOUND) — wraps `gog-firewall docs create`.

    Every engine flag passes through opaquely: `--parent <folderId>` to
    place the new Doc in a specific folder, `--no-input` for scripted
    runs, `--json` for machine-readable output.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["docs", "create", title, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@docs_app.command(
    "copy",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def copy(
    ctx: typer.Context,
    doc_id: str = typer.Argument(..., help="Source Google Docs docId to copy."),
    title: str = typer.Option(
        ...,
        "--title",
        metavar="TITLE",
        help=(
            "New title for the copy. gog-firewall takes this as a required "
            "positional; we accept `--title` for readability and emit the "
            "positional."
        ),
    ),
) -> None:
    """Copy a Google Doc (WRITE) — wraps `gog-firewall docs copy`.

    gog-firewall's argv shape is `docs copy <docId> <title>`, so we emit
    the title positional (from `--title`). Extras pass through so
    `--parent <folderId>` (destination folder) and `--no-input` work
    unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["docs", "copy", doc_id, title, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@docs_app.command(
    "from-html",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def from_html(
    ctx: typer.Context,
    html_path: str = typer.Argument(
        ...,
        metavar="HTML_PATH",
        help="Local path to an HTML file to convert and upload.",
    ),
    name: str = typer.Option(
        ...,
        "--name",
        metavar="NAME",
        help=(
            "Name for the resulting Drive file. `.docx` is appended if the "
            "operator doesn't include an extension, so Drive recognizes the "
            "MIME type and offers 'Open with Google Docs' as expected."
        ),
    ),
    parent: str | None = typer.Option(
        None,
        "--parent",
        metavar="FOLDER_ID",
        help="Destination folder ID (defaults to Drive root when omitted).",
    ),
) -> None:
    """Convert an HTML file to DOCX and upload it to Drive (WRITE, OUTBOUND).

    Two-step workflow, mirroring the `mineru__google-docs-export` skill:
      1. Local pandoc: `pandoc <html_path> -s -o <tmp>.docx`. Pandoc is
         a pure local converter with no network access; running it does
         NOT bypass the injection firewall. Extras that begin with
         `--reference-doc=` are peeled off before the upload step and
         forwarded to pandoc, so operators can supply a bordered
         reference doc for table styling (matching the skill's default).
      2. Firewall upload: `gog-firewall drive upload <tmp>.docx
         --name <name>.docx [--parent <folder>]` — routes through the
         SAME firewalled wrapper as every other write, so the wrapper-
         is-single-source invariant holds for the Drive side.

    Every remaining extra passes through to gog upload opaquely
    (`--no-input`, etc.). Confirm with the operator before invoking (per
    AGENTS.md External Comms) unless the upload is pre-authorized.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    # Peel off pandoc-only extras so they don't get forwarded to gog upload
    # (which would reject them). The only one we support today is
    # `--reference-doc=<path>` / `--reference-doc <path>` (matches the skill's
    # bordered-table workflow). Everything else is treated as an upload flag.
    # A dangling `--reference-doc` with no value at the end of argv is a
    # user error: silently forwarding it to gog upload (as the old code did)
    # confused pandoc-vs-upload routing and made table styling degrade
    # opaquely. Fail loud instead.
    raw_extras = list(ctx.args)
    pandoc_extras: list[str] = []
    upload_extras: list[str] = []
    i = 0
    while i < len(raw_extras):
        tok = raw_extras[i]
        if tok.startswith("--reference-doc="):
            pandoc_extras.append(tok)
            i += 1
        elif tok == "--reference-doc":
            if i + 1 >= len(raw_extras):
                raise typer.BadParameter(
                    "--reference-doc requires a path value"
                )
            pandoc_extras.extend([tok, raw_extras[i + 1]])
            i += 2
        else:
            upload_extras.append(tok)
            i += 1

    # Reject `--name` values that could escape the temp dir via path
    # traversal (e.g. `--name ../etc/passwd`). `os.path.join(tmp_dir, name)`
    # would normalize a `..` segment straight out of the TemporaryDirectory,
    # letting pandoc clobber a file elsewhere on the filesystem AND polluting
    # the Drive-side name.
    if "/" in name or "\\" in name or "\x00" in name:
        raise typer.BadParameter(
            "--name must not contain path separators (/, \\) or NUL characters"
        )

    # Ensure the Drive file name carries `.docx` so Drive knows what MIME to
    # assign and the "Open with Google Docs" convert-in-place path works.
    upload_name = name if name.lower().endswith(".docx") else f"{name}.docx"

    pandoc_bin = _resolve_pandoc_bin()

    # Temp DOCX lives for exactly the pandoc→upload window. TemporaryDirectory
    # self-cleans on exit (including on typer.Exit or a crash), so there is no
    # explicit unlink syscall here — complies with the "never rm / no unlink"
    # rule (SOUL.md / WORKING_STYLE.md).
    with tempfile.TemporaryDirectory(prefix="mineru-docs-from-html-") as tmp_dir:
        tmp_docx = os.path.join(tmp_dir, upload_name)
        pandoc_cmd = [pandoc_bin, html_path, "-s", *pandoc_extras, "-o", tmp_docx]
        try:
            completed = subprocess.run(pandoc_cmd, check=False)
        except FileNotFoundError:
            typer.echo(
                f"mineru docs from-html: pandoc binary not found at {pandoc_bin!r} "
                f"(set {PANDOC_BIN_ENV} to override, default {DEFAULT_PANDOC_BIN!r}).",
                err=True,
            )
            raise typer.Exit(code=MISSING_BIN_EXIT_CODE)

        if completed.returncode != 0:
            typer.echo(
                f"mineru docs from-html: pandoc exited {completed.returncode} "
                f"converting {html_path!r} → {tmp_docx!r}. Aborting upload.",
                err=True,
            )
            raise typer.Exit(code=completed.returncode)

        upload_argv: list[str] = ["drive", "upload", tmp_docx, "--name", upload_name]
        if parent is not None:
            upload_argv.extend(["--parent", parent])
        # Root-level --json / --pretty apply to the visible engine call (the
        # upload), not to the pandoc invocation.
        upload_argv.extend(_propagate_global_flags(ctx, upload_extras))
        rc = run_gog_firewall(upload_argv, ctx=ctx)
        raise typer.Exit(code=rc)
