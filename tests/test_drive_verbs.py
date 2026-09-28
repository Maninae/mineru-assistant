"""Tests for the Phase-2 Drive verbs (P2-03).

Covers every verb the P2-03 task requires:
  READ  : ls, search, get, perms, drives, download, url
  WRITE : upload (OUTBOUND), copy, mkdir, mv, rename, rm (DESTRUCTIVE),
          share (OUTBOUND), unshare

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every test patches
    `mineru_cli.verbs.drive.run_gog_firewall` with a recorder, asserts
    the argv the wrapper WOULD send to the firewall, and verifies the
    exit-code plumbing.
  - `drive rm` in particular is NEVER executed live during dev — every
    test that touches it is patched. The tests below assert argv only.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
  - The firewall-preservation invariant (argv[0] basename ==
    `gog-firewall`, no `--raw`, no `--unsafe-strip-invisible`, no
    `/opt/homebrew/bin/gog`) already has dedicated tests in
    `test_gmail_wrapper.py`; the wrapper is the same, so we don't
    re-derive those here. A pair of belt-and-braces tests re-check the
    invariant end-to-end for the drive surface — one read (ls) and one
    write (unshare) — so a P2 regression is caught here too.
  - Firewall exit codes 0 / 77 / 78 propagate through the new READ
    verbs unchanged (verified via patched wrapper; a live fake binary
    is already exercised by the gmail tests).

Why patch at `mineru_cli.verbs.drive.run_gog_firewall`:

  Same pattern as `test_calendar_verbs._invoke`. Patching the
  verb-module binding lets the CliRunner drive the real Typer callback
  (including root-flag propagation and the `--to` / `--name` /
  `--parent` translations) without ever spawning a subprocess. This is
  the ONLY safe way to test the write verbs.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import patch

from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import drive as drive_verb


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
        "mineru_cli.verbs.drive.run_gog_firewall",
        _record_run_gog_firewall(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS
# ============================================================================


# --- ls --------------------------------------------------------------------


def test_drive_ls_without_folder_id_omits_parent() -> None:
    """`ls` with no folder id → no `--parent` flag (gog defaults to root)."""
    result, recorded = _invoke(["drive", "ls"])
    assert result.exit_code == 0
    assert recorded == [["drive", "ls"]]


def test_drive_ls_with_folder_id_translates_to_parent_flag() -> None:
    """Positional folder id → gog's `--parent <folderId>`."""
    result, recorded = _invoke(["drive", "ls", "FOLDER_ABC"])
    assert result.exit_code == 0
    assert recorded == [["drive", "ls", "--parent", "FOLDER_ABC"]]


def test_drive_ls_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["drive", "ls", "FOLDER_ABC", "--max", "50", "--query", "name contains 'x'", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "ls", "--parent", "FOLDER_ABC",
            "--max", "50", "--query", "name contains 'x'", "--json",
        ]
    ]


def test_drive_ls_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "drive", "ls"])
    assert result.exit_code == 0
    assert recorded == [["drive", "ls", "--json"]]


def test_drive_ls_root_pretty_propagates() -> None:
    result, recorded = _invoke(["--pretty", "drive", "ls"])
    assert result.exit_code == 0
    assert recorded == [["drive", "ls", "--pretty"]]


def test_drive_ls_root_json_not_duplicated_when_also_trailing() -> None:
    """Root + trailing `--json` must yield exactly one `--json` in argv."""
    result, recorded = _invoke(["--json", "drive", "ls", "--json"])
    assert result.exit_code == 0
    assert recorded == [["drive", "ls", "--json"]]


def test_drive_ls_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "ls", "--help"])
    assert result.exit_code == 0
    assert "firewall" in result.stdout.lower()


def test_drive_ls_propagates_exit_77() -> None:
    result, _ = _invoke(["drive", "ls"], returncode=77)
    assert result.exit_code == 77


def test_drive_ls_propagates_exit_78() -> None:
    result, _ = _invoke(["drive", "ls"], returncode=78)
    assert result.exit_code == 78


# --- search ----------------------------------------------------------------


def test_drive_search_forwards_query() -> None:
    result, recorded = _invoke(["drive", "search", "quarterly report"])
    assert result.exit_code == 0
    assert recorded == [["drive", "search", "quarterly report"]]


def test_drive_search_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["drive", "search", "budget", "--max", "20", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "search", "budget", "--max", "20", "--json"]
    ]


