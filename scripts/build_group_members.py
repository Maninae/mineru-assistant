#!/usr/bin/env python3
"""
Build group membership cache from Messages database.

Creates a JSON mapping of chat_id → [member identifiers] for filtering
group chats that contain excluded contacts.

Output: cache/group_members.json
"""

import os
import sqlite3
import json
import shutil
from pathlib import Path
from collections import defaultdict
from typing import Dict, List


def build_group_members():
    """Build and save group membership mapping."""
    db_path = Path.home() / "Library/Messages/chat.db"

    if not db_path.exists():
        print("❌ Could not find Messages database.")
        return False

    print(f"📖 Reading Messages database: {db_path}")

    temp_db = Path("/tmp/chat-db-temp.db")
    try:
        # Copy to temp location to avoid database lock issues
        shutil.copy2(db_path, temp_db)
        # chat.db is full message PII — lock down the temp copy immediately
        try:
            os.chmod(temp_db, 0o600)
        except OSError:
            pass

        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        # Get all chat → handle mappings
        query = """
            SELECT
                c.ROWID as chat_id,
                c.chat_identifier,
                h.id as member_identifier
            FROM chat c
            JOIN chat_handle_join chj ON c.ROWID = chj.chat_id
            JOIN handle h ON chj.handle_id = h.ROWID
        """
        cursor.execute(query)

        # Build mapping: chat_id → list of member identifiers
        group_members: Dict[int, List[str]] = defaultdict(list)
        chat_identifiers: Dict[int, str] = {}

        for chat_id, chat_identifier, member_identifier in cursor.fetchall():
            group_members[chat_id].append(member_identifier)
            chat_identifiers[chat_id] = chat_identifier

        conn.close()

        # Only keep chats with multiple members (actual groups) or all chats for completeness
        # Actually, keep all chats - 1:1 chats will have 1 member, groups will have 2+

        # Convert to serializable format
        # Only include chats with 2+ members (actual group chats)
        # 1-member chats are 1:1 conversations, handled by direct identifier filtering
        output = {
            "chats": {
                str(chat_id): {
                    "identifier": chat_identifiers.get(chat_id, ""),
                    "members": members
                }
                for chat_id, members in group_members.items()
                if len(members) >= 2
            }
        }

        # Save to cache
        output_dir = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "cache"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "group_members.json"

        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)

        # group_members.json holds member phone/email PII — restrict perms
        try:
            os.chmod(output_path, 0o600)
        except OSError:
            pass

        # Stats
        group_chats = len(output["chats"])

        print(f"✅ Mapped {group_chats} group chats (skipped 1:1 chats)")
        print(f"📁 Saved to: {output_path}")
        return True

    except Exception as e:
        print(f"❌ Error building group members: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Always remove the temp DB copy, even on exception
        try:
            temp_db.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    build_group_members()
