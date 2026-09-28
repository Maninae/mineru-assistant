#!/usr/bin/env python3
"""
Boundary Validator — runtime validation for tool output at every layer boundary.

Validates that CLI tool outputs match expected schemas. Runs inline in the pipeline,
warns on stderr, logs violations to cache/boundary_warnings.log. Fast enough to be
invisible to human attention.

Usage (pipe mode — reads stdin, validates, passes through unchanged):
    imsg chats --json | python3 boundary_validator.py --surface imsg.chats | next_step
    imsg history --json | python3 boundary_validator.py --surface imsg.history | next_step

    # After lookup_contact.py resolves names:
    ... | python3 boundary_validator.py --surface imsg_named.chats | next_step
    ... | python3 boundary_validator.py --surface imsg_named.history | next_step

    # After gog CLI:
    gog gmail search ... | python3 boundary_validator.py --surface gog.gmail_search | next_step

    # After gog_cleaner.py:
    ... | python3 boundary_validator.py --surface gog_cleaned.gmail_search | next_step

Surfaces:
    imsg.chats              — raw imsg chats output
    imsg.history            — raw imsg history output
    imsg_named.chats        — after lookup_contact.py (chats)
    imsg_named.history      — after lookup_contact.py (history)
    gog.gmail_search        — raw gog gmail search output
    gog.gmail_get           — raw gog gmail get output
    gog.calendar_events     — raw gog calendar events output
    gog_cleaned.gmail_search — after gog_cleaner.py
    gog_cleaned.calendar_events — after gog_cleaner.py

Design:
    - One file, one entry point, branching by --surface argument
    - Each surface has a validate_* function that checks a parsed JSON object
    - Violations are warnings (stderr + log), never block the pipeline
    - stdin is passed through to stdout unchanged (transparent proxy)
"""

import argparse
import json
import re
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional


# ============================================================================
# Config
# ============================================================================

_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
LOG_FILE = _MINERU_HOME / "cache/boundary_warnings.log"
MAX_LOG_SIZE_BYTES = 500_000  # Rotate after 500KB


# ============================================================================
# Warning Infrastructure
# ============================================================================

_warnings: List[str] = []


def warn(surface: str, message: str, obj: Optional[dict] = None):
    """Record a validation warning."""
    context = ""
    if obj:
        # Include just enough context to identify the problematic record
        obj_id = obj.get("id", obj.get("chat_id", "?"))
        context = f" [id={obj_id}]"
    _warnings.append(f"[{surface}]{context} {message}")


def flush_warnings(surface: str):
    """Write accumulated warnings to stderr and log file."""
    if not _warnings:
        return

    # stderr (visible to the LLM / caller)
    count = len(_warnings)
    sys.stderr.write(f"⚠️  boundary_validator ({surface}): {count} warning(s)\n")
    for w in _warnings[:5]:  # Show max 5 on stderr
        sys.stderr.write(f"   {w}\n")
    if count > 5:
        sys.stderr.write(f"   ... and {count - 5} more (see cache/boundary_warnings.log)\n")

    # Log file (full details)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Simple rotation: truncate if too large
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_SIZE_BYTES:
            LOG_FILE.write_text("")

        with open(LOG_FILE, "a") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n--- {timestamp} | surface={surface} | {count} warning(s) ---\n")
            for w in _warnings:
                f.write(f"  {w}\n")
    except IOError:
        pass  # Don't break the pipeline over logging


# ============================================================================
# Helpers
# ============================================================================

def check_field_exists(obj: dict, field: str, surface: str, expected_type: Optional[type] = None):
    """Check that a field exists and optionally has the expected type."""
    if field not in obj:
        warn(surface, f"missing field '{field}'", obj)
        return False
    if expected_type and not isinstance(obj[field], expected_type):
        actual = type(obj[field]).__name__
        warn(surface, f"field '{field}' expected {expected_type.__name__}, got {actual}", obj)
        return False
    return True


def looks_like_phone(value: str) -> bool:
    """Check if a string looks like a phone number."""
    if not value:
        return False
    return bool(re.match(r'^\+?\d[\d\s\-()]{6,}$', value.strip()))


def looks_like_email(value: str) -> bool:
    """Check if a string looks like an email address."""
    return bool(value and '@' in value and '.' in value)


def looks_like_identifier(value: str) -> bool:
    """Check if a string looks like a raw chat identifier (not human-readable)."""
    if not value:
        return False
    # chat + digits
    if re.match(r'^chat\d+$', value):
        return True
    # 32-char hex
    if len(value) == 32 and all(c in '0123456789abcdef' for c in value.lower()):
        return True
    return False


def looks_like_sms_shortcode(value: str) -> bool:
    """Check if a string looks like an SMS shortcode (e.g. '57685', '57685(smsft)')."""
    if not value:
        return False
    # Strip suffixes like (smsft), (smsft_rm), (smsft_fi)
    clean = re.sub(r'\(smsft[^)]*\)', '', value).strip()
    # Shortcodes are typically 5-6 digits
    return bool(re.match(r'^\d{4,6}$', clean))


