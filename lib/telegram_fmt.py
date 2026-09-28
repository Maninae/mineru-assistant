"""Compatibility shim — telegram_fmt now lives in the Landline package.

The real module is ``landline.telegram_fmt`` in the Landline repo (the
``LANDLINE_REPO`` env var, default ``~/Developer/claude-landline`` — the
extracted public daemon repo; see TOOLS.md → Telegram). This shim keeps
workspace consumers (scripts/deliver-output.py) working unchanged: it loads
the real module and replaces itself in sys.modules, so every attribute —
md_to_telegram_html, bold, italic, code, pre, escape_html, … — resolves on
the genuine module.

telegram_fmt is a leaf module (stdlib-only imports), so this shim preserves
deliver-output.py's isolation rationale: a broken module elsewhere in the
Landline package cannot take down cron deliveries through this import.
"""

import importlib
import os
import sys
from pathlib import Path

# Sibling-repo location seam (§4.6): env override, default ~/Developer/claude-landline.
_LANDLINE_REPO = str(
    Path(os.environ.get("LANDLINE_REPO", str(Path.home() / "Developer" / "claude-landline")))
)
if _LANDLINE_REPO not in sys.path:
    sys.path.insert(0, _LANDLINE_REPO)

sys.modules[__name__] = importlib.import_module("landline.telegram_fmt")
