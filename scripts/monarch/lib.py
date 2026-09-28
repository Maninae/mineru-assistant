"""
Shared Monarch Money client setup.
"""

import asyncio
import json
import pickle
import sys
import warnings
from pathlib import Path

# Suppress gql SSL verification warning (connection is still encrypted)
warnings.filterwarnings("ignore", message=".*AIOHTTPTransport does not verify ssl.*")

# Ensure we use the local venv's monarchmoney (already patched for api.monarch.com)
SCRIPT_DIR = Path(__file__).parent
VENV_PACKAGES = SCRIPT_DIR / ".venv/lib/python3.14/site-packages"
if str(VENV_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_PACKAGES))

from monarchmoney import MonarchMoney

SESSION_FILE = Path.home() / ".monarch" / "session.json"
CONFIG_FILE = Path.home() / ".monarch" / "config.json"


def _load_device_uuid() -> str | None:
    """Load Device UUID from ~/.monarch/config.json if it exists."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
            return config.get("device_uuid")
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def get_client() -> MonarchMoney:
    """Get a MonarchMoney client with saved session loaded (no health check)."""
    mm = MonarchMoney()

    if SESSION_FILE.exists():
        try:
            mm.load_session(str(SESSION_FILE))
        except Exception as e:
            print(f"Error loading session: {e}", file=sys.stderr)
            print("Run: monarch login", file=sys.stderr)
            sys.exit(1)
    else:
        print("Not authenticated. Run: monarch login", file=sys.stderr)
        sys.exit(1)

    # Restore Device-UUID header
    device_uuid = _load_device_uuid()
    if device_uuid:
        mm._headers["Device-UUID"] = device_uuid

    return mm


async def verify_session(mm: MonarchMoney) -> None:
    """Health check: verify session is still valid. Call from async context."""
    try:
        await mm.get_subscription_details()
    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err or "not authenticated" in err.lower():
            print("Session expired or invalid. Run: monarch login", file=sys.stderr)
            sys.exit(1)
        raise


def output_json(data):
    """Print data as formatted JSON."""
    print(json.dumps(data, indent=2, default=str))
