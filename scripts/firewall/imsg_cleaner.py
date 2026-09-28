#!/usr/bin/env python3
"""
imsg_cleaner.py - Clean imsg-named output to reduce token usage.

Strips unnecessary metadata from iMessage JSON while preserving essential
information for the morning briefing. Configuration is loaded from
imsg_cleaner_config.json.

Usage:
    # As module (called by imsg-firewall):
    from imsg_cleaner import clean_imsg_output
    cleaned = clean_imsg_output("history", raw_json_lines)

    # As CLI (for testing):
    cat messages.json | python3 imsg_cleaner.py history
"""
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Load config once at module import.
# Fail SAFE (block) with a clear message if the config is missing or corrupt —
# otherwise a bare traceback at import time crashes every firewall call with
# exit 78 and no explanation, silently breaking iMessage reads.
CONFIG_PATH = Path(__file__).parent / "imsg_cleaner_config.json"
try:
    with open(CONFIG_PATH) as _cfg_f:
        CONFIG = json.load(_cfg_f)
except FileNotFoundError:
    print(
        f"FATAL: imsg_cleaner config not found at {CONFIG_PATH}. "
        f"Firewall cannot screen output — blocking for safety.",
        file=sys.stderr,
    )
    sys.exit(78)  # EXIT_ERROR (fail-safe block)
except (json.JSONDecodeError, OSError) as _e:
    print(
        f"FATAL: imsg_cleaner config at {CONFIG_PATH} is unreadable/corrupt: {_e}. "
        f"Firewall cannot screen output — blocking for safety.",
        file=sys.stderr,
    )
    sys.exit(78)  # EXIT_ERROR (fail-safe block)

REACTION_TYPE_TO_EMOJI = CONFIG.get("reaction_type_to_emoji", {})


def normalize_message_text(text: str) -> str:
    """Replace Unicode right single quotation mark (\\u2019) with ASCII apostrophe."""
    if not text:
        return text
    return text.replace("\u2019", "'")


def simplify_attachment_metadata(attachment: Dict[str, Any],
                                  keep_fields: List[str]) -> Dict[str, Any]:
    """Reduce an attachment dict to only the fields specified in keep_fields."""
    return {key: attachment[key] for key in keep_fields if key in attachment}


def simplify_reaction_metadata(reaction: Dict[str, Any],
                                keep_fields: List[str]) -> Dict[str, Any]:
    """
    Reduce a reaction dict to only the fields specified in keep_fields.

    If the reaction has no 'emoji' field but has a 'type' field, resolve the
    type code to an emoji character using the reaction_type_to_emoji lookup.
    """
    # Resolve emoji from type code if needed
    if "emoji" not in reaction and "type" in reaction:
        resolved_emoji = REACTION_TYPE_TO_EMOJI.get(reaction["type"])
        if resolved_emoji:
            reaction = dict(reaction)  # avoid mutating original
            reaction["emoji"] = resolved_emoji

    return {key: reaction[key] for key in keep_fields if key in reaction}


def clean_imsg_history_message(raw_message: Dict[str, Any],
                                rules: Dict[str, Any]) -> Dict[str, Any]:
    """
    Clean a single iMessage history JSON object.

    Drops blacklisted fields, simplifies attachments and reactions,
    and normalizes text content.
    """
    drop_fields = set(rules.get("drop", []))
    keep_fields = set(rules.get("keep", []))
    transform_rules = rules.get("transform", {})

    cleaned_message = {}

    for field_name, field_value in raw_message.items():
        if field_name in drop_fields:
            continue
        # Keep-list enforcement: when the config declares a keep list, fields
        # outside it are stripped — a new upstream field must be audited into
        # the config before it reaches agent context. Transform-handled fields
        # (attachments/reactions) pass through to their own keep rules below.
        if (keep_fields and field_name not in keep_fields
                and field_name not in transform_rules):
            continue

        if field_name == "text" and isinstance(field_value, str):
            cleaned_message[field_name] = normalize_message_text(field_value)

        elif field_name == "attachments" and isinstance(field_value, list):
            attachment_rules = transform_rules.get("attachments", {})
            attachment_keep = attachment_rules.get("keep", [])
            remove_if_empty = attachment_rules.get("remove_if_empty", False)

            if not field_value and remove_if_empty:
                continue  # skip empty attachments entirely

            simplified_attachments = [
                simplify_attachment_metadata(att, attachment_keep)
                for att in field_value
                if isinstance(att, dict)
            ]
            cleaned_message[field_name] = simplified_attachments

        elif field_name == "reactions" and isinstance(field_value, list):
            reaction_rules = transform_rules.get("reactions", {})
            reaction_keep = reaction_rules.get("keep", [])
            remove_if_empty = reaction_rules.get("remove_if_empty", False)

            if not field_value and remove_if_empty:
                continue  # skip empty reactions entirely

            simplified_reactions = [
                simplify_reaction_metadata(rxn, reaction_keep)
                for rxn in field_value
                if isinstance(rxn, dict)
            ]
            cleaned_message[field_name] = simplified_reactions

        else:
            cleaned_message[field_name] = field_value

    return cleaned_message


def clean_imsg_output(command: str, raw_output: str) -> str:
    """
    Main entry point: clean imsg-named output based on command type.

    Processes JSON-lines output (one JSON object per line). Non-JSON lines
    (like '--- chat 241 ---' separators) are passed through unchanged.

    Args:
        command: The imsg subcommand (e.g., "history", "chats")
        raw_output: Raw output string from imsg-named (JSON lines)

    Returns:
        Cleaned output string with reduced metadata
    """
    command_rules = CONFIG.get(command)
    if not command_rules:
        return raw_output  # no rules for this command, pass through

    cleaned_lines = []
    for raw_line in raw_output.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line:
            cleaned_lines.append(raw_line)
            continue

        # Try to parse as JSON
        try:
            message_data = json.loads(stripped_line)
        except json.JSONDecodeError:
            # Not JSON (e.g., chat separator line), pass through
            cleaned_lines.append(raw_line)
            continue

        if not isinstance(message_data, dict):
            cleaned_lines.append(raw_line)
            continue

        if command == "history":
            cleaned_data = clean_imsg_history_message(message_data, command_rules)
        else:
            cleaned_data = message_data

        cleaned_lines.append(json.dumps(cleaned_data, separators=(',', ':'), ensure_ascii=False))

    return '\n'.join(cleaned_lines)


# CLI for testing
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: cat messages.json | python3 imsg_cleaner.py <command>", file=sys.stderr)
        print("Example: cat messages.json | python3 imsg_cleaner.py history", file=sys.stderr)
        sys.exit(1)

    command_arg = sys.argv[1]
    input_data = sys.stdin.read()

    input_bytes = len(input_data.encode('utf-8'))
    result = clean_imsg_output(command_arg, input_data)
    output_bytes = len(result.encode('utf-8'))

    savings_pct = ((input_bytes - output_bytes) / input_bytes * 100) if input_bytes > 0 else 0
    print(f"Before: {input_bytes:,} bytes | After: {output_bytes:,} bytes | "
          f"Saved: {input_bytes - output_bytes:,} bytes ({savings_pct:.1f}%)", file=sys.stderr)

    print(result)
