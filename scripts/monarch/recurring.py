#!/usr/bin/env python3
"""
List recurring transactions (subscriptions, bills, etc.).

Usage:
  python recurring.py [--format table]
"""

import argparse
import asyncio
from lib import get_client, output_json

from rich.console import Console
from rich.table import Table


async def list_recurring(format: str = "json"):
    mm = get_client()
    
    data = await mm.get_recurring_transactions()
    
    if format == "table":
        console = Console()
        items = data.get("recurringTransactionItems", [])
        
        if not items:
            console.print("[yellow]No recurring transactions found[/yellow]")
            return
        
        table = Table(title=f"Recurring Transactions ({len(items)} items)")
        table.add_column("Merchant", max_width=30)
        table.add_column("Category")
        table.add_column("Amount", justify="right")
        table.add_column("Frequency")
        table.add_column("Next Date")
        table.add_column("Account", max_width=20)
        
        monthly_total = 0
        
        for item in items:
            stream = item.get("stream", {}) or {}
            merchant = stream.get("merchant", {}) or {}
            # Category and account are at the item level, not stream level
            category = item.get("category", {}) or {}
            account = item.get("account", {}) or {}

            merchant_name = merchant.get("name", "Unknown")[:30]
            category_name = category.get("name", "-")
            amount = abs(stream.get("amount", 0))
            frequency = stream.get("frequency", "-")
            next_date = item.get("date", "-")
            account_name = account.get("displayName", "-")[:20]
            
            # Estimate monthly amount
            if frequency == "weekly":
                monthly_total += amount * 4.33
            elif frequency == "biweekly":
                monthly_total += amount * 2.17
            elif frequency == "monthly":
                monthly_total += amount
            elif frequency == "quarterly":
                monthly_total += amount / 3
            elif frequency == "yearly" or frequency == "annual":
                monthly_total += amount / 12
            
            # Color code amount
            amount_str = f"[red]${amount:,.2f}[/red]"
            
            table.add_row(
                merchant_name,
                category_name,
                amount_str,
                frequency,
                next_date,
                account_name,
            )
        
        console.print(table)
        console.print(f"\n[bold]Estimated Monthly Total: ${monthly_total:,.2f}[/bold]")
    else:
        output_json(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List recurring transactions")
    parser.add_argument("--format", "-f", choices=["json", "table"], default="json")
    args = parser.parse_args()
    
    asyncio.run(list_recurring(args.format))
