"""`mineru finance` sub-app - Monarch Money surface (P2-09 wire-up).

Every verb wraps the live `monarch` CLI at `$MINERU_HOME/bin/monarch` via
`mineru_cli.wrappers.monarch.run_monarch` (the single subprocess call
site). Sub-groups: auth, accounts, tx (Monarch's `transactions` group;
mineru surface uses the shorter alias per P2-09), budgets, cashflow,
categories, tags, recurring, institutions.

SAFETY: READ verbs are safe live. WRITE verbs (`auth login/logout`,
`accounts refresh/create/update/delete`, `tx create/update/delete`,
`budgets set`, `categories create/delete`, `tags create/set`) are
MOCK-ONLY in tests -- a live shell invocation IS a real mutation of
the operator's finance account. `auth login` is INTERACTIVE and would hang
headlessly.

Root-level `--json` / `--pretty` are deliberately NOT forwarded into
monarch argv (monarch rejects them; see wrappers/monarch.py for the
full rationale). Extras (everything Typer didn't consume) pass through
opaquely; exit codes propagate unchanged.
"""

from __future__ import annotations

import re

import typer

from mineru_cli.profile import get_profile
from mineru_cli.wrappers.monarch import run_monarch

# Amount tokens accepted by `tx create --amount` and `budgets set AMOUNT`.
# Fail-loud regex guard: monarch's own AMOUNT type is a float, so accepting
# NaN / Infinity from Python's float() coercion silently would send the
# literal strings "nan" / "inf" to a live write. Restricting the surface
# to a plain decimal literal blocks that class of bug at the CLI boundary.
_AMOUNT_TOKEN_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _validate_amount_token(amount: str, *, flag: str) -> None:
    """Raise BadParameter if `amount` isn't a plain decimal literal.

    Rejects NaN / Infinity / non-numeric strings before the value ever
    reaches monarch. Accepts optional leading `-`, integer digits, and
    an optional decimal fraction. Anything else fails loud with a
    hint naming the offending flag / argument.
    """
    if not _AMOUNT_TOKEN_RE.match(amount):
        raise typer.BadParameter(
            f"{flag} must be a plain decimal number (e.g. -12.34, 0, 500); "
            f"got {amount!r}. NaN and Infinity are rejected on purpose.",
            param_hint=flag,
        )


# ---------------------------------------------------------------------------
# Top-level `mineru finance` app.
# ---------------------------------------------------------------------------

