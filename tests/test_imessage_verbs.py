"""Tests for the Phase-2 iMessage verbs (P2-06).

Covers every verb the P2-06 task requires:
  READS  (via imsg_firewall): chats, history, group, search, watch,
                              whois, nickname, status
  WRITES (via imsg, mock-only): send, react, edit, unsend, delete,
                                mark-read, typing, notify,
                                chat create/rename/photo/add/remove/leave/delete,
                                launch, rpc

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every write test
    patches `mineru_cli.verbs.imessage.run_imsg` with a recorder,
    asserts the argv the wrapper WOULD send to imsg, and verifies the
    exit-code plumbing. Every read test patches
    `mineru_cli.verbs.imessage.run_imsg_firewall` the same way.
  - `imessage send` in particular is OUTBOUND and NEVER executed live
    against the operator's Messages - every test that touches it is patched.
    The docstring for `send` also documents the self-text-only rule
    from MEMORY.md.
  - The firewall-preservation invariant (argv[0] basename resolves to
    `imsg-firewall`, no bypass paths) already has dedicated tests in
    `test_imsg_firewall_wrapper.py`. A pair of belt-and-braces
    grep-style tests re-checks this specific verb file for the
    FORBIDDEN `imsg-named` literal and confirms writes don't leak
    through the firewall wrapper (or vice versa).
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List
from unittest.mock import patch

from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import imessage as imessage_verb


runner = CliRunner()


# --------------------------------------------------------------------- helpers


def _record(recorded: List[List[str]], returncode: int = 0):
    """Return a fake run_* recorder that captures argv and returns `returncode`.

    Shallow-copies the argv list so a later mutation of the recorded
    list cannot retroactively rewrite what we recorded.
    """

    def fake(args):
        recorded.append(list(args))
        return returncode

    return fake


def _invoke_read(args: List[str], returncode: int = 0):
    """Run CLI with the firewall wrapper patched. Returns (result, recorded)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.imessage.run_imsg_firewall",
        _record(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


def _invoke_write(args: List[str], returncode: int = 0):
    """Run CLI with the outbound imsg wrapper patched. Returns (result, recorded)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.imessage.run_imsg",
        _record(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS (via imsg_firewall)
# ============================================================================


# --- chats ----------------------------------------------------------------


def test_imessage_chats_default_no_extras() -> None:
    result, recorded = _invoke_read(["imessage", "chats"])
    assert result.exit_code == 0
    assert recorded == [["chats"]]


def test_imessage_chats_limit_passes_through() -> None:
    result, recorded = _invoke_read(["imessage", "chats", "--limit", "10"])
    assert result.exit_code == 0
    assert recorded == [["chats", "--limit", "10"]]


def test_imessage_chats_json_and_max_pass_through() -> None:
    """`--max` is accepted at the firewall level (translated to --limit); pass through unchanged."""
    result, recorded = _invoke_read(
        ["imessage", "chats", "--max", "5", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["chats", "--max", "5", "--json"]]


def test_imessage_chats_root_json_propagates() -> None:
    result, recorded = _invoke_read(["--json", "imessage", "chats"])
    assert result.exit_code == 0
    assert recorded == [["chats", "--json"]]


def test_imessage_chats_flag_not_duplicated_when_present_twice() -> None:
    result, recorded = _invoke_read(
        ["--json", "imessage", "chats", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["chats", "--json"]]


def test_imessage_chats_root_json_not_duplicated_when_alias_used() -> None:
    """A root `--json` must NOT stack with an alias like `--jsonOutput` in extras.

    Regression guard for the alias-blind idempotency check. The shared
    propagator only compared the literal `--json`, so
    `mineru --json imessage chats --jsonOutput` produced argv with
    both flags on the wire. imsg tolerates that today but a future
    alias would silently double-emit. This asserts the alias-aware
    override in `verbs/imessage.py` collapses the duplicate.
    """
    for alias in ("-j", "--json-output", "--jsonOutput"):
        result, recorded = _invoke_read(
            ["--json", "imessage", "chats", alias]
        )
        assert result.exit_code == 0, f"alias={alias!r} failed: {result.output}"
        # Only the user-supplied alias reaches the wire; no auto-appended `--json`.
        assert recorded == [["chats", alias]], (
            f"alias={alias!r} produced {recorded!r} (expected only the alias, no --json)"
        )


def test_imessage_chats_root_pretty_propagates() -> None:
    result, recorded = _invoke_read(["--pretty", "imessage", "chats"])
    assert result.exit_code == 0
    assert recorded == [["chats", "--pretty"]]


def test_imessage_chats_propagates_exit_77() -> None:
    """Firewall's 'all blocked' code surfaces as CLI exit 77."""
    result, _ = _invoke_read(["imessage", "chats"], returncode=77)
    assert result.exit_code == 77


def test_imessage_chats_propagates_exit_78() -> None:
    """Firewall's own error code surfaces as CLI exit 78."""
    result, _ = _invoke_read(["imessage", "chats"], returncode=78)
    assert result.exit_code == 78


def test_imessage_chats_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "chats", "--help"])
    assert result.exit_code == 0
    assert "firewall" in result.stdout.lower()


