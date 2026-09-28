"""`mineru gmail` sub-app.

Phase 2 status (P2-01): every user-facing Gmail verb the spec lists is
wired end-to-end to the live gog-firewall engine at
$MINERU_HOME/bin/gog-firewall through `mineru_cli.wrappers.gog_firewall`.
Reads and writes both route through the SAME firewalled wrapper - the
write verbs (`label`, `labels create/modify`, `batch modify/delete`,
`drafts create/update/delete/send`, `send`) are only ever executed live
when the operator explicitly invokes them, but the wrapper is the same code path
so the firewall-preservation invariant is uniform. `settings` and `track`
remain discoverable stubs; they'll be wired in a later increment. §2.1 +
§3.1 of the capability spec define the full surface and the
non-negotiable firewall-preservation rule.

FIREWALL-PRESERVATION CONTRACT (non-negotiable, §0 + §3.1):

  All Gmail verbs in this codebase route through
  `$MINERU_HOME/bin/gog-firewall` (or the path set on
  `MINERU_GOG_FIREWALL_BIN`). The raw `gog` binary at
  `/opt/homebrew/bin/gog` is NEVER called by any verb. External mail
  content is always screened for prompt-injection before it reaches the
  LLM context on read paths, and write paths route through the same
  firewalled tool so no verb ever escapes to the unwrapped binary.

  The single argv[0] source of truth is
  `mineru_cli.wrappers.gog_firewall.build_gog_firewall_argv`. A unit test
  asserts the built argv[0]'s basename resolves to `gog-firewall`, so a
  regression that swapped the binary for bare `gog` would fail the test
  suite immediately.

  Firewall exit codes propagate unchanged: 0 delivered, 77 all units
  blocked (prompt-injection detected everywhere), 78 firewall itself
  errored. Stderr redaction notices (`redacted N of M units`) flow through
  untouched - this file never captures, filters, or reformats them.

  Explicitly out of scope: `--raw`, `--unsafe-strip-invisible`, or any
  other firewall-bypassing flag. Those are workspace-level escape
  hatches (see prompts/GMAIL.md Gotcha 9), not CLI-level knobs. The
  grep-based tests in tests/test_gmail_wrapper.py block them from ever
  landing here as string literals.

Wire-up rules (mirror the foundation `search` verb and the memory/msearch
pattern):

  - The wrapper is the ONLY subprocess call site for gog-firewall. Each
    verb picks the sub-verb, folds root-level flags into the extras
    list, and hands the argv to `run_gog_firewall`.
  - Extras (everything after the required positional) pass straight
    through to gog-firewall, so engine flags like `--max N` / `--json` /
    `--page TOKEN` / `--no-input` / `--to` / `--subject` / `--body-file`
    / `--add` / `--remove` / `--format full` work with zero wrapping.
  - Root-level `--pretty` / `--json` (set on `ctx.obj` by the app
    callback) are propagated so `mineru --json gmail get <id>` behaves
    the same as `mineru gmail get <id> --json`. Idempotent: if the user
    also passed the flag as a trailing extra we don't duplicate it.
  - Exit code from gog-firewall propagates unchanged via typer.Exit.

Verb → gog-firewall subverb map (kept close to what an operator would
type by hand against gog-firewall directly):

  Reads:
    mineru gmail search <query>            → gog-firewall gmail search <query>
    mineru gmail get <messageId>           → gog-firewall gmail get <messageId>
    mineru gmail thread <threadId>         → gog-firewall gmail thread get <threadId>
    mineru gmail thread <tid> --attachments → gog-firewall gmail thread attachments <tid>
    mineru gmail url <threadId>            → gog-firewall gmail url <threadId>
    mineru gmail history                   → gog-firewall gmail history
    mineru gmail attachment <msg> <att>    → gog-firewall gmail attachment <msg> <att>

  Writes (never executed live during dev/test; the tests patch
  run_gog_firewall and assert argv only):
    mineru gmail label <threadId> ...      → gog-firewall gmail thread modify <threadId> ...
    mineru gmail labels list               → gog-firewall gmail labels list
    mineru gmail labels get <name>         → gog-firewall gmail labels get <name>
    mineru gmail labels create <name>      → gog-firewall gmail labels create <name>
    mineru gmail labels modify <tid>...    → gog-firewall gmail labels modify <tid>...
    mineru gmail batch modify <mid>...     → gog-firewall gmail batch modify <mid>...
    mineru gmail batch delete <mid>...     → gog-firewall gmail batch delete <mid>...
    mineru gmail drafts list               → gog-firewall gmail drafts list
    mineru gmail drafts get <did>          → gog-firewall gmail drafts get <did>
    mineru gmail drafts create ...         → gog-firewall gmail drafts create ...
    mineru gmail drafts update <did> ...   → gog-firewall gmail drafts update <did> ...
    mineru gmail drafts delete <did>       → gog-firewall gmail drafts delete <did>
    mineru gmail drafts send <did>         → gog-firewall gmail drafts send <did>
    mineru gmail send --to ...             → gog-firewall gmail send --to ...

  The `label` shorthand routes to `thread modify` (single thread, --add
  / --remove) because that's the by-far common case (`gog-firewall gmail
  thread modify <tid> --add X --remove Y` per GMAIL.md's "mark read"
  example). For multi-thread label ops the caller uses `mineru gmail
  labels modify <tid1> <tid2> --add X` which routes to `gog-firewall
  gmail labels modify`.
"""

