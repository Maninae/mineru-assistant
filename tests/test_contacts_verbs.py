"""Tests for the Phase-2 Contacts verbs (P2-05, contacts quarter).

Covers every verb the P2-05 task requires for contacts:
  READ  : lookup, search, list, get, directory, other
  WRITE : create, update, delete (DESTRUCTIVE)

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every test patches
    `mineru_cli.verbs.contacts.run_gog_firewall` with a recorder,
    asserts the argv the wrapper WOULD send to the firewall, and
    verifies the exit-code plumbing.
  - `contacts delete` in particular is DESTRUCTIVE and NEVER executed
    live against the operator's Google account — every test that touches it
    is patched. The tests below assert argv only.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
  - The firewall-preservation invariant (argv[0] basename ==
    `gog-firewall`, no bypass flags) already has dedicated tests in
    `test_gmail_wrapper.py`; the wrapper is the same. A pair of
    belt-and-braces grep-style tests re-checks that this specific verb
    file has no bypass literals.
  - `contacts lookup` is a mineru convenience alias that routes to
    `gog-firewall contacts search`, per the module docstring. Test
    proves the alias emits the right argv.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import patch

from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import contacts as contacts_verb


runner = CliRunner()


# --------------------------------------------------------------------- helpers


def _record_run_gog_firewall(recorded: List[List[str]], returncode: int = 0):
    """Return a fake `run_gog_firewall` that records argv and returns `returncode`.

    Shallow-copies the argv list so a later mutation of the recorded
    list cannot retroactively rewrite what we recorded.
    """

    def fake(args, **kwargs):
        recorded.append(list(args))
        return returncode

    return fake


def _invoke(args: List[str], returncode: int = 0):
    """Run the CLI with a patched wrapper. Returns (CliResult, recorded_argv_list)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.contacts.run_gog_firewall",
        _record_run_gog_firewall(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS
# ============================================================================


# --- lookup (mineru alias → gog contacts search) --------------------------


def test_contacts_lookup_routes_to_gog_contacts_search() -> None:
    """`contacts lookup <handle>` → `gog contacts search <handle>` (mineru alias)."""
    result, recorded = _invoke(["contacts", "lookup", "alice@example.com"])
    assert result.exit_code == 0
    assert recorded == [["contacts", "search", "alice@example.com"]]


def test_contacts_lookup_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["contacts", "lookup", "+14155551234", "--max", "5", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["contacts", "search", "+14155551234", "--max", "5", "--json"]
    ]


def test_contacts_lookup_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "contacts", "lookup", "alice"])
    assert result.exit_code == 0
    assert recorded == [["contacts", "search", "alice", "--json"]]


def test_contacts_lookup_help_smoke() -> None:
    result = runner.invoke(app, ["contacts", "lookup", "--help"])
    assert result.exit_code == 0
    assert "alias" in result.stdout.lower() or "search" in result.stdout.lower()


# --- search ---------------------------------------------------------------


def test_contacts_search_forwards_query() -> None:
    result, recorded = _invoke(["contacts", "search", "alice"])
    assert result.exit_code == 0
    assert recorded == [["contacts", "search", "alice"]]


def test_contacts_search_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["contacts", "search", "alice", "--max", "10", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["contacts", "search", "alice", "--max", "10", "--json"]
    ]


def test_contacts_search_help_smoke() -> None:
    result = runner.invoke(app, ["contacts", "search", "--help"])
    assert result.exit_code == 0
    assert "firewall" in result.stdout.lower()


def test_contacts_search_propagates_exit_77() -> None:
    result, _ = _invoke(["contacts", "search", "x"], returncode=77)
    assert result.exit_code == 77


def test_contacts_search_propagates_exit_78() -> None:
    result, _ = _invoke(["contacts", "search", "x"], returncode=78)
    assert result.exit_code == 78


# --- list -----------------------------------------------------------------


def test_contacts_list_no_positional_needed() -> None:
    result, recorded = _invoke(["contacts", "list"])
    assert result.exit_code == 0
    assert recorded == [["contacts", "list"]]


def test_contacts_list_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["contacts", "list", "--max", "50", "--page", "abc", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["contacts", "list", "--max", "50", "--page", "abc", "--json"]
    ]


def test_contacts_list_root_pretty_propagates() -> None:
    result, recorded = _invoke(["--pretty", "contacts", "list"])
    assert result.exit_code == 0
    assert recorded == [["contacts", "list", "--pretty"]]


def test_contacts_list_help_smoke() -> None:
    result = runner.invoke(app, ["contacts", "list", "--help"])
    assert result.exit_code == 0


# --- get ------------------------------------------------------------------


