"""Tests for the Phase-2 Directory verbs (P2-05, directory quarter).

Covers every verb the P2-05 task requires for the Google Workspace directory:
  READ  : me, get, search, relations

`directory` is a pure read surface — no writes at this layer. Every verb is
still tested via a patched wrapper (never live) so the test suite matches
the P2 hard safety rule uniformly across the surface.

RENAMED 2026-09-16 (audit §2B): this sub-app used to be `mineru people`. The
canonical spelling is now `mineru directory`; the old `mineru people`
spelling remains a HIDDEN alias for the standard 90-day compat window (see
`mineru_cli/verbs/directory.py::people_alias_app`). This file exercises the
canonical `directory` verbs; the two hidden-alias tests at the bottom pin
that the old spelling still dispatches AND emits the DEPRECATED notice.

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every test patches
    `mineru_cli.verbs.directory.run_gog_firewall` with a recorder, asserts
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
from mineru_cli.verbs import directory as directory_verb


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
        "mineru_cli.verbs.directory.run_gog_firewall",
        _record_run_gog_firewall(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS (canonical `mineru directory ...`)
# ============================================================================


# --- me -------------------------------------------------------------------


def test_directory_me_no_positional_needed() -> None:
    result, recorded = _invoke(["directory", "me"])
    assert result.exit_code == 0
    assert recorded == [["people", "me"]]


def test_directory_me_extras_pass_through() -> None:
    result, recorded = _invoke(["directory", "me", "--json"])
    assert result.exit_code == 0
    assert recorded == [["people", "me", "--json"]]


def test_directory_me_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "directory", "me"])
    assert result.exit_code == 0
    assert recorded == [["people", "me", "--json"]]


def test_directory_me_help_smoke() -> None:
    result = runner.invoke(app, ["directory", "me", "--help"])
    assert result.exit_code == 0


def test_directory_me_propagates_exit_77() -> None:
    result, _ = _invoke(["directory", "me"], returncode=77)
    assert result.exit_code == 77


def test_directory_me_propagates_exit_78() -> None:
    result, _ = _invoke(["directory", "me"], returncode=78)
    assert result.exit_code == 78


# --- get ------------------------------------------------------------------


def test_directory_get_forwards_user_id() -> None:
    result, recorded = _invoke(["directory", "get", "people/1234567890"])
    assert result.exit_code == 0
    assert recorded == [["people", "get", "people/1234567890"]]


def test_directory_get_extras_pass_through_json_and_fields() -> None:
    result, recorded = _invoke(
        [
            "directory", "get", "people/1234567890",
            "--fields", "names,emailAddresses",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "people", "get", "people/1234567890",
            "--fields", "names,emailAddresses",
            "--json",
        ]
    ]


def test_directory_get_help_smoke() -> None:
    result = runner.invoke(app, ["directory", "get", "--help"])
    assert result.exit_code == 0


# --- search ---------------------------------------------------------------


def test_directory_search_forwards_query() -> None:
    result, recorded = _invoke(["directory", "search", "alice"])
    assert result.exit_code == 0
    assert recorded == [["people", "search", "alice"]]


def test_directory_search_extras_pass_through_max_and_json() -> None:
    result, recorded = _invoke(
        ["directory", "search", "alice", "--max", "10", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["people", "search", "alice", "--max", "10", "--json"]
    ]


def test_directory_search_root_pretty_propagates() -> None:
    result, recorded = _invoke(["--pretty", "directory", "search", "alice"])
    assert result.exit_code == 0
    assert recorded == [["people", "search", "alice", "--pretty"]]


def test_directory_search_help_smoke() -> None:
    result = runner.invoke(app, ["directory", "search", "--help"])
    assert result.exit_code == 0


# --- relations ------------------------------------------------------------


def test_directory_relations_without_user_id_omits_positional() -> None:
    """`directory relations` (no id) -> gog defaults to caller's own relations."""
    result, recorded = _invoke(["directory", "relations"])
    assert result.exit_code == 0
    assert recorded == [["people", "relations"]]


