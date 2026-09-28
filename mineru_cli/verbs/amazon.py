"""`mineru amazon` sub-app - Amazon order history surface (P2-10 wire-up).

Every verb wraps the live `amazon-orders` CLI at
`$MINERU_HOME/bin/amazon-orders` via
`mineru_cli.wrappers.amazon_orders.run_amazon_orders` (the single
subprocess call site).

SAFETY: READ verbs (`history`, `order`, `invoice`, `transactions`,
`check-session`) are safe live; the amazon-orders session is already
established. WRITE verbs (`login`, `logout`) are MOCK-ONLY in tests --
a live invocation mutates the operator's Amazon cookie jar. `login` is
INTERACTIVE and would hang headlessly.

Root-level `--json` / `--pretty` are deliberately NOT forwarded into
amazon-orders argv (the engine has no such flags on any subverb).
Extras pass through opaquely; exit codes propagate unchanged. Dead
siblings `artifact-detect` / `artifact-remove` are intentionally left
unwrapped per spec §7.
"""

from __future__ import annotations

from typing import List

import typer

from mineru_cli.profile import get_profile
from mineru_cli.wrappers.amazon_orders import run_amazon_orders

# `history --last N` only maps cleanly to amazon-orders' two native
# shortcuts. Anything else is user error; reject at the CLI boundary
# with a clear message rather than forwarding a flag the engine will
# blame amazon-orders for.
_SUPPORTED_LAST_DAYS = (30, 90)

# ---------------------------------------------------------------------------
# Top-level `mineru amazon` app.
# ---------------------------------------------------------------------------

amazon_app = typer.Typer(
    name="amazon",
    help=(
        "Amazon order history (via amazon-orders): reads (history, order, "
        "invoice, transactions, check-session) plus writes (login, logout). "
        "SAFETY: writes are MOCK-ONLY in tests; a live shell invocation IS "
        "a real mutation of the operator's Amazon session cookies."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ===========================================================================
# READ verbs
# ===========================================================================


@amazon_app.command(
    "history",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def history(
    ctx: typer.Context,
    year: int = typer.Option(
        None,
        "--year",
        "-y",
        metavar="YEAR",
        help="Year to fetch order history for (defaults to current year).",
    ),
    last: int = typer.Option(
        None,
        "--last",
        "-l",
        metavar="DAYS",
        help=(
            "Convenience: last N days of history. Only 30 (maps to "
            "`--last-30-days`) and 90 (maps to `--last-3-months`) are "
            "supported; other values fail loud at the CLI boundary."
        ),
    ),
) -> None:
    """List Amazon order history (READ) -- wraps `amazon-orders history`.

    `--year N` picks a specific year; `--last 30` or `--last 90` map to
    amazon-orders' own `--last-30-days` / `--last-3-months` shortcuts.
    Every other engine flag passes through opaquely: `--start-index`,
    `--single-page`, `--full-details`, `--order-filter`.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    if last is not None and last not in _SUPPORTED_LAST_DAYS:
        raise typer.BadParameter(
            f"--last N supports only {_SUPPORTED_LAST_DAYS[0]} or "
            f"{_SUPPORTED_LAST_DAYS[1]} today; got {last}.",
            param_hint="--last",
        )
    extras = list(ctx.args)
    argv: List[str] = ["history"]
    if year is not None:
        argv += ["--year", str(year)]
    if last is not None:
        # Both supported values already validated above; map to the
        # amazon-orders native flag for the shortcut.
        if last == 30:
            argv.append("--last-30-days")
        else:  # last == 90
            argv.append("--last-3-months")
    argv += extras
    rc = run_amazon_orders(argv)
    raise typer.Exit(code=rc)


@amazon_app.command(
    "order",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def order(
    ctx: typer.Context,
    order_id: str = typer.Argument(
        ..., metavar="ORDER_ID", help="Amazon order ID (e.g. 111-1234567-1234567)."
    ),
) -> None:
    """Get details for one Amazon order (READ) -- wraps `amazon-orders order`.

    Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_amazon_orders(["order", order_id, *extras])
    raise typer.Exit(code=rc)


@amazon_app.command(
    "invoice",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def invoice(
    ctx: typer.Context,
    order_id: str = typer.Argument(
        ..., metavar="ORDER_ID", help="Amazon order ID to fetch the invoice for."
    ),
) -> None:
    """Get the invoice for an Amazon order (READ) -- wraps `amazon-orders invoice`.

    Returns the invoice text amazon-orders scrapes off Amazon's
    order-details page. Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_amazon_orders(["invoice", order_id, *extras])
    raise typer.Exit(code=rc)


@amazon_app.command(
    "transactions",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def transactions(ctx: typer.Context) -> None:
    """List Amazon transactions (READ) -- wraps `amazon-orders transactions`.

    Amazon transactions (credit-card charges, gift-card debits) for a
    date range. Extras pass through opaquely so `--days N` works
    unchanged.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_amazon_orders(["transactions", *extras])
    raise typer.Exit(code=rc)


@amazon_app.command(
    "check-session",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def check_session(ctx: typer.Context) -> None:
    """Check if a persisted Amazon session exists (READ) -- wraps `amazon-orders check-session`.

    Reports whether the local cookie jar
    (`~/.config/amazonorders/cookies.json`) still authenticates. Safe
    to invoke live -- pure read against local state.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_amazon_orders(["check-session", *extras])
    raise typer.Exit(code=rc)


# ===========================================================================
# WRITE verbs -- MOCK-ONLY in tests
# ===========================================================================


@amazon_app.command(
    "login",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def login(ctx: typer.Context) -> None:
    """Establish an Amazon session (WRITE, INTERACTIVE) -- wraps `amazon-orders login`.

    Prompts on the real TTY for username / password / OTP and stashes
    the resulting cookies in `~/.config/amazonorders/cookies.json`.
    NEVER executed live during dev / test (headless invocation would
    hang on the password prompt). Extras pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_amazon_orders(["login", *extras])
    raise typer.Exit(code=rc)


@amazon_app.command(
    "logout",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def logout(ctx: typer.Context) -> None:
    """Clear the saved Amazon session (WRITE) -- wraps `amazon-orders logout`.

    Invalidates the cookie jar; a subsequent read will fail until
    `login` runs again. Confirm with the operator before invoking. Extras
    pass through opaquely.
    """
    get_profile(ctx)  # hydrate + fail loud on bogus profile
    extras = list(ctx.args)
    rc = run_amazon_orders(["logout", *extras])
    raise typer.Exit(code=rc)


__all__ = ["amazon_app"]