import typer

from mineru_cli.profile import get_profile
from mineru_cli.wrappers.gog_firewall import run_gog_firewall
from mineru_cli.verbs._helpers import propagate_global_flags as _propagate_global_flags

# ---------------------------------------------------------------------------
# Top-level `mineru gmail` app.
# ---------------------------------------------------------------------------

gmail_app = typer.Typer(
    name="gmail",
    help=(
        "Gmail: firewall-preserving reads and writes via gog-firewall. "
        "Every verb routes through the injection firewall; raw `gog` is never called."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# READ verbs (safe to run live; all pass through the injection firewall).
# ---------------------------------------------------------------------------


@gmail_app.command(
    "search",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Gmail search query (Gmail query syntax)."),
) -> None:
    """Search threads via the injection firewall (wraps `gog-firewall gmail search`).

    Firewall-preservation contract (§3.1): this verb routes exclusively
    through `gog-firewall`. Raw `gog` is never invoked. Firewall exit
    codes 0 / 77 / 78 propagate unchanged, and stderr redaction notices
    stream through untouched.

    Extra arguments after the query pass straight through to gog-firewall, so
    flags like `--max N` / `--json` / `--page TOKEN` work unchanged. Root-level
    `--pretty` / `--json` are also propagated to the wrapped engine.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "search", query, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@gmail_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def get(
    ctx: typer.Context,
    message_id: str = typer.Argument(..., help="Gmail messageId."),
) -> None:
    """Get a single Gmail message (wraps `gog-firewall gmail get`).

    Firewall-preserving. Extras pass through, so `--format full` /
    `--format metadata` / `--format raw` and `--json` work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "get", message_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@gmail_app.command(
    "thread",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def thread(
    ctx: typer.Context,
    thread_id: str = typer.Argument(..., help="Gmail threadId."),
    attachments: bool = typer.Option(
        False,
        "--attachments",
        help=(
            "List the thread's attachments (routes to `gog-firewall gmail thread "
            "attachments`) instead of fetching the full thread body (`thread get`)."
        ),
    ),
) -> None:
    """Get a full Gmail thread, or list its attachments (firewall-preserving).

    Default routing: `gog-firewall gmail thread get <threadId>` - returns
    every message in the thread. With `--attachments`, routes to
    `gog-firewall gmail thread attachments <threadId>` instead. Extras
    pass through opaquely so engine flags like `--json` /
    `--download-dir` (for attachments) work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    subverb = "attachments" if attachments else "get"
    rc = run_gog_firewall(["gmail", "thread", subverb, thread_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@gmail_app.command(
    "url",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def url(
    ctx: typer.Context,
    thread_id: str = typer.Argument(..., help="Gmail threadId."),
) -> None:
    """Print the Gmail web URL for a thread (wraps `gog-firewall gmail url`).

    Extras pass through so additional threadIds work: `mineru gmail url
    <tid1> <tid2>` becomes `gog-firewall gmail url <tid1> <tid2>`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "url", thread_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@gmail_app.command(
    "history",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def history(ctx: typer.Context) -> None:
    """Gmail history delta (wraps `gog-firewall gmail history`).

    Extras pass through, so `--start-history-id N` / `--json` / etc. all
    work unchanged. Firewall-preserving.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "history", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@gmail_app.command(
    "attachment",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def attachment(
    ctx: typer.Context,
    message_id: str = typer.Argument(..., help="Gmail messageId that owns the attachment."),
    attachment_id: str = typer.Argument(..., help="Gmail attachmentId."),
) -> None:
    """Download a single Gmail attachment (wraps `gog-firewall gmail attachment`).

    Firewall-preserving. Extras pass through, so `--out <path>` and other
    engine flags work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(
        ["gmail", "attachment", message_id, attachment_id, *extras], ctx=ctx
    )
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# WRITE verbs (must never be executed live during dev/test - the tests patch
# run_gog_firewall and assert argv only). the operator invokes them explicitly.
# ---------------------------------------------------------------------------


@gmail_app.command(
    "label",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def label(
    ctx: typer.Context,
    thread_id: str = typer.Argument(..., help="Gmail threadId to modify labels on."),
) -> None:
    """Add or remove labels on a single thread (WRITE).

    Routes to `gog-firewall gmail thread modify <threadId>`. The
    engine-level flags `--add "LabelA,LabelB"` and `--remove UNREAD` pass
    straight through as extras. Add `--no-input` for scripted use to
    skip any interactive prompt.

    Example: `mineru gmail label <tid> --remove UNREAD --no-input`
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "thread", "modify", thread_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@gmail_app.command(
    "send",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def send(ctx: typer.Context) -> None:
    """Send an email (WRITE, OUTBOUND) - wraps `gog-firewall gmail send`.

    Every flag pass through opaquely - `--to`, `--subject`, `--body-file
    -`, `--cc`, `--bcc`, `--reply-to-message-id`, `--thread-id`,
    `--reply-all`, `--attach`, `--from`. Firewall-preserving.

    the operator's convention is draft-first (see GMAIL.md): create a draft
    (`mineru gmail drafts create ...`), review, then send it
    (`mineru gmail drafts send <draftId>`). Use direct `send` only when
    the send has been pre-authorized (self-test, scripted notify).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "send", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# `mineru gmail labels` sub-app: list / get / create / modify.
# ---------------------------------------------------------------------------

labels_app = typer.Typer(
    name="labels",
    help="Gmail label operations: list / get / create (WRITE) / modify (WRITE).",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@labels_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def labels_list(ctx: typer.Context) -> None:
    """List all Gmail labels (wraps `gog-firewall gmail labels list`).

    Read verb; firewall-preserving. Extras pass through so `--json` and
    other engine flags work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "labels", "list", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@labels_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def labels_get(
    ctx: typer.Context,
    name_or_id: str = typer.Argument(..., help="Label name or ID."),
) -> None:
    """Get label details + counts (wraps `gog-firewall gmail labels get`)."""
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "labels", "get", name_or_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@labels_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def labels_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="New label name."),
) -> None:
    """Create a new Gmail label (WRITE) - wraps `gog-firewall gmail labels create`."""
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "labels", "create", name, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@labels_app.command(
    "modify",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def labels_modify(
    ctx: typer.Context,
    thread_id: str = typer.Argument(
        ...,
        help=(
            "Gmail threadId (first of one-or-more; additional threadIds pass "
            "through as extras: `mineru gmail labels modify <tid1> <tid2> --add X`)."
        ),
    ),
) -> None:
    """Modify labels on one or many threads (WRITE).

    Wraps `gog-firewall gmail labels modify <threadId>...`. Extras pass
    through, so additional threadIds and `--add X,Y` / `--remove Z` /
    `--no-input` work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "labels", "modify", thread_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


gmail_app.add_typer(labels_app, name="labels")


# ---------------------------------------------------------------------------
# `mineru gmail batch` sub-app: modify / delete (both WRITE).
# ---------------------------------------------------------------------------

batch_app = typer.Typer(
    name="batch",
    help="Batch operations at the message level: modify labels (WRITE) / delete (WRITE).",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@batch_app.command(
    "modify",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def batch_modify(
    ctx: typer.Context,
    message_id: str = typer.Argument(
        ...,
        help=(
            "Gmail messageId (first of one-or-more; additional messageIds "
            "pass through as extras)."
        ),
    ),
) -> None:
    """Batch modify labels on many messages (WRITE).

    Wraps `gog-firewall gmail batch modify <messageId>...`. Extras pass
    through - additional messageIds, `--add`, `--remove`, `--no-input`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "batch", "modify", message_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@batch_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def batch_delete(
    ctx: typer.Context,
    message_id: str = typer.Argument(
        ...,
        help=(
            "Gmail messageId (first of one-or-more; additional messageIds "
            "pass through as extras). PERMANENT DELETE - no trash."
        ),
    ),
) -> None:
    """Permanently delete messages (WRITE, DESTRUCTIVE).

    Wraps `gog-firewall gmail batch delete <messageId>...`. Extras pass
    through - additional messageIds, `--force`, `--no-input`. Confirm
    every delete with the operator before invoking (per AGENTS.md External
    Comms).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "batch", "delete", message_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


gmail_app.add_typer(batch_app, name="batch")


# ---------------------------------------------------------------------------
# `mineru gmail drafts` sub-app: list / get / create / update / delete / send.
# All write verbs; the operator typically drafts, reviews, then sends.
# ---------------------------------------------------------------------------

drafts_app = typer.Typer(
    name="drafts",
    help=(
        "Gmail draft lifecycle: list / get (READ), create / update / delete / "
        "send (WRITE). Draft-first is the operator's default for outbound mail."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@drafts_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def drafts_list(ctx: typer.Context) -> None:
    """List existing drafts (wraps `gog-firewall gmail drafts list`).

    Read verb; firewall-preserving. Extras pass through, so `--json` and
    `--max N` work unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "drafts", "list", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drafts_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def drafts_get(
    ctx: typer.Context,
    draft_id: str = typer.Argument(..., help="Gmail draftId."),
) -> None:
    """Get a single draft (wraps `gog-firewall gmail drafts get`)."""
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "drafts", "get", draft_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drafts_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def drafts_create(ctx: typer.Context) -> None:
    """Create a draft (WRITE) - wraps `gog-firewall gmail drafts create`.

    Every flag passes through opaquely: `--to`, `--subject`,
    `--body-file -`, `--cc`, `--bcc`, `--body-html`,
    `--reply-to-message-id`, `--reply-to`, `--attach`, `--from`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "drafts", "create", *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drafts_app.command(
    "update",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def drafts_update(
    ctx: typer.Context,
    draft_id: str = typer.Argument(..., help="Gmail draftId to update."),
) -> None:
    """Update an existing draft (WRITE) - wraps `gog-firewall gmail drafts update`.

    Same flag set as `drafts create`, plus every unset flag preserves
    whatever the draft already had.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "drafts", "update", draft_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drafts_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def drafts_delete(
    ctx: typer.Context,
    draft_id: str = typer.Argument(..., help="Gmail draftId to delete."),
) -> None:
    """Delete a draft (WRITE) - wraps `gog-firewall gmail drafts delete`.

    Not destructive at the mailbox level (the underlying messages don't
    move to Trash), but confirm with the operator if the draft represents a
    real reviewed outbound.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "drafts", "delete", draft_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


@drafts_app.command(
    "send",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def drafts_send(
    ctx: typer.Context,
    draft_id: str = typer.Argument(..., help="Gmail draftId to send."),
) -> None:
    """Send a draft (WRITE, OUTBOUND) - wraps `gog-firewall gmail drafts send`.

    The mainline outbound path: create a draft with `drafts create`,
    show the operator, then send it here on approval.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = _propagate_global_flags(ctx, list(ctx.args))
    rc = run_gog_firewall(["gmail", "drafts", "send", draft_id, *extras], ctx=ctx)
    raise typer.Exit(code=rc)


gmail_app.add_typer(drafts_app, name="drafts")