def test_contacts_get_forwards_resource_name() -> None:
    result, recorded = _invoke(["contacts", "get", "people/c1234567890"])
    assert result.exit_code == 0
    assert recorded == [["contacts", "get", "people/c1234567890"]]


def test_contacts_get_extras_pass_through_json() -> None:
    result, recorded = _invoke(
        ["contacts", "get", "people/c1234567890", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["contacts", "get", "people/c1234567890", "--json"]]


def test_contacts_get_help_smoke() -> None:
    result = runner.invoke(app, ["contacts", "get", "--help"])
    assert result.exit_code == 0


# --- directory ------------------------------------------------------------


def test_contacts_directory_routes_to_gog_directory_list() -> None:
    """`contacts directory` → `gog contacts directory list` (mineru surface picks the default subverb)."""
    result, recorded = _invoke(["contacts", "directory"])
    assert result.exit_code == 0
    assert recorded == [["contacts", "directory", "list"]]


def test_contacts_directory_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["contacts", "directory", "--max", "25", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["contacts", "directory", "list", "--max", "25", "--json"]
    ]


def test_contacts_directory_help_smoke() -> None:
    result = runner.invoke(app, ["contacts", "directory", "--help"])
    assert result.exit_code == 0


# --- other ----------------------------------------------------------------


def test_contacts_other_routes_to_gog_other_list() -> None:
    result, recorded = _invoke(["contacts", "other"])
    assert result.exit_code == 0
    assert recorded == [["contacts", "other", "list"]]


def test_contacts_other_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["contacts", "other", "--max", "100", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["contacts", "other", "list", "--max", "100", "--json"]
    ]


def test_contacts_other_help_smoke() -> None:
    result = runner.invoke(app, ["contacts", "other", "--help"])
    assert result.exit_code == 0


# ============================================================================
# WRITE VERBS — PATCHED, NEVER EXECUTED LIVE
# ============================================================================


# --- create (WRITE) ------------------------------------------------------


def test_contacts_create_name_two_word_splits_into_given_family() -> None:
    """`--name "Alice Smith"` → gog `--given Alice --family Smith`."""
    result, recorded = _invoke(
        [
            "contacts", "create",
            "--name", "Alice Smith",
            "--email", "alice@example.com",
            "--phone", "+14155551234",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "contacts", "create",
            "--given", "Alice",
            "--family", "Smith",
            "--email", "alice@example.com",
            "--phone", "+14155551234",
        ]
    ]


def test_contacts_create_name_one_word_emits_only_given() -> None:
    """A single-token name emits only `--given` (no `--family`)."""
    result, recorded = _invoke(
        ["contacts", "create", "--name", "Alice"]
    )
    assert result.exit_code == 0
    assert recorded == [["contacts", "create", "--given", "Alice"]]


def test_contacts_create_name_multi_word_family_uses_first_space_split() -> None:
    """`--name "Alice van der Rohe"` → given=Alice, family="van der Rohe"."""
    result, recorded = _invoke(
        ["contacts", "create", "--name", "Alice van der Rohe"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "contacts", "create",
            "--given", "Alice",
            "--family", "van der Rohe",
        ]
    ]


def test_contacts_create_without_name_still_forwards_email_and_phone() -> None:
    result, recorded = _invoke(
        [
            "contacts", "create",
            "--email", "alice@example.com",
            "--phone", "+14155551234",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "contacts", "create",
            "--email", "alice@example.com",
            "--phone", "+14155551234",
        ]
    ]


def test_contacts_create_extras_pass_through_given_family_directly() -> None:
    """Power users can bypass `--name` and pass `--given`/`--family` via extras."""
    result, recorded = _invoke(
        [
            "contacts", "create",
            "--given", "Alice",
            "--family", "Smith",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "contacts", "create",
            "--given", "Alice",
            "--family", "Smith",
            "--no-input",
        ]
    ]


def test_contacts_create_root_json_propagates() -> None:
    result, recorded = _invoke(
        [
            "--json", "contacts", "create",
            "--name", "Alice", "--email", "alice@example.com",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "contacts", "create",
            "--given", "Alice",
            "--email", "alice@example.com",
            "--json",
        ]
    ]


def test_contacts_create_help_smoke() -> None:
    result = runner.invoke(app, ["contacts", "create", "--help"])
    assert result.exit_code == 0
    assert "--name" in result.stdout
    assert "--email" in result.stdout
    assert "--phone" in result.stdout


def test_contacts_create_propagates_engine_exit_code() -> None:
    """Belt-and-braces: create never runs live; synthetic non-zero exit propagates."""
    result, _ = _invoke(
        ["contacts", "create", "--name", "Alice"], returncode=3
    )
    assert result.exit_code == 3


def test_contacts_create_rejects_empty_name() -> None:
    """`--name ""` and `--name "   "` are rejected as BadParameter; gog never runs.

    Old behavior emitted `--given ""` to gog, which either errored three
    layers down or created a nameless contact the operator had to hunt for.
    """
    for empty in ("", "   ", "\t"):
        result, recorded = _invoke(
            ["contacts", "create", "--name", empty]
        )
        assert result.exit_code != 0, (
            f"--name {empty!r} should be rejected but exited 0"
        )
        assert recorded == [], (
            f"gog-firewall must never run for empty --name {empty!r}; recorded {recorded!r}"
        )


def test_split_name_helper_rejects_empty_name() -> None:
    """The split helper itself raises BadParameter on an empty/whitespace name."""
    import typer as _typer
    for empty in ("", "  ", "\t"):
        raised = False
        try:
            contacts_verb._split_name_into_given_and_family(empty)
        except _typer.BadParameter:
            raised = True
        assert raised, (
            f"_split_name_into_given_and_family({empty!r}) should raise BadParameter"
        )


# --- update (WRITE) ------------------------------------------------------


def test_contacts_update_forwards_resource_name_only() -> None:
    result, recorded = _invoke(
        ["contacts", "update", "people/c1234567890"]
    )
    assert result.exit_code == 0
    assert recorded == [["contacts", "update", "people/c1234567890"]]


def test_contacts_update_extras_pass_through_field_flags() -> None:
    result, recorded = _invoke(
        [
            "contacts", "update", "people/c1234567890",
            "--email", "new@example.com",
            "--phone", "+14155551234",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "contacts", "update", "people/c1234567890",
            "--email", "new@example.com",
            "--phone", "+14155551234",
            "--no-input",
        ]
    ]


def test_contacts_update_help_smoke() -> None:
    result = runner.invoke(app, ["contacts", "update", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "write" in combined


# --- delete (WRITE, DESTRUCTIVE — NEVER executed live) -------------------


def test_contacts_delete_forwards_resource_name_only() -> None:
    """`contacts delete <resourceName>` — argv only; NEVER runs live against the operator's account."""
    result, recorded = _invoke(
        ["contacts", "delete", "people/c1234567890"]
    )
    assert result.exit_code == 0
    assert recorded == [["contacts", "delete", "people/c1234567890"]]


def test_contacts_delete_extras_pass_through_no_input() -> None:
    result, recorded = _invoke(
        ["contacts", "delete", "people/c1234567890", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["contacts", "delete", "people/c1234567890", "--no-input"]
    ]


def test_contacts_delete_help_smoke() -> None:
    result = runner.invoke(app, ["contacts", "delete", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "destructive" in combined or "write" in combined


def test_contacts_delete_propagates_engine_exit_code() -> None:
    """Belt-and-braces: delete never runs live; synthetic exit propagates."""
    result, _ = _invoke(
        ["contacts", "delete", "people/c1234567890"], returncode=4
    )
    assert result.exit_code == 4


# ============================================================================
# FIREWALL-PRESERVATION BELT-AND-BRACES (grep the source for bypass literals).
# ============================================================================


VERB_SRC = Path(contacts_verb.__file__).read_text()


def test_contacts_verb_source_has_no_bypass_flags() -> None:
    """The contacts verb file must not inject firewall-bypassing flags anywhere."""
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_contacts_verb_source_never_invokes_raw_gog_binary() -> None:
    """A regression that reached for a direct raw-gog string literal fails here.

    Prose mentions in docstrings are OK; live subprocess strings are not.
    Contacts should NOT import subprocess at all (no pandoc-style carve-out).
    """
    for forbidden in ("import subprocess", "from subprocess"):
        assert forbidden not in VERB_SRC, (
            f"contacts verb must route via the wrapper, not direct subprocess; "
            f"found {forbidden!r}"
        )


# ============================================================================
# ROOT `--help` REGRESSION GUARD
# ============================================================================


def test_contacts_help_lists_every_wired_verb() -> None:
    """`mineru contacts --help` surfaces every wired sub-verb."""
    result = runner.invoke(app, ["contacts", "--help"])
    assert result.exit_code == 0
    expected_verbs = (
        "lookup", "search", "list", "get",
        "directory", "other",
        "create", "update", "delete",
    )
    for verb in expected_verbs:
        assert verb in result.stdout, (
            f"`mineru contacts --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_contacts_help_mentions_firewall_and_gog_firewall() -> None:
    """The noun-level help surfaces the firewall preservation + gog-firewall backing."""
    result = runner.invoke(app, ["contacts", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "gog-firewall" in lowered
    assert "firewall" in lowered
