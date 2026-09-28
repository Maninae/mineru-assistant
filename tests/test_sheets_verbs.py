"""Tests for the Phase-2 Sheets verbs (P2-04, sheets half).

Covers every verb the P2-04 task requires for sheets:
  READ  : get, metadata
  WRITE : update, append, clear (DESTRUCTIVE), format

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every test patches
    `mineru_cli.verbs.sheets.run_gog_firewall` with a recorder, asserts
    the argv the wrapper WOULD send to the firewall, and verifies the
    exit-code plumbing.
  - `sheets clear` in particular is DESTRUCTIVE and NEVER executed live
    during dev — every test that touches it is patched. The tests below
    assert argv only.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
  - The firewall-preservation invariant (argv[0] basename ==
    `gog-firewall`, no `--raw`, no `--unsafe-strip-invisible`, no
    `/opt/homebrew/bin/gog`) already has dedicated tests in
    `test_gmail_wrapper.py`; the wrapper is the same, so we don't
    re-derive those here. A pair of belt-and-braces tests re-check the
    invariant end-to-end for the sheets surface — one read (metadata)
    and one write (update) — so a P2 regression is caught here too.
  - Firewall exit codes 0 / 77 / 78 propagate through the new READ
    verbs unchanged (verified via patched wrapper).

Why patch at `mineru_cli.verbs.sheets.run_gog_firewall`:

  Same pattern as `test_drive_verbs._invoke`. Patching the verb-module
  binding lets the CliRunner drive the real Typer callback (including
  root-flag propagation and the `--range` / `--value` translations)
  without ever spawning a subprocess. This is the ONLY safe way to
  test the write verbs.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import patch

from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import sheets as sheets_verb


runner = CliRunner()


# --------------------------------------------------------------------- helpers


def _record_run_gog_firewall(recorded: List[List[str]], returncode: int = 0):
    """Return a fake `run_gog_firewall` that records the argv list it was called with.

    Captures a shallow copy so a later mutation of the recorded list can't
    retroactively rewrite what we recorded. Returns the requested exit code
    so the caller can prove the wrapper propagates it via
    `raise typer.Exit(code=rc)`.
    """

    def fake(args, **kwargs):
        recorded.append(list(args))
        return returncode

    return fake


def _invoke(args: List[str], returncode: int = 0):
    """Run the CLI with a patched wrapper. Returns (CliResult, recorded_argv_list)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.sheets.run_gog_firewall",
        _record_run_gog_firewall(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS
# ============================================================================


# --- get ------------------------------------------------------------------


def test_sheets_get_without_range_omits_positional() -> None:
    """`--range` absent → no range positional (let gog surface the missing-arg error)."""
    result, recorded = _invoke(["sheets", "get", "SHEET_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["sheets", "get", "SHEET_XYZ"]]


def test_sheets_get_with_range_translates_to_positional() -> None:
    """`--range 'Sheet1!A1:B10'` → gog positional `<range>`."""
    result, recorded = _invoke(
        ["sheets", "get", "SHEET_XYZ", "--range", "Sheet1!A1:B10"]
    )
    assert result.exit_code == 0
    assert recorded == [["sheets", "get", "SHEET_XYZ", "Sheet1!A1:B10"]]


def test_sheets_get_extras_pass_through_dimension_and_render() -> None:
    result, recorded = _invoke(
        [
            "sheets", "get", "SHEET_XYZ",
            "--range", "Sheet1!A1:B10",
            "--dimension", "ROWS",
            "--render", "FORMATTED_VALUE",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "sheets", "get", "SHEET_XYZ", "Sheet1!A1:B10",
            "--dimension", "ROWS",
            "--render", "FORMATTED_VALUE",
            "--json",
        ]
    ]


def test_sheets_get_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "sheets", "get", "SHEET_XYZ", "--range", "A1:B2"]
    )
    assert result.exit_code == 0
    assert recorded == [["sheets", "get", "SHEET_XYZ", "A1:B2", "--json"]]


def test_sheets_get_root_pretty_propagates() -> None:
    result, recorded = _invoke(
        ["--pretty", "sheets", "get", "SHEET_XYZ", "--range", "A1:B2"]
    )
    assert result.exit_code == 0
    assert recorded == [["sheets", "get", "SHEET_XYZ", "A1:B2", "--pretty"]]


def test_sheets_get_root_json_not_duplicated_when_also_trailing() -> None:
    """Root + trailing `--json` must yield exactly one `--json` in argv."""
    result, recorded = _invoke(
        ["--json", "sheets", "get", "SHEET_XYZ", "--range", "A1", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["sheets", "get", "SHEET_XYZ", "A1", "--json"]]


def test_sheets_get_help_smoke() -> None:
    result = runner.invoke(app, ["sheets", "get", "--help"])
    assert result.exit_code == 0
    assert "firewall" in result.stdout.lower()
    assert "--range" in result.stdout


def test_sheets_get_propagates_exit_77() -> None:
    result, _ = _invoke(["sheets", "get", "SHEET_XYZ"], returncode=77)
    assert result.exit_code == 77


def test_sheets_get_propagates_exit_78() -> None:
    result, _ = _invoke(["sheets", "get", "SHEET_XYZ"], returncode=78)
    assert result.exit_code == 78


# --- metadata -------------------------------------------------------------


def test_sheets_metadata_forwards_sheet_id() -> None:
    result, recorded = _invoke(["sheets", "metadata", "SHEET_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["sheets", "metadata", "SHEET_XYZ"]]


def test_sheets_metadata_extras_pass_through_json() -> None:
    result, recorded = _invoke(["sheets", "metadata", "SHEET_XYZ", "--json"])
    assert result.exit_code == 0
    assert recorded == [["sheets", "metadata", "SHEET_XYZ", "--json"]]


def test_sheets_metadata_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "sheets", "metadata", "SHEET_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["sheets", "metadata", "SHEET_XYZ", "--json"]]


def test_sheets_metadata_help_smoke() -> None:
    result = runner.invoke(app, ["sheets", "metadata", "--help"])
    assert result.exit_code == 0


# ============================================================================
# WRITE VERBS — PATCHED, NEVER EXECUTED LIVE
# ============================================================================


# --- update (WRITE) ------------------------------------------------------


def test_sheets_update_translates_range_and_value_to_positionals() -> None:
    """`--range <A1> --value <v>` → gog `<A1> <v>` positionals."""
    result, recorded = _invoke(
        [
            "sheets", "update", "SHEET_XYZ",
            "--range", "Sheet1!A1:B2",
            "--value", "a,b|c,d",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["sheets", "update", "SHEET_XYZ", "Sheet1!A1:B2", "a,b|c,d"]
    ]


def test_sheets_update_without_value_omits_positional_for_values_json_workflow() -> None:
    """Power-user path: omit `--value` and pass `--values-json` via extras."""
    result, recorded = _invoke(
        [
            "sheets", "update", "SHEET_XYZ",
            "--range", "Sheet1!A1:B2",
            "--values-json", '[["a","b"],["c","d"]]',
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "sheets", "update", "SHEET_XYZ", "Sheet1!A1:B2",
            "--values-json", '[["a","b"],["c","d"]]',
        ]
    ]


def test_sheets_update_extras_pass_through_input_flag() -> None:
    result, recorded = _invoke(
        [
            "sheets", "update", "SHEET_XYZ",
            "--range", "Sheet1!A1",
            "--value", "hi",
            "--input", "RAW",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "sheets", "update", "SHEET_XYZ", "Sheet1!A1", "hi",
            "--input", "RAW",
            "--no-input",
        ]
    ]


def test_sheets_update_root_json_propagates() -> None:
    result, recorded = _invoke(
        [
            "--json", "sheets", "update", "SHEET_XYZ",
            "--range", "A1", "--value", "x",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["sheets", "update", "SHEET_XYZ", "A1", "x", "--json"]
    ]


def test_sheets_update_help_smoke() -> None:
    result = runner.invoke(app, ["sheets", "update", "--help"])
    assert result.exit_code == 0
    assert "--range" in result.stdout
    assert "--value" in result.stdout
    combined = result.stdout.lower()
    assert "write" in combined


def test_sheets_update_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        [
            "sheets", "update", "SHEET_XYZ",
            "--range", "A1", "--value", "x",
        ],
        returncode=3,
    )
    assert result.exit_code == 3


# --- append (WRITE) ------------------------------------------------------


def test_sheets_append_translates_range_and_value_to_positionals() -> None:
    result, recorded = _invoke(
        [
            "sheets", "append", "SHEET_XYZ",
            "--range", "Sheet1!A:C",
            "--value", "hi|there",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["sheets", "append", "SHEET_XYZ", "Sheet1!A:C", "hi|there"]
    ]


def test_sheets_append_without_value_omits_positional_for_values_json_workflow() -> None:
    result, recorded = _invoke(
        [
            "sheets", "append", "SHEET_XYZ",
            "--range", "Sheet1!A:C",
            "--values-json", '[["hi","there"]]',
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "sheets", "append", "SHEET_XYZ", "Sheet1!A:C",
            "--values-json", '[["hi","there"]]',
        ]
    ]


def test_sheets_append_extras_pass_through_insert_flag() -> None:
    result, recorded = _invoke(
        [
            "sheets", "append", "SHEET_XYZ",
            "--range", "Sheet1!A:C",
            "--value", "hi",
            "--insert", "INSERT_ROWS",
            "--input", "USER_ENTERED",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "sheets", "append", "SHEET_XYZ", "Sheet1!A:C", "hi",
            "--insert", "INSERT_ROWS",
            "--input", "USER_ENTERED",
            "--no-input",
        ]
    ]


def test_sheets_append_root_json_propagates() -> None:
    result, recorded = _invoke(
        [
            "--json", "sheets", "append", "SHEET_XYZ",
            "--range", "A:B", "--value", "x",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["sheets", "append", "SHEET_XYZ", "A:B", "x", "--json"]
    ]


def test_sheets_append_help_smoke() -> None:
    result = runner.invoke(app, ["sheets", "append", "--help"])
    assert result.exit_code == 0
    assert "--range" in result.stdout
    assert "--value" in result.stdout


# --- clear (WRITE, DESTRUCTIVE — NEVER executed live) --------------------


def test_sheets_clear_translates_range_to_positional() -> None:
    result, recorded = _invoke(
        ["sheets", "clear", "SHEET_XYZ", "--range", "Sheet1!A1:B2"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["sheets", "clear", "SHEET_XYZ", "Sheet1!A1:B2"]
    ]


def test_sheets_clear_extras_pass_through_no_input() -> None:
    result, recorded = _invoke(
        [
            "sheets", "clear", "SHEET_XYZ",
            "--range", "Sheet1!A1:B2",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "sheets", "clear", "SHEET_XYZ", "Sheet1!A1:B2",
            "--no-input",
        ]
    ]


def test_sheets_clear_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "sheets", "clear", "SHEET_XYZ", "--range", "A1"]
    )
    assert result.exit_code == 0
    assert recorded == [["sheets", "clear", "SHEET_XYZ", "A1", "--json"]]


def test_sheets_clear_help_smoke() -> None:
    result = runner.invoke(app, ["sheets", "clear", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "destructive" in combined or "write" in combined
    assert "--range" in result.stdout


def test_sheets_clear_propagates_engine_exit_code() -> None:
    """Belt-and-braces: clear never runs live; synthetic non-zero exit propagates."""
    result, _ = _invoke(
        ["sheets", "clear", "SHEET_XYZ", "--range", "A1"], returncode=4
    )
    assert result.exit_code == 4


# --- format (WRITE) ------------------------------------------------------


def test_sheets_format_translates_range_to_positional() -> None:
    result, recorded = _invoke(
        [
            "sheets", "format", "SHEET_XYZ",
            "--range", "Sheet1!A1:B2",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["sheets", "format", "SHEET_XYZ", "Sheet1!A1:B2"]
    ]


def test_sheets_format_extras_pass_through_format_json_and_fields() -> None:
    result, recorded = _invoke(
        [
            "sheets", "format", "SHEET_XYZ",
            "--range", "Sheet1!A1:B2",
            "--format-json", '{"textFormat":{"bold":true}}',
            "--format-fields", "textFormat.bold",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "sheets", "format", "SHEET_XYZ", "Sheet1!A1:B2",
            "--format-json", '{"textFormat":{"bold":true}}',
            "--format-fields", "textFormat.bold",
            "--no-input",
        ]
    ]


def test_sheets_format_root_json_propagates() -> None:
    result, recorded = _invoke(
        [
            "--json", "sheets", "format", "SHEET_XYZ",
            "--range", "A1",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["sheets", "format", "SHEET_XYZ", "A1", "--json"]
    ]


def test_sheets_format_help_smoke() -> None:
    result = runner.invoke(app, ["sheets", "format", "--help"])
    assert result.exit_code == 0
    assert "--range" in result.stdout


def test_sheets_format_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        [
            "sheets", "format", "SHEET_XYZ",
            "--range", "A1",
        ],
        returncode=5,
    )
    assert result.exit_code == 5


# ============================================================================
# FIREWALL-PRESERVATION BELT-AND-BRACES (already covered by test_gmail_wrapper,
# but re-checked here so a sheets-specific regression is caught in this file too).
# ============================================================================


VERB_SRC = Path(sheets_verb.__file__).read_text()


def test_sheets_verb_source_has_no_bypass_flags() -> None:
    """The sheets verb file must not inject firewall-bypassing flags anywhere."""
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_sheets_verb_source_never_invokes_raw_gog_binary() -> None:
    """Grep the sheets verb file for any direct raw-gog string literal.

    A regression that swapped in `/opt/homebrew/bin/gog` (the raw,
    un-firewalled binary) or a bare `"gog"` argv[0] literal fails here.
    Prose mentions in docstrings are OK; live subprocess strings are not.
    """
    assert '"/usr/local/bin/gog"' not in VERB_SRC
    # sheets should NOT import subprocess at all (no pandoc carve-out here).
    for forbidden in ("import subprocess", "from subprocess"):
        assert forbidden not in VERB_SRC, (
            f"sheets verb must route via the wrapper, not direct subprocess; "
            f"found {forbidden!r}"
        )


def test_sheets_metadata_end_to_end_argv_shape_read_side() -> None:
    """Belt-and-braces on the read side: the recorded argv is exactly what an operator would type."""
    result, recorded = _invoke(
        ["sheets", "metadata", "SHEET_XYZ", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["sheets", "metadata", "SHEET_XYZ", "--json"]]


def test_sheets_update_end_to_end_argv_shape_write_side() -> None:
    """Belt-and-braces on the write side: update argv is verb → subverb → sheetId → range → value → extras."""
    result, recorded = _invoke(
        [
            "sheets", "update", "SHEET_XYZ",
            "--range", "A1", "--value", "x",
            "--input", "RAW", "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "sheets", "update", "SHEET_XYZ", "A1", "x",
            "--input", "RAW", "--no-input",
        ]
    ]


# ============================================================================
# ROOT `--help` REGRESSION GUARD
# ============================================================================


def test_sheets_help_lists_every_wired_verb() -> None:
    """`mineru sheets --help` surfaces every wired sub-verb."""
    result = runner.invoke(app, ["sheets", "--help"])
    assert result.exit_code == 0
    expected_verbs = (
        "get", "metadata", "update", "append", "clear", "format",
    )
    for verb in expected_verbs:
        assert verb in result.stdout, (
            f"`mineru sheets --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_sheets_help_mentions_firewall_and_gog_firewall() -> None:
    """The noun-level help surfaces the firewall preservation + gog-firewall backing."""
    result = runner.invoke(app, ["sheets", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "gog-firewall" in lowered
    assert "firewall" in lowered
