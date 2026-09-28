#!/usr/bin/env python3
"""
Look up contact names from phone numbers or emails.

Usage:
    python3 lookup_contact.py "+15551234567"
    python3 lookup_contact.py --json "+15551234567" "+15559876543"
    python3 lookup_contact.py --search "Matt"
    echo '{"identifier":"+15551234567"}' | python3 lookup_contact.py --stdin
"""

import json
import sys
import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# Workspace root; overridable via MINERU_HOME, defaults to ~/.mineru.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))


# ============================================================================
# Timestamp Conversion
# ============================================================================

def utc_to_local(iso_string: str) -> str:
    """
    Convert UTC ISO8601 timestamp to local time.
    
    Input:  "2026-02-22T06:27:44.088Z" (UTC)
    Output: "2026-02-21T22:27:44-08:00" (local, with offset)
    
    Returns the original string unchanged if parsing fails.
    """
    if not iso_string or not isinstance(iso_string, str):
        return iso_string
    
    # Only convert UTC timestamps (ending in 'Z')
    if not iso_string.endswith('Z'):
        return iso_string
    
    try:
        # Parse UTC timestamp
        dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        # Convert to local timezone
        local_dt = dt.astimezone()
        # Return ISO8601 format with offset (no 'Z', shows actual timezone)
        return local_dt.isoformat()
    except (ValueError, TypeError):
        return iso_string


def convert_timestamps(obj: dict) -> dict:
    """
    Convert known timestamp fields from UTC to local time.
    
    Converts: created_at, last_message_at
    """
    timestamp_fields = ('created_at', 'last_message_at')
    
    for field in timestamp_fields:
        if field in obj:
            obj[field] = utc_to_local(obj[field])
    
    return obj


# ============================================================================
# Recursive Contact Resolution
# ============================================================================

# Fields that contain phone numbers/emails needing resolution
RESOLVABLE_FIELDS = {'sender', 'identifier', 'from', 'to', 'participant'}


def is_phone_or_email(value: str) -> bool:
    """Check if a string looks like a phone number or email."""
    if not value or not isinstance(value, str):
        return False
    # Email
    if '@' in value:
        return True
    # Phone number (starts with + or is mostly digits)
    if value.startswith('+'):
        return True
    # 10+ digit string
    digits = ''.join(c for c in value if c.isdigit())
    if len(digits) >= 10:
        return True
    return False


def resolve_recursive(obj, resolver: "ContactResolver"):
    """
    Recursively walk a JSON object and resolve phone numbers/emails to names.
    
    For dict: adds '{field}_name' for any resolvable field
    For list: processes each item
    
    Modifies obj in place and returns it.
    """
    if isinstance(obj, dict):
        # Collect new fields to add (to avoid modifying dict during iteration)
        new_fields = {}
        
        for key, value in obj.items():
            if isinstance(value, str):
                # Check if this field should be resolved
                if key in RESOLVABLE_FIELDS and is_phone_or_email(value):
                    name_key = f"{key}_name"
                    if name_key not in obj:  # Don't overwrite existing
                        new_fields[name_key] = resolver.resolve(value)
            elif isinstance(value, (dict, list)):
                # Recurse into nested structures
                resolve_recursive(value, resolver)
        
        # Add resolved names
        obj.update(new_fields)
        
    elif isinstance(obj, list):
        for item in obj:
            resolve_recursive(item, resolver)
    
    return obj


class GroupMembershipCache:
    """Cache of chat_id → member identifiers for group chat filtering."""

    DEFAULT_PATH = _MINERU_HOME / "cache/group_members.json"

    def __init__(self, path: Optional[Path] = None):
        self.path = path or self.DEFAULT_PATH
        self._chats: dict[int, list[str]] = {}  # chat_id → [member identifiers]
        self._load()

    def _load(self):
        """Load group membership cache."""
        if not self.path.exists():
            return

        try:
            with open(self.path, "r") as f:
                data = json.load(f)

            for chat_id_str, info in data.get("chats", {}).items():
                self._chats[int(chat_id_str)] = info.get("members", [])

        except (json.JSONDecodeError, IOError):
            pass

    def get_members(self, chat_id: int) -> list[str]:
        """Get member identifiers for a chat."""
        return self._chats.get(chat_id, [])


