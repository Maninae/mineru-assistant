#!/usr/bin/env python3
"""
List holdings for an investment/brokerage account.

Usage:
  python holdings.py --account "Brokerage" [--format table]
  python holdings.py --account-id "abc123" [--format table]
"""

import argparse
import asyncio
import sys
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
        if name_lower in display_name or name_lower in institution:
            return acc.get("id")
    
    # List available accounts if not found
    available = [f"{a.get('displayName')} ({(a.get('institution') or {}).get('name', 'Manual')})" 
                 for a in accounts[:10]]
    raise ValueError(f"No account matching '{account_name}'. Try: {', '.join(available)}...")


async def list_holdings(account_name: str = None, account_id: str = None, format: str = "json"):
    mm = get_client()
    
    # Resolve account name to ID if needed
    if account_name and not account_id:
        account_id = await find_account_id(mm, account_name)
    
    data = await mm.get_account_holdings(account_id)
    
    if format == "table":
        console = Console()
        holdings = data.get("portfolio", {}).get("aggregateHoldings", {}).get("edges", [])
        
        table = Table(title=f"Holdings ({len(holdings)} positions)")
        table.add_column("Ticker", style="cyan")
        table.add_column("Name", max_width=30)
        table.add_column("Quantity", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("Change", justify="right")
        
        total_value = 0
        for edge in holdings:
            h = edge.get("node", {})
            security = h.get("holdings", [{}])[0] if h.get("holdings") else {}
            
            ticker = security.get("ticker") or "-"
            name = (security.get("name") or "Unknown")[:30]
            quantity = h.get("quantity") or 0
            price = security.get("closingPrice") or 0
            value = h.get("totalValue") or 0
            change_pct = h.get("securityPriceChangePercent") or 0
            
            total_value += value
            
            # Format change with color
            if change_pct and change_pct > 0:
                change_str = f"[green]+{change_pct:.2f}%[/green]"
            elif change_pct < 0:
                change_str = f"[red]{change_pct:.2f}%[/red]"
            else:
                change_str = "0.00%"
            
            table.add_row(
                ticker,
                name,
                f"{quantity:,.4f}" if quantity < 100 else f"{quantity:,.2f}",
                f"${price:,.2f}",
                f"${value:,.2f}",
                change_str,
            )
        
        console.print(table)
        console.print(f"\n[bold]Total Value: ${total_value:,.2f}[/bold]")
    else:
        output_json(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List holdings for an investment account")
    parser.add_argument("--account", "-a", help="Account name (partial match)")
    parser.add_argument("--account-id", help="Account ID (exact)")
    parser.add_argument("--format", "-f", choices=["json", "table"], default="json")
    args = parser.parse_args()

    if not args.account and not args.account_id:
        parser.error("Must specify --account or --account-id")

    try:
        asyncio.run(list_holdings(args.account, args.account_id, args.format))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