# --- history --------------------------------------------------------------


def test_imessage_history_translates_chat_id_positional_to_flag() -> None:
    """`imessage history <id>` -> `imsg-firewall history --chat-id <id>`."""
    result, recorded = _invoke_read(["imessage", "history", "299"])
    assert result.exit_code == 0
    assert recorded == [["history", "--chat-id", "299"]]


def test_imessage_history_extras_pass_through() -> None:
    result, recorded = _invoke_read(
        [
            "imessage", "history", "299",
            "--limit", "20",
            "--attachments",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "history", "--chat-id", "299",
            "--limit", "20",
            "--attachments",
            "--json",
        ]
    ]


def test_imessage_history_start_end_iso_pass_through() -> None:
    result, recorded = _invoke_read(
        [
            "imessage", "history", "1",
            "--start", "2026-07-01T00:00:00Z",
            "--end", "2026-07-27T00:00:00Z",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "history", "--chat-id", "1",
            "--start", "2026-07-01T00:00:00Z",
            "--end", "2026-07-27T00:00:00Z",
        ]
    ]


def test_imessage_history_root_json_propagates() -> None:
    result, recorded = _invoke_read(["--json", "imessage", "history", "1"])
    assert result.exit_code == 0
    assert recorded == [["history", "--chat-id", "1", "--json"]]


def test_imessage_history_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "history", "--help"])
    assert result.exit_code == 0


# --- group ----------------------------------------------------------------


def test_imessage_group_translates_chat_id_positional_to_flag() -> None:
    result, recorded = _invoke_read(["imessage", "group", "1"])
    assert result.exit_code == 0
    assert recorded == [["group", "--chat-id", "1"]]


def test_imessage_group_extras_pass_through() -> None:
    result, recorded = _invoke_read(
        ["imessage", "group", "1", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["group", "--chat-id", "1", "--json"]]


def test_imessage_group_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "group", "--help"])
    assert result.exit_code == 0


# --- search ---------------------------------------------------------------


def test_imessage_search_translates_query_positional_to_flag() -> None:
    """`imessage search "pizza"` -> `imsg-firewall search --query "pizza"`."""
    result, recorded = _invoke_read(["imessage", "search", "pizza tonight"])
    assert result.exit_code == 0
    assert recorded == [["search", "--query", "pizza tonight"]]


def test_imessage_search_match_and_limit_pass_through() -> None:
    result, recorded = _invoke_read(
        [
            "imessage", "search", "pizza",
            "--match", "exact",
            "--limit", "5",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "search", "--query", "pizza",
            "--match", "exact",
            "--limit", "5",
            "--json",
        ]
    ]


def test_imessage_search_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "search", "--help"])
    assert result.exit_code == 0


# --- watch ----------------------------------------------------------------


def test_imessage_watch_default_no_extras() -> None:
    result, recorded = _invoke_read(["imessage", "watch"])
    assert result.exit_code == 0
    assert recorded == [["watch"]]


def test_imessage_watch_all_streaming_flags_pass_through() -> None:
    result, recorded = _invoke_read(
        [
            "imessage", "watch",
            "--chat-id", "1",
            "--debounce", "250ms",
            "--attachments",
            "--reactions",
            "--bb-events",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "watch",
            "--chat-id", "1",
            "--debounce", "250ms",
            "--attachments",
            "--reactions",
            "--bb-events",
            "--json",
        ]
    ]


def test_imessage_watch_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "watch", "--help"])
    assert result.exit_code == 0


# --- whois ----------------------------------------------------------------


def test_imessage_whois_address_flag_passes_through() -> None:
    result, recorded = _invoke_read(
        ["imessage", "whois", "--address", "+14155551234"]
    )
    assert result.exit_code == 0
    assert recorded == [["whois", "--address", "+14155551234"]]


