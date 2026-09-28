"""Tests for the Phase-2 Gmail verbs beyond `search` (P2-01).

Covers the full verb tree the P2-01 task requires:
  READ  : get, thread (default + --attachments), url, history, attachment
  WRITE : label, labels {list, get, create, modify},
          batch {modify, delete}, drafts {list, get, create, update,
          delete, send}, send

Test discipline (P2 hard safety rule):
  - No verb in this file is ever executed live. Every test patches
    `mineru_cli.verbs.gmail.run_gog_firewall` with a recorder, asserts the
    argv the wrapper WOULD send to the firewall, and verifies the exit
    code plumbing.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and is discoverable from the CLI surface.
  - The firewall-preservation invariant (argv[0] basename == `gog-firewall`,
    no `--raw`, no `--unsafe-strip-invisible`, no `/opt/homebrew/bin/gog`)
    already has dedicated tests in `test_gmail_wrapper.py`; this file
    only extends the argv-routing surface. A pair of belt-and-braces
    tests re-check the invariant end-to-end for two representative
    verbs (a read and a write) so a P2 regression is caught here too.
  - Firewall exit codes 0 / 77 / 78 propagate through the new READ verbs
    unchanged (verified via a fake `gog-firewall` on disk for one
    representative read verb, mirroring the search-verb pattern).

Why patch at `mineru_cli.verbs.gmail.run_gog_firewall`:
  Same pattern as `test_gmail_search_forwards_query_and_extras` in
  `test_gmail_wrapper.py`. Patching the verb-module binding lets the
  CliRunner drive the real Typer callback (including root-flag
  propagation) without ever spawning a subprocess. This is the ONLY
  safe way to test the write verbs.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import gmail as gmail_verb
from mineru_cli.wrappers.gog_firewall import GOG_FIREWALL_BIN_ENV


runner = CliRunner()


# --------------------------------------------------------------------- helpers


def _record_run_gog_firewall(recorded: List[List[str]], returncode: int = 0):
    """Return a fake `run_gog_firewall` that records the argv list it was called with.

    The recorder captures a shallow copy so a later mutation of the list
    can't retroactively rewrite what we recorded. Returns the requested
    exit code so the caller can prove the wrapper propagates it via
    `raise typer.Exit(code=rc)`.
    """

    def fake(args, **kwargs):
        recorded.append(list(args))
        return returncode

    return fake


def _invoke(args: List[str], returncode: int = 0) -> tuple:
    """Run the CLI with a patched wrapper. Returns (CliResult, recorded_argv_list)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.gmail.run_gog_firewall",
        _record_run_gog_firewall(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS
# ============================================================================


# --- get -------------------------------------------------------------------


def test_gmail_get_forwards_message_id_and_extras() -> None:
    """`mineru gmail get <mid> --format full --json` builds the expected argv."""
    result, recorded = _invoke(
        ["gmail", "get", "18e0abc123", "--format", "full", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["gmail", "get", "18e0abc123", "--format", "full", "--json"]]


def test_gmail_get_root_json_propagates() -> None:
    """Root `--json` (before the noun) is folded into the extras list."""
    result, recorded = _invoke(["--json", "gmail", "get", "18e0abc123"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "get", "18e0abc123", "--json"]]


def test_gmail_get_root_pretty_propagates() -> None:
    result, recorded = _invoke(["--pretty", "gmail", "get", "18e0abc123"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "get", "18e0abc123", "--pretty"]]


def test_gmail_get_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "get", "--help"])
    assert result.exit_code == 0
    assert "gmail get" in result.stdout.lower() or "message" in result.stdout.lower()


def test_gmail_get_propagates_exit_77() -> None:
    result, _ = _invoke(["gmail", "get", "x"], returncode=77)
    assert result.exit_code == 77


def test_gmail_get_propagates_exit_78() -> None:
    result, _ = _invoke(["gmail", "get", "x"], returncode=78)
    assert result.exit_code == 78


# --- thread ----------------------------------------------------------------


def test_gmail_thread_default_routes_to_thread_get() -> None:
    """No `--attachments` -> routes to `gog-firewall gmail thread get <tid>`."""
    result, recorded = _invoke(["gmail", "thread", "18e0deadbeef", "--json"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "thread", "get", "18e0deadbeef", "--json"]]


