"""`mineru drive` sub-app.

Phase 2 status (P2-03): every Google Drive verb the task lists is wired
end-to-end to the live `gog-firewall` engine at
`$MINERU_HOME/bin/gog-firewall` through
`mineru_cli.wrappers.gog_firewall`. Reads (`ls`, `search`, `get`,
`perms`, `drives`, `download`) and writes (`upload`, `copy`, `mkdir`,
`mv`, `rename`, `rm`, `share`, `unshare`) both route through the SAME
firewalled wrapper — the write verbs (upload/copy/mkdir/mv/rename/rm/
share/unshare) are only ever executed live when the operator explicitly
invokes them, but the wrapper is the same code path so the
firewall-preservation invariant is uniform. §2.1 + §3.1 of the
capability spec define the full surface and the non-negotiable
firewall-preservation rule.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  All Drive verbs in this codebase route through
  `$MINERU_HOME/bin/gog-firewall` (or the path set on
  `MINERU_GOG_FIREWALL_BIN`). The raw `gog` binary at
  `/opt/homebrew/bin/gog` is NEVER called by any verb. External Drive
  content (file names, descriptions, comments) is always screened for
  prompt-injection before it reaches the LLM context on read paths,
  and write paths route through the same firewalled tool so no verb
  ever escapes to the unwrapped binary.

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
  hatches (see prompts/GMAIL.md Gotcha 9 for the sibling Gmail
  discussion), not CLI-level knobs. The grep-based tests in
  tests/test_drive_verbs.py block them from ever landing here as
  string literals.

Wire-up rules (mirror the P2-01 Gmail file exactly):

  - The wrapper is the ONLY subprocess call site for gog-firewall. Each
    verb picks the sub-verb, folds root-level flags into the extras
    list, translates the small handful of mineru→gog argument shapes
    that differ (see below), and hands the argv to `run_gog_firewall`.
  - Extras (everything Typer didn't consume) pass straight through to
    gog-firewall, so engine flags like `--json` / `--max N` / `--page` /
    `--query` / `--parent` / `--force` / `--no-input` / `--role` /
    `--anyone` / `--out` / `--format` all work with zero wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json drive ls` behaves the
    same as `mineru drive ls --json`. Idempotent: if the user also
    passed the flag as a trailing extra we don't duplicate it.
  - Exit code from gog-firewall propagates unchanged via `typer.Exit`.

mineru → gog-firewall argument-shape translations (kept small and local
so the raw gog interface stays discoverable):

  - `drive ls [<folderId>]`   → optional positional becomes `--parent`
    on gog. gog's own default (root) applies when omitted, so no
    positional is emitted when the operator doesn't supply one.
  - `drive perms <fileId>`    → gog's subcommand is `permissions`; the
    short name matches Drive UI parlance.
  - `drive mv <fileId> --to`  → gog's subcommand is `move`; `--to`
    translates to gog's `--parent` (the destination folder).
  - `drive rename <fileId> --name <new>` → gog takes `<newName>` as a
    required positional (not a flag); we accept `--name` for
    readability and emit the positional.
  - `drive rm <fileId>`       → gog's subcommand is `delete` (with `rm`
    and `del` as gog-side aliases); we emit `delete` for the canonical
    argv shape that shows up in tests and logs.
  - `drive copy <fileId> --to <folderId>` → gog requires `<name>` as a
    positional in addition to `--parent <folderId>`. We accept the
    optional `--name <new>` (defaults to the source filename inferred
    by gog when omitted; gog rejects with a clear error if it can't
    infer). `--to` translates to gog's `--parent`.
  - `drive unshare <fileId> <permissionId>` — mirrors gog exactly.
    The task's spec listed `--email X` shorthand, but gog itself takes
    a `<permissionId>` positional; adding an email→permissionId lookup
    would require a second subprocess call site (violating the
    wrapper-is-single-source invariant). The docstring below tells
    operators to run `mineru drive perms <fileId> --json` first to find
    the id for a given email. If the operator wants the shortcut later we'll
    add it deliberately (with a capture helper in the wrapper), not by
    accident here.

Verb → gog-firewall subverb map (kept close to what an operator would
type by hand against gog-firewall directly):

  Reads (safe to run live; all pass through the injection firewall):
    mineru drive ls [<folderId>]         → gog-firewall drive ls [--parent <folderId>]
    mineru drive search <query>          → gog-firewall drive search <query>
    mineru drive get <fileId>            → gog-firewall drive get <fileId>
    mineru drive perms <fileId>          → gog-firewall drive permissions <fileId>
    mineru drive drives                  → gog-firewall drive drives
    mineru drive download <fileId>       → gog-firewall drive download <fileId>
    mineru drive url <fileId>...         → gog-firewall drive url <fileId>...

  Writes (never executed live during dev/test; the tests patch
  run_gog_firewall and assert argv only):
    mineru drive upload <path> [--parent] → gog-firewall drive upload <path> [--parent]
    mineru drive copy <fileId> --to <fid> → gog-firewall drive copy <fileId> <name> --parent <fid>
    mineru drive mkdir <name> [--parent]  → gog-firewall drive mkdir <name> [--parent]
    mineru drive mv <fileId> --to <fid>   → gog-firewall drive move <fileId> --parent <fid>
    mineru drive rename <fileId> --name X → gog-firewall drive rename <fileId> X
    mineru drive rm <fileId>              → gog-firewall drive delete <fileId>
    mineru drive share <fileId> ...       → gog-firewall drive share <fileId> ...
    mineru drive unshare <fileId> <pid>   → gog-firewall drive unshare <fileId> <pid>
"""