def test_imessage_whois_type_and_local_pass_through() -> None:
    result, recorded = _invoke_read(
        [
            "imessage", "whois",
            "--address", "foo@bar.com",
            "--type", "email",
            "--local",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "whois",
            "--address", "foo@bar.com",
            "--type", "email",
            "--local",
            "--json",
        ]
    ]


def test_imessage_whois_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "whois", "--help"])
    assert result.exit_code == 0


# --- nickname -------------------------------------------------------------


def test_imessage_nickname_address_flag_passes_through() -> None:
    result, recorded = _invoke_read(
        ["imessage", "nickname", "--address", "+14155551234"]
    )
    assert result.exit_code == 0
    assert recorded == [["nickname", "--address", "+14155551234"]]


def test_imessage_nickname_local_passes_through() -> None:
    result, recorded = _invoke_read(
        [
            "imessage", "nickname",
            "--address", "+14155551234",
            "--local",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "nickname",
            "--address", "+14155551234",
            "--local",
            "--json",
        ]
    ]


def test_imessage_nickname_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "nickname", "--help"])
    assert result.exit_code == 0


# --- status ---------------------------------------------------------------


def test_imessage_status_default_no_extras() -> None:
    result, recorded = _invoke_read(["imessage", "status"])
    assert result.exit_code == 0
    assert recorded == [["status"]]


def test_imessage_status_json_passes_through() -> None:
    result, recorded = _invoke_read(["imessage", "status", "--json"])
    assert result.exit_code == 0
    assert recorded == [["status", "--json"]]


def test_imessage_status_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "status", "--help"])
    assert result.exit_code == 0


# ============================================================================
# WRITE VERBS — PATCHED, NEVER EXECUTED LIVE (mocked via run_imsg recorder).
# ============================================================================


# --- send (WRITE, OUTBOUND) ----------------------------------------------


def test_imessage_send_to_and_text_flags_pass_through() -> None:
    """`imessage send --to X --text Y` -> `imsg send --to X --text Y`."""
    result, recorded = _invoke_write(
        ["imessage", "send", "--to", "+14155551212", "--text", "hi"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["send", "--to", "+14155551212", "--text", "hi"]
    ]


def test_imessage_send_service_flag_passes_through() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "send",
            "--to", "+14155551212",
            "--text", "hi",
            "--service", "imessage",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "send",
            "--to", "+14155551212",
            "--text", "hi",
            "--service", "imessage",
        ]
    ]


def test_imessage_send_extras_pass_through_attachment_and_chat_id() -> None:
    """Extras like --file, --chat-id, --no-sms-fallback pass through opaquely.

    Note on argv ordering: the verb builds `send --text <text>` first
    (from the Typer-consumed `--text` option), THEN appends everything
    else Typer didn't consume as trailing extras. So the recorded argv
    starts with `--text hi` even though the user typed `--chat-id`
    first on the CLI. `imsg send` is order-insensitive for its own
    flags, so this reordering is behavior-preserving.
    """
    result, recorded = _invoke_write(
        [
            "imessage", "send",
            "--chat-id", "1",
            "--text", "hi",
            "--file", "/tmp/pic.jpg",
            "--no-sms-fallback",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "send",
            "--text", "hi",
            "--chat-id", "1",
            "--file", "/tmp/pic.jpg",
            "--no-sms-fallback",
        ]
    ]


def test_imessage_send_root_json_propagates() -> None:
    result, recorded = _invoke_write(
        [
            "--json", "imessage", "send",
            "--to", "+14155551212",
            "--text", "hi",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "send",
            "--to", "+14155551212",
            "--text", "hi",
            "--json",
        ]
    ]