def test_gmail_thread_with_attachments_routes_to_thread_attachments() -> None:
    """`--attachments` -> routes to `gog-firewall gmail thread attachments <tid>`."""
    result, recorded = _invoke(
        ["gmail", "thread", "18e0deadbeef", "--attachments", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["gmail", "thread", "attachments", "18e0deadbeef", "--json"]
    ]


def test_gmail_thread_attachments_alone_still_gets_positional_arg() -> None:
    """`--attachments` doesn't consume the threadId positional."""
    result, recorded = _invoke(["gmail", "thread", "TID", "--attachments"])
    assert result.exit_code == 0
    # argv[3] must still be the threadId, not a Typer-injected substitute.
    assert recorded[0][3] == "TID"


def test_gmail_thread_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "gmail", "thread", "TID"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "thread", "get", "TID", "--json"]]


def test_gmail_thread_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "thread", "--help"])
    assert result.exit_code == 0
    assert "--attachments" in result.stdout


def test_gmail_thread_propagates_exit_77_all_blocked() -> None:
    result, _ = _invoke(["gmail", "thread", "TID"], returncode=77)
    assert result.exit_code == 77


# --- url -------------------------------------------------------------------


def test_gmail_url_forwards_thread_id() -> None:
    result, recorded = _invoke(["gmail", "url", "TID"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "url", "TID"]]


def test_gmail_url_forwards_multiple_thread_ids_as_extras() -> None:
    """gog-firewall accepts multiple threadIds; extras pass through."""
    result, recorded = _invoke(["gmail", "url", "TID1", "TID2", "TID3"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "url", "TID1", "TID2", "TID3"]]


def test_gmail_url_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "url", "--help"])
    assert result.exit_code == 0


# --- history ---------------------------------------------------------------


def test_gmail_history_forwards_no_positional() -> None:
    result, recorded = _invoke(["gmail", "history", "--start-history-id", "12345"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "history", "--start-history-id", "12345"]]


def test_gmail_history_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "gmail", "history"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "history", "--json"]]


def test_gmail_history_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "history", "--help"])
    assert result.exit_code == 0


# --- attachment ------------------------------------------------------------


def test_gmail_attachment_forwards_both_positionals() -> None:
    result, recorded = _invoke(
        ["gmail", "attachment", "MID", "ATT", "--out", "/tmp/x.pdf"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["gmail", "attachment", "MID", "ATT", "--out", "/tmp/x.pdf"]
    ]


def test_gmail_attachment_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "attachment", "--help"])
    assert result.exit_code == 0


# ============================================================================
# WRITE VERBS - PATCHED, NEVER EXECUTED LIVE
# ============================================================================


# --- label (single thread modify) ------------------------------------------


def test_gmail_label_routes_to_thread_modify() -> None:
    """`gmail label <tid> --remove UNREAD` -> `gmail thread modify <tid> --remove UNREAD`."""
    result, recorded = _invoke(
        ["gmail", "label", "TID", "--remove", "UNREAD", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["gmail", "thread", "modify", "TID", "--remove", "UNREAD", "--no-input"]
    ]


def test_gmail_label_forwards_add_and_remove() -> None:
    result, recorded = _invoke(
        [
            "gmail", "label", "TID",
            "--add", "STARRED",
            "--remove", "UNREAD",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "gmail", "thread", "modify", "TID",
            "--add", "STARRED",
            "--remove", "UNREAD",
            "--no-input",
        ]
    ]


def test_gmail_label_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "gmail", "label", "TID", "--add", "X"])
    assert result.exit_code == 0
    assert recorded == [
        ["gmail", "thread", "modify", "TID", "--add", "X", "--json"]
    ]


def test_gmail_label_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "label", "--help"])
    assert result.exit_code == 0
    assert "write" in result.stdout.lower()


# --- send (OUTBOUND) -------------------------------------------------------


def test_gmail_send_forwards_all_flags_opaquely() -> None:
    """The direct send path: all engine flags pass through untouched.

    IMPORTANT: this test PATCHES the wrapper. No mail is ever sent.
    """
    result, recorded = _invoke(
        [
            "gmail", "send",
            "--to", "test-user@example.com",
            "--subject", "Test",
            "--body", "hello",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "gmail", "send",
            "--to", "test-user@example.com",
            "--subject", "Test",
            "--body", "hello",
        ]
    ]


