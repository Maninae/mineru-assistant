#!/usr/bin/env python3
"""
List Monarch Money accounts.

Usage:
  python accounts.py [--format table]
"""

import argparse
import asyncio
from lib import get_client, output_json

from rich.console import Console
from rich.table import Table


async def list_accounts(format: str = "json"):
    mm = get_client()
    data = await mm.get_accounts()
    
    if format == "table":
        console = Console()
        accounts = data.get("accounts", [])
        table = Table(title="Accounts")
        table.add_column("ID", style="dim", max_width=12)
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Balance", justify="right")
        table.add_column("Institution")
        
        for acc in accounts:
            table.add_row(
                acc.get("id", "")[:12],
                acc.get("displayName", ""),
                acc.get("type", {}).get("name", ""),
                f"${acc.get('currentBalance', 0):,.2f}",
                acc.get("institution", {}).get("name", "") if acc.get("institution") else "Manual",
            )
        console.print(table)
    else:
        output_json(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List Monarch Money accounts")
    parser.add_argument("--format", "-f", choices=["json", "table"], default="json")
    args = parser.parse_args()
    
    asyncio.run(list_accounts(args.format))