import typer

from mineru_cli.profile import get_profile
from mineru_cli.wrappers.gog_firewall import run_gog_firewall
from mineru_cli.verbs._helpers import propagate_global_flags as _propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru drive` app.
# ---------------------------------------------------------------------------

drive_app = typer.Typer(
    name="drive",
    help=(
        "Google Drive: firewall-preserving reads and writes via gog-firewall. "
        "Every verb routes through the injection firewall; raw `gog` is never called."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@drive_app.command(
    "ls",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def ls(
    ctx: typer.Context,
    folder_id: str | None = typer.Argument(
        None,
        metavar="[FOLDER_ID]",
        help=(
            "Optional folder ID to list. If omitted, gog-firewall lists the "
            "Drive root. Translated to gog's `--parent <FOLDER_ID>` flag."
        ),
    ),
) -> None:
    """List files in a Drive folder (wraps `gog-firewall drive ls`).

    Firewall-preserving. Extras pass through, so engine flags like
    `--max N` / `--page <token>` / `--query "..."` / `--json` work
    unchanged. If FOLDER_ID is omitted, gog-firewall lists the root
    (its documented default).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    raw_extras = list(ctx.args)
    # Guard: with `ignore_unknown_options=True` Typer will greedily
    # consume a leading `--flag` as the Optional positional folder_id
    # (e.g. `mineru drive ls --json` would otherwise pass `--json` as
    # the folder id). Google Drive IDs never start with `--`, so a
    # `--`-prefixed folder_id is always a flag that belongs in extras.
    if folder_id is not None and folder_id.startswith("--"):
        raw_extras.insert(0, folder_id)
        folder_id = None
    extras = _propagate_global_flags(ctx, raw_extras)
    argv: list[str] = ["drive", "ls"]
    if folder_id is not None:
        argv.extend(["--parent", folder_id])
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "search",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Full-text search query."),
) -> None:
    """Full-text search across Drive (wraps `gog-firewall drive search`).

    Firewall-preserving. Extras pass through so engine flags like
    `--max N` / `--page <token>` / `--json` work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["drive", "search", query, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def get(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Drive fileId."),
) -> None:
    """Get file metadata (wraps `gog-firewall drive get`).

    Firewall-preserving. Extras pass through so `--json` works unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["drive", "get", file_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "perms",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def perms(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Drive fileId to list permissions on."),
) -> None:
    """List permissions on a file (wraps `gog-firewall drive permissions`).

    Short verb name `perms` matches Drive UI parlance; the underlying
    gog-firewall subcommand is the fuller `permissions`. Firewall-preserving.
    Extras pass through so `--max N` / `--page <token>` / `--json` work
    unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["drive", "permissions", file_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "drives",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def drives(ctx: typer.Context) -> None:
    """List shared drives / Team Drives (wraps `gog-firewall drive drives`).

    Firewall-preserving. Extras pass through so `--max N` / `--page` /
    `--query "..."` / `--json` work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["drive", "drives", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "download",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def download(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Drive fileId to download."),
    export_format: str | None = typer.Option(
        None,
        "--format",
        metavar="FORMAT",
        help=(
            "Export format for Google Docs / Sheets / Slides files: "
            "pdf|csv|xlsx|pptx|txt|png|docx. Ignored for native binary files "
            "(gog-firewall downloads them verbatim)."
        ),
    ),
) -> None:
    """Download a file (wraps `gog-firewall drive download`).

    Firewall-preserving. Extras pass through so gog's `--out <path>` and
    any other engine flags work unchanged. For Google-native files
    (Docs / Sheets / Slides) use `--format` to select the export
    format; for uploaded binary files the format is ignored and the
    file downloads verbatim.

    Note: `download` is a READ verb — nothing is modified on Drive. It
    can be run live like the other reads. The written artifact is a
    local file on disk; that's an intended side effect, not a Drive
    mutation.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["drive", "download", file_id]
    if export_format is not None:
        argv.extend(["--format", export_format])
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "url",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def url(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Drive fileId (extras pass more IDs)."),
) -> None:
    """Print the Drive web URL(s) for one or more files (wraps `gog-firewall drive url`).

    Firewall-preserving. Extras pass through so `mineru drive url <fid1> <fid2>`
    becomes `gog-firewall drive url <fid1> <fid2>`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["drive", "url", file_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# WRITE verbs (must never be executed live during dev/test — the tests patch
# run_gog_firewall and assert argv only). the operator invokes them explicitly.
# ---------------------------------------------------------------------------


@drive_app.command(
    "upload",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def upload(
    ctx: typer.Context,
    local_path: str = typer.Argument(..., help="Local file path to upload."),
    parent: str | None = typer.Option(
        None,
        "--parent",
        metavar="FOLDER_ID",
        help="Destination folder ID (defaults to Drive root when omitted).",
    ),
) -> None:
    """Upload a local file to Drive (WRITE, OUTBOUND) — wraps `gog-firewall drive upload`.

    Every engine flag passes through opaquely — `--name <override>`,
    `--no-input`, and any other gog upload flag. Confirm with the operator
    before invoking (per AGENTS.md External Comms) unless the upload
    is already pre-authorized.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["drive", "upload", local_path]
    if parent is not None:
        argv.extend(["--parent", parent])
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "copy",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def copy(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Source Drive fileId to copy."),
    to: str = typer.Option(
        ...,
        "--to",
        metavar="FOLDER_ID",
        help="Destination folder ID (translated to gog's `--parent`).",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        metavar="NEW_NAME",
        help=(
            "Name for the copy. gog-firewall requires a name positional; "
            "when omitted, gog rejects with a clear error naming what's "
            "missing. Pass `--name` explicitly to avoid the round-trip."
        ),
    ),
) -> None:
    """Copy a Drive file to another folder (WRITE) — wraps `gog-firewall drive copy`.

    gog-firewall's argv shape is `drive copy <fileId> <name> --parent <folderId>`,
    so we emit the name positional (from `--name`) and the parent flag
    (from `--to`). Extras pass through so any additional gog copy flag
    (`--no-input`, etc.) works unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["drive", "copy", file_id]
    if name is not None:
        argv.append(name)
    argv.extend(["--parent", to])
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "mkdir",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def mkdir(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Folder name to create."),
    parent: str | None = typer.Option(
        None,
        "--parent",
        metavar="FOLDER_ID",
        help="Parent folder ID (defaults to Drive root when omitted).",
    ),
) -> None:
    """Create a folder in Drive (WRITE) — wraps `gog-firewall drive mkdir`.

    Every engine flag passes through opaquely — `--no-input`, etc.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["drive", "mkdir", name]
    if parent is not None:
        argv.extend(["--parent", parent])
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "mv",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def mv(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Drive fileId to move."),
    to: str = typer.Option(
        ...,
        "--to",
        metavar="FOLDER_ID",
        help="Destination folder ID (translated to gog's `--parent`).",
    ),
) -> None:
    """Move a Drive file to a new parent folder (WRITE) — wraps `gog-firewall drive move`.

    Verb-name translation: `mv` → gog's `move` subcommand; `--to` →
    gog's `--parent`. Every engine flag passes through opaquely
    (`--no-input`, etc.).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["drive", "move", file_id, "--parent", to]
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "rename",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def rename(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Drive fileId to rename."),
    name: str = typer.Option(
        ...,
        "--name",
        metavar="NEW_NAME",
        help=(
            "New name for the file or folder. gog-firewall takes this as a "
            "positional (`<newName>`); we accept `--name` for readability and "
            "emit the positional."
        ),
    ),
) -> None:
    """Rename a Drive file or folder (WRITE) — wraps `gog-firewall drive rename`.

    Every engine flag passes through opaquely (`--no-input`, etc.).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["drive", "rename", file_id, name, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "rm",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def rm(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Drive fileId to trash."),
) -> None:
    """Trash a Drive file (WRITE, DESTRUCTIVE) — wraps `gog-firewall drive delete`.

    Verb-name translation: `rm` → gog's `delete` subcommand (gog itself
    accepts `rm` / `del` as aliases; we emit the canonical `delete` so
    tests and logs show a stable argv shape).

    Drive's `delete` moves the file to the Drive Trash rather than
    permanently removing it, so a mistaken `rm` is recoverable from
    the trash for the retention window Google enforces. Confirm every
    `rm` with the operator before invoking (per AGENTS.md External Comms and
    the workspace's `trash` > `rm` rule); the destructive semantics
    still warrant explicit approval even though it's technically
    trash-bin recoverable.

    Every engine flag passes through opaquely (`--force`, `--no-input`,
    etc.).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["drive", "delete", file_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "share",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def share(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Drive fileId to share."),
    email: str | None = typer.Option(
        None,
        "--email",
        metavar="EMAIL",
        help="Share with a specific user (their Google account email).",
    ),
    role: str | None = typer.Option(
        None,
        "--role",
        metavar="ROLE",
        help="Permission role: reader|writer|commenter (gog default: reader).",
    ),
) -> None:
    """Share a Drive file (WRITE, OUTBOUND) — wraps `gog-firewall drive share`.

    Both `--email` and `--role` pass through unchanged to gog. Other
    gog flags (`--anyone`, `--discoverable`, `--no-input`) also pass
    through as extras. OUTBOUND: sharing typically sends a
    notification email; confirm with the operator before invoking unless the
    share is already pre-authorized.

    Example:
      mineru drive share <fileId> --email alice@example.com --role writer --no-input
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    argv: list[str] = ["drive", "share", file_id]
    if email is not None:
        argv.extend(["--email", email])
    if role is not None:
        argv.extend(["--role", role])
    argv.extend(extras)
    rc = run_gog_firewall(argv, ctx=ctx)
    raise typer.Exit(code=rc)


@drive_app.command(
    "unshare",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def unshare(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="Drive fileId to remove a permission from."),
    permission_id: str = typer.Argument(
        ...,
        help=(
            "Drive permissionId to remove (find via `mineru drive perms "
            "<fileId> --json` — look up the `id` field of the entry whose "
            "`emailAddress` matches the collaborator you want to remove)."
        ),
    ),
) -> None:
    """Remove a share permission from a Drive file (WRITE) — wraps `gog-firewall drive unshare`.

    Mirrors gog's argv shape exactly (`<fileId> <permissionId>`). The
    spec sketch listed `--email X` as shorthand, but adding an
    email→permissionId lookup here would require a second subprocess
    call site and violate the wrapper-is-single-source invariant. Use
    `mineru drive perms <fileId> --json` first to look up the id;
    that's a firewall-preserving read that's safe to run live.

    Every engine flag passes through opaquely (`--no-input`, etc.).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(
        ["drive", "unshare", file_id, permission_id, *extras], ctx=ctx
    )
    raise typer.Exit(code=rc)