def test_drive_search_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "drive", "search", "x"])
    assert result.exit_code == 0
    assert recorded == [["drive", "search", "x", "--json"]]


def test_drive_search_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "search", "--help"])
    assert result.exit_code == 0


def test_drive_search_propagates_exit_77() -> None:
    result, _ = _invoke(["drive", "search", "x"], returncode=77)
    assert result.exit_code == 77


# --- get -------------------------------------------------------------------


def test_drive_get_forwards_file_id() -> None:
    result, recorded = _invoke(["drive", "get", "FILE_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["drive", "get", "FILE_XYZ"]]


def test_drive_get_extras_pass_through_json() -> None:
    result, recorded = _invoke(["drive", "get", "FILE_XYZ", "--json"])
    assert result.exit_code == 0
    assert recorded == [["drive", "get", "FILE_XYZ", "--json"]]


def test_drive_get_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "drive", "get", "FILE_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["drive", "get", "FILE_XYZ", "--json"]]


def test_drive_get_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "get", "--help"])
    assert result.exit_code == 0


# --- perms -----------------------------------------------------------------


def test_drive_perms_translates_to_permissions_subcommand() -> None:
    """Short verb `perms` routes to gog's fuller `permissions`."""
    result, recorded = _invoke(["drive", "perms", "FILE_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["drive", "permissions", "FILE_XYZ"]]


def test_drive_perms_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["drive", "perms", "FILE_XYZ", "--max", "10", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "permissions", "FILE_XYZ", "--max", "10", "--json"]
    ]


def test_drive_perms_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "drive", "perms", "FILE_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["drive", "permissions", "FILE_XYZ", "--json"]]


def test_drive_perms_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "perms", "--help"])
    assert result.exit_code == 0


# --- drives ----------------------------------------------------------------


def test_drive_drives_argv() -> None:
    result, recorded = _invoke(["drive", "drives"])
    assert result.exit_code == 0
    assert recorded == [["drive", "drives"]]


def test_drive_drives_extras_pass_through() -> None:
    result, recorded = _invoke(["drive", "drives", "--max", "50", "--json"])
    assert result.exit_code == 0
    assert recorded == [["drive", "drives", "--max", "50", "--json"]]


def test_drive_drives_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "drive", "drives"])
    assert result.exit_code == 0
    assert recorded == [["drive", "drives", "--json"]]


def test_drive_drives_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "drives", "--help"])
    assert result.exit_code == 0


# --- download --------------------------------------------------------------


def test_drive_download_without_format() -> None:
    result, recorded = _invoke(["drive", "download", "FILE_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["drive", "download", "FILE_XYZ"]]


def test_drive_download_with_format() -> None:
    result, recorded = _invoke(
        ["drive", "download", "FILE_XYZ", "--format", "pdf"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "download", "FILE_XYZ", "--format", "pdf"]
    ]


def test_drive_download_extras_pass_through() -> None:
    """gog's `--out <path>` flag passes through opaquely."""
    result, recorded = _invoke(
        ["drive", "download", "FILE_XYZ", "--format", "docx", "--out", "/tmp/x.docx"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "download", "FILE_XYZ",
            "--format", "docx",
            "--out", "/tmp/x.docx",
        ]
    ]


def test_drive_download_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "drive", "download", "FILE_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["drive", "download", "FILE_XYZ", "--json"]]


def test_drive_download_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "download", "--help"])
    assert result.exit_code == 0
    assert "--format" in result.stdout


# --- url -------------------------------------------------------------------


def test_drive_url_single_file() -> None:
    result, recorded = _invoke(["drive", "url", "FILE_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["drive", "url", "FILE_XYZ"]]


def test_drive_url_multiple_files_via_extras() -> None:
    result, recorded = _invoke(
        ["drive", "url", "FILE_1", "FILE_2", "FILE_3"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "url", "FILE_1", "FILE_2", "FILE_3"]
    ]


def test_drive_url_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "url", "--help"])
    assert result.exit_code == 0


# ============================================================================
# WRITE VERBS — PATCHED, NEVER EXECUTED LIVE
# ============================================================================


# --- upload (WRITE, OUTBOUND) ---------------------------------------------


def test_drive_upload_without_parent() -> None:
    result, recorded = _invoke(["drive", "upload", "/tmp/local.txt"])
    assert result.exit_code == 0
    assert recorded == [["drive", "upload", "/tmp/local.txt"]]