finance_app = typer.Typer(
    name="finance",
    help=(
        "Finance (Monarch Money): reads (accounts / tx / budgets / cashflow / "
        "categories / tags / recurring / institutions) plus writes (auth login/"
        "logout, accounts + tx + budgets + categories + tags create/update/"
        "delete). SAFETY: writes are MOCK-ONLY in tests; a live shell "
        "invocation IS a real mutation of the operator's finance account."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ===========================================================================
# `mineru finance auth` sub-app: login / logout (WRITE) / status (READ)
# ===========================================================================

auth_app = typer.Typer(
    name="auth",
    help=(
        "Monarch authentication: login (WRITE, INTERACTIVE), logout (WRITE), "
        "status (READ). `auth login` prompts for email / password / MFA on a "
        "real TTY; do not invoke headlessly."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@auth_app.command(
    "login",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def auth_login(ctx: typer.Context) -> None:
    """Login to Monarch (WRITE, INTERACTIVE) -- wraps `monarch auth login`.

    Prompts on the real TTY for email / password / MFA + trusted-
    device toggle. NEVER executed live during dev / test (headless
    invocation would hang on the password prompt). Extras pass
    through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["auth", "login", *extras])
    raise typer.Exit(code=rc)


@auth_app.command(
    "logout",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def auth_logout(ctx: typer.Context) -> None:
    """Clear the saved Monarch session (WRITE) -- wraps `monarch auth logout`.

    Invalidates the cached session token; a subsequent read will
    fail until `auth login` runs again. Confirm with the operator before
    invoking.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["auth", "logout", *extras])
    raise typer.Exit(code=rc)


@auth_app.command(
    "status",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def auth_status(ctx: typer.Context) -> None:
    """Check auth status (READ) -- wraps `monarch auth status`.

    Reports whether the local session token is present and (if the
    CLI checks) still valid. Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["auth", "status", *extras])
    raise typer.Exit(code=rc)


finance_app.add_typer(auth_app, name="auth")


# ===========================================================================
# `mineru finance accounts` sub-app.
# ===========================================================================

accounts_app = typer.Typer(
    name="accounts",
    help=(
        "Account management: list / get / holdings / history / refresh (WRITE, "
        "triggers real bank sync) / refresh-status / types / create (WRITE) / "
        "update (WRITE) / delete (WRITE, DESTRUCTIVE)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@accounts_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_list(ctx: typer.Context) -> None:
    """List all Monarch accounts (READ) -- wraps `monarch accounts list`.

    Extras pass through opaquely so `--format json|table` works
    unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["accounts", "list", *extras])
    raise typer.Exit(code=rc)


@accounts_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_get(
    ctx: typer.Context,
    account_id: str = typer.Argument(
        ..., metavar="ACCOUNT_ID", help="Monarch account ID."
    ),
) -> None:
    """Get details for one account (READ) -- wraps `monarch accounts get`.

    Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["accounts", "get", account_id, *extras])
    raise typer.Exit(code=rc)


@accounts_app.command(
    "holdings",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_holdings(
    ctx: typer.Context,
    account_id: str = typer.Argument(
        ..., metavar="ACCOUNT_ID", help="Investment account ID."
    ),
) -> None:
    """Get holdings for an investment account (READ) -- wraps `monarch accounts holdings`.

    Extras pass through opaquely so `--format json|table` works unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["accounts", "holdings", account_id, *extras])
    raise typer.Exit(code=rc)


@accounts_app.command(
    "history",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_history(
    ctx: typer.Context,
    account_id: str = typer.Argument(
        ..., metavar="ACCOUNT_ID", help="Account ID to fetch balance history for."
    ),
) -> None:
    """Get balance history for an account (READ) -- wraps `monarch accounts history`.

    Extras pass through opaquely so `--format json|table` works unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["accounts", "history", account_id, *extras])
    raise typer.Exit(code=rc)


@accounts_app.command(
    "refresh",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_refresh(ctx: typer.Context) -> None:
    """Trigger account sync with financial institutions (WRITE) -- wraps `monarch accounts refresh`.

    Kicks off a real refresh against every linked bank / brokerage. Not
    destructive but IS a mutation (creates a job on Monarch's side and
    hits every institution). Extras pass through so `--wait` blocks
    until completion.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["accounts", "refresh", *extras])
    raise typer.Exit(code=rc)


@accounts_app.command(
    "refresh-status",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_refresh_status(ctx: typer.Context) -> None:
    """Check whether an in-flight refresh has completed (READ) -- wraps `monarch accounts refresh-status`.

    Read-only; safe to poll after `accounts refresh` fires (or just to
    peek at whether a scheduled sync finished).
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["accounts", "refresh-status", *extras])
    raise typer.Exit(code=rc)


@accounts_app.command(
    "types",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_types(ctx: typer.Context) -> None:
    """List available account types + subtypes (READ) -- wraps `monarch accounts types`.

    Handy before calling `accounts create` (which requires `--type` from
    this enumerated set). Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["accounts", "types", *extras])
    raise typer.Exit(code=rc)


@accounts_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_create(
    ctx: typer.Context,
    name: str = typer.Option(
        ..., "--name", "-n", metavar="NAME", help="Account name (required by monarch)."
    ),
    type_: str = typer.Option(
        ...,
        "--type",
        "-t",
        metavar="TYPE",
        help="Account type (required; enumerate via `accounts types`).",
    ),
) -> None:
    """Create a manual account (WRITE) -- wraps `monarch accounts create`.

    Only `--name` + `--type` are required at the mineru surface (they
    match monarch's own required flags). Every other engine flag
    passes through opaquely: `--subtype`, `--balance`. Confirm with
    the operator before invoking.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(
        ["accounts", "create", "--name", name, "--type", type_, *extras]
    )
    raise typer.Exit(code=rc)


@accounts_app.command(
    "update",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_update(
    ctx: typer.Context,
    account_id: str = typer.Argument(
        ..., metavar="ACCOUNT_ID", help="Account ID to update."
    ),
) -> None:
    """Update an account's settings (WRITE, partial) -- wraps `monarch accounts update`.

    Every field-mutation flag passes through opaquely: `--name`,
    `--balance`, `--hidden` / `--visible`. Confirm with the operator before
    invoking.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["accounts", "update", account_id, *extras])
    raise typer.Exit(code=rc)


@accounts_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def accounts_delete(
    ctx: typer.Context,
    account_id: str = typer.Argument(
        ..., metavar="ACCOUNT_ID", help="Account ID to delete."
    ),
) -> None:
    """Delete an account (WRITE, DESTRUCTIVE) -- wraps `monarch accounts delete`.

    Removes the account from Monarch entirely; a manual account's
    history is lost. NEVER executed live during dev / test. Confirm
    with the operator before invoking. Extras pass through so `--yes` skips
    the interactive confirmation prompt.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["accounts", "delete", account_id, *extras])
    raise typer.Exit(code=rc)


finance_app.add_typer(accounts_app, name="accounts")


# ===========================================================================
# `mineru finance tx` sub-app (Monarch's `transactions` group).
# The mineru surface uses the shorter `tx` per the P2-09 task; the underlying
# monarch verb is `transactions`.
# ===========================================================================

tx_app = typer.Typer(
    name="tx",
    help=(
        "Transactions: list / get / summary / splits (READ) plus create / "
        "update / delete (WRITE). `finance tx` is the mineru alias for "
        "monarch's `transactions` group."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@tx_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tx_list(ctx: typer.Context) -> None:
    """List transactions with optional filters (READ) -- wraps `monarch transactions list`.

    Extras pass through opaquely: `--limit N` / `--offset N` /
    `--start YYYY-MM-DD` / `--end YYYY-MM-DD` / `--search QUERY` /
    `--accounts a,b` / `--categories c,d` / `--format json|table`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["transactions", "list", *extras])
    raise typer.Exit(code=rc)


@tx_app.command(
    "get",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tx_get(
    ctx: typer.Context,
    transaction_id: str = typer.Argument(
        ..., metavar="TRANSACTION_ID", help="Monarch transaction ID."
    ),
) -> None:
    """Get one transaction (READ) -- wraps `monarch transactions get`.

    Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["transactions", "get", transaction_id, *extras])
    raise typer.Exit(code=rc)


@tx_app.command(
    "summary",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tx_summary(ctx: typer.Context) -> None:
    """Get transaction summary for a date range (READ) -- wraps `monarch transactions summary`.

    Extras pass through opaquely: `--start YYYY-MM-DD` / `--end
    YYYY-MM-DD`. Handy for "how much did we spend on X this month?"
    -style rollups.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["transactions", "summary", *extras])
    raise typer.Exit(code=rc)


@tx_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tx_create(
    ctx: typer.Context,
    date: str = typer.Option(
        ...,
        "--date",
        "-d",
        metavar="YYYY-MM-DD",
        help="Transaction date (required by monarch).",
    ),
    account: str = typer.Option(
        ...,
        "--account",
        "-a",
        metavar="ACCOUNT_ID",
        help="Account ID (required by monarch).",
    ),
    amount: str = typer.Option(
        ...,
        "--amount",
        metavar="AMOUNT",
        help=(
            "Transaction amount as a plain decimal string, e.g. `-12.34` for "
            "an expense or `100` for income (required by monarch). NaN / "
            "Infinity are rejected at the CLI boundary."
        ),
    ),
) -> None:
    """Create a new transaction (WRITE) -- wraps `monarch transactions create`.

    `--date`, `--account`, `--amount` are required (matching
    monarch's own required flags). The `--amount` token is passed
    through verbatim so `100` stays `100` (no `100.0` float
    round-trip) and NaN / Infinity fail loud. Every other engine
    flag passes through opaquely: `--merchant`, `--category`,
    `--notes`. Confirm with the operator before invoking.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    _validate_amount_token(amount, flag="--amount")
    extras = list(ctx.args)
    rc = run_monarch(
        [
            "transactions",
            "create",
            "--date",
            date,
            "--account",
            account,
            "--amount",
            amount,
            *extras,
        ]
    )
    raise typer.Exit(code=rc)


@tx_app.command(
    "update",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tx_update(
    ctx: typer.Context,
    transaction_id: str = typer.Argument(
        ..., metavar="TRANSACTION_ID", help="Transaction ID to update."
    ),
) -> None:
    """Update an existing transaction (WRITE, partial) -- wraps `monarch transactions update`.

    Every field-mutation flag passes through opaquely: `--category`,
    `--merchant`, `--notes`, `--hide` / `--show`. Confirm with the operator
    before invoking.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["transactions", "update", transaction_id, *extras])
    raise typer.Exit(code=rc)