def test_gmail_send_forwards_body_file_stdin_marker() -> None:
    """`--body-file -` (stdin) round-trips as an opaque extra."""
    result, recorded = _invoke(
        [
            "gmail", "send",
            "--to", "x@y.com",
            "--subject", "S",
            "--body-file", "-",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "gmail", "send",
            "--to", "x@y.com",
            "--subject", "S",
            "--body-file", "-",
        ]
    ]


def test_gmail_send_reply_flags_pass_through() -> None:
    result, recorded = _invoke(
        [
            "gmail", "send",
            "--thread-id", "TID",
            "--reply-to-message-id", "MID",
            "--reply-all",
            "--subject", "Re: ...",
            "--body", "hi",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "gmail", "send",
            "--thread-id", "TID",
            "--reply-to-message-id", "MID",
            "--reply-all",
            "--subject", "Re: ...",
            "--body", "hi",
        ]
    ]


def test_gmail_send_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "send", "--help"])
    assert result.exit_code == 0
    assert "outbound" in result.stdout.lower() or "write" in result.stdout.lower()


def test_gmail_send_propagates_engine_exit_code() -> None:
    """Non-zero exit codes surface unchanged even for write verbs."""
    result, _ = _invoke(
        ["gmail", "send", "--to", "x@y.com", "--subject", "S", "--body", "b"],
        returncode=2,
    )
    assert result.exit_code == 2


# --- labels {list, get, create, modify} ------------------------------------


def test_gmail_labels_list_argv() -> None:
    result, recorded = _invoke(["gmail", "labels", "list", "--json"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "labels", "list", "--json"]]


def test_gmail_labels_get_argv() -> None:
    result, recorded = _invoke(["gmail", "labels", "get", "MyLabel"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "labels", "get", "MyLabel"]]


def test_gmail_labels_create_argv() -> None:
    """WRITE — verify the argv, never actually create a label."""
    result, recorded = _invoke(
        ["gmail", "labels", "create", "NewLabel", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["gmail", "labels", "create", "NewLabel", "--no-input"]
    ]


def test_gmail_labels_modify_multi_thread_argv() -> None:
    """`gog-firewall gmail labels modify <tid1> <tid2> --add X --no-input` — argv only."""
    result, recorded = _invoke(
        [
            "gmail", "labels", "modify",
            "TID1", "TID2", "TID3",
            "--add", "Important",
            "--remove", "UNREAD",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "gmail", "labels", "modify",
            "TID1", "TID2", "TID3",
            "--add", "Important",
            "--remove", "UNREAD",
            "--no-input",
        ]
    ]


def test_gmail_labels_root_json_propagates_across_subverbs() -> None:
    result, recorded = _invoke(["--json", "gmail", "labels", "list"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "labels", "list", "--json"]]


def test_gmail_labels_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "labels", "--help"])
    assert result.exit_code == 0
    # Every sub-verb is discoverable from the noun help.
    for sub in ("list", "get", "create", "modify"):
        assert sub in result.stdout


def test_gmail_labels_list_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "labels", "list", "--help"])
    assert result.exit_code == 0


def test_gmail_labels_get_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "labels", "get", "--help"])
    assert result.exit_code == 0


def test_gmail_labels_create_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "labels", "create", "--help"])
    assert result.exit_code == 0


def test_gmail_labels_modify_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "labels", "modify", "--help"])
    assert result.exit_code == 0


# --- batch {modify, delete} ------------------------------------------------


def test_gmail_batch_modify_argv() -> None:
    result, recorded = _invoke(
        [
            "gmail", "batch", "modify",
            "MID1", "MID2",
            "--add", "STARRED",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "gmail", "batch", "modify",
            "MID1", "MID2",
            "--add", "STARRED",
            "--no-input",
        ]
    ]