def test_imessage_send_help_smoke_contains_self_text_rule() -> None:
    """`imessage send --help` MUST document the always-self-text safety rule.

    Load-bearing per MEMORY.md's `feedback_imessage_testing.md` entry.
    A regression that dropped this warning would fail here.
    """
    result = runner.invoke(app, ["imessage", "send", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    # The self-text rule must be visible somewhere in the help output.
    assert "self-text" in lowered or "self text" in lowered or "own" in lowered
    # And the outbound / write intent must be clear.
    assert "write" in lowered or "outbound" in lowered or "send" in lowered


def test_imessage_send_propagates_engine_exit_code() -> None:
    """Belt-and-braces: send never runs live; synthetic non-zero exit propagates."""
    result, _ = _invoke_write(
        ["imessage", "send", "--to", "+15551234", "--text", "x"],
        returncode=3,
    )
    assert result.exit_code == 3


# --- react (WRITE) -------------------------------------------------------


def test_imessage_react_extras_pass_through() -> None:
    result, recorded = _invoke_write(
        ["imessage", "react", "--chat-id", "1", "--reaction", "like"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["react", "--chat-id", "1", "--reaction", "like"]
    ]


def test_imessage_react_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "react", "--help"])
    assert result.exit_code == 0


# --- edit (WRITE) --------------------------------------------------------


def test_imessage_edit_extras_pass_through() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "edit",
            "--chat", "iMessage;-;+15551234",
            "--message", "abc-guid",
            "--new-text", "updated",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "edit",
            "--chat", "iMessage;-;+15551234",
            "--message", "abc-guid",
            "--new-text", "updated",
        ]
    ]


def test_imessage_edit_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "edit", "--help"])
    assert result.exit_code == 0


# --- unsend (WRITE) ------------------------------------------------------


def test_imessage_unsend_extras_pass_through() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "unsend",
            "--chat", "iMessage;-;+15551234",
            "--message", "abc-guid",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "unsend",
            "--chat", "iMessage;-;+15551234",
            "--message", "abc-guid",
        ]
    ]


def test_imessage_unsend_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "unsend", "--help"])
    assert result.exit_code == 0


# --- delete (WRITE, DESTRUCTIVE) — routes to delete-message --------------


def test_imessage_delete_routes_to_delete_message_subverb() -> None:
    """`mineru imessage delete ...` -> `imsg delete-message ...` (name translation)."""
    result, recorded = _invoke_write(
        [
            "imessage", "delete",
            "--chat", "iMessage;-;+15551234",
            "--message", "abc-guid",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "delete-message",
            "--chat", "iMessage;-;+15551234",
            "--message", "abc-guid",
        ]
    ]


def test_imessage_delete_help_smoke_mentions_destructive() -> None:
    result = runner.invoke(app, ["imessage", "delete", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "destructive" in lowered or "write" in lowered


# --- mark-read (WRITE) — routes to imsg's `read` subverb -----------------


def test_imessage_mark_read_routes_to_read_subverb() -> None:
    """`mineru imessage mark-read ...` -> `imsg read ...` (clearer name)."""
    result, recorded = _invoke_write(
        ["imessage", "mark-read", "--chat-id", "1"]
    )
    assert result.exit_code == 0
    assert recorded == [["read", "--chat-id", "1"]]


def test_imessage_mark_read_handle_flag_passes_through() -> None:
    result, recorded = _invoke_write(
        ["imessage", "mark-read", "--to", "+14155551234"]
    )
    assert result.exit_code == 0
    assert recorded == [["read", "--to", "+14155551234"]]


def test_imessage_mark_read_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "mark-read", "--help"])
    assert result.exit_code == 0


# --- typing (WRITE) ------------------------------------------------------


def test_imessage_typing_extras_pass_through() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "typing",
            "--to", "+14155551234",
            "--duration", "5s",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "typing",
            "--to", "+14155551234",
            "--duration", "5s",
        ]
    ]


def test_imessage_typing_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "typing", "--help"])
    assert result.exit_code == 0


# --- notify (WRITE) — routes to notify-anyways ---------------------------


def test_imessage_notify_routes_to_notify_anyways() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "notify",
            "--chat", "iMessage;-;+15551234",
            "--message", "abc-guid",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "notify-anyways",
            "--chat", "iMessage;-;+15551234",
            "--message", "abc-guid",
        ]
    ]


def test_imessage_notify_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "notify", "--help"])
    assert result.exit_code == 0


# --- launch (WRITE) ------------------------------------------------------


def test_imessage_launch_default_no_extras() -> None:
    result, recorded = _invoke_write(["imessage", "launch"])
    assert result.exit_code == 0
    assert recorded == [["launch"]]


def test_imessage_launch_kill_only_passes_through() -> None:
    result, recorded = _invoke_write(["imessage", "launch", "--kill-only"])
    assert result.exit_code == 0
    assert recorded == [["launch", "--kill-only"]]


