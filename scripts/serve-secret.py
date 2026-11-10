#!/usr/bin/env python3
"""Serve a Keychain-held personal secret to the operator's own phone over iMessage.

The one-ask retrieval path for numbers the operator needs occasionally but must
never sit in a file, a memory note, a Telegram message, or the agent's context
(SSNs, document numbers, account numbers). The value travels Keychain -> `imsg`
-> the operator's own iMessage thread and nowhere else.

Usage:
    serve-secret.py <name>            # serves Keychain item mineru-secret-<name>
    serve-secret.py --list            # names that exist (no values)

Invariants (the audit depends on every one of these):
- Only Keychain services prefixed `mineru-secret-` are servable, so this script
  cannot be pointed at API tokens or other machine credentials.
- The recipient is the operator's own number from Keychain item
  `mineru-imessage-recipient-owner`. No flag or env var can change it, the
  Keychain account, or the `imsg` binary, so a hijacked caller can at worst
  text the operator their own data.
- Sent as iMessage only (`--service imessage --no-sms-fallback`): no plaintext
  SMS fallback.
- The value is never printed, logged, or echoed, on success or on any failure
  (timeouts and OS errors are caught; no traceback can carry the argv). stdout
  is a one-line receipt; `logs/secret-serve/serve.log` (dir 700, file 600)
  records name, masked recipient, outcome.
- Every message carries the firewall's SERVED_SECRET_MARKER, so any later
  iMessage read through `imsg-firewall` (interactive or a headless sweep)
  redacts the unit instead of surfacing the value.
- Accepted residual: the body is an `imsg` argv for ~1 s, visible in `ps` to
  same-uid processes, which can already read Keychain. `imsg send` has no
  stdin or body-file input.
- Python 3.9 compatible (runs under /usr/bin/python3).

Storing values is a separate, interactive step: `scripts/store-secret.sh`.
"""

import argparse
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
KEYCHAIN_ACCOUNT = "mineru"
SECRET_SERVICE_PREFIX = "mineru-secret-"
RECIPIENT_SERVICE = "mineru-imessage-recipient-owner"
IMSG_BIN = "/opt/homebrew/bin/imsg"
# Must equal scripts/firewall/patterns.py SERVED_SECRET_MARKER (Tier A redaction).
SERVED_SECRET_MARKER = "⟦mineru-secret⟧"
AUDIT_LOG = MINERU_HOME / "logs" / "secret-serve" / "serve.log"
SECRET_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
IMSG_TIMEOUT_SECONDS = 30


def keychain_get(service: str) -> Optional[str]:
    """Return the password field of a Keychain item, or None if absent."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def keychain_comment(service: str) -> Optional[str]:
    """Return the item's comment attribute (non-secret metadata like 'card is in the safe')."""
    # Without -g/-w, `security` prints attributes only; the password never leaves Keychain here.
    result = subprocess.run(
        ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service],
        capture_output=True, text=True,
    )
    match = re.search(r'"icmt"<blob>="(.*)"', result.stdout)
    if match is None or match.group(1) == "<NULL>":
        return None
    return match.group(1)


def list_secret_names() -> List[str]:
    """Names of servable items (the part after the prefix). Values are never touched."""
    result = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True)
    services = re.findall(r'"svce"<blob>="(' + re.escape(SECRET_SERVICE_PREFIX) + r'[^"]+)"', result.stdout)
    return sorted({service[len(SECRET_SERVICE_PREFIX):] for service in services})


def mask_recipient(number: str) -> str:
    """Log-safe recipient: only the last two digits."""
    return "…" + number[-2:]


def append_audit(name: str, recipient_masked: str, outcome: str) -> None:
    """One line per attempt, never a value. Log dir 700, file 600."""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(AUDIT_LOG.parent, 0o700)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    log_fd = os.open(str(AUDIT_LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(AUDIT_LOG, 0o600)
    with os.fdopen(log_fd, "a") as log_file:
        log_file.write(f"{stamp}\t{name}\t{recipient_masked}\t{outcome}\n")


def send_imessage(recipient: str, body: str) -> bool:
    """iMessage-only send. Any failure returns False; nothing about the body is surfaced."""
    try:
        result = subprocess.run(
            [IMSG_BIN, "send", "--to", recipient, "--text", body,
             "--service", "imessage", "--no-sms-fallback"],
            capture_output=True, text=True, timeout=IMSG_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def serve(name: str) -> int:
    """Look up mineru-secret-<name> and text it to the operator. Returns a process exit code."""
    if not SECRET_NAME_PATTERN.fullmatch(name):
        print(f"refusing: '{name}' is not a valid secret name (lowercase letters, digits, hyphens)", file=sys.stderr)
        return 2
    recipient = keychain_get(RECIPIENT_SERVICE)
    if not recipient:
        print(f"refusing: Keychain item {RECIPIENT_SERVICE} (operator's own number) is missing", file=sys.stderr)
        return 2
    service = SECRET_SERVICE_PREFIX + name
    value = keychain_get(service)
    if value is None:
        append_audit(name, mask_recipient(recipient), "missing")
        print(f"no such secret: {name} (run scripts/store-secret.sh {name} to add it)", file=sys.stderr)
        return 1
    # From here on the value is in memory: no exception may escape with it attached.
    try:
        comment = keychain_comment(service)
        body = f"{name}: {value}"
        if comment:
            body += f"\n{comment}"
        body += f"\n{SERVED_SECRET_MARKER} sent by Mineru on request; delete after use"
        sent = send_imessage(recipient, body)
    except BaseException as unexpected:  # noqa: BLE001 - deliberate: never let argv/value reach a traceback
        append_audit(name, mask_recipient(recipient), f"error:{type(unexpected).__name__}")
        print(f"serve failed for {name} ({type(unexpected).__name__}); nothing delivered", file=sys.stderr)
        return 1
    append_audit(name, mask_recipient(recipient), "sent" if sent else "send-failed")
    if not sent:
        print(f"iMessage send failed for {name}; nothing delivered", file=sys.stderr)
        return 1
    print(f"served {name} to {mask_recipient(recipient)} via iMessage")
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Serve a Keychain-held personal secret to the operator's phone over iMessage.")
    parser.add_argument("name", nargs="?", help="secret name (Keychain item mineru-secret-<name>)")
    parser.add_argument("--list", action="store_true", help="list servable secret names (no values)")
    args = parser.parse_args(argv)
    if args.list:
        for name in list_secret_names():
            print(name)
        return 0
    if not args.name:
        parser.print_usage()
        return 2
    return serve(args.name)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
