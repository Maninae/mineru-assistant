"""mineru: unified CLI front door over every workspace capability.

The persona name that appears in `mineru --help` (default `Mineru` in the
seed profile) is read from the active profile's `assistant_name` field, so
a profile with `assistant_name: Alfred` renders "every Alfred capability".
See `mineru_cli.app._ASSISTANT_NAME_FOR_HELP` for the import-time hookup.

Foundation increment: registers the full spelled-out verb tree (profile, secrets,
memory, gmail, telegram, calendar, imessage) so `mineru --help` is discoverable.
Only two read paths are wired to real engines in the foundation:

- `mineru memory search` shells out to $MINERU_HOME/bin/msearch
- `mineru gmail search` shells out to $MINERU_HOME/bin/gog-firewall gmail search

Everything else is a stub that prints a not-yet-implemented notice and exits 0.
The engines under $MINERU_HOME/bin/ are never rewritten (facade-first, per §0 of
the 2026-07-25 capability spec).
"""

__version__ = "0.1.0"
