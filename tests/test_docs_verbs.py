"""Tests for the Phase-2 Docs verbs (P2-04, docs half).

Covers every verb the P2-04 task requires for docs:
  READ  : export, info, cat
  WRITE : create (OUTBOUND), copy, from-html (OUTBOUND — pandoc + drive upload)

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every test patches
    `mineru_cli.verbs.docs.run_gog_firewall` with a recorder, asserts the
    argv the wrapper WOULD send to the firewall, and verifies the
    exit-code plumbing.
  - `from-html` additionally patches `mineru_cli.verbs.docs.subprocess.run`
    so pandoc is never actually executed. The tests assert the pandoc argv
    the wrapper WOULD send (binary path + input html + `-o <tmp>.docx`)
    and the downstream `drive upload` argv fed to the firewall.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
  - The firewall-preservation invariant (argv[0] basename ==
    `gog-firewall`, no `--raw`, no `--unsafe-strip-invisible`, no
    `/opt/homebrew/bin/gog`) already has dedicated tests in
    `test_gmail_wrapper.py`; the wrapper is the same, so we don't
    re-derive those here. A pair of belt-and-braces tests re-check the
    invariant end-to-end for the docs surface — one read (info) and one
    write (create) — so a P2 regression is caught here too.
  - Firewall exit codes 0 / 77 / 78 propagate through the new READ verbs
    unchanged (verified via patched wrapper).

Why patch at `mineru_cli.verbs.docs.run_gog_firewall`:

  Same pattern as `test_drive_verbs._invoke`. Patching the verb-module
  binding lets the CliRunner drive the real Typer callback (including
  root-flag propagation and the `--title` / `--name` / `--parent`
  translations) without ever spawning a subprocess. This is the ONLY
  safe way to test the write verbs.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import List
from unittest.mock import patch

from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import docs as docs_verb


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
        "mineru_cli.verbs.docs.run_gog_firewall",
        _record_run_gog_firewall(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


def _fake_subprocess_run(
    recorded: List[List[str]], returncode: int = 0,
):
    """Return a fake `subprocess.run` that records the pandoc argv it was handed.

    Returns a CompletedProcess-shaped SimpleNamespace so the verb's
    `completed.returncode` attribute access works, and does NOT actually
    execute anything.
    """

    def fake(cmd, *args, **kwargs):
        recorded.append(list(cmd))
        return types.SimpleNamespace(returncode=returncode)

    return fake


def _invoke_from_html(
    args: List[str],
    pandoc_returncode: int = 0,
    upload_returncode: int = 0,
):
    """Run `docs from-html` with pandoc AND run_gog_firewall both patched.

    Returns (CliResult, pandoc_argv_list, upload_argv_list).
    """
    recorded_pandoc: List[List[str]] = []
    recorded_upload: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.docs.subprocess.run",
        _fake_subprocess_run(recorded_pandoc, returncode=pandoc_returncode),
    ), patch(
        "mineru_cli.verbs.docs.run_gog_firewall",
        _record_run_gog_firewall(recorded_upload, returncode=upload_returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded_pandoc, recorded_upload


# ============================================================================
# READ VERBS
# ============================================================================


# --- export ---------------------------------------------------------------


def test_docs_export_without_format() -> None:
    result, recorded = _invoke(["docs", "export", "DOC_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["docs", "export", "DOC_XYZ"]]


def test_docs_export_with_format() -> None:
    result, recorded = _invoke(["docs", "export", "DOC_XYZ", "--format", "docx"])
    assert result.exit_code == 0
    assert recorded == [["docs", "export", "DOC_XYZ", "--format", "docx"]]


def test_docs_export_extras_pass_through_out() -> None:
    """gog's `--out <path>` flag passes through opaquely."""
    result, recorded = _invoke(
        ["docs", "export", "DOC_XYZ", "--format", "pdf", "--out", "/tmp/x.pdf"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["docs", "export", "DOC_XYZ", "--format", "pdf", "--out", "/tmp/x.pdf"]
    ]


def test_docs_export_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "docs", "export", "DOC_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["docs", "export", "DOC_XYZ", "--json"]]


def test_docs_export_help_smoke() -> None:
    result = runner.invoke(app, ["docs", "export", "--help"])
    assert result.exit_code == 0
    assert "--format" in result.stdout


def test_docs_export_propagates_exit_77() -> None:
    result, _ = _invoke(["docs", "export", "DOC_XYZ"], returncode=77)
    assert result.exit_code == 77


def test_docs_export_propagates_exit_78() -> None:
    result, _ = _invoke(["docs", "export", "DOC_XYZ"], returncode=78)
    assert result.exit_code == 78


# --- info -----------------------------------------------------------------


def test_docs_info_forwards_doc_id() -> None:
    result, recorded = _invoke(["docs", "info", "DOC_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["docs", "info", "DOC_XYZ"]]


def test_docs_info_extras_pass_through_json() -> None:
    result, recorded = _invoke(["docs", "info", "DOC_XYZ", "--json"])
    assert result.exit_code == 0
    assert recorded == [["docs", "info", "DOC_XYZ", "--json"]]


def test_docs_info_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "docs", "info", "DOC_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["docs", "info", "DOC_XYZ", "--json"]]


def test_docs_info_root_pretty_propagates() -> None:
    result, recorded = _invoke(["--pretty", "docs", "info", "DOC_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["docs", "info", "DOC_XYZ", "--pretty"]]


def test_docs_info_root_json_not_duplicated_when_also_trailing() -> None:
    """Root + trailing `--json` must yield exactly one `--json` in argv."""
    result, recorded = _invoke(["--json", "docs", "info", "DOC_XYZ", "--json"])
    assert result.exit_code == 0
    assert recorded == [["docs", "info", "DOC_XYZ", "--json"]]


def test_docs_info_help_smoke() -> None:
    result = runner.invoke(app, ["docs", "info", "--help"])
    assert result.exit_code == 0
    assert "firewall" in result.stdout.lower()


# --- cat ------------------------------------------------------------------


def test_docs_cat_forwards_doc_id() -> None:
    result, recorded = _invoke(["docs", "cat", "DOC_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["docs", "cat", "DOC_XYZ"]]


def test_docs_cat_extras_pass_through_max_bytes() -> None:
    """gog's `--max-bytes` flag passes through opaquely."""
    result, recorded = _invoke(
        ["docs", "cat", "DOC_XYZ", "--max-bytes", "500000"]
    )
    assert result.exit_code == 0
    assert recorded == [["docs", "cat", "DOC_XYZ", "--max-bytes", "500000"]]


def test_docs_cat_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "docs", "cat", "DOC_XYZ"])
    assert result.exit_code == 0
    assert recorded == [["docs", "cat", "DOC_XYZ", "--json"]]


def test_docs_cat_help_smoke() -> None:
    result = runner.invoke(app, ["docs", "cat", "--help"])
    assert result.exit_code == 0


# ============================================================================
# WRITE VERBS — PATCHED, NEVER EXECUTED LIVE
# ============================================================================


# --- create (WRITE, OUTBOUND) --------------------------------------------


def test_docs_create_translates_title_flag_to_positional() -> None:
    """`--title T` → gog positional `<title>`."""
    result, recorded = _invoke(["docs", "create", "--title", "My New Doc"])
    assert result.exit_code == 0
    assert recorded == [["docs", "create", "My New Doc"]]


def test_docs_create_extras_pass_through_parent_and_no_input() -> None:
    result, recorded = _invoke(
        [
            "docs", "create",
            "--title", "My Doc",
            "--parent", "FOLDER_ABC",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["docs", "create", "My Doc", "--parent", "FOLDER_ABC", "--no-input"]
    ]


def test_docs_create_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "docs", "create", "--title", "My Doc"]
    )
    assert result.exit_code == 0
    assert recorded == [["docs", "create", "My Doc", "--json"]]


def test_docs_create_help_smoke() -> None:
    result = runner.invoke(app, ["docs", "create", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "outbound" in combined or "write" in combined
    assert "--title" in result.stdout


def test_docs_create_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        ["docs", "create", "--title", "X"], returncode=3
    )
    assert result.exit_code == 3


# --- copy (WRITE) --------------------------------------------------------


def test_docs_copy_translates_title_flag_to_positional() -> None:
    """`docs copy <docId> --title T` → gog `docs copy <docId> T`."""
    result, recorded = _invoke(
        ["docs", "copy", "DOC_XYZ", "--title", "Copy of X"]
    )
    assert result.exit_code == 0
    assert recorded == [["docs", "copy", "DOC_XYZ", "Copy of X"]]


def test_docs_copy_extras_pass_through_parent() -> None:
    result, recorded = _invoke(
        [
            "docs", "copy", "DOC_XYZ",
            "--title", "Copy of X",
            "--parent", "FOLDER_DEST",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "docs", "copy", "DOC_XYZ", "Copy of X",
            "--parent", "FOLDER_DEST",
            "--no-input",
        ]
    ]


def test_docs_copy_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "docs", "copy", "DOC_XYZ", "--title", "Copy"]
    )
    assert result.exit_code == 0
    assert recorded == [["docs", "copy", "DOC_XYZ", "Copy", "--json"]]


def test_docs_copy_help_smoke() -> None:
    result = runner.invoke(app, ["docs", "copy", "--help"])
    assert result.exit_code == 0
    assert "--title" in result.stdout


# --- from-html (WRITE, OUTBOUND — pandoc + drive upload) ------------------


def test_docs_from_html_pandoc_argv_and_upload_argv(tmp_path: Path) -> None:
    """`from-html` calls pandoc with the html + `-o <tmp>.docx`, then routes upload through firewall."""
    html_path = tmp_path / "in.html"
    html_path.write_text("<h1>hi</h1>")

    result, pandoc_argv_list, upload_argv_list = _invoke_from_html(
        ["docs", "from-html", str(html_path), "--name", "My Report"]
    )
    assert result.exit_code == 0
    # Pandoc invoked exactly once with argv[0] == pandoc bin, input path, -s, -o <tmp>.docx.
    assert len(pandoc_argv_list) == 1
    pandoc_argv = pandoc_argv_list[0]
    assert pandoc_argv[0] == docs_verb._resolve_pandoc_bin()
    assert pandoc_argv[1] == str(html_path)
    assert "-s" in pandoc_argv
    assert "-o" in pandoc_argv
    tmp_docx_index = pandoc_argv.index("-o") + 1
    tmp_docx = pandoc_argv[tmp_docx_index]
    assert tmp_docx.endswith("My Report.docx"), (
        f"pandoc output path must end with the resolved upload name: {tmp_docx!r}"
    )

    # Firewall upload invoked exactly once with drive upload argv shape.
    assert len(upload_argv_list) == 1
    upload_argv = upload_argv_list[0]
    # verb → subverb → tmp_docx path → --name <upload_name>
    assert upload_argv[:3] == ["drive", "upload", tmp_docx]
    assert "--name" in upload_argv
    name_index = upload_argv.index("--name") + 1
    assert upload_argv[name_index] == "My Report.docx"


def test_docs_from_html_appends_docx_extension_when_missing(tmp_path: Path) -> None:
    """`--name My Report` (no ext) becomes `My Report.docx` for both tmp file and upload name."""
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    result, pandoc_argv_list, upload_argv_list = _invoke_from_html(
        ["docs", "from-html", str(html_path), "--name", "MyReport"]
    )
    assert result.exit_code == 0
    tmp_docx = pandoc_argv_list[0][pandoc_argv_list[0].index("-o") + 1]
    assert tmp_docx.endswith("MyReport.docx")
    upload_argv = upload_argv_list[0]
    assert upload_argv[upload_argv.index("--name") + 1] == "MyReport.docx"


def test_docs_from_html_preserves_docx_extension_when_provided(tmp_path: Path) -> None:
    """`--name My Report.docx` stays as-is (no double extension)."""
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    result, pandoc_argv_list, upload_argv_list = _invoke_from_html(
        ["docs", "from-html", str(html_path), "--name", "MyReport.docx"]
    )
    assert result.exit_code == 0
    tmp_docx = pandoc_argv_list[0][pandoc_argv_list[0].index("-o") + 1]
    assert tmp_docx.endswith("MyReport.docx")
    assert not tmp_docx.endswith("MyReport.docx.docx")
    upload_argv = upload_argv_list[0]
    assert upload_argv[upload_argv.index("--name") + 1] == "MyReport.docx"


def test_docs_from_html_with_parent_folder(tmp_path: Path) -> None:
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    result, _, upload_argv_list = _invoke_from_html(
        [
            "docs", "from-html", str(html_path),
            "--name", "My Report",
            "--parent", "FOLDER_ABC",
        ]
    )
    assert result.exit_code == 0
    upload_argv = upload_argv_list[0]
    assert "--parent" in upload_argv
    parent_index = upload_argv.index("--parent") + 1
    assert upload_argv[parent_index] == "FOLDER_ABC"


def test_docs_from_html_peels_off_reference_doc_extra_to_pandoc(tmp_path: Path) -> None:
    """`--reference-doc=<path>` is a pandoc flag; it must NOT reach the upload extras."""
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")
    ref_docx = tmp_path / "ref.docx"
    ref_docx.write_bytes(b"")  # existence check only; pandoc is mocked

    result, pandoc_argv_list, upload_argv_list = _invoke_from_html(
        [
            "docs", "from-html", str(html_path),
            "--name", "My Report",
            f"--reference-doc={ref_docx}",
        ]
    )
    assert result.exit_code == 0
    pandoc_argv = pandoc_argv_list[0]
    assert f"--reference-doc={ref_docx}" in pandoc_argv, (
        f"--reference-doc must forward to pandoc: {pandoc_argv!r}"
    )
    upload_argv = upload_argv_list[0]
    assert f"--reference-doc={ref_docx}" not in upload_argv, (
        f"--reference-doc must NOT reach gog upload: {upload_argv!r}"
    )


def test_docs_from_html_peels_off_reference_doc_space_form(tmp_path: Path) -> None:
    """`--reference-doc <path>` (space form) peels off cleanly too."""
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")
    ref_docx = tmp_path / "ref.docx"
    ref_docx.write_bytes(b"")

    result, pandoc_argv_list, upload_argv_list = _invoke_from_html(
        [
            "docs", "from-html", str(html_path),
            "--name", "My Report",
            "--reference-doc", str(ref_docx),
        ]
    )
    assert result.exit_code == 0
    pandoc_argv = pandoc_argv_list[0]
    assert "--reference-doc" in pandoc_argv
    ref_index = pandoc_argv.index("--reference-doc") + 1
    assert pandoc_argv[ref_index] == str(ref_docx)
    upload_argv = upload_argv_list[0]
    assert "--reference-doc" not in upload_argv


def test_docs_from_html_forwards_upload_extras(tmp_path: Path) -> None:
    """Non-pandoc extras (e.g. `--no-input`) reach gog upload only."""
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    result, pandoc_argv_list, upload_argv_list = _invoke_from_html(
        [
            "docs", "from-html", str(html_path),
            "--name", "My Report",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    upload_argv = upload_argv_list[0]
    assert "--no-input" in upload_argv
    pandoc_argv = pandoc_argv_list[0]
    assert "--no-input" not in pandoc_argv


def test_docs_from_html_propagates_pandoc_nonzero_exit(tmp_path: Path) -> None:
    """A non-zero pandoc exit aborts the upload and surfaces as the CLI exit code."""
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    result, pandoc_argv_list, upload_argv_list = _invoke_from_html(
        ["docs", "from-html", str(html_path), "--name", "R"],
        pandoc_returncode=2,
    )
    assert result.exit_code == 2
    # Upload never runs when pandoc fails.
    assert upload_argv_list == []


def test_docs_from_html_propagates_upload_nonzero_exit(tmp_path: Path) -> None:
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    result, _, _ = _invoke_from_html(
        ["docs", "from-html", str(html_path), "--name", "R"],
        upload_returncode=77,
    )
    # Firewall 77 (all-blocked) propagates through unchanged.
    assert result.exit_code == 77


def test_docs_from_html_root_json_propagates_to_upload_only(tmp_path: Path) -> None:
    """Root `--json` reaches the visible engine call (upload), not pandoc."""
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    result, pandoc_argv_list, upload_argv_list = _invoke_from_html(
        ["--json", "docs", "from-html", str(html_path), "--name", "R"]
    )
    assert result.exit_code == 0
    assert "--json" in upload_argv_list[0]
    assert "--json" not in pandoc_argv_list[0]


def test_docs_from_html_help_smoke() -> None:
    result = runner.invoke(app, ["docs", "from-html", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "outbound" in combined or "write" in combined
    assert "--name" in result.stdout


def test_docs_from_html_pandoc_missing_returns_127(tmp_path: Path) -> None:
    """When pandoc isn't present the verb exits 127 with an actionable stderr."""
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    # Patch subprocess.run to raise FileNotFoundError, mimicking a missing binary,
    # while still patching run_gog_firewall so a bug that bypassed the abort
    # would be visible via a recorded upload.
    recorded_upload: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.docs.subprocess.run",
        side_effect=FileNotFoundError("pandoc: not found"),
    ), patch(
        "mineru_cli.verbs.docs.run_gog_firewall",
        _record_run_gog_firewall(recorded_upload, returncode=0),
    ):
        result = runner.invoke(
            app,
            ["docs", "from-html", str(html_path), "--name", "R"],
        )
    assert result.exit_code == 127
    assert recorded_upload == [], (
        "Upload must NOT run when pandoc is missing; recorded: "
        f"{recorded_upload!r}"
    )


# ============================================================================
# INPUT VALIDATION — from-html
# ============================================================================


def test_docs_from_html_dangling_reference_doc_is_user_error(tmp_path: Path) -> None:
    """A trailing `--reference-doc` with no value must be a BadParameter, not a silent misroute.

    Old behavior fell through the else branch and appended the bare
    `--reference-doc` token to upload_extras, which then reached gog upload
    as an unknown flag — pandoc never saw it and table-formatting silently
    degraded. The fix raises typer.BadParameter at the CLI boundary.
    """
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    recorded_pandoc: List[List[str]] = []
    recorded_upload: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.docs.subprocess.run",
        _fake_subprocess_run(recorded_pandoc, returncode=0),
    ), patch(
        "mineru_cli.verbs.docs.run_gog_firewall",
        _record_run_gog_firewall(recorded_upload, returncode=0),
    ):
        result = runner.invoke(
            app,
            ["docs", "from-html", str(html_path), "--name", "R", "--reference-doc"],
        )
    # Typer BadParameter surfaces as a non-zero exit and never invokes
    # either the pandoc conversion or the gog upload.
    assert result.exit_code != 0
    assert recorded_pandoc == []
    assert recorded_upload == []


def test_docs_from_html_rejects_path_traversal_in_name(tmp_path: Path) -> None:
    """`--name ../escape` is rejected as a BadParameter (no pandoc, no upload).

    Without the check, `os.path.join(tmp_dir, "../escape")` normalizes to a
    path OUTSIDE the TemporaryDirectory, letting pandoc clobber files on
    disk and polluting the Drive-side upload name.
    """
    html_path = tmp_path / "in.html"
    html_path.write_text("<p>x</p>")

    for bad_name in ("../escape", "sub/name", "back\\slash", "with\x00nul"):
        recorded_pandoc: List[List[str]] = []
        recorded_upload: List[List[str]] = []
        with patch(
            "mineru_cli.verbs.docs.subprocess.run",
            _fake_subprocess_run(recorded_pandoc, returncode=0),
        ), patch(
            "mineru_cli.verbs.docs.run_gog_firewall",
            _record_run_gog_firewall(recorded_upload, returncode=0),
        ):
            result = runner.invoke(
                app,
                ["docs", "from-html", str(html_path), "--name", bad_name],
            )
        assert result.exit_code != 0, (
            f"--name {bad_name!r} should be rejected but exited 0"
        )
        assert recorded_pandoc == [], f"pandoc must not run for {bad_name!r}"
        assert recorded_upload == [], f"upload must not run for {bad_name!r}"


# ============================================================================
# FIREWALL-PRESERVATION BELT-AND-BRACES (already covered by test_gmail_wrapper,
# but re-checked here so a docs-specific regression is caught in this file too).
# ============================================================================


VERB_SRC = Path(docs_verb.__file__).read_text()


def test_docs_verb_source_has_no_bypass_flags() -> None:
    """The docs verb file must not inject firewall-bypassing flags anywhere."""
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_docs_verb_source_never_invokes_raw_gog_binary() -> None:
    """Grep the docs verb file for any direct raw-gog string literal.

    A regression that swapped in `/opt/homebrew/bin/gog` (the raw, un-firewalled
    binary) or a bare `"gog"` argv[0] literal fails here. Prose mentions in
    docstrings are OK; live subprocess strings for gog-firewall are not.

    Unlike the drive verb file, docs DOES import subprocess for pandoc — that
    is the documented carve-out. What we forbid is any *gog* invocation
    outside the wrapper.
    """
    assert '"/usr/local/bin/gog"' not in VERB_SRC
    # A subprocess.run(["gog", ...]) or subprocess.run(["/opt/homebrew/bin/gog"...])
    # regression would fail here.
    for forbidden in ('subprocess.run(["gog"', "subprocess.run(['gog'"):
        assert forbidden not in VERB_SRC, (
            f"docs verb must never subprocess-call bare gog; found {forbidden!r}"
        )


def test_docs_info_end_to_end_argv_shape_read_side() -> None:
    """Belt-and-braces on the read side: the recorded argv is exactly what an operator would type."""
    result, recorded = _invoke(
        ["docs", "info", "DOC_XYZ", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["docs", "info", "DOC_XYZ", "--json"]]


def test_docs_create_end_to_end_argv_shape_write_side() -> None:
    """Belt-and-braces on the write side: create argv is verb → subverb → title → extras."""
    result, recorded = _invoke(
        ["docs", "create", "--title", "My Doc", "--parent", "FOLDER_ABC", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["docs", "create", "My Doc", "--parent", "FOLDER_ABC", "--no-input"]
    ]


# ============================================================================
# ROOT `--help` REGRESSION GUARD
# ============================================================================


def test_docs_help_lists_every_wired_verb() -> None:
    """`mineru docs --help` surfaces every wired sub-verb."""
    result = runner.invoke(app, ["docs", "--help"])
    assert result.exit_code == 0
    expected_verbs = (
        "export", "info", "cat", "create", "copy", "from-html",
    )
    for verb in expected_verbs:
        assert verb in result.stdout, (
            f"`mineru docs --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_docs_help_mentions_firewall_and_gog_firewall() -> None:
    """The noun-level help surfaces the firewall preservation + gog-firewall backing."""
    result = runner.invoke(app, ["docs", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "gog-firewall" in lowered
    assert "firewall" in lowered