def test_gmail_batch_delete_argv_never_executed_live() -> None:
    """DESTRUCTIVE WRITE — this test asserts the argv only.

    A regression that flipped the sub-verb from `delete` to something
    else would fail here, but no real deletion ever occurs because
    `run_gog_firewall` is patched.
    """
    result, recorded = _invoke(
        [
            "gmail", "batch", "delete",
            "MID_TO_DELETE",
            "--force",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "gmail", "batch", "delete",
            "MID_TO_DELETE",
            "--force",
            "--no-input",
        ]
    ]


def test_gmail_batch_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "batch", "--help"])
    assert result.exit_code == 0
    for sub in ("modify", "delete"):
        assert sub in result.stdout


def test_gmail_batch_modify_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "batch", "modify", "--help"])
    assert result.exit_code == 0


def test_gmail_batch_delete_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "batch", "delete", "--help"])
    assert result.exit_code == 0
    assert "destructive" in result.stdout.lower() or "delete" in result.stdout.lower()


# --- drafts {list, get, create, update, delete, send} ----------------------


def test_gmail_drafts_list_argv() -> None:
    result, recorded = _invoke(["gmail", "drafts", "list", "--json"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "drafts", "list", "--json"]]


def test_gmail_drafts_get_argv() -> None:
    result, recorded = _invoke(["gmail", "drafts", "get", "DID"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "drafts", "get", "DID"]]


def test_gmail_drafts_create_argv() -> None:
    """Create-draft is the mainline outbound prep step — argv only."""
    result, recorded = _invoke(
        [
            "gmail", "drafts", "create",
            "--to", "recipient@example.com",
            "--subject", "Subject",
            "--body-file", "/tmp/body.txt",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "gmail", "drafts", "create",
            "--to", "recipient@example.com",
            "--subject", "Subject",
            "--body-file", "/tmp/body.txt",
        ]
    ]


def test_gmail_drafts_update_argv() -> None:
    result, recorded = _invoke(
        [
            "gmail", "drafts", "update", "DID",
            "--subject", "New Subject",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["gmail", "drafts", "update", "DID", "--subject", "New Subject"]
    ]


def test_gmail_drafts_delete_argv() -> None:
    result, recorded = _invoke(["gmail", "drafts", "delete", "DID"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "drafts", "delete", "DID"]]


def test_gmail_drafts_send_argv_never_actually_sends() -> None:
    """OUTBOUND WRITE - argv only, no real send.

    The end-of-loop path in the operator's draft-first workflow. A regression
    that mis-routed this to a wrong sub-verb (or worse, straight
    `send`) would fail this argv check.
    """
    result, recorded = _invoke(["gmail", "drafts", "send", "DID_APPROVED"])
    assert result.exit_code == 0
    assert recorded == [["gmail", "drafts", "send", "DID_APPROVED"]]


def test_gmail_drafts_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "drafts", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "get", "create", "update", "delete", "send"):
        assert sub in result.stdout


def test_gmail_drafts_list_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "drafts", "list", "--help"])
    assert result.exit_code == 0


def test_gmail_drafts_get_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "drafts", "get", "--help"])
    assert result.exit_code == 0


def test_gmail_drafts_create_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "drafts", "create", "--help"])
    assert result.exit_code == 0


def test_gmail_drafts_update_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "drafts", "update", "--help"])
    assert result.exit_code == 0


def test_gmail_drafts_delete_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "drafts", "delete", "--help"])
    assert result.exit_code == 0


def test_gmail_drafts_send_help_smoke() -> None:
    result = runner.invoke(app, ["gmail", "drafts", "send", "--help"])
    assert result.exit_code == 0


# ============================================================================
# BELT-AND-BRACES FIREWALL-PRESERVATION INVARIANT (end-to-end, one read + one write)
# ============================================================================


@pytest.fixture
def fake_gog_firewall(tmp_path: Path) -> Path:
    """Fake `gog-firewall` on disk that records argv to $MINERU_TEST_ARGV_FILE and exits 0.

    Deliberately named `gog-firewall` so the argv[0] basename check is a
    real filesystem fact, not a mock assertion. Used to verify BOTH the
    firewall-preservation invariant AND the argv routing for the new
    P2-01 verbs through the actual subprocess.run code path.
    """
    script = tmp_path / "gog-firewall"
    script.write_text(
        "#!/bin/sh\n"
        "# Fake firewall for P2-01 gmail verb tests. Records argv, exits 0.\n"
        ': > "$MINERU_TEST_ARGV_FILE"\n'
        'printf "argv0=%s\\n" "$0" >> "$MINERU_TEST_ARGV_FILE"\n'
        'for a in "$@"; do\n'
        '  printf "arg=%s\\n" "$a" >> "$MINERU_TEST_ARGV_FILE"\n'
        'done\n'
        "exit 0\n"
    )
    mode = script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    script.chmod(mode)
    return script