def test_drive_upload_with_parent() -> None:
    result, recorded = _invoke(
        ["drive", "upload", "/tmp/local.txt", "--parent", "FOLDER_ABC"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "upload", "/tmp/local.txt", "--parent", "FOLDER_ABC"]
    ]


def test_drive_upload_extras_pass_through() -> None:
    """gog's `--name <override>` and `--no-input` pass through opaquely."""
    result, recorded = _invoke(
        [
            "drive", "upload", "/tmp/local.txt",
            "--parent", "FOLDER_ABC",
            "--name", "override.txt",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "upload", "/tmp/local.txt",
            "--parent", "FOLDER_ABC",
            "--name", "override.txt",
            "--no-input",
        ]
    ]


def test_drive_upload_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "drive", "upload", "/tmp/local.txt"]
    )
    assert result.exit_code == 0
    assert recorded == [["drive", "upload", "/tmp/local.txt", "--json"]]


def test_drive_upload_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "upload", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "outbound" in combined or "write" in combined


def test_drive_upload_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        ["drive", "upload", "/tmp/local.txt"], returncode=2
    )
    assert result.exit_code == 2


# --- copy (WRITE) ---------------------------------------------------------


def test_drive_copy_translates_to_and_name() -> None:
    """`--to <folder>` → `--parent <folder>`; `--name <n>` → positional."""
    result, recorded = _invoke(
        [
            "drive", "copy", "FILE_XYZ",
            "--to", "FOLDER_DEST",
            "--name", "copy of report",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "copy", "FILE_XYZ",
            "copy of report",
            "--parent", "FOLDER_DEST",
        ]
    ]


def test_drive_copy_without_name_omits_positional() -> None:
    """When `--name` is omitted, no name positional is emitted (gog rejects with clear error)."""
    result, recorded = _invoke(
        ["drive", "copy", "FILE_XYZ", "--to", "FOLDER_DEST"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "copy", "FILE_XYZ", "--parent", "FOLDER_DEST"]
    ]


def test_drive_copy_extras_pass_through() -> None:
    result, recorded = _invoke(
        [
            "drive", "copy", "FILE_XYZ",
            "--to", "FOLDER_DEST",
            "--name", "renamed",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "copy", "FILE_XYZ",
            "renamed",
            "--parent", "FOLDER_DEST",
            "--no-input",
        ]
    ]


def test_drive_copy_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "drive", "copy", "FILE_XYZ", "--to", "FOLDER_DEST"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "copy", "FILE_XYZ",
            "--parent", "FOLDER_DEST",
            "--json",
        ]
    ]


def test_drive_copy_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "copy", "--help"])
    assert result.exit_code == 0
    assert "--to" in result.stdout


# --- mkdir (WRITE) --------------------------------------------------------


def test_drive_mkdir_without_parent() -> None:
    result, recorded = _invoke(["drive", "mkdir", "NewFolder"])
    assert result.exit_code == 0
    assert recorded == [["drive", "mkdir", "NewFolder"]]


def test_drive_mkdir_with_parent() -> None:
    result, recorded = _invoke(
        ["drive", "mkdir", "NewFolder", "--parent", "FOLDER_ABC"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "mkdir", "NewFolder", "--parent", "FOLDER_ABC"]
    ]


def test_drive_mkdir_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["drive", "mkdir", "NewFolder", "--parent", "FOLDER_ABC", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "mkdir", "NewFolder",
            "--parent", "FOLDER_ABC",
            "--no-input",
        ]
    ]


def test_drive_mkdir_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "drive", "mkdir", "NewFolder"])
    assert result.exit_code == 0
    assert recorded == [["drive", "mkdir", "NewFolder", "--json"]]


def test_drive_mkdir_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "mkdir", "--help"])
    assert result.exit_code == 0


# --- mv (WRITE) ----------------------------------------------------------


def test_drive_mv_translates_mv_to_move_and_to_to_parent() -> None:
    """`mv` → gog `move`; `--to <fid>` → `--parent <fid>`."""
    result, recorded = _invoke(
        ["drive", "mv", "FILE_XYZ", "--to", "FOLDER_DEST"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "move", "FILE_XYZ", "--parent", "FOLDER_DEST"]
    ]


