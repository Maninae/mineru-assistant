"""Tests for the Phase-2 Tasks verbs (P2-05, tasks quarter).

Covers every verb the P2-05 task requires for tasks:
  READ  : lists, list, get
  WRITE : add, update, done, undo, delete (DESTRUCTIVE), clear (DESTRUCTIVE)

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every test patches
    `mineru_cli.verbs.tasks.run_gog_firewall` with a recorder, asserts
    the argv the wrapper WOULD send to the firewall, and verifies the
    exit-code plumbing.
  - `tasks delete` and `tasks clear` are DESTRUCTIVE and NEVER executed
    live during dev — every test that touches them is patched.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
  - The firewall-preservation invariant (argv[0] basename ==
    `gog-firewall`, no bypass flags) already has dedicated tests in
    `test_gmail_wrapper.py`; the wrapper is the same. A pair of
    belt-and-braces grep-style tests re-checks that this specific verb
    file has no bypass literals.
  - The "`lists` (plural) vs `list` (singular)" distinction is
    load-bearing — plural enumerates task LISTS, singular enumerates
    TASKS in one list. Dedicated tests prove both routes.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import patch

from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import tasks as tasks_verb


runner = CliRunner()


# --------------------------------------------------------------------- helpers


def _record_run_gog_firewall(recorded: List[List[str]], returncode: int = 0):
    def fake(args, **kwargs):
        recorded.append(list(args))
        return returncode

    return fake


def _invoke(args: List[str], returncode: int = 0):
    """Run the CLI with a patched wrapper. Returns (CliResult, recorded_argv_list)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.tasks.run_gog_firewall",
        _record_run_gog_firewall(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS
# ============================================================================


# --- lists (plural: list the task LISTS) --------------------------------


def test_tasks_lists_routes_to_gog_lists_list_default_subverb() -> None:
    """`tasks lists` → `gog tasks lists list` (mineru omits the default subverb)."""
    result, recorded = _invoke(["tasks", "lists"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "lists", "list"]]


def test_tasks_lists_extras_pass_through() -> None:
    result, recorded = _invoke(["tasks", "lists", "--json"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "lists", "list", "--json"]]


def test_tasks_lists_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "tasks", "lists"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "lists", "list", "--json"]]


