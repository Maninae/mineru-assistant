#!/usr/bin/env python3
"""
Build contact mapping from macOS Contacts database.

Reads the AddressBook SQLite database and exports a JSON mapping
of phone numbers and emails to contact names.

Based on: ~/Developer/mineru/scripts/build_contact_map.py
"""

from __future__ import annotations

import os
import sqlite3
import json
import shutil
from pathlib import Path
from contextlib import closing


def find_address_book_db() -> Path | None:
    """Find the iCloud-synced AddressBook database."""
    base_path = Path.home() / "Library/Application Support/AddressBook/Sources"
    if not base_path.exists():
        return None

    # Look for any subdirectory containing AddressBook-v22.abcddb
    for source_dir in base_path.iterdir():
        if source_dir.is_dir():
            db_path = source_dir / "AddressBook-v22.abcddb"
            if db_path.exists():
                return db_path
    return None


def normalize_handle(handle: str) -> str:
    """Normalize phone/email for consistent mapping keys."""
    if not handle:
        return ""
    # Remove spaces, parens, dashes - keep alphanumeric and +
    return "".join(c for c in handle if c.isalnum() or c == "+" or c == "@" or c == ".")


def build_contact_map():
    """Build and save contact mapping."""
    db_path = find_address_book_db()
    if not db_path:
        print("❌ Could not find AddressBook database.")
        print("   Make sure Contacts.app has synced at least once.")
        return False

    print(f"📖 Reading AddressBook from: {db_path}")

    mapping = {}

    try:
        # Open read-only to avoid conflicts with Contacts.app
        # (no temp copy needed — SQLite WAL mode supports concurrent readers)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        # Get all records with names
        query = """
            SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION
            FROM ZABCDRECORD
        """
        cursor.execute(query)
        people = {}
        for row in cursor.fetchall():
            pk, first, last, org = row
            name_parts = []
            if first:
                name_parts.append(first)
            if last:
                name_parts.append(last)

            full_name = " ".join(name_parts)
            if not full_name and org:
                full_name = org

            if full_name:
                people[pk] = full_name

        # Get phone numbers
        cursor.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER")
        for owner_pk, number in cursor.fetchall():
            if owner_pk in people and number:
                clean_number = normalize_handle(number)
                mapping[clean_number] = people[owner_pk]
                # Also add variants for US numbers
                digits_only = "".join(c for c in clean_number if c.isdigit())
                if len(digits_only) == 10:
                    mapping["1" + digits_only] = people[owner_pk]
                    mapping["+1" + digits_only] = people[owner_pk]
                elif len(digits_only) == 11 and digits_only.startswith("1"):
                    mapping["+" + digits_only] = people[owner_pk]
                    mapping[digits_only[1:]] = people[owner_pk]

        # Get emails
        cursor.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS")
        for owner_pk, email in cursor.fetchall():
            if owner_pk in people and email:
                mapping[email.lower()] = people[owner_pk]

        conn.close()

        # Save to cache (with backup for safety)
        output_dir = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "cache"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "contact_mapping.json"
        backup_path = output_dir / "contact_mapping.json.bak"

        # Load existing mapping for comparison
        old_mapping = {}
        if output_path.exists():
            try:
                with open(output_path) as f:
                    old_mapping = json.load(f)
            except:
                pass

        # Sanity check: new mapping shouldn't be drastically smaller
        # (protects against DB read errors, corruption, etc.)
        if old_mapping and len(mapping) < len(old_mapping) * 0.5:
            print(f"⚠️  Warning: New mapping ({len(mapping)}) is <50% of old ({len(old_mapping)})")
            print(f"   This could indicate a problem. Backing up old and proceeding anyway.")
        
        # Backup existing file before overwriting
        if output_path.exists():
            shutil.copy2(output_path, backup_path)
            # Backup contains full contact PII dump — restrict to owner read/write only
            try:
                os.chmod(backup_path, 0o600)
            except OSError:
                pass

        with open(output_path, "w") as f:
            json.dump(mapping, f, indent=2)

        # contact_mapping.json holds full phone/email→name PII — restrict perms
        try:
            os.chmod(output_path, 0o600)
        except OSError:
            pass

        unique_names = len(set(mapping.values()))
        new_contacts = len(mapping) - len(old_mapping) if old_mapping else 0
        print(f"✅ Mapped {len(mapping)} handles → {unique_names} contacts")
        if new_contacts > 0:
            print(f"   (+{new_contacts} new handles since last run)")
        print(f"📁 Saved to: {output_path}")
        return True

    except Exception as e:
        print(f"❌ Error building contact map: {e}")
        return False


if __name__ == "__main__":
    build_contact_map()