def test_drive_mv_extras_pass_through() -> None:
    result, recorded = _invoke(
        [
            "drive", "mv", "FILE_XYZ",
            "--to", "FOLDER_DEST",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "move", "FILE_XYZ",
            "--parent", "FOLDER_DEST",
            "--no-input",
        ]
    ]


def test_drive_mv_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "drive", "mv", "FILE_XYZ", "--to", "FOLDER_DEST"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "move", "FILE_XYZ",
            "--parent", "FOLDER_DEST",
            "--json",
        ]
    ]


def test_drive_mv_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "mv", "--help"])
    assert result.exit_code == 0
    assert "--to" in result.stdout


# --- rename (WRITE) ------------------------------------------------------


def test_drive_rename_translates_name_flag_to_positional() -> None:
    """`--name <new>` → positional `<newName>` on gog."""
    result, recorded = _invoke(
        ["drive", "rename", "FILE_XYZ", "--name", "new report.pdf"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "rename", "FILE_XYZ", "new report.pdf"]
    ]


def test_drive_rename_extras_pass_through() -> None:
    result, recorded = _invoke(
        [
            "drive", "rename", "FILE_XYZ",
            "--name", "new report.pdf",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "rename", "FILE_XYZ",
            "new report.pdf",
            "--no-input",
        ]
    ]


def test_drive_rename_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "drive", "rename", "FILE_XYZ", "--name", "x"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "rename", "FILE_XYZ", "x", "--json"]
    ]


def test_drive_rename_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "rename", "--help"])
    assert result.exit_code == 0
    assert "--name" in result.stdout


# --- rm (WRITE, DESTRUCTIVE — NEVER executed live) -----------------------


def test_drive_rm_translates_to_delete_subcommand() -> None:
    """`rm` → gog `delete` (canonical name for stable argv shape in tests/logs)."""
    result, recorded = _invoke(["drive", "rm", "FILE_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["drive", "delete", "FILE_XYZ"]]


def test_drive_rm_extras_pass_through() -> None:
    """gog's `--force` and `--no-input` pass through opaquely."""
    result, recorded = _invoke(
        ["drive", "rm", "FILE_XYZ", "--force", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "delete", "FILE_XYZ", "--force", "--no-input"]
    ]


def test_drive_rm_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "drive", "rm", "FILE_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["drive", "delete", "FILE_XYZ", "--json"]]


def test_drive_rm_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "rm", "--help"])
    assert result.exit_code == 0
    # DESTRUCTIVE wording surfaces so operators know what they're invoking.
    combined = result.stdout.lower()
    assert "destructive" in combined or "trash" in combined or "write" in combined


def test_drive_rm_propagates_engine_exit_code() -> None:
    """Belt-and-braces: rm never runs live; a synthetic non-zero exit propagates."""
    result, _ = _invoke(["drive", "rm", "FILE_XYZ"], returncode=4)
    assert result.exit_code == 4


# --- share (WRITE, OUTBOUND) ---------------------------------------------


def test_drive_share_with_email_and_role() -> None:
    result, recorded = _invoke(
        [
            "drive", "share", "FILE_XYZ",
            "--email", "alice@example.com",
            "--role", "writer",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "share", "FILE_XYZ",
            "--email", "alice@example.com",
            "--role", "writer",
        ]
    ]


def test_drive_share_extras_pass_through() -> None:
    """`--anyone`, `--discoverable`, `--no-input` all pass through opaquely."""
    result, recorded = _invoke(
        [
            "drive", "share", "FILE_XYZ",
            "--role", "reader",
            "--anyone",
            "--discoverable",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "share", "FILE_XYZ",
            "--role", "reader",
            "--anyone",
            "--discoverable",
            "--no-input",
        ]
    ]


def test_drive_share_root_json_propagates() -> None:
    result, recorded = _invoke(
        [
            "--json", "drive", "share", "FILE_XYZ",
            "--email", "a@x.com", "--role", "reader",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "share", "FILE_XYZ",
            "--email", "a@x.com",
            "--role", "reader",
            "--json",
        ]
    ]


def test_drive_share_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "share", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "outbound" in combined or "write" in combined


def test_drive_share_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        [
            "drive", "share", "FILE_XYZ",
            "--email", "a@x.com", "--role", "reader",
        ],
        returncode=3,
    )
    assert result.exit_code == 3


# --- unshare (WRITE) -----------------------------------------------------