def test_tasks_lists_help_smoke() -> None:
    result = runner.invoke(app, ["tasks", "lists", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "lists" in lowered


# --- list (singular: list TASKS in one list) ---------------------------


def test_tasks_list_without_list_id_omits_positional() -> None:
    """`tasks list` (no id) → gog default list."""
    result, recorded = _invoke(["tasks", "list"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "list"]]


def test_tasks_list_with_list_id_forwards_positional() -> None:
    result, recorded = _invoke(["tasks", "list", "LIST_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "list", "LIST_XYZ"]]


def test_tasks_list_extras_pass_through_filters() -> None:
    result, recorded = _invoke(
        [
            "tasks", "list", "LIST_XYZ",
            "--show-completed",
            "--due-min", "2026-01-01",
            "--max", "50",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "tasks", "list", "LIST_XYZ",
            "--show-completed",
            "--due-min", "2026-01-01",
            "--max", "50",
            "--json",
        ]
    ]


def test_tasks_list_root_pretty_propagates_without_list_id() -> None:
    result, recorded = _invoke(["--pretty", "tasks", "list"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "list", "--pretty"]]


def test_tasks_list_help_smoke() -> None:
    result = runner.invoke(app, ["tasks", "list", "--help"])
    assert result.exit_code == 0


def test_tasks_list_propagates_exit_77() -> None:
    result, _ = _invoke(["tasks", "list"], returncode=77)
    assert result.exit_code == 77


def test_tasks_list_propagates_exit_78() -> None:
    result, _ = _invoke(["tasks", "list"], returncode=78)
    assert result.exit_code == 78


# --- items (renamed from `list` on 2026-09-16) --------------------------
#
# The canonical spelling is now `tasks items`; `tasks list` remains as a
# hidden Typer alias for ~90 days, with a one-line stderr deprecation
# notice on use. All the `tasks list` tests above continue to run under
# the alias to lock in backward compatibility; the block below pins the
# canonical name + the deprecation-notice contract.


def test_tasks_items_without_list_id_matches_list_alias() -> None:
    """`tasks items` produces the same argv as the deprecated `tasks list`."""
    result, recorded = _invoke(["tasks", "items"])
    assert result.exit_code == 0
    # Both spellings dispatch to gog's own singular `tasks list` subverb —
    # the rename lives on the mineru surface only.
    assert recorded == [["tasks", "list"]]


def test_tasks_items_with_list_id_forwards_positional() -> None:
    result, recorded = _invoke(["tasks", "items", "LIST_ABC"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "list", "LIST_ABC"]]


def test_tasks_items_help_smoke_mentions_items_and_lists_distinction() -> None:
    result = runner.invoke(app, ["tasks", "items", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "items" in lowered
    # Docstring explicitly names the sibling `lists` verb so future readers
    # see both halves of the collision resolution.
    assert "lists" in lowered


def test_tasks_list_alias_still_works_and_emits_deprecation_notice() -> None:
    """The hidden `list` alias dispatches identically AND emits DEPRECATED on stderr."""
    result, recorded = _invoke(["tasks", "list", "LIST_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "list", "LIST_XYZ"]]
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "DEPRECATED:" in combined, combined
    assert "tasks list" in combined
    assert "tasks items" in combined


# --- get -----------------------------------------------------------------


def test_tasks_get_forwards_list_and_task_ids() -> None:
    result, recorded = _invoke(["tasks", "get", "LIST_XYZ", "TASK_ABC"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "get", "LIST_XYZ", "TASK_ABC"]]


def test_tasks_get_extras_pass_through_json() -> None:
    result, recorded = _invoke(
        ["tasks", "get", "LIST_XYZ", "TASK_ABC", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["tasks", "get", "LIST_XYZ", "TASK_ABC", "--json"]
    ]


def test_tasks_get_help_smoke() -> None:
    result = runner.invoke(app, ["tasks", "get", "--help"])
    assert result.exit_code == 0


# ============================================================================
# WRITE VERBS — PATCHED, NEVER EXECUTED LIVE
# ============================================================================


# --- add (WRITE) --------------------------------------------------------


def test_tasks_add_forwards_list_id_and_title() -> None:
    result, recorded = _invoke(
        ["tasks", "add", "LIST_XYZ", "--title", "Pick up milk"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["tasks", "add", "LIST_XYZ", "--title", "Pick up milk"]
    ]


def test_tasks_add_extras_pass_through_notes_due_and_no_input() -> None:
    result, recorded = _invoke(
        [
            "tasks", "add", "LIST_XYZ",
            "--title", "Pick up milk",
            "--notes", "2%, whole for River",
            "--due", "2026-08-01",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "tasks", "add", "LIST_XYZ",
            "--title", "Pick up milk",
            "--notes", "2%, whole for River",
            "--due", "2026-08-01",
            "--no-input",
        ]
    ]


def test_tasks_add_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "tasks", "add", "LIST_XYZ", "--title", "t"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["tasks", "add", "LIST_XYZ", "--title", "t", "--json"]
    ]


def test_tasks_add_help_smoke() -> None:
    result = runner.invoke(app, ["tasks", "add", "--help"])
    assert result.exit_code == 0
    assert "--title" in result.stdout
    combined = result.stdout.lower()
    assert "write" in combined


def test_tasks_add_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        ["tasks", "add", "LIST_XYZ", "--title", "t"], returncode=3
    )
    assert result.exit_code == 3


# --- update (WRITE) -----------------------------------------------------


def test_tasks_update_forwards_list_and_task_ids() -> None:
    result, recorded = _invoke(
        ["tasks", "update", "LIST_XYZ", "TASK_ABC"]
    )
    assert result.exit_code == 0
    assert recorded == [["tasks", "update", "LIST_XYZ", "TASK_ABC"]]


def test_tasks_update_extras_pass_through_field_flags() -> None:
    result, recorded = _invoke(
        [
            "tasks", "update", "LIST_XYZ", "TASK_ABC",
            "--title", "New title",
            "--notes", "New notes",
            "--due", "2026-09-01",
            "--status", "needsAction",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "tasks", "update", "LIST_XYZ", "TASK_ABC",
            "--title", "New title",
            "--notes", "New notes",
            "--due", "2026-09-01",
            "--status", "needsAction",
            "--no-input",
        ]
    ]


def test_tasks_update_help_smoke() -> None:
    result = runner.invoke(app, ["tasks", "update", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "write" in combined


# --- done (WRITE) -------------------------------------------------------


def test_tasks_done_forwards_list_and_task_ids() -> None:
    result, recorded = _invoke(["tasks", "done", "LIST_XYZ", "TASK_ABC"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "done", "LIST_XYZ", "TASK_ABC"]]


def test_tasks_done_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["tasks", "done", "LIST_XYZ", "TASK_ABC", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["tasks", "done", "LIST_XYZ", "TASK_ABC", "--no-input"]
    ]


def test_tasks_done_help_smoke() -> None:
    result = runner.invoke(app, ["tasks", "done", "--help"])
    assert result.exit_code == 0


# --- undo (WRITE) -------------------------------------------------------


def test_tasks_undo_forwards_list_and_task_ids() -> None:
    result, recorded = _invoke(["tasks", "undo", "LIST_XYZ", "TASK_ABC"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "undo", "LIST_XYZ", "TASK_ABC"]]


def test_tasks_undo_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["tasks", "undo", "LIST_XYZ", "TASK_ABC", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["tasks", "undo", "LIST_XYZ", "TASK_ABC", "--no-input"]
    ]


def test_tasks_undo_help_smoke() -> None:
    result = runner.invoke(app, ["tasks", "undo", "--help"])
    assert result.exit_code == 0


# --- delete (WRITE, DESTRUCTIVE — NEVER executed live) ------------------


def test_tasks_delete_forwards_list_and_task_ids() -> None:
    result, recorded = _invoke(
        ["tasks", "delete", "LIST_XYZ", "TASK_ABC"]
    )
    assert result.exit_code == 0
    assert recorded == [["tasks", "delete", "LIST_XYZ", "TASK_ABC"]]


def test_tasks_delete_extras_pass_through_force() -> None:
    result, recorded = _invoke(
        ["tasks", "delete", "LIST_XYZ", "TASK_ABC", "--force", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "tasks", "delete", "LIST_XYZ", "TASK_ABC",
            "--force", "--no-input",
        ]
    ]


def test_tasks_delete_help_smoke() -> None:
    result = runner.invoke(app, ["tasks", "delete", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "destructive" in combined or "write" in combined


def test_tasks_delete_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        ["tasks", "delete", "LIST_XYZ", "TASK_ABC"], returncode=4
    )
    assert result.exit_code == 4


# --- clear (WRITE, DESTRUCTIVE — NEVER executed live) -------------------


def test_tasks_clear_forwards_list_id() -> None:
    result, recorded = _invoke(["tasks", "clear", "LIST_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["tasks", "clear", "LIST_XYZ"]]


def test_tasks_clear_extras_pass_through_force() -> None:
    result, recorded = _invoke(
        ["tasks", "clear", "LIST_XYZ", "--force", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["tasks", "clear", "LIST_XYZ", "--force", "--no-input"]
    ]


def test_tasks_clear_help_smoke() -> None:
    result = runner.invoke(app, ["tasks", "clear", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "destructive" in combined or "write" in combined


def test_tasks_clear_propagates_engine_exit_code() -> None:
    result, _ = _invoke(["tasks", "clear", "LIST_XYZ"], returncode=5)
    assert result.exit_code == 5


# ============================================================================
# FIREWALL-PRESERVATION BELT-AND-BRACES (grep the source for bypass literals).
# ============================================================================


VERB_SRC = Path(tasks_verb.__file__).read_text()


def test_tasks_verb_source_has_no_bypass_flags() -> None:
    """The tasks verb file must not inject firewall-bypassing flags anywhere."""
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_tasks_verb_source_never_invokes_raw_gog_binary() -> None:
    """A regression that reached for a direct raw-gog string literal fails here."""
    for forbidden in ("import subprocess", "from subprocess"):
        assert forbidden not in VERB_SRC, (
            f"tasks verb must route via the wrapper, not direct subprocess; "
            f"found {forbidden!r}"
        )


# ============================================================================
# ROOT `--help` REGRESSION GUARD
# ============================================================================


def test_tasks_help_lists_every_wired_verb() -> None:
    """`mineru tasks --help` surfaces every wired sub-verb."""
    result = runner.invoke(app, ["tasks", "--help"])
    assert result.exit_code == 0
    expected_verbs = (
        "lists", "list", "get",
        "add", "update", "done", "undo", "delete", "clear",
    )
    for verb in expected_verbs:
        assert verb in result.stdout, (
            f"`mineru tasks --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_tasks_help_mentions_firewall_and_gog_firewall() -> None:
    """The noun-level help surfaces the firewall preservation + gog-firewall backing."""
    result = runner.invoke(app, ["tasks", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "gog-firewall" in lowered
    assert "firewall" in lowered
