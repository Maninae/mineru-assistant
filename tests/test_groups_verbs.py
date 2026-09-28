"""Tests for the Phase-2 Groups verbs (P2-05, groups quarter).

Covers every verb the P2-05 task requires for groups:
  READ  : list, members

`groups` is a pure read surface — no writes at this layer. Every verb
is still tested via a patched wrapper (never live) so the test suite
matches the P2 hard safety rule uniformly across the surface.

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every test patches
    `mineru_cli.verbs.groups.run_gog_firewall` with a recorder, asserts
    the argv the wrapper WOULD send to the firewall, and verifies the
    exit-code plumbing.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
  - Firewall-preservation invariant grep-tests re-check that this
    specific verb file has no bypass literals.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import patch

from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import groups as groups_verb


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
        "mineru_cli.verbs.groups.run_gog_firewall",
        _record_run_gog_firewall(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS
# ============================================================================


# --- list -----------------------------------------------------------------


def test_groups_list_no_positional_needed() -> None:
    result, recorded = _invoke(["groups", "list"])
    assert result.exit_code == 0
    assert recorded == [["groups", "list"]]


def test_groups_list_extras_pass_through() -> None:
    result, recorded = _invoke(["groups", "list", "--max", "50", "--json"])
    assert result.exit_code == 0
    assert recorded == [
        ["groups", "list", "--max", "50", "--json"]
    ]


def test_groups_list_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "groups", "list"])
    assert result.exit_code == 0
    assert recorded == [["groups", "list", "--json"]]


def test_groups_list_root_pretty_propagates() -> None:
    result, recorded = _invoke(["--pretty", "groups", "list"])
    assert result.exit_code == 0
    assert recorded == [["groups", "list", "--pretty"]]


def test_groups_list_help_smoke() -> None:
    result = runner.invoke(app, ["groups", "list", "--help"])
    assert result.exit_code == 0


def test_groups_list_propagates_exit_77() -> None:
    result, _ = _invoke(["groups", "list"], returncode=77)
    assert result.exit_code == 77


def test_groups_list_propagates_exit_78() -> None:
    result, _ = _invoke(["groups", "list"], returncode=78)
    assert result.exit_code == 78


# --- members --------------------------------------------------------------


def test_groups_members_forwards_group_id() -> None:
    result, recorded = _invoke(
        ["groups", "members", "team@example.com"]
    )
    assert result.exit_code == 0
    assert recorded == [["groups", "members", "team@example.com"]]


def test_groups_members_extras_pass_through_max_page_json() -> None:
    result, recorded = _invoke(
        [
            "groups", "members", "team@example.com",
            "--max", "100",
            "--page", "abc",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "groups", "members", "team@example.com",
            "--max", "100",
            "--page", "abc",
            "--json",
        ]
    ]


def test_groups_members_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "groups", "members", "team@example.com"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["groups", "members", "team@example.com", "--json"]
    ]


def test_groups_members_help_smoke() -> None:
    result = runner.invoke(app, ["groups", "members", "--help"])
    assert result.exit_code == 0
    assert "email" in result.stdout.lower()


# ============================================================================
# FIREWALL-PRESERVATION BELT-AND-BRACES (grep the source for bypass literals).
# ============================================================================


VERB_SRC = Path(groups_verb.__file__).read_text()


def test_groups_verb_source_has_no_bypass_flags() -> None:
    """The groups verb file must not inject firewall-bypassing flags anywhere."""
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_groups_verb_source_never_invokes_raw_gog_binary() -> None:
    """A regression that reached for a direct raw-gog string literal fails here."""
    for forbidden in ("import subprocess", "from subprocess"):
        assert forbidden not in VERB_SRC, (
            f"groups verb must route via the wrapper, not direct subprocess; "
            f"found {forbidden!r}"
        )


# ============================================================================
# ROOT `--help` REGRESSION GUARD
# ============================================================================


def test_groups_help_lists_every_wired_verb() -> None:
    """`mineru groups --help` surfaces every wired sub-verb."""
    result = runner.invoke(app, ["groups", "--help"])
    assert result.exit_code == 0
    for verb in ("list", "members"):
        assert verb in result.stdout, (
            f"`mineru groups --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_groups_help_mentions_firewall_and_gog_firewall() -> None:
    result = runner.invoke(app, ["groups", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "gog-firewall" in lowered
    assert "firewall" in lowered