def _parse_argv(argv_file: Path) -> tuple:
    """Return (argv0, args) from the fake's recording file."""
    argv0 = ""
    args: List[str] = []
    for line in argv_file.read_text().splitlines():
        if line.startswith("argv0="):
            argv0 = line[len("argv0="):]
        elif line.startswith("arg="):
            args.append(line[len("arg="):])
    return argv0, args


REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_MINERU = REPO_ROOT / ".venv" / "bin" / "mineru"


def _require_venv_mineru() -> Path:
    """Return the installed CLI path or skip cleanly."""
    if not VENV_MINERU.exists():
        pytest.skip(
            f"venv mineru missing at {VENV_MINERU}; run `pip install -e .` "
            "inside .venv/ first."
        )
    return VENV_MINERU


# Verbs that would dispatch a REAL outbound/state-changing action if they ever
# reached a live wrapper instead of a fake fixture.
_LIVE_BIN_DIR = str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "bin")
_OUTBOUND_VERBS = {
    "send", "create", "update", "delete", "upload", "rm", "mv", "rename",
    "deliver", "photo", "react", "edit", "unsend", "share",
}


def _spawn(args: List[str], env_overrides: dict) -> subprocess.CompletedProcess:
    # Safety guard (audit finding, 2026-07-27): never let a test dispatch a real
    # outbound action just because a fake-binary override failed to propagate.
    # Any *_BIN override must point at a fake fixture (never the live tool dir),
    # and an outbound verb must run under such an override — so an env-strip /
    # propagation regression fails loudly HERE instead of firing a real email.
    bin_overrides = {k: v for k, v in env_overrides.items() if k.endswith("_BIN")}
    for k, v in bin_overrides.items():
        assert not str(v).startswith(_LIVE_BIN_DIR), (
            f"_spawn refused: {k}={v} points at the LIVE tool dir; tests must use a fake fixture"
        )
        assert Path(v).exists(), f"_spawn refused: {k}={v} missing (fake fixture not created)"
    if any(a in _OUTBOUND_VERBS for a in args):
        assert bin_overrides, (
            "_spawn refused: outbound verb in args without a fake *_BIN override — "
            "would risk dispatching a REAL action"
        )
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [str(_require_venv_mineru()), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=30,
    )


def test_gmail_get_end_to_end_argv0_is_gog_firewall(
    tmp_path: Path,
    fake_gog_firewall: Path,
) -> None:
    """Belt-and-braces: `mineru gmail get <mid>` argv[0] basename is `gog-firewall`.

    Same regression guard as the F5 search-verb test, applied to the new
    P2-01 READ verb. If a future edit swapped the wrapper for bare `gog`,
    this test fails.
    """
    argv_file = tmp_path / "recorded_argv.txt"
    result = _spawn(
        ["gmail", "get", "MID", "--json"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_gog_firewall),
            "MINERU_TEST_ARGV_FILE": str(argv_file),
        },
    )
    assert result.returncode == 0, (
        f"unexpected rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    argv0, args = _parse_argv(argv_file)
    assert os.path.basename(argv0) == "gog-firewall"
    assert args == ["gmail", "get", "MID", "--json"]


def test_gmail_send_end_to_end_argv0_is_gog_firewall(
    tmp_path: Path,
    fake_gog_firewall: Path,
) -> None:
    """Belt-and-braces: `mineru gmail send` (WRITE) also uses `gog-firewall`.

    The fake exits 0 and RECORDS argv - it doesn't actually send mail.
    A regression that split write verbs onto bare `gog` (bypassing the
    firewall) would fail here. Read AND write must route through the same
    wrapper.
    """
    argv_file = tmp_path / "recorded_argv.txt"
    result = _spawn(
        ["gmail", "send", "--to", "test-user@example.com", "--subject", "T", "--body", "b"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_gog_firewall),
            "MINERU_TEST_ARGV_FILE": str(argv_file),
        },
    )
    assert result.returncode == 0
    argv0, args = _parse_argv(argv_file)
    assert os.path.basename(argv0) == "gog-firewall"
    assert args == [
        "gmail", "send",
        "--to", "test-user@example.com",
        "--subject", "T",
        "--body", "b",
    ]