@tx_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tx_delete(
    ctx: typer.Context,
    transaction_id: str = typer.Argument(
        ..., metavar="TRANSACTION_ID", help="Transaction ID to delete."
    ),
) -> None:
    """Delete a transaction (WRITE, DESTRUCTIVE) -- wraps `monarch transactions delete`.

    Removes the transaction record entirely. NEVER executed live
    during dev / test. Confirm with the operator before invoking. Extras pass
    through so `--yes` skips the interactive confirmation prompt.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["transactions", "delete", transaction_id, *extras])
    raise typer.Exit(code=rc)


@tx_app.command(
    "splits",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tx_splits(
    ctx: typer.Context,
    transaction_id: str = typer.Argument(
        ..., metavar="TRANSACTION_ID", help="Transaction ID to fetch splits for."
    ),
) -> None:
    """Get splits for a transaction (READ) -- wraps `monarch transactions splits`.

    Read-only; returns the child transactions a parent was split
    into (e.g. a warehouse-store run split across Groceries and Household).
    Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["transactions", "splits", transaction_id, *extras])
    raise typer.Exit(code=rc)


finance_app.add_typer(tx_app, name="tx")


# ===========================================================================
# `mineru finance budgets` sub-app.
# ===========================================================================

budgets_app = typer.Typer(
    name="budgets",
    help="Budgets: list (READ), set (WRITE, monthly budget amounts per category).",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@budgets_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def budgets_list(ctx: typer.Context) -> None:
    """List all budgets with actual amounts (READ) -- wraps `monarch budgets list`.

    Extras pass through opaquely: `--start YYYY-MM-DD` / `--end
    YYYY-MM-DD` / `--format json|table`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["budgets", "list", *extras])
    raise typer.Exit(code=rc)


@budgets_app.command(
    "set",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def budgets_set(
    ctx: typer.Context,
    category_id: str = typer.Argument(
        ..., metavar="CATEGORY_ID", help="Category ID to set a budget on."
    ),
    amount: str = typer.Argument(
        ...,
        metavar="AMOUNT",
        help=(
            "Budget amount as a plain decimal string (0 clears the budget). "
            "NaN / Infinity are rejected at the CLI boundary."
        ),
    ),
) -> None:
    """Set a budget amount for a category (WRITE) -- wraps `monarch budgets set`.

    Amount=0 clears the budget. The amount is passed through
    verbatim so `500` stays `500` (no float round-trip) and NaN /
    Infinity fail loud. Extras pass through opaquely: `--date
    YYYY-MM-DD` picks the month; `--future` applies the same value
    to future months as well. Confirm with the operator before invoking.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    _validate_amount_token(amount, flag="AMOUNT")
    extras = list(ctx.args)
    rc = run_monarch(["budgets", "set", category_id, amount, *extras])
    raise typer.Exit(code=rc)


finance_app.add_typer(budgets_app, name="budgets")


# ===========================================================================
# `mineru finance cashflow` sub-app (READ-ONLY analytics).
# ===========================================================================

cashflow_app = typer.Typer(
    name="cashflow",
    help=(
        "Cashflow analysis (READ-ONLY): summary (income / expenses / savings / "
        "savings rate), details (per category / group / merchant)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@cashflow_app.command(
    "summary",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def cashflow_summary(ctx: typer.Context) -> None:
    """Get cashflow summary (READ) -- wraps `monarch cashflow summary`.

    Returns income, expenses, savings, and savings rate for a date
    range. Extras pass through opaquely: `--start YYYY-MM-DD` /
    `--end YYYY-MM-DD` / `--format json|table`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["cashflow", "summary", *extras])
    raise typer.Exit(code=rc)


@cashflow_app.command(
    "details",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def cashflow_details(ctx: typer.Context) -> None:
    """Get detailed cashflow (READ) -- wraps `monarch cashflow details`.

    Breaks the summary down by category, category group, and
    merchant. Extras pass through opaquely: `--start YYYY-MM-DD` /
    `--end YYYY-MM-DD`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["cashflow", "details", *extras])
    raise typer.Exit(code=rc)


finance_app.add_typer(cashflow_app, name="cashflow")


# ===========================================================================
# `mineru finance categories` sub-app.
# ===========================================================================

categories_app = typer.Typer(
    name="categories",
    help=(
        "Category management: list (READ), groups (READ), create (WRITE), "
        "delete (WRITE, DESTRUCTIVE)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@categories_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def categories_list(ctx: typer.Context) -> None:
    """List all transaction categories (READ) -- wraps `monarch categories list`.

    Extras pass through opaquely so `--format json|table` works
    unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["categories", "list", *extras])
    raise typer.Exit(code=rc)


@categories_app.command(
    "groups",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def categories_groups(ctx: typer.Context) -> None:
    """List all category groups (READ) -- wraps `monarch categories groups`.

    Category groups are the parent buckets (e.g. "Food & Dining")
    that hold multiple categories (e.g. "Groceries", "Restaurants").
    Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["categories", "groups", *extras])
    raise typer.Exit(code=rc)


@categories_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def categories_create(
    ctx: typer.Context,
    name: str = typer.Argument(
        ..., metavar="NAME", help="New category name (required by monarch)."
    ),
) -> None:
    """Create a new transaction category (WRITE) -- wraps `monarch categories create`.

    `name` is required. Extras pass through opaquely: `--group
    GROUP_ID`, `--icon ICON_NAME`. Confirm with the operator before invoking.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["categories", "create", name, *extras])
    raise typer.Exit(code=rc)


@categories_app.command(
    "delete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def categories_delete(
    ctx: typer.Context,
    category_id: str = typer.Argument(
        ..., metavar="CATEGORY_ID", help="Category ID to delete."
    ),
) -> None:
    """Delete a category (WRITE, DESTRUCTIVE) -- wraps `monarch categories delete`.

    Removes the category entirely; transactions using it become
    uncategorized. NEVER executed live during dev / test. Confirm
    with the operator before invoking. Extras pass through so `--yes` skips
    the interactive confirmation prompt.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["categories", "delete", category_id, *extras])
    raise typer.Exit(code=rc)


finance_app.add_typer(categories_app, name="categories")


# ===========================================================================
# `mineru finance tags` sub-app.
# ===========================================================================

tags_app = typer.Typer(
    name="tags",
    help=(
        "Tag management: list (READ), create (WRITE), set (WRITE, apply tags "
        "to a transaction)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@tags_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tags_list(ctx: typer.Context) -> None:
    """List all transaction tags (READ) -- wraps `monarch tags list`.

    Extras pass through opaquely so `--format json|table` works
    unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["tags", "list", *extras])
    raise typer.Exit(code=rc)


@tags_app.command(
    "create",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tags_create(
    ctx: typer.Context,
    name: str = typer.Argument(
        ..., metavar="NAME", help="New tag name (required by monarch)."
    ),
) -> None:
    """Create a new transaction tag (WRITE) -- wraps `monarch tags create`.

    `name` is required. Extras pass through opaquely: `--color HEX`
    for a color. Confirm with the operator before invoking.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["tags", "create", name, *extras])
    raise typer.Exit(code=rc)


@tags_app.command(
    "set",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tags_set(
    ctx: typer.Context,
    transaction_id: str = typer.Argument(
        ..., metavar="TRANSACTION_ID", help="Transaction ID to apply tags to."
    ),
    tag_ids: str = typer.Argument(
        ...,
        metavar="TAG_IDS",
        help="Comma-separated tag IDs to set on the transaction.",
    ),
) -> None:
    """Set tags on a transaction (WRITE, REPLACES existing) -- wraps `monarch tags set`.

    Replaces the transaction's tag set entirely with `tag_ids`;
    pass an empty string to clear all tags (if the underlying CLI
    supports it). Confirm with the operator before invoking. Extras pass
    through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["tags", "set", transaction_id, tag_ids, *extras])
    raise typer.Exit(code=rc)


finance_app.add_typer(tags_app, name="tags")


# ===========================================================================
# `mineru finance recurring` -- top-level verb that routes to `monarch
# recurring list`. The underlying `recurring` group has only one subverb
# today (`list`). We accept both `finance recurring` and
# `finance recurring list` so muscle memory from the raw monarch CLI keeps
# working: a leading `list` in the extras is dropped before forwarding, so
# neither spelling produces the `recurring list list` argv that monarch
# would reject.
# ===========================================================================


@finance_app.command(
    "recurring",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def recurring(ctx: typer.Context) -> None:
    """List recurring transactions (READ) -- wraps `monarch recurring list`.

    Reports the recurring transactions Monarch has identified plus
    estimated monthly totals. Both `finance recurring` and
    `finance recurring list` work; the trailing `list` (if present)
    is stripped so the argv sent to monarch is always
    `recurring list [extras...]`. Extras pass through opaquely so
    `--format json|table` works unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    # Accept `finance recurring list` (muscle memory from raw monarch CLI)
    # by dropping a leading `list` from the extras; otherwise we'd emit
    # `recurring list list` and monarch would reject the extra positional.
    if extras and extras[0] == "list":
        extras = extras[1:]
    rc = run_monarch(["recurring", "list", *extras])
    raise typer.Exit(code=rc)


# ===========================================================================
# `mineru finance institutions` sub-app (READ-ONLY).
# ===========================================================================

institutions_app = typer.Typer(
    name="institutions",
    help=(
        "Linked institutions (READ-ONLY): list (banks / brokerages), "
        "subscription (Monarch subscription details)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@institutions_app.command(
    "list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def institutions_list(ctx: typer.Context) -> None:
    """List linked financial institutions (READ) -- wraps `monarch institutions list`.

    Shows every bank / brokerage the Monarch account has connected.
    Extras pass through opaquely so `--format json|table` works
    unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["institutions", "list", *extras])
    raise typer.Exit(code=rc)


@institutions_app.command(
    "subscription",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def institutions_subscription(ctx: typer.Context) -> None:
    """Get Monarch subscription details (READ) -- wraps `monarch institutions subscription`.

    Reports the paying-user subscription state (plan, renewal date,
    trial status). Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_monarch(["institutions", "subscription", *extras])
    raise typer.Exit(code=rc)


finance_app.add_typer(institutions_app, name="institutions")


__all__ = ["finance_app"]