# Opt-in escape hatch: treat a missing exclusion file as an empty list.
ALLOW_MISSING_EXCLUSIONS_ENV_VAR = "MINERU_ALLOW_MISSING_EXCLUSIONS"


class ExclusionList:
    """Manages contacts excluded from iMessage processing."""

    DEFAULT_PATH = _MINERU_HOME / "cache/excluded_imsg_contacts.json"

    def __init__(self, path: Optional[Path] = None):
        self.path = path or self.DEFAULT_PATH
        self._identifiers: set[str] = set()
        self._normalized: set[str] = set()  # Normalized versions for matching
        self._group_cache = GroupMembershipCache()
        self._load()

    def _load(self):
        """Load the exclusion list, failing loud when it is missing or unreadable.

        - A missing file raises, because treating it as "nobody excluded" silently
          re-enables processing for people who opted out. Set
          MINERU_ALLOW_MISSING_EXCLUSIONS=1 to opt into the empty list deliberately
          (e.g. a fresh install with no exclusions yet).
        - A malformed file always raises, for the same reason.
        """
        if not self.path.exists():
            if os.environ.get(ALLOW_MISSING_EXCLUSIONS_ENV_VAR) == "1":
                return
            raise FileNotFoundError(
                f"iMessage exclusion list not found at {self.path}. Create it as "
                '{"identifiers": []} (or list opted-out handles), or set '
                f"{ALLOW_MISSING_EXCLUSIONS_ENV_VAR}=1 to run with nobody excluded."
            )

        with open(self.path, "r") as f:
            data = json.load(f)

        for identifier in data.get("identifiers", []):
            self._identifiers.add(identifier)
            self._normalized.add(self._normalize(identifier))

    def _normalize(self, handle: str) -> str:
        """Normalize phone number for comparison."""
        if not handle or "@" in handle:
            return handle.lower() if handle else ""

        # Extract digits only
        digits = "".join(c for c in handle if c.isdigit())

        # Normalize US numbers to 10 digits
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]

        return digits

    def is_excluded(self, identifier: str) -> bool:
        """Check if an identifier is in the exclusion list."""
        if not identifier:
            return False

        # Direct match
        if identifier in self._identifiers:
            return True

        # Normalized match (handles phone number format variations)
        return self._normalize(identifier) in self._normalized

    def is_chat_excluded(self, chat_id: int) -> tuple[bool, Optional[str]]:
        """
        Check if a chat should be excluded based on its members.
        
        Returns (is_excluded, excluded_member_identifier).
        """
        members = self._group_cache.get_members(chat_id)
        for member in members:
            if self.is_excluded(member):
                return True, member
        return False, None

    def get_excluded_name(self, identifier: str, resolver: "ContactResolver") -> Optional[str]:
        """Get the display name for an excluded identifier (for error messages)."""
        if self.is_excluded(identifier):
            name = resolver.get_name(identifier)
            return name if name else identifier
        return None


