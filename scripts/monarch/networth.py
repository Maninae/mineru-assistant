#!/usr/bin/env python3
"""
Get net worth snapshots over time.

Usage:
  python networth.py [--start 2025-01-01] [--end 2026-02-09] [--format table]
"""

import argparse
import asyncio
from datetime import datetime, timedelta
from lib import get_client, output_json

from rich.console import Console
from rich.table import Table


async def networth_history(start_date: str = None, end_date: str = None, format: str = "json"):
    mm = get_client()
    
    # Default to last 30 days
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        start = datetime.now() - timedelta(days=30)
        start_date = start.strftime("%Y-%m-%d")
    
    data = await mm.get_aggregate_snapshots(start_date=start_date, end_date=end_date)
    
    if format == "table":
        console = Console()
        snapshots = data.get("aggregateSnapshots", [])
        
        if not snapshots:
            console.print("[yellow]No snapshots found for this date range[/yellow]")
            return
        
        # Show summary stats
        first = snapshots[0]
        last = snapshots[-1]
        first_balance = first.get("balance", 0)
        last_balance = last.get("balance", 0)
        change = last_balance - first_balance
        change_pct = (change / first_balance * 100) if first_balance else 0
        
        console.print(f"\n[bold]Net Worth: {first.get('date')} → {last.get('date')}[/bold]\n")
        console.print(f"  Start:  ${first_balance:,.2f}")
        console.print(f"  End:    ${last_balance:,.2f}")
        
        if change >= 0:
            console.print(f"  Change: [green]+${change:,.2f} (+{change_pct:.2f}%)[/green]")
        else:
            console.print(f"  Change: [red]-${abs(change):,.2f} ({change_pct:.2f}%)[/red]")
        
        # Show recent data points (last 10 or weekly samples)
        console.print(f"\n[dim]Showing {min(len(snapshots), 10)} of {len(snapshots)} data points:[/dim]")
        
        table = Table()
        table.add_column("Date")
        table.add_column("Net Worth", justify="right")
        table.add_column("Daily Change", justify="right")
        
        # Sample: take last 10 or every Nth point
        if len(snapshots) <= 10:
            sample = snapshots
        else:
            # Take last 10
            sample = snapshots[-10:]
        
        prev_balance = None
        for s in sample:
            balance = s.get("balance", 0)
            date = s.get("date", "")
            
            if prev_balance is not None:
                daily_change = balance - prev_balance
                if daily_change >= 0:
                    change_str = f"[green]+${daily_change:,.0f}[/green]"
                else:
                    change_str = f"[red]-${abs(daily_change):,.0f}[/red]"
            else:
                change_str = "-"
            
            table.add_row(date, f"${balance:,.2f}", change_str)
            prev_balance = balance
        
        console.print(table)
    else:
        output_json(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Get net worth snapshots over time")
    parser.add_argument("--start", "-s", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", "-e", help="End date (YYYY-MM-DD)")
    parser.add_argument("--format", "-f", choices=["json", "table"], default="json")
    args = parser.parse_args()
    
    asyncio.run(networth_history(args.start, args.end, args.format))