def test_imessage_launch_dylib_passes_through() -> None:
    result, recorded = _invoke_write(
        ["imessage", "launch", "--dylib", "/tmp/mydylib.dylib"]
    )
    assert result.exit_code == 0
    assert recorded == [["launch", "--dylib", "/tmp/mydylib.dylib"]]


def test_imessage_launch_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "launch", "--help"])
    assert result.exit_code == 0


# --- rpc (WRITE, INTERACTIVE) -------------------------------------------


def test_imessage_rpc_default_no_extras() -> None:
    result, recorded = _invoke_write(["imessage", "rpc"])
    assert result.exit_code == 0
    assert recorded == [["rpc"]]


def test_imessage_rpc_db_extras_pass_through() -> None:
    result, recorded = _invoke_write(
        ["imessage", "rpc", "--db", "/tmp/chat.db"]
    )
    assert result.exit_code == 0
    assert recorded == [["rpc", "--db", "/tmp/chat.db"]]


def test_imessage_rpc_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "rpc", "--help"])
    assert result.exit_code == 0


# ============================================================================
# CHAT LIFECYCLE (WRITE, mock-only) -- all under `imessage chat` sub-app
# ============================================================================


def test_imessage_chat_create_extras_pass_through() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "chat", "create",
            "--addresses", "+15551234567,+15559876543",
            "--name", "Crew",
            "--text", "gm",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "chat-create",
            "--addresses", "+15551234567,+15559876543",
            "--name", "Crew",
            "--text", "gm",
        ]
    ]


def test_imessage_chat_create_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "chat", "create", "--help"])
    assert result.exit_code == 0


def test_imessage_chat_rename_routes_to_chat_name() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "chat", "rename",
            "--chat", "iMessage;+;chat0000",
            "--name", "New Name",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "chat-name",
            "--chat", "iMessage;+;chat0000",
            "--name", "New Name",
        ]
    ]


def test_imessage_chat_rename_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "chat", "rename", "--help"])
    assert result.exit_code == 0


def test_imessage_chat_photo_extras_pass_through() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "chat", "photo",
            "--chat", "iMessage;+;chat0000",
            "--file", "/tmp/group.jpg",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "chat-photo",
            "--chat", "iMessage;+;chat0000",
            "--file", "/tmp/group.jpg",
        ]
    ]


def test_imessage_chat_photo_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "chat", "photo", "--help"])
    assert result.exit_code == 0


def test_imessage_chat_add_routes_to_chat_add_member() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "chat", "add",
            "--chat", "iMessage;+;chat0000",
            "--address", "+15551234567",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "chat-add-member",
            "--chat", "iMessage;+;chat0000",
            "--address", "+15551234567",
        ]
    ]


def test_imessage_chat_add_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "chat", "add", "--help"])
    assert result.exit_code == 0


def test_imessage_chat_remove_routes_to_chat_remove_member() -> None:
    result, recorded = _invoke_write(
        [
            "imessage", "chat", "remove",
            "--chat", "iMessage;+;chat0000",
            "--address", "+15551234567",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "chat-remove-member",
            "--chat", "iMessage;+;chat0000",
            "--address", "+15551234567",
        ]
    ]


def test_imessage_chat_remove_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "chat", "remove", "--help"])
    assert result.exit_code == 0


def test_imessage_chat_leave_extras_pass_through() -> None:
    result, recorded = _invoke_write(
        ["imessage", "chat", "leave", "--chat", "iMessage;+;chat0000"]
    )
    assert result.exit_code == 0
    assert recorded == [["chat-leave", "--chat", "iMessage;+;chat0000"]]


def test_imessage_chat_leave_help_smoke() -> None:
    result = runner.invoke(app, ["imessage", "chat", "leave", "--help"])
    assert result.exit_code == 0


def test_imessage_chat_delete_extras_pass_through() -> None:
    """Chat-level delete (WRITE, DESTRUCTIVE). NEVER executed live."""
    result, recorded = _invoke_write(
        ["imessage", "chat", "delete", "--chat", "iMessage;-;+15551234"]
    )
    assert result.exit_code == 0
    assert recorded == [["chat-delete", "--chat", "iMessage;-;+15551234"]]