class ContactResolver:
    """Bidirectional lookup between contact names and handles."""

    DEFAULT_MAPPING_PATH = _MINERU_HOME / "cache/contact_mapping.json"

    def __init__(self, mapping_path: Optional[Path] = None):
        self.mapping_path = mapping_path or self.DEFAULT_MAPPING_PATH
        self._forward_map: dict[str, str] = {}  # handle -> name
        self._reverse_map: dict[str, str] = {}  # name (lowercase) -> handle
        self._load_mapping()

    def _load_mapping(self):
        """Load contact mapping from JSON file."""
        if not self.mapping_path.exists():
            return

        try:
            with open(self.mapping_path, "r") as f:
                self._forward_map = json.load(f)

            # Build reverse mapping
            seen_names: dict[str, str] = {}
            for handle, name in self._forward_map.items():
                name_lower = name.lower()
                if name_lower not in seen_names:
                    seen_names[name_lower] = handle
            self._reverse_map = seen_names

        except (json.JSONDecodeError, IOError):
            self._forward_map = {}
            self._reverse_map = {}

    def _normalize_phone(self, phone: str) -> str:
        """Normalize phone number."""
        if not phone:
            return phone
        if phone.startswith("+"):
            return "+" + "".join(c for c in phone[1:] if c.isdigit())
        return "".join(c for c in phone if c.isdigit())

    def get_name(self, handle: str) -> Optional[str]:
        """Get contact name from handle (phone number or email)."""
        # Try exact match
        if handle in self._forward_map:
            return self._forward_map[handle]

        # Try lowercase for emails
        if "@" in handle:
            lower = handle.lower()
            if lower in self._forward_map:
                return self._forward_map[lower]

        # Normalize phone and try variations
        normalized = self._normalize_phone(handle)

        if normalized in self._forward_map:
            return self._forward_map[normalized]

        # Try with +1 for 10-digit US numbers
        if not normalized.startswith("+") and not normalized.startswith("1"):
            if len(normalized) == 10:
                with_plus = f"+1{normalized}"
                if with_plus in self._forward_map:
                    return self._forward_map[with_plus]

        # Try without + for +1 numbers
        if normalized.startswith("+1") and len(normalized) == 12:
            without_plus = normalized[1:]
            if without_plus in self._forward_map:
                return self._forward_map[without_plus]

        return None

    def get_handle(self, name: str) -> Optional[str]:
        """Get handle from contact name (case-insensitive)."""
        name_lower = name.lower()

        if name_lower in self._reverse_map:
            return self._reverse_map[name_lower]

        # Partial match (first name only)
        for known_name, handle in self._reverse_map.items():
            if known_name.startswith(name_lower + " ") or known_name == name_lower:
                return handle

        return None

    def search(self, query: str) -> list[tuple[str, str]]:
        """Search for contacts matching a query."""
        query_lower = query.lower()
        results = []

        seen_names = set()
        for handle, name in self._forward_map.items():
            if query_lower in name.lower() and name not in seen_names:
                results.append((handle, name))
                seen_names.add(name)

        return results

    def resolve(self, handle: str) -> str:
        """Get display name, falling back to handle if not found."""
        name = self.get_name(handle)
        return name if name else handle


