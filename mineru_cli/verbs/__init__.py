"""Verb sub-apps registered on the root `mineru` Typer app.

Each module in this package defines one Typer sub-app named `<noun>_app`.
The root app (mineru_cli.app) mounts them under their full spelled-out
noun (profile, secrets, memory, gmail, telegram, calendar, imessage).
"""