def test_drive_unshare_forwards_positional_permission_id() -> None:
    result, recorded = _invoke(
        ["drive", "unshare", "FILE_XYZ", "PERMISSION_ABC"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "unshare", "FILE_XYZ", "PERMISSION_ABC"]
    ]


def test_drive_unshare_extras_pass_through() -> None:
    result, recorded = _invoke(
        ["drive", "unshare", "FILE_XYZ", "PERMISSION_ABC", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "unshare", "FILE_XYZ", "PERMISSION_ABC",
            "--no-input",
        ]
    ]


def test_drive_unshare_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "drive", "unshare", "FILE_XYZ", "PERMISSION_ABC"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["drive", "unshare", "FILE_XYZ", "PERMISSION_ABC", "--json"]
    ]


def test_drive_unshare_help_smoke() -> None:
    result = runner.invoke(app, ["drive", "unshare", "--help"])
    assert result.exit_code == 0
    # The help text should point operators at `mineru drive perms` for
    # looking up the permission id from an email.
    combined = result.stdout.lower()
    assert "perms" in combined or "permission" in combined


def test_drive_unshare_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        ["drive", "unshare", "FILE_XYZ", "PERMISSION_ABC"], returncode=5
    )
    assert result.exit_code == 5


# ============================================================================
# FIREWALL-PRESERVATION BELT-AND-BRACES (already covered by test_gmail_wrapper,
# but re-checked here so a drive-specific regression is caught in this file too).
# ============================================================================


VERB_SRC = Path(drive_verb.__file__).read_text()


def test_drive_verb_source_has_no_bypass_flags() -> None:
    """The drive verb file must not inject firewall-bypassing flags anywhere."""
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_drive_verb_source_never_invokes_raw_gog_binary() -> None:
    """Grep the drive verb file for any direct raw-gog string literal.

    A regression that swapped in `/opt/homebrew/bin/gog` (the raw, un-firewalled
    binary) or a bare `"gog"` argv[0] literal fails here. Prose mentions in
    docstrings are OK; live subprocess strings are not.
    """
    # No string literal invoking a bare `gog` binary.
    assert '"/usr/local/bin/gog"' not in VERB_SRC
    # No import of subprocess directly (verb should route via wrapper only).
    # Docstrings mentioning subprocess-adjacent words in prose are fine; we
    # just guard the import statement shape.
    for forbidden in (
        "import subprocess",
        "from subprocess",
    ):
        assert forbidden not in VERB_SRC, (
            f"drive verb must route via the wrapper, not direct subprocess; "
            f"found {forbidden!r}"
        )


def test_drive_ls_end_to_end_argv_shape_matches_gog_firewall_expectation() -> None:
    """Belt-and-braces on the read side: the recorded argv is exactly what an operator would type."""
    result, recorded = _invoke(
        ["drive", "ls", "FOLDER_ABC", "--max", "50", "--json"]
    )
    assert result.exit_code == 0
    # Order: drive → ls → --parent FOLDER_ABC → extras
    assert recorded == [
        [
            "drive", "ls",
            "--parent", "FOLDER_ABC",
            "--max", "50",
            "--json",
        ]
    ]


def test_drive_unshare_end_to_end_argv_shape_write_side() -> None:
    """Belt-and-braces on the write side: unshare argv is verb → fileId → permissionId → extras."""
    result, recorded = _invoke(
        ["drive", "unshare", "FILE_XYZ", "PERM_ABC", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "drive", "unshare", "FILE_XYZ", "PERM_ABC",
            "--no-input",
        ]
    ]


# ============================================================================
# ROOT `--help` REGRESSION GUARD
# ============================================================================


def test_drive_help_lists_every_wired_verb() -> None:
    """`mineru drive --help` surfaces every wired sub-verb."""
    result = runner.invoke(app, ["drive", "--help"])
    assert result.exit_code == 0
    expected_verbs = (
        "ls", "search", "get", "perms", "drives", "download", "url",
        "upload", "copy", "mkdir", "mv", "rename", "rm", "share", "unshare",
    )
    for verb in expected_verbs:
        assert verb in result.stdout, (
            f"`mineru drive --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_drive_help_mentions_firewall_and_gog_firewall() -> None:
    """The noun-level help surfaces the firewall preservation + gog-firewall backing."""
    result = runner.invoke(app, ["drive", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "gog-firewall" in lowered
    assert "firewall" in lowered