def main():
    parser = argparse.ArgumentParser(description="Look up contact names")
    parser.add_argument("handles", nargs="*", help="Phone numbers or emails to look up")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--search", metavar="QUERY", help="Search contacts by name")
    parser.add_argument("--stdin", action="store_true", help="Read JSON lines from stdin and resolve 'identifier' fields")
    parser.add_argument("--mapping", type=Path, help="Path to contact_mapping.json")
    args = parser.parse_args()

    resolver = ContactResolver(args.mapping)

    if not resolver._forward_map:
        print("⚠️  No contact mapping found. Run build_contact_map.py first.", file=sys.stderr)

    # Search mode
    if args.search:
        results = resolver.search(args.search)
        if args.json:
            print(json.dumps([{"handle": h, "name": n} for h, n in results], indent=2))
        else:
            if not results:
                print(f"No contacts matching '{args.search}'")
            for handle, name in results:
                print(f"{name}: {handle}")
        return

    # Stdin mode (for piping imsg output)
    if args.stdin:
        exclusions = ExclusionList()
        excluded_count = 0
        output_count = 0
        excluded_identifiers: set[str] = set()

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)

                # Determine the identifier to check for exclusion
                # - For chats output: use "identifier" field
                # - For history output: use "sender" field (which is the chat's identifier)
                check_identifier = obj.get("identifier") or obj.get("sender")

                # Check 1: Direct identifier exclusion (1:1 chats)
                if check_identifier and exclusions.is_excluded(check_identifier):
                    excluded_count += 1
                    excluded_identifiers.add(check_identifier)
                    continue  # Skip this entry

                # Check 2: Group chat member exclusion
                # If this chat contains any excluded member, exclude the whole chat
                # Note: For chats output, "id" is the chat_id
                #       For history output, "chat_id" is the chat_id (and "id" is message id)
                chat_id = obj.get("chat_id") or obj.get("id")
                if chat_id:
                    is_excluded, excluded_member = exclusions.is_chat_excluded(chat_id)
                    if is_excluded and excluded_member:
                        excluded_count += 1
                        excluded_identifiers.add(excluded_member)
                        continue  # Skip this entry

                # Recursively resolve all phone numbers/emails in the object
                # This handles: sender, reactions[].sender, members, etc.
                resolve_recursive(obj, resolver)

                if "identifier" in obj:
                    identifier = obj["identifier"]
                    explicit_name = obj.get("name", "").strip()
                    
                    # Check if this is a group chat (hash identifier, not phone/email)
                    is_group = (
                        identifier and 
                        "@" not in identifier and 
                        not identifier.startswith("+") and
                        (
                            # Format 1: 32-char hex (older iMessage groups)
                            (len(identifier) == 32 and all(c in "0123456789abcdef" for c in identifier.lower()))
                            or
                            # Format 2: "chat" + digits (newer iMessage groups)
                            (identifier.startswith("chat") and identifier[4:].isdigit())
                        )
                    )
                    
                    if is_group:
                        # Group chat - always try to populate members
                        chat_id = obj.get("id")
                        members = []
                        resolved_members = []
                        if chat_id:
                            group_cache = GroupMembershipCache()
                            members = group_cache.get_members(chat_id)
                            for member in members:
                                name = resolver.get_name(member)
                                if name:
                                    resolved_members.append(name)
                                else:
                                    # Format phone number nicely if no contact
                                    if member.startswith("+1") and len(member) == 12:
                                        resolved_members.append(f"({member[2:5]}) {member[5:8]}-{member[8:]}")
                                    else:
                                        resolved_members.append(member)

                        # Always include raw member list if available
                        if members:
                            obj["members"] = members

                        # For resolved_name: prefer explicit name, then resolved members, then identifier
                        if explicit_name:
                            obj["resolved_name"] = explicit_name
                        elif resolved_members:
                            obj["resolved_name"] = ", ".join(resolved_members)
                        else:
                            obj["resolved_name"] = identifier
                    else:
                        # 1:1 chat - use standard resolution
                        obj["resolved_name"] = resolver.resolve(identifier)
                
                # Fix sender attribution for owner's messages
                if obj.get("is_from_me") is True:
                    obj["sender_name"] = "the operator"

                # Convert timestamps from UTC to local time
                obj = convert_timestamps(obj)
                
                print(json.dumps(obj))
                output_count += 1

            except json.JSONDecodeError:
                print(line)
                output_count += 1

        # If we filtered everything and got no output, this was likely a history
        # query for an excluded contact - give an informative error
        if excluded_count > 0 and output_count == 0:
            # Resolve names for the error message
            excluded_names = []
            for ident in excluded_identifiers:
                name = resolver.get_name(ident)
                excluded_names.append(name if name else ident)

            names_str = ", ".join(excluded_names)
            print(f"⛔ Contact excluded: {names_str}", file=sys.stderr)
            print(f"   This contact (or a group chat member) has opted out of iMessage data processing.", file=sys.stderr)
            print(f"   See: cache/excluded_imsg_contacts.json", file=sys.stderr)
            sys.exit(1)

        elif excluded_count > 0:
            print(f"[Filtered {excluded_count} entries from excluded contacts]", file=sys.stderr)

        return

    # Direct lookup mode
    if not args.handles:
        parser.print_help()
        return

    results = {}
    for handle in args.handles:
        results[handle] = resolver.resolve(handle)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for handle, name in results.items():
            if name != handle:
                print(f"{handle} → {name}")
            else:
                print(f"{handle} → (not found)")


if __name__ == "__main__":
    main()
