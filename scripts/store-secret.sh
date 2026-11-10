#!/bin/bash
# Store a personal secret in macOS Keychain so scripts/serve-secret.py can text it to you later.
#
# Usage (run this yourself in Terminal, not through the agent):
#   scripts/store-secret.sh <name> ["note shown alongside the value, e.g. where the card is"]
#
# Example:
#   scripts/store-secret.sh passport-number "card is in the safe"
#
# A bare trailing `-w` makes `security` prompt for the value (hidden, asked twice), so it never
# appears in argv, shell history, or any file. (Omitting -w entirely would store an EMPTY value.) Re-running with the same name replaces the old value (-U).
# Item lands at service mineru-secret-<name>, account mineru.
set -euo pipefail

name="${1:-}"
note="${2:-}"
account="mineru"
prefix="mineru-secret-"

if [[ -z "$name" ]]; then
    echo "usage: $0 <name> [note]" >&2
    exit 2
fi
if [[ ! "$name" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]]; then
    echo "name must be lowercase letters, digits, hyphens (got '$name')" >&2
    exit 2
fi
if [[ ! -t 0 ]]; then
    echo "refusing: needs an interactive terminal so the value is typed at a hidden prompt" >&2
    exit 2
fi

echo "Storing Keychain item ${prefix}${name} (account ${account})."
echo "Type the value at the prompt; it is hidden and asked twice."
if [[ -n "$note" ]]; then
    security add-generic-password -U -a "$account" -s "${prefix}${name}" -j "$note" -l "Mineru secret: ${name}" -w
else
    security add-generic-password -U -a "$account" -s "${prefix}${name}" -l "Mineru secret: ${name}" -w
fi
echo "Stored. Retrieve any time by asking Mineru to send you '${name}' (it arrives as an iMessage to your own number)."
