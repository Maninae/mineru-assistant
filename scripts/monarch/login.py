#!/usr/bin/env python3
"""
Login to Monarch Money (interactive, supports MFA + trusted device).
"""

import asyncio
import getpass
import json
import os
import pickle
import sys
from pathlib import Path

# Use the local venv's monarchmoney (already patched)
sys.path.insert(0, str(Path(__file__).parent / ".venv/lib/python3.14/site-packages"))

from monarchmoney import MonarchMoney, RequireMFAException

SESSION_FILE = Path.home() / ".monarch" / "session.json"
CONFIG_FILE = Path.home() / ".monarch" / "config.json"


def load_config() -> dict:
    """Load config from ~/.monarch/config.json."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(config: dict):
    """Save config to ~/.monarch/config.json."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_device_uuid(config: dict) -> str:
    """Get Device UUID from CLI arg, config, or prompt."""
    # Check CLI args
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--device-uuid" and i < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--device-uuid="):
            return arg.split("=", 1)[1]

    # Check config
    if config.get("device_uuid"):
        print(f"Using saved Device UUID from {CONFIG_FILE}")
        return config["device_uuid"]

    # Prompt
    print("\nDevice UUID required for long-lived tokens.")
    print("To get it: Log into app.monarchmoney.com in your browser,")
    print('  open DevTools Console, run: localStorage.getItem("monarchDeviceUUID")')
    print()
    uuid = input("Device UUID: ").strip()
    if not uuid:
        print("Error: Device UUID is required.", file=sys.stderr)
        sys.exit(1)
    return uuid


def save_session(mm):
    """Save session token."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_FILE, "wb") as f:
        pickle.dump({"token": mm._token}, f)


async def main():
    config = load_config()
    device_uuid = get_device_uuid(config)

    mm = MonarchMoney()

    email = input("Email: ")
    password = getpass.getpass("Password: ")

    try:
        await mm.login(
            email, password,
            use_saved_session=False,
            save_session=False,
            device_uuid=device_uuid,
        )
    except RequireMFAException:
        print("MFA required.")
        mfa_code = input("MFA Code: ")
        await mm.multi_factor_authenticate(email, password, mfa_code, device_uuid=device_uuid)

    save_session(mm)

    # Save device UUID to config for future use
    config["device_uuid"] = device_uuid
    save_config(config)

    print("Login successful. Session saved.")


if __name__ == "__main__":
    asyncio.run(main())
