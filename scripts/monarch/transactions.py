#!/usr/bin/env python3
"""
List Monarch Money transactions.

Usage:
  python transactions.py [--limit 100] [--start 2024-01-01] [--end 2024-12-31] [--search "Amazon"]
  python transactions.py --account "Amex Gold" --limit 20
  python transactions.py --category "Dining" --start 2026-02-01
"""

import argparse
import asyncio
import sys
from datetime import datetime
from lib import get_client, output_json

from rich.console import Console
from rich.table import Table


async def find_account_id(mm, account_name: str) -> str:
    """Find account ID by name or institution (partial match, case-insensitive)."""
    data = await mm.get_accounts()
    accounts = data.get("accounts", [])
    
    name_lower = account_name.lower()
    for acc in accounts:
        display_name = acc.get("displayName", "").lower()
        institution = (acc.get("institution") or {}).get("name", "").lower()
        # Match against display name or institution
        if name_lower in display_name or name_lower in institution:
            return acc.get("id")
    
    # List available accounts if not found
    available = [f"{a.get('displayName')} ({(a.get('institution') or {}).get('name', 'Manual')})" 
                 for a in accounts[:10]]
    raise ValueError(f"No account matching '{account_name}'. Available: {', '.join(available)}...")


async def find_category_id(mm, category_name: str) -> str:
    """Find category ID by name (partial match, case-insensitive)."""
    data = await mm.get_transaction_categories()
    categories = data.get("categories", [])
    
    name_lower = category_name.lower()
    for cat in categories:
        cat_name = cat.get("name", "").lower()
        if name_lower in cat_name:
            return cat.get("id")
    
    available = [c.get("name", "") for c in categories if c.get("name")][:20]
    raise ValueError(f"No category matching '{category_name}'. Available: {', '.join(available)}...")


async def list_transactions(
    limit: int = 100,
    start_date: str = None,
    end_date: str = None,
    search: str = None,
    account: str = None,
    account_id: str = None,
    category: str = None,
    category_id: str = None,
    format: str = "json"
):
    mm = get_client()
    
    kwargs = {"limit": limit}
    # API requires both start and end if either is given
    if start_date and not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if end_date and not start_date:
        start_date = "2000-01-01"
    if start_date:
        kwargs["start_date"] = start_date
    if end_date:
        kwargs["end_date"] = end_date
    if search:
        kwargs["search"] = search
    
    # Resolve account name to ID
    if account and not account_id:
        account_id = await find_account_id(mm, account)
    if account_id:
        kwargs["account_ids"] = [account_id]
    
    # Resolve category name to ID
    if category and not category_id:
        category_id = await find_category_id(mm, category)
    if category_id:
        kwargs["category_ids"] = [category_id]
    
    data = await mm.get_transactions(**kwargs)
    
    if format == "table":
        console = Console()
        txns = data.get("allTransactions", {}).get("results", [])
        table = Table(title=f"Transactions ({len(txns)} shown)")
        table.add_column("Date")
        table.add_column("Merchant", max_width=25)
        table.add_column("Category")
        table.add_column("Amount", justify="right")
        table.add_column("Account", max_width=15)
        
        for t in txns:
            amount = t.get("amount", 0)
            amount_str = f"${abs(amount):,.2f}"
            if amount < 0:
                amount_str = f"[red]-{amount_str}[/red]"
            else:
                amount_str = f"[green]+{amount_str}[/green]"
            
            merchant = (t.get("merchant", {}) or {}).get("name", "")
            if not merchant:
                merchant = t.get("plaidName", "") or t.get("originalName", "")
            
            table.add_row(
                t.get("date", ""),
                merchant[:25],
                (t.get("category", {}) or {}).get("name", ""),
                amount_str,
                (t.get("account", {}) or {}).get("displayName", "")[:15],
            )
        console.print(table)
    else:
        output_json(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List Monarch Money transactions")
    parser.add_argument("--limit", "-l", type=int, default=100)
    parser.add_argument("--start", "-s", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", "-e", help="End date (YYYY-MM-DD)")
    parser.add_argument("--search", "-q", help="Search query")
    parser.add_argument("--account", "-a", help="Filter by account name (partial match)")
    parser.add_argument("--account-id", help="Filter by account ID (exact)")
    parser.add_argument("--category", "-c", help="Filter by category name (partial match)")
    parser.add_argument("--category-id", help="Filter by category ID (exact)")
    parser.add_argument("--format", "-f", choices=["json", "table"], default="json")
    args = parser.parse_args()
    
    try:
        asyncio.run(list_transactions(
            args.limit, args.start, args.end, args.search,
            args.account, args.account_id,
            args.category, args.category_id,
            args.format
        ))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