def looks_like_group_identifier(identifier: str) -> bool:
    """Check if an identifier represents a group chat (not 1:1, not shortcode)."""
    if not identifier:
        return False
    # Skip phone numbers, emails, SMS shortcodes
    if looks_like_phone(identifier):
        return False
    if looks_like_email(identifier):
        return False
    if looks_like_sms_shortcode(identifier):
        return False
    # What's left should be group identifiers (chat+digits or hex)
    if looks_like_identifier(identifier):
        return True
    return False


def looks_like_iso_timestamp(value: str) -> bool:
    """Check if a string looks like an ISO 8601 timestamp."""
    if not value or not isinstance(value, str):
        return False
    return bool(re.match(r'^\d{4}-\d{2}-\d{2}T', value))


# ============================================================================
# Surface Validators: imsg (raw CLI output)
# ============================================================================

def validate_imsg_chats(obj: dict):
    """Validate a single chat record from `imsg chats --json`."""
    surface = "imsg.chats"
    check_field_exists(obj, "id", surface, int)
    check_field_exists(obj, "identifier", surface, str)
    check_field_exists(obj, "service", surface, str)
    check_field_exists(obj, "last_message_at", surface, str)

    # Timestamp should be ISO format
    ts = obj.get("last_message_at", "")
    if ts and not looks_like_iso_timestamp(ts):
        warn(surface, f"'last_message_at' doesn't look like ISO timestamp: {ts[:30]}", obj)

    # Service should be a known value
    service = obj.get("service", "")
    known_services = {"iMessage", "SMS", "RCS"}
    if service and service not in known_services:
        warn(surface, f"unknown service '{service}' (known: {known_services})", obj)


def validate_imsg_history(obj: dict):
    """Validate a single message record from `imsg history --json`."""
    surface = "imsg.history"
    check_field_exists(obj, "id", surface, int)
    check_field_exists(obj, "chat_id", surface, int)
    check_field_exists(obj, "sender", surface, str)
    check_field_exists(obj, "text", surface)  # Can be str or None
    check_field_exists(obj, "created_at", surface, str)
    check_field_exists(obj, "is_from_me", surface, bool)
    check_field_exists(obj, "guid", surface, str)

    # Sender should look like a phone number or email
    sender = obj.get("sender", "")
    if sender and not looks_like_phone(sender) and not looks_like_email(sender):
        warn(surface, f"'sender' doesn't look like phone/email: {sender[:30]}", obj)

    # Timestamp check
    ts = obj.get("created_at", "")
    if ts and not looks_like_iso_timestamp(ts):
        warn(surface, f"'created_at' doesn't look like ISO timestamp: {ts[:30]}", obj)


# ============================================================================
# Surface Validators: imsg_named (after lookup_contact.py)
# ============================================================================

def validate_imsg_named_chats(obj: dict):
    """Validate a chat record after contact resolution."""
    surface = "imsg_named.chats"

    # First check raw fields are still intact
    check_field_exists(obj, "id", surface, int)
    check_field_exists(obj, "identifier", surface, str)
    check_field_exists(obj, "resolved_name", surface, str)

    resolved = obj.get("resolved_name", "")
    identifier = obj.get("identifier", "")

    # resolved_name should be human-readable, not a raw identifier
    if looks_like_identifier(resolved):
        warn(surface, f"'resolved_name' is a raw identifier, not a name: {resolved}", obj)

    # For group chats, members should be populated
    is_group = looks_like_group_identifier(identifier)
    if is_group:
        members = obj.get("members", [])
        if not members:
            # Not necessarily wrong (could be an edge case), but worth noting
            warn(surface, f"group chat has no 'members' array populated", obj)
        else:
            # Each member should look like a phone or email
            for m in members:
                if not looks_like_phone(m) and not looks_like_email(m):
                    warn(surface, f"group member doesn't look like phone/email: {m[:30]}", obj)

    # For 1:1 chats with phone identifiers, resolved_name shouldn't be a raw phone number
    if looks_like_phone(identifier):
        if resolved == identifier or looks_like_phone(resolved):
            # This is a soft warning — contact might just not be in the address book
            pass  # Don't warn, this is expected for unknown contacts


def validate_imsg_named_history(obj: dict):
    """Validate a message record after contact resolution."""
    surface = "imsg_named.history"

    check_field_exists(obj, "id", surface, int)
    check_field_exists(obj, "chat_id", surface, int)
    check_field_exists(obj, "sender", surface, str)
    check_field_exists(obj, "created_at", surface, str)

    # After resolution, sender_name should exist
    if "sender_name" not in obj:
        warn(surface, "'sender_name' missing (contact resolution may have failed)", obj)
    else:
        sender_name = obj["sender_name"]
        sender = obj.get("sender", "")
        # sender_name shouldn't be identical to raw phone (means resolution failed)
        if sender_name == sender and looks_like_phone(sender):
            # Soft — unknown contacts are fine, but log it
            pass


