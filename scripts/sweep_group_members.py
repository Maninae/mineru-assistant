#!/usr/bin/env python3
"""
Sweep for new group chats and update the group_members.json cache.

Compares current Messages database against cached group_members.json,
identifies new groups, and generates a report.

Usage:
    python3 sweep_group_members.py              # Dry run (report only)
    python3 sweep_group_members.py --update     # Update cache and report
    python3 sweep_group_members.py --report     # Output report to stdout
"""

import argparse
import json
import os
import sqlite3
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple


_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
CACHE_DIR = _MINERU_HOME / "cache"
CACHE_FILE = CACHE_DIR / "group_members.json"
REPORTS_DIR = _MINERU_HOME / "briefs_imsg_group_members_updates"
CONTACT_CACHE = CACHE_DIR / "contact_mapping.json"


def load_contacts() -> Dict[str, str]:
    """Load contact name mapping."""
    if not CONTACT_CACHE.exists():
        return {}
    try:
        with open(CONTACT_CACHE) as f:
            return json.load(f)
    except:
        return {}


def resolve_name(identifier: str, contacts: Dict[str, str]) -> str:
    """Resolve identifier to contact name."""
    if identifier in contacts:
        return contacts[identifier]
    
    # Try normalized phone variations
    digits = "".join(c for c in identifier if c.isdigit())
    if len(digits) == 10:
        for prefix in ["+1", "1", ""]:
            key = prefix + digits
            if key in contacts:
                return contacts[key]
    elif len(digits) == 11 and digits.startswith("1"):
        for fmt in [f"+{digits}", digits, digits[1:]]:
            if fmt in contacts:
                return contacts[fmt]
    
    return identifier  # Fallback to raw identifier


def get_current_groups() -> Dict[int, dict]:
    """Get current group chats from Messages database."""
    db_path = Path.home() / "Library/Messages/chat.db"
    if not db_path.exists():
        return {}

    temp_db = Path("/tmp/chat-db-sweep.db")
    try:
        shutil.copy2(db_path, temp_db)
        # chat.db is full message PII — lock down the temp copy immediately
        try:
            os.chmod(temp_db, 0o600)
        except OSError:
            pass

        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

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

        groups: Dict[int, dict] = defaultdict(lambda: {"identifier": "", "members": []})
        for chat_id, chat_identifier, member_identifier in cursor.fetchall():
            groups[chat_id]["identifier"] = chat_identifier
            groups[chat_id]["members"].append(member_identifier)

        conn.close()
    finally:
        # Always remove the temp DB copy, even on exception
        try:
            temp_db.unlink(missing_ok=True)
        except OSError:
            pass

    # Filter to only groups (2+ members)
    return {k: dict(v) for k, v in groups.items() if len(v["members"]) >= 2}


def load_cached_groups() -> Dict[int, dict]:
    """Load cached group membership."""
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return {int(k): v for k, v in data.get("chats", {}).items()}
    except:
        return {}


def find_new_groups(current: Dict[int, dict], cached: Dict[int, dict]) -> List[Tuple[int, dict]]:
    """Find groups that exist now but not in cache."""
    new_ids = current.keys() - cached.keys()  # Set difference, O(n)
    return [(chat_id, current[chat_id]) for chat_id in new_ids]


def generate_report(new_groups: List[Tuple[int, dict]], contacts: Dict[str, str]) -> str:
    """Generate a markdown report of new groups."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    lines = [
        f"# Group Chat Sweep — {date_str}",
        "",
    ]
    
    if not new_groups:
        lines.append("**No new group chats discovered.**")
    else:
        lines.append(f"**{len(new_groups)} new group chat(s) discovered:**")
        lines.append("")
        
        for chat_id, info in new_groups:
            members = info.get("members", [])
            member_names = [resolve_name(m, contacts) for m in members]
            lines.append(f"- **Chat {chat_id}**: {', '.join(member_names)}")
        
    lines.append("")
    lines.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Sweep for new group chats")
    parser.add_argument("--update", action="store_true", help="Update the cache file")
    parser.add_argument("--report", action="store_true", help="Output report to stdout only")
    parser.add_argument("--save-report", action="store_true", help="Save report to briefs folder")
    args = parser.parse_args()

    contacts = load_contacts()
    current = get_current_groups()
    cached = load_cached_groups()
    
    new_groups = find_new_groups(current, cached)
    report = generate_report(new_groups, contacts)
    
    if args.report:
        print(report)
        return
    
    # Summary output
    print(f"📊 Group chat sweep")
    print(f"   Current groups: {len(current)}")
    print(f"   Cached groups: {len(cached)}")
    print(f"   New groups: {len(new_groups)}")
    
    if new_groups:
        print()
        for chat_id, info in new_groups:
            members = [resolve_name(m, contacts) for m in info.get("members", [])]
            print(f"   + Chat {chat_id}: {', '.join(members)}")
    
    if args.update:
        # Update cache
        output = {"chats": {str(k): v for k, v in current.items()}}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(output, f, indent=2)
        # group_members.json holds member phone/email PII — restrict perms
        try:
            os.chmod(CACHE_FILE, 0o600)
        except OSError:
            pass
        print()
        print(f"✅ Updated cache: {CACHE_FILE}")
    
    if args.save_report:
        # Save report
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        report_file = REPORTS_DIR / f"group-updates-{date_str}.md"
        with open(report_file, "w") as f:
            f.write(report)
        print(f"📝 Saved report: {report_file}")


if __name__ == "__main__":
    main()
