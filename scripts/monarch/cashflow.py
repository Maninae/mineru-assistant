#!/usr/bin/env python3
"""
Get Monarch Money cashflow summary.

Usage:
  python cashflow.py [--start 2024-01-01] [--end 2024-01-31] [--format table]
"""

import argparse
import asyncio
from datetime import datetime
from lib import get_client, output_json

from rich.console import Console
from rich.table import Table


async def cashflow_summary(start_date: str = None, end_date: str = None, format: str = "json"):
    mm = get_client()
    
    # Default to current month
    if not start_date:
        today = datetime.now()
        start_date = today.replace(day=1).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    
    data = await mm.get_cashflow_summary(start_date=start_date, end_date=end_date)
    
    if format == "table":
        console = Console()
        outer = data.get("summary", [{}])[0] if data.get("summary") else {}
        summary = outer.get("summary", {}) if isinstance(outer, dict) else {}
        
        income = summary.get("sumIncome", 0)
        expenses = abs(summary.get("sumExpense", 0))
        savings = income - expenses
        savings_rate = (savings / income * 100) if income else 0
        
        table = Table(title=f"Cashflow Summary ({start_date} to {end_date})")
        table.add_column("Metric")
        table.add_column("Amount", justify="right")
        
        table.add_row("Income", f"[green]${income:,.2f}[/green]")
        table.add_row("Expenses", f"[red]${expenses:,.2f}[/red]")
        table.add_row("Net Savings", f"${savings:,.2f}")
        table.add_row("Savings Rate", f"{savings_rate:.1f}%")
        
        console.print(table)
    else:
        output_json(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Get Monarch Money cashflow summary")
    parser.add_argument("--start", "-s", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", "-e", help="End date (YYYY-MM-DD)")
    parser.add_argument("--format", "-f", choices=["json", "table"], default="json")
    args = parser.parse_args()
    
    asyncio.run(cashflow_summary(args.start, args.end, args.format))