# ============================================================================
# Surface Validators: gog (raw CLI output)
# ============================================================================

def validate_gog_gmail_search(obj: dict):
    """Validate gmail search result record (or wrapper with threads array)."""
    surface = "gog.gmail_search"
    # gog wraps results in {"threads": [...]}
    if "threads" in obj and isinstance(obj["threads"], list):
        for thread in obj["threads"]:
            if isinstance(thread, dict):
                check_field_exists(thread, "id", surface, str)
        return
    check_field_exists(obj, "id", surface, str)


def validate_gog_gmail_get(obj: dict):
    """Validate gmail get (single message) record."""
    surface = "gog.gmail_get"
    check_field_exists(obj, "id", surface, str)


def validate_gog_calendar_events(obj: dict):
    """Validate calendar events record (or wrapper containing events array)."""
    surface = "gog.calendar_events"
    # gog wraps events in {"events": [...]} — validate each event inside
    if "events" in obj and isinstance(obj["events"], list):
        for event in obj["events"]:
            if isinstance(event, dict):
                if "summary" not in event and "title" not in event:
                    warn(surface, "event missing 'summary' or 'title' field", event)
        return
    # Single event object
    if "summary" not in obj and "title" not in obj:
        warn(surface, "missing 'summary' or 'title' field", obj)


# ============================================================================
# Surface Validators: gog_cleaned (after gog_cleaner.py)
# ============================================================================

def validate_gog_cleaned_gmail_search(obj: dict):
    """Validate gmail search results after cleaning (or wrapper)."""
    surface = "gog_cleaned.gmail_search"
    if "threads" in obj and isinstance(obj["threads"], list):
        for thread in obj["threads"]:
            if isinstance(thread, dict):
                check_field_exists(thread, "id", surface, str)
        return
    check_field_exists(obj, "id", surface, str)


def validate_gog_cleaned_calendar_events(obj: dict):
    """Validate calendar events after cleaning (or wrapper)."""
    surface = "gog_cleaned.calendar_events"
    if "events" in obj and isinstance(obj["events"], list):
        for event in obj["events"]:
            if isinstance(event, dict):
                if "summary" not in event and "title" not in event:
                    warn(surface, "event missing 'summary' or 'title' after cleaning", event)
        return
    if "summary" not in obj and "title" not in obj:
        warn(surface, "missing 'summary' or 'title' after cleaning", obj)


# ============================================================================
# Surface Registry
# ============================================================================

SURFACE_VALIDATORS = {
    "imsg.chats": validate_imsg_chats,
    "imsg.history": validate_imsg_history,
    "imsg_named.chats": validate_imsg_named_chats,
    "imsg_named.history": validate_imsg_named_history,
    "gog.gmail_search": validate_gog_gmail_search,
    "gog.gmail_get": validate_gog_gmail_get,
    "gog.calendar_events": validate_gog_calendar_events,
    "gog_cleaned.gmail_search": validate_gog_cleaned_gmail_search,
    "gog_cleaned.calendar_events": validate_gog_cleaned_calendar_events,
}


# ============================================================================
# Main: Transparent Pipe Mode
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Boundary validator — validates tool output inline, passes through unchanged."
    )
    parser.add_argument(
        "--surface",
        required=True,
        choices=sorted(SURFACE_VALIDATORS.keys()),
        help="Which boundary surface to validate against."
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stderr warnings (still logs to file)."
    )
    args = parser.parse_args()

    validator = SURFACE_VALIDATORS[args.surface]
    line_count = 0
    json_count = 0
    header_count = 0

    for line in sys.stdin:
        # Always pass through unchanged
        sys.stdout.write(line)

        # Try to validate if it's JSON
        stripped = line.strip()
        if not stripped:
            continue
        line_count += 1

        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                json_count += 1
                validator(obj)
        except json.JSONDecodeError:
            # Wrapper status headers like "[Filtered 3 entries from excluded
            # contacts]" are expected non-JSON — an output that is ONLY
            # headers (empty chat) is normal, not a boundary violation.
            if stripped.startswith("[") and stripped.endswith("]"):
                header_count += 1
            # Other non-JSON lines (stderr passthrough, etc.) — skip

    # If we expected JSON but got none, that's a warning — unless the output
    # was nothing but status headers.
    if line_count > 0 and json_count == 0 and header_count < line_count:
        warn(args.surface, f"no valid JSON objects found in {line_count} lines of output")

    if not args.quiet:
        flush_warnings(args.surface)
    elif _warnings:
        # Even in quiet mode, log to file
        flush_warnings.__wrapped__ = True  # hack to skip stderr
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_SIZE_BYTES:
                LOG_FILE.write_text("")
            with open(LOG_FILE, "a") as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n--- {timestamp} | surface={args.surface} | {len(_warnings)} warning(s) [quiet] ---\n")
                for w in _warnings:
                    f.write(f"  {w}\n")
        except IOError:
            pass


if __name__ == "__main__":
    main()