def test_directory_relations_with_user_id_forwards_positional() -> None:
    result, recorded = _invoke(
        ["directory", "relations", "people/1234567890"]
    )
    assert result.exit_code == 0
    assert recorded == [["people", "relations", "people/1234567890"]]


def test_directory_relations_extras_pass_through_json() -> None:
    result, recorded = _invoke(
        ["directory", "relations", "people/1234567890", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["people", "relations", "people/1234567890", "--json"]
    ]


def test_directory_relations_root_json_propagates_without_user_id() -> None:
    result, recorded = _invoke(["--json", "directory", "relations"])
    assert result.exit_code == 0
    assert recorded == [["people", "relations", "--json"]]


def test_directory_relations_help_smoke() -> None:
    result = runner.invoke(app, ["directory", "relations", "--help"])
    assert result.exit_code == 0


# ============================================================================
# GOOGLE-CONNECTOR `people` ALIAS: DROPPED (audit chain sequencing note).
# ============================================================================
#
# The prior commit in the 2026-09-16 audit rename chain added a HIDDEN
# `mineru people ...` alias so muscle-memory kept working while
# `directory` took the canonical spot. The FOLLOWING commit in the chain
# (§2A F3, `humans` -> `people` for the machine-level human registry)
# had to take the `people` name for itself — a Typer sub-app cannot be
# registered twice under the same name, and the human registry is the
# stronger claim on the plain English word. The transient hidden alias
# was therefore dropped at that commit. Nothing in the CLI now mounts
# `directory_verb.people_alias_app`; muscle-memory continuity for the
# Google directory rename was one commit long.
#
# Regression pin: `mineru people ...` MUST now route to the human
# registry (people list / people path), NOT to the Google directory
# read surface. `test_cli_people_list_pretty` in test_people_verb.py
# is the load-bearing check for that; here we just assert the alias
# sub-app symbol is no longer wired into the root app.


def test_google_connector_people_alias_no_longer_wired() -> None:
    """The transient `mineru people` alias for the Google directory is gone.

    Prior audit chain commit added `directory_verb.people_alias_app` as
    a hidden alias under `name="people"`. The follow-up commit (`humans`
    -> `people` for the human registry) claimed the canonical `people`
    name; the transient alias had to be dropped.
    """
    people_groups = [g for g in app.registered_groups if g.name == "people"]
    assert len(people_groups) == 1, (
        f"expected exactly one `people` sub-app, got {len(people_groups)}"
    )
    the_people_group = people_groups[0]
    # The one remaining `people` sub-app must be the human registry — its
    # typer_instance is `people_verb.people_app`, NOT
    # `directory_verb.people_alias_app`.
    from mineru_cli.verbs import people as human_registry_verb
    assert the_people_group.typer_instance is human_registry_verb.people_app, (
        "the `people` sub-app must be the human registry, not the "
        "transient Google-connector alias (audit §2A F3)."
    )


# ============================================================================
# FIREWALL-PRESERVATION BELT-AND-BRACES (grep the source for bypass literals).
# ============================================================================


VERB_SRC = Path(directory_verb.__file__).read_text()


def test_directory_verb_source_has_no_bypass_flags() -> None:
    """The directory verb file must not inject firewall-bypassing flags anywhere."""
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_directory_verb_source_never_invokes_raw_gog_binary() -> None:
    """A regression that reached for a direct raw-gog string literal fails here."""
    for forbidden in ("import subprocess", "from subprocess"):
        assert forbidden not in VERB_SRC, (
            f"directory verb must route via the wrapper, not direct subprocess; "
            f"found {forbidden!r}"
        )


# ============================================================================
# ROOT `--help` REGRESSION GUARD
# ============================================================================


def test_directory_help_lists_every_wired_verb() -> None:
    """`mineru directory --help` surfaces every wired sub-verb."""
    result = runner.invoke(app, ["directory", "--help"])
    assert result.exit_code == 0
    for verb in ("me", "get", "search", "relations"):
        assert verb in result.stdout, (
            f"`mineru directory --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_directory_help_mentions_firewall_and_gog_firewall() -> None:
    result = runner.invoke(app, ["directory", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "gog-firewall" in lowered
    assert "firewall" in lowered