# ============================================================================
# READ-VERB END-TO-END: FIREWALL EXIT CODES (0 / 77 / 78) propagate for a new verb
# ============================================================================


@pytest.fixture
def fake_gog_firewall_exit_77(tmp_path: Path) -> Path:
    """Fake firewall that exits 77 (all-blocked) for the READ path."""
    script = tmp_path / "gog-firewall"
    script.write_text(
        "#!/bin/sh\n"
        'printf "redacted 2 of 2 units (fake)\\n" >&2\n'
        "exit 77\n"
    )
    mode = script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    script.chmod(mode)
    return script


def test_gmail_thread_end_to_end_propagates_exit_77(
    tmp_path: Path,
    fake_gog_firewall_exit_77: Path,
) -> None:
    """The firewall's 'all blocked' code (77) surfaces intact through a new READ verb.

    Uses the real subprocess.run code path (no mocks) so a regression
    that captured stderr, remapped the exit, or bypassed the wrapper
    fails here.
    """
    result = _spawn(
        ["gmail", "thread", "TID"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_gog_firewall_exit_77),
            "MINERU_TEST_ARGV_FILE": str(tmp_path / "unused.txt"),
        },
    )
    assert result.returncode == 77
    # Stderr redaction notice flows through untouched.
    assert "redacted 2 of 2 units" in result.stderr


@pytest.fixture
def fake_gog_firewall_exit_78(tmp_path: Path) -> Path:
    """Fake firewall that exits 78 (firewall-error) for the READ path."""
    script = tmp_path / "gog-firewall"
    script.write_text(
        "#!/bin/sh\n"
        'printf "firewall internal error (fake)\\n" >&2\n'
        "exit 78\n"
    )
    mode = script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    script.chmod(mode)
    return script


def test_gmail_get_end_to_end_propagates_exit_78(
    tmp_path: Path,
    fake_gog_firewall_exit_78: Path,
) -> None:
    """The firewall's own error code (78) surfaces intact through a new READ verb."""
    result = _spawn(
        ["gmail", "get", "MID"],
        env_overrides={
            "MINERU_GOG_FIREWALL_BIN": str(fake_gog_firewall_exit_78),
            "MINERU_TEST_ARGV_FILE": str(tmp_path / "unused.txt"),
        },
    )
    assert result.returncode == 78


# ============================================================================
# STATIC SOURCE INVARIANT: no bypass literals in the new verb code.
# ============================================================================


VERB_SRC = Path(gmail_verb.__file__).read_text()


def test_verb_source_still_has_no_bypass_flags_after_p2_extension() -> None:
    """P2 extension must not introduce firewall-bypass flags.

    Duplicates the F5 grep-guard so a regression that appears only in
    P2-added code (e.g. someone adds `--raw` to `send` for a fake
    "escape hatch") is caught here as well as in the foundation test.
    """
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_verb_source_never_calls_run_gog_directly() -> None:
    """No verb imports or invokes the raw `gog` engine.

    The only allowed subprocess argv[0] is the one built inside
    run_gog_firewall from the resolved gog-firewall path. A P2 regression
    that added a `subprocess.run(["gog", ...])` bypass would fail here.
    """
    # No direct subprocess call: verbs delegate to run_gog_firewall only.
    assert "subprocess.run" not in VERB_SRC
    assert "subprocess.Popen" not in VERB_SRC
    # No import of a raw-gog module (there isn't one, but guard preemptively
    # against a future one). The tighter shape check matches actual Python
    # import syntax so the docstring phrase "from gog-firewall" doesn't false-fire.
    for py_import in ("\nimport gog\n", "\nimport gog ", "\nfrom gog import "):
        assert py_import not in VERB_SRC
