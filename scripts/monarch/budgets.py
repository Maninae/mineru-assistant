#!/usr/bin/env python3
"""
List Monarch Money budgets.

Usage:
  python budgets.py [--start 2024-01-01] [--end 2024-01-31] [--format table]
"""

import argparse
import asyncio
import sys
from datetime import datetime
from lib import get_client, output_json

from rich.console import Console
from rich.table import Table

# Custom budget query — the monarchmoney library's get_budgets() query includes
# fields (e.g. budgetSystem) that Monarch's API no longer supports, causing a
# server-side GraphQL error. This slimmer query fetches only what we need.
BUDGET_QUERY = """
query GetBudgetData($startDate: Date!, $endDate: Date!) {
  budgetData(startMonth: $startDate, endMonth: $endDate) {
    monthlyAmountsByCategory {
      category {
        id
        name
        __typename
      }
      monthlyAmounts {
        month
        plannedCashFlowAmount
        actualAmount
        remainingAmount
        __typename
      }
      __typename
    }
    totalsByMonth {
      month
      totalIncome {
        plannedAmount
        actualAmount
        remainingAmount
        __typename
      }
      totalExpenses {
        plannedAmount
        actualAmount
        remainingAmount
        __typename
      }
      __typename
    }
    __typename
  }
}
"""


async def get_budgets_custom(mm, start_date: str, end_date: str):
    """Call budget query directly, bypassing the library's broken version."""
    from gql import gql
    return await mm.gql_call(
        operation="GetBudgetData",
        graphql_query=gql(BUDGET_QUERY),
        variables={"startDate": start_date, "endDate": end_date},
    )


async def list_budgets(start_date: str = None, end_date: str = None, format: str = "json"):
    mm = get_client()

    # Default to current month
    if not start_date:
        today = datetime.now()
        start_date = today.replace(day=1).strftime("%Y-%m-%d")
    if not end_date:
        today = datetime.now()
        if today.month == 12:
            end_date = today.replace(year=today.year + 1, month=1, day=1).strftime("%Y-%m-%d")
        else:
            end_date = today.replace(month=today.month + 1, day=1).strftime("%Y-%m-%d")

    data = await get_budgets_custom(mm, start_date, end_date)

    if format == "table":
        console = Console()
        categories = data.get("budgetData", {}).get("monthlyAmountsByCategory", [])

        table = Table(title=f"Budgets ({start_date} to {end_date})")
        table.add_column("Category")
        table.add_column("Budgeted", justify="right")
        table.add_column("Actual", justify="right")
        table.add_column("Remaining", justify="right")
        table.add_column("Progress")

        for entry in categories:
            cat_name = (entry.get("category") or {}).get("name", "Unknown")
            amounts = entry.get("monthlyAmounts", [{}])
            # Sum across months if multiple
            budgeted = sum(a.get("plannedCashFlowAmount") or 0 for a in amounts)
            actual = sum(abs(a.get("actualAmount") or 0) for a in amounts)
            remaining = budgeted - actual

            if budgeted <= 0:
                continue  # Skip categories with no budget set

            pct = (actual / budgeted) * 100
            if pct > 100:
                progress = f"[red]{pct:.0f}%[/red]"
            elif pct > 80:
                progress = f"[yellow]{pct:.0f}%[/yellow]"
            else:
                progress = f"[green]{pct:.0f}%[/green]"

            remaining_str = f"${remaining:,.2f}" if remaining >= 0 else f"[red]-${abs(remaining):,.2f}[/red]"

            table.add_row(
                cat_name,
                f"${budgeted:,.2f}",
                f"${actual:,.2f}",
                remaining_str,
                progress,
            )
        console.print(table)
    else:
        output_json(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List Monarch Money budgets")
    parser.add_argument("--start", "-s", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", "-e", help="End date (YYYY-MM-DD)")
    parser.add_argument("--format", "-f", choices=["json", "table"], default="json")
    args = parser.parse_args()

    try:
        asyncio.run(list_budgets(args.start, args.end, args.format))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