def test_imessage_chat_delete_help_smoke_mentions_destructive() -> None:
    result = runner.invoke(app, ["imessage", "chat", "delete", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "destructive" in lowered or "write" in lowered


# ============================================================================
# READ/WRITE BOUNDARY (grep-invariant guards on the verb file).
# ============================================================================


VERB_SRC = Path(imessage_verb.__file__).read_text()


def test_imessage_verb_source_never_invokes_forbidden_imsg_named_path() -> None:
    """The verb file must not contain the FORBIDDEN `imsg-named` path anywhere.

    TOOLS.md explicitly marks `$MINERU_HOME/bin/imsg-named` as forbidden
    for reads. A regression that reached for it as a live subprocess
    string literal would fail here. Docstring prose mentions are OK
    (they're educational context, not code paths).
    """
    assert 'imsg-named"' not in VERB_SRC
    assert "imsg-named'" not in VERB_SRC


# Files across mineru_cli/ that are load-bearing for imessage routing.
# Broader than the single verbs/imessage.py file so a wrapper regression
# that reached for `imsg-named` would also fail here.
_MINERU_CLI_DIR = Path(imessage_verb.__file__).parent.parent
_IMSG_NAMED_SCAN_FILES: tuple[Path, ...] = (
    _MINERU_CLI_DIR / "verbs" / "imessage.py",
    _MINERU_CLI_DIR / "wrappers" / "imsg.py",
    _MINERU_CLI_DIR / "wrappers" / "imsg_firewall.py",
)


def _string_literals_outside_docstrings(source: str) -> list[str]:
    """Return every str-constant literal in `source` that is NOT a docstring.

    A docstring is defined here as the very first statement of a Module /
    FunctionDef / AsyncFunctionDef / ClassDef whose value is a plain string
    constant (matches `ast.get_docstring`'s definition). Docstrings are
    intentionally allowed to mention `imsg-named` for educational context;
    a subprocess argument or `shutil.which` argument is not.

    Error-message strings (e.g. inside an `f"...forbidden `imsg-named`..."`
    in a stderr echo) are NOT filtered out here — they're rare and callers
    of this helper handle them via the caller-side path/spawn context
    check.
    """
    tree = ast.parse(source)

    docstring_nodes: set[int] = set()
    for parent in ast.walk(tree):
        if isinstance(parent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(parent, "body", [])
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstring_nodes.add(id(value))

    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_nodes:
                continue
            literals.append(node.value)
    return literals


def test_imessage_routing_never_names_imsg_named_as_spawn_target() -> None:
    """Scan verb + both wrapper modules for spawn-shaped uses of `imsg-named`.

    Broader than the character-literal path check above: also catches a
    regression that used the bare name via `subprocess.run(['imsg-named',
    ...])` or `shutil.which('imsg-named')` — both of which would slip
    past the absolute-path grep in the sibling test.

    The check is over non-docstring string literals. A literal that
    equals `imsg-named`, or ends with `/imsg-named`, is flagged. Error
    messages that mention the forbidden path *by name* are still allowed
    when they're part of a diagnostic string that also contains
    contextual prose (e.g. `f"the forbidden `imsg-named` path..."`) —
    those exceed the strict equality check.
    """
    forbidden_exact = "imsg-named"
    forbidden_suffix = "/imsg-named"
    for path in _IMSG_NAMED_SCAN_FILES:
        assert path.is_file(), f"expected {path} to exist in the build"
        source = path.read_text()
        for literal in _string_literals_outside_docstrings(source):
            # Skip diagnostic prose: only bare-name and path-ending
            # forms are spawn targets.
            if literal == forbidden_exact:
                raise AssertionError(
                    f"{path}: contains bare string literal 'imsg-named'; "
                    "could be used as a spawn target (subprocess argv, "
                    "shutil.which). TOOLS.md marks this path as FORBIDDEN "
                    "for reads."
                )
            if literal.endswith(forbidden_suffix):
                raise AssertionError(
                    f"{path}: contains path literal ending in '/imsg-named' "
                    f"({literal!r}); would be a spawn-target regression."
                )


def test_imessage_verb_source_never_directly_invokes_subprocess() -> None:
    """The verb file must route via the wrappers, never directly via subprocess."""
    for forbidden in ("import subprocess", "from subprocess"):
        assert forbidden not in VERB_SRC, (
            f"imessage verb must route via the wrapper modules, not "
            f"direct subprocess; found {forbidden!r}"
        )


def test_imessage_verb_source_uses_both_wrappers() -> None:
    """The verb file imports BOTH wrappers - reads through firewall, writes direct.

    Both imports must be present. A regression that funneled writes
    through the firewall (or reads through the write wrapper) would
    lose the read/write boundary this test file exists to enforce.
    """
    assert "from mineru_cli.wrappers.imsg_firewall import" in VERB_SRC
    assert "from mineru_cli.wrappers.imsg import" in VERB_SRC


def test_imessage_verb_source_write_verbs_never_call_run_imsg_firewall() -> None:
    """Manual sanity check on the mapping table.

    Each WRITE verb body (`send`, `react`, `edit`, `unsend`, `delete`,
    `mark_read`, `typing`, `notify`, `launch`, `rpc`, `chat_*`) uses
    `run_imsg(...)`; each READ verb body uses `run_imsg_firewall(...)`.
    A crossover would be a firewall-preservation regression (READ
    through non-firewalled binary) or an unnecessary firewall check
    (WRITE through firewall). We assert BOTH directions programmatically
    by walking the module's AST and inspecting the `Call.func` names
    inside each verb body — a robust replacement for the earlier
    `.find()` + character-offset heuristic, which silently missed write
    verbs entirely.
    """
    tree = ast.parse(VERB_SRC)

    def called_names(func_node: ast.FunctionDef) -> set[str]:
        """Return the set of top-level callable names invoked in `func_node`."""
        names: set[str] = set()
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
        return names

    # Collect every FunctionDef in the file keyed by its Python name.
    # (For the `chat` sub-app, the def names are `chat_create`, `chat_rename`, etc.)
    verb_defs: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            verb_defs[node.name] = node

    read_verb_names = [
        "chats", "history", "group", "search",
        "watch", "whois", "nickname", "status",
    ]
    write_verb_names = [
        # Top-level write verbs.
        "send", "react", "edit", "unsend", "delete",
        "mark_read", "typing", "notify", "launch", "rpc",
        # `chat` sub-app write verbs.
        "chat_create", "chat_rename", "chat_photo",
        "chat_add", "chat_remove", "chat_leave", "chat_delete",
    ]

    for verb_name in read_verb_names:
        assert verb_name in verb_defs, (
            f"missing read verb def {verb_name!r} in source"
        )
        called = called_names(verb_defs[verb_name])
        assert "run_imsg_firewall" in called, (
            f"READ verb {verb_name!r} must call run_imsg_firewall(...); "
            f"got calls={called}"
        )
        assert "run_imsg" not in called, (
            f"READ verb {verb_name!r} must NOT call run_imsg(...) directly; "
            f"got calls={called}"
        )

    for verb_name in write_verb_names:
        assert verb_name in verb_defs, (
            f"missing write verb def {verb_name!r} in source"
        )
        called = called_names(verb_defs[verb_name])
        assert "run_imsg" in called, (
            f"WRITE verb {verb_name!r} must call run_imsg(...); "
            f"got calls={called}"
        )
        assert "run_imsg_firewall" not in called, (
            f"WRITE verb {verb_name!r} must NOT call run_imsg_firewall(...); "
            "SECURITY.md flags this as a firewall-boundary regression. "
            f"got calls={called}"
        )


# ============================================================================
# ROOT `--help` REGRESSION GUARD
# ============================================================================


def test_imessage_help_lists_every_wired_verb() -> None:
    """`mineru imessage --help` surfaces every wired sub-verb."""
    result = runner.invoke(app, ["imessage", "--help"])
    assert result.exit_code == 0
    expected_verbs = (
        # reads
        "chats", "history", "group", "search", "watch",
        "whois", "nickname", "status",
        # writes
        "send", "react", "edit", "unsend", "delete",
        "mark-read", "typing", "notify", "launch", "rpc",
        # chat sub-app
        "chat",
    )
    for verb in expected_verbs:
        assert verb in result.stdout, (
            f"`mineru imessage --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_imessage_help_mentions_firewall_and_boundary() -> None:
    """Noun-level help surfaces the firewall + read/write boundary intent."""
    result = runner.invoke(app, ["imessage", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "imsg-firewall" in lowered or "firewall" in lowered


def test_imessage_chat_help_lists_every_lifecycle_verb() -> None:
    """`mineru imessage chat --help` surfaces every chat-lifecycle sub-verb."""
    result = runner.invoke(app, ["imessage", "chat", "--help"])
    assert result.exit_code == 0
    expected_verbs = (
        "create", "rename", "photo", "add", "remove", "leave", "delete",
    )
    for verb in expected_verbs:
        assert verb in result.stdout, (
            f"`mineru imessage chat --help` missing {verb!r}. "
            f"Output:\n{result.stdout}"
        )
