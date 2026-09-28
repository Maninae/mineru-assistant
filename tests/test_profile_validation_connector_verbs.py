"""Phase 1.6 regression: every connector verb validates the active profile.

The Fable review that closed Phase 1 flagged one gap: the connector verbs
that route through `gog-firewall` / `monarch` / `imsg-firewall` /
`amazon-orders` did NOT call `get_profile(ctx)` at the top of their
function bodies. That meant a typo'd `--profile` value (e.g.
`mineru --profile nik gmail send ...`) returned rc=0 and ran against
machine-wide credentials instead of failing loud — a cross-profile
isolation leak that mirrored the F3 bug in a different corner.

Phase 1.6 threads `get_profile(ctx)` as the FIRST statement of every
real-operation verb across:

    gmail    drive    docs    sheets    contacts
    tasks    finance  amazon  imessage

Reads and writes are treated uniformly: for these connector nouns
there is no verb that should run without a profile, and treating
reads and writes the same makes the "every real operation validates
the profile" invariant a one-line rule anyone can enforce.

This file pins the invariant two ways:

  1. **Bogus-profile fail-loud** — for every connector verb, invoking
     `mineru --profile bogus <verb> <minimal valid args>` (with the
     underlying wrapper mocked and the workspace root pointed at an
     empty tmp dir so `bogus` has no profile.yaml) must:
       - exit NON-zero
       - never call the mocked wrapper (no side-effect argv)
     That second property is the load-bearing one: it proves the profile
     check ran BEFORE the verb built and dispatched an argv. Mocking the
     wrapper also means a would-be side effect (subprocess) is
     detectable, and we can assert it did not fire.

  2. **`--help` still renders on an empty workspace** — a group callback
     placement of `get_profile(ctx)` would run during `mineru <noun>
     --help` on a fresh clone and re-break help. Verb-body placement is
     safe because Click renders `--help` BEFORE the verb callback runs.
     For each connector noun, we assert both `mineru <noun> --help` and
     `mineru <noun> <verb> --help` succeed with rc=0 on an empty
     workspace root. (Nested sub-groups like `gmail labels`, `finance
     accounts`, `imessage chat` get the same treatment.)

Under the F3 lazy-hydration design, the root callback merely stashes
`--profile <name>` on `ctx.obj["profile"]`. `get_profile(ctx)` is what
loads / validates the profile at the point of use. This file confirms
every connector verb reaches that helper before touching the wrapper.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app


runner = CliRunner()


# ---------------------------------------------------------------------------
# Per-noun mock targets. Each connector noun has ONE wrapper function that
# every verb in that noun dispatches through; patching it lets us prove no
# side-effect argv was assembled when profile validation failed. imessage
# is the exception — reads go via imsg_firewall, writes via imsg — so it
# needs both wrappers patched.
# ---------------------------------------------------------------------------

_WRAPPER_TARGETS_BY_NOUN: dict = {
    "gmail":    ("mineru_cli.verbs.gmail.run_gog_firewall",),
    "drive":    ("mineru_cli.verbs.drive.run_gog_firewall",),
    "docs":     ("mineru_cli.verbs.docs.run_gog_firewall",),
    "sheets":   ("mineru_cli.verbs.sheets.run_gog_firewall",),
    "contacts": ("mineru_cli.verbs.contacts.run_gog_firewall",),
    "tasks":    ("mineru_cli.verbs.tasks.run_gog_firewall",),
    "finance":  ("mineru_cli.verbs.finance.run_monarch",),
    "amazon":   ("mineru_cli.verbs.amazon.run_amazon_orders",),
    "imessage": (
        "mineru_cli.verbs.imessage.run_imsg_firewall",
        "mineru_cli.verbs.imessage.run_imsg",
    ),
}


# ---------------------------------------------------------------------------
# The verb catalogue. Every entry is (noun_key, cli_argv_after_profile) where
# argv is the smallest valid invocation of that verb — enough for Typer to
# parse it and dispatch into the function body. Verbs with required
# arguments get placeholders; verbs with required options get the option
# spelling. `get_profile(ctx)` is the first line of the body, so no
# real subprocess ever runs; the placeholder values only need to satisfy
# Typer's own parsing (not gog's).
#
# Every real-operation verb in every connector file MUST appear here. The
# `docs from-html` verb is deliberately excluded from this catalogue
# because its verb body runs pandoc *before* the wrapper — validating
# get_profile(ctx) placement there is done as a dedicated case below
# (the wrapper mock alone would not prove side-effect absence). Every
# other verb is uniform.
# ---------------------------------------------------------------------------

_VERB_CATALOGUE: List[Tuple[str, List[str]]] = [
    # ---- gmail (20 real verbs) ----
    ("gmail",    ["gmail", "search", "from:test"]),
    ("gmail",    ["gmail", "get", "msg_id_placeholder"]),
    ("gmail",    ["gmail", "thread", "thread_id_placeholder"]),
    ("gmail",    ["gmail", "url", "thread_id_placeholder"]),
    ("gmail",    ["gmail", "history"]),
    ("gmail",    ["gmail", "attachment", "msg_id", "att_id"]),
    ("gmail",    ["gmail", "label", "thread_id_placeholder"]),
    ("gmail",    ["gmail", "send"]),
    ("gmail",    ["gmail", "labels", "list"]),
    ("gmail",    ["gmail", "labels", "get", "INBOX"]),
    ("gmail",    ["gmail", "labels", "create", "New-Label"]),
    ("gmail",    ["gmail", "labels", "modify", "thread_id_placeholder"]),
    ("gmail",    ["gmail", "batch", "modify", "msg_id"]),
    ("gmail",    ["gmail", "batch", "delete", "msg_id"]),
    ("gmail",    ["gmail", "drafts", "list"]),
    ("gmail",    ["gmail", "drafts", "get", "draft_id"]),
    ("gmail",    ["gmail", "drafts", "create"]),
    ("gmail",    ["gmail", "drafts", "update", "draft_id"]),
    ("gmail",    ["gmail", "drafts", "delete", "draft_id"]),
    ("gmail",    ["gmail", "drafts", "send", "draft_id"]),

    # ---- drive (15 real verbs) ----
    ("drive",    ["drive", "ls"]),
    ("drive",    ["drive", "search", "keyword"]),
    ("drive",    ["drive", "get", "file_id"]),
    ("drive",    ["drive", "perms", "file_id"]),
    ("drive",    ["drive", "drives"]),
    ("drive",    ["drive", "download", "file_id"]),
    ("drive",    ["drive", "url", "file_id"]),
    ("drive",    ["drive", "upload", "/tmp/nonexistent-placeholder.txt"]),
    ("drive",    ["drive", "copy", "file_id", "--to", "folder_id"]),
    ("drive",    ["drive", "mkdir", "folder_name"]),
    ("drive",    ["drive", "mv", "file_id", "--to", "folder_id"]),
    ("drive",    ["drive", "rename", "file_id", "--name", "new_name"]),
    ("drive",    ["drive", "rm", "file_id"]),
    ("drive",    ["drive", "share", "file_id"]),
    ("drive",    ["drive", "unshare", "file_id", "permission_id"]),

    # ---- docs (5 real verbs; from-html handled separately below) ----
    ("docs",     ["docs", "export", "doc_id"]),
    ("docs",     ["docs", "info", "doc_id"]),
    ("docs",     ["docs", "cat", "doc_id"]),
    ("docs",     ["docs", "create", "--title", "Some Title"]),
    ("docs",     ["docs", "copy", "doc_id", "--title", "New Title"]),

    # ---- sheets (6 real verbs) ----
    ("sheets",   ["sheets", "get", "sheet_id"]),
    ("sheets",   ["sheets", "metadata", "sheet_id"]),
    ("sheets",   ["sheets", "update", "sheet_id", "--range", "A1"]),
    ("sheets",   ["sheets", "append", "sheet_id", "--range", "A1"]),
    ("sheets",   ["sheets", "clear", "sheet_id", "--range", "A1"]),
    ("sheets",   ["sheets", "format", "sheet_id", "--range", "A1"]),

    # ---- contacts (9 real verbs) ----
    ("contacts", ["contacts", "lookup", "someone@example.com"]),
    ("contacts", ["contacts", "search", "query"]),
    ("contacts", ["contacts", "list"]),
    ("contacts", ["contacts", "get", "people/c123"]),
    ("contacts", ["contacts", "directory"]),
    ("contacts", ["contacts", "other"]),
    ("contacts", ["contacts", "create", "--name", "Alice"]),
    ("contacts", ["contacts", "update", "people/c123"]),
    ("contacts", ["contacts", "delete", "people/c123"]),

    # ---- tasks (9 real verbs) ----
    ("tasks",    ["tasks", "lists"]),
    ("tasks",    ["tasks", "list"]),
    ("tasks",    ["tasks", "get", "list_id", "task_id"]),
    ("tasks",    ["tasks", "add", "list_id", "--title", "Buy milk"]),
    ("tasks",    ["tasks", "update", "list_id", "task_id"]),
    ("tasks",    ["tasks", "done", "list_id", "task_id"]),
    ("tasks",    ["tasks", "undo", "list_id", "task_id"]),
    ("tasks",    ["tasks", "delete", "list_id", "task_id"]),
    ("tasks",    ["tasks", "clear", "list_id"]),

    # ---- finance (34 real verbs) ----
    ("finance",  ["finance", "auth", "login"]),
    ("finance",  ["finance", "auth", "logout"]),
    ("finance",  ["finance", "auth", "status"]),
    ("finance",  ["finance", "accounts", "list"]),
    ("finance",  ["finance", "accounts", "get", "acct_id"]),
    ("finance",  ["finance", "accounts", "holdings", "acct_id"]),
    ("finance",  ["finance", "accounts", "history", "acct_id"]),
    ("finance",  ["finance", "accounts", "refresh"]),
    ("finance",  ["finance", "accounts", "refresh-status"]),
    ("finance",  ["finance", "accounts", "types"]),
    ("finance",  ["finance", "accounts", "create", "--name", "Checking", "--type", "depository"]),
    ("finance",  ["finance", "accounts", "update", "acct_id"]),
    ("finance",  ["finance", "accounts", "delete", "acct_id"]),
    ("finance",  ["finance", "tx", "list"]),
    ("finance",  ["finance", "tx", "get", "tx_id"]),
    ("finance",  ["finance", "tx", "summary"]),
    ("finance",  ["finance", "tx", "create", "--date", "2026-08-28", "--account", "acct_id", "--amount", "-12.34"]),
    ("finance",  ["finance", "tx", "update", "tx_id"]),
    ("finance",  ["finance", "tx", "delete", "tx_id"]),
    ("finance",  ["finance", "tx", "splits", "tx_id"]),
    ("finance",  ["finance", "budgets", "list"]),
    ("finance",  ["finance", "budgets", "set", "cat_id", "500"]),
    ("finance",  ["finance", "cashflow", "summary"]),
    ("finance",  ["finance", "cashflow", "details"]),
    ("finance",  ["finance", "categories", "list"]),
    ("finance",  ["finance", "categories", "groups"]),
    ("finance",  ["finance", "categories", "create", "Groceries"]),
    ("finance",  ["finance", "categories", "delete", "cat_id"]),
    ("finance",  ["finance", "tags", "list"]),
    ("finance",  ["finance", "tags", "create", "Reimbursable"]),
    ("finance",  ["finance", "tags", "set", "tx_id", "tag1,tag2"]),
    ("finance",  ["finance", "recurring"]),
    ("finance",  ["finance", "institutions", "list"]),
    ("finance",  ["finance", "institutions", "subscription"]),

    # ---- amazon (7 real verbs) ----
    ("amazon",   ["amazon", "history"]),
    ("amazon",   ["amazon", "order", "111-1234567-1234567"]),
    ("amazon",   ["amazon", "invoice", "111-1234567-1234567"]),
    ("amazon",   ["amazon", "transactions"]),
    ("amazon",   ["amazon", "check-session"]),
    ("amazon",   ["amazon", "login"]),
    ("amazon",   ["amazon", "logout"]),

    # ---- imessage (25 real verbs; reads via imsg-firewall, writes via imsg) ----
    ("imessage", ["imessage", "chats"]),
    ("imessage", ["imessage", "history", "999"]),
    ("imessage", ["imessage", "group", "999"]),
    ("imessage", ["imessage", "search", "query"]),
    ("imessage", ["imessage", "watch"]),
    ("imessage", ["imessage", "whois"]),
    ("imessage", ["imessage", "nickname"]),
    ("imessage", ["imessage", "status"]),
    ("imessage", ["imessage", "send"]),
    ("imessage", ["imessage", "react"]),
    ("imessage", ["imessage", "edit"]),
    ("imessage", ["imessage", "unsend"]),
    ("imessage", ["imessage", "delete"]),
    ("imessage", ["imessage", "mark-read"]),
    ("imessage", ["imessage", "typing"]),
    ("imessage", ["imessage", "notify"]),
    ("imessage", ["imessage", "launch"]),
    ("imessage", ["imessage", "rpc"]),
    ("imessage", ["imessage", "chat", "create"]),
    ("imessage", ["imessage", "chat", "rename"]),
    ("imessage", ["imessage", "chat", "photo"]),
    ("imessage", ["imessage", "chat", "add"]),
    ("imessage", ["imessage", "chat", "remove"]),
    ("imessage", ["imessage", "chat", "leave"]),
    ("imessage", ["imessage", "chat", "delete"]),
]


# ---------------------------------------------------------------------------
# The load-bearing test — one row per verb.
# ---------------------------------------------------------------------------


def _verb_id(noun_and_argv: Tuple[str, List[str]]) -> str:
    """Readable pytest id: `gmail:gmail send`, `imessage:imessage chat create`."""
    noun, argv = noun_and_argv
    return f"{noun}:{' '.join(argv)}"


@pytest.mark.parametrize(
    "noun_and_argv", _VERB_CATALOGUE, ids=_verb_id
)
def test_connector_verb_rejects_bogus_profile_and_never_dispatches(
    noun_and_argv: Tuple[str, List[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bogus `--profile` must fail loud BEFORE the wrapper is called.

    The mocked wrapper records every argv it receives; a passing profile
    check must produce ZERO recorded argvs (the verb short-circuits at
    `get_profile(ctx)` and raises `typer.BadParameter` before assembling
    or dispatching anything). If a recorded argv appears, either the
    profile check is missing or it runs after the verb has already built
    (and would have dispatched) a real subprocess argv.
    """
    noun, argv_after_profile = noun_and_argv
    # Empty workspace root so `bogus` has no `profile.yaml` and no
    # `current` symlink to muddy the signal.
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)

    # Patch every wrapper this noun could dispatch through. The recorder
    # captures argv into a shared list so we can assert absence-of-call
    # across all wrappers uniformly.
    recorded: List[List[str]] = []

    def fake(args, **kwargs):
        recorded.append(list(args))
        return 0

    targets = _WRAPPER_TARGETS_BY_NOUN[noun]
    with _patch_many(targets, fake):
        result = runner.invoke(
            app, ["--profile", "bogus", *argv_after_profile]
        )

    assert result.exit_code != 0, (
        f"bogus --profile must fail loud on {argv_after_profile!r}; "
        f"got rc={result.exit_code}, output={result.output!r}"
    )
    assert recorded == [], (
        f"profile validation must run BEFORE the wrapper: "
        f"{argv_after_profile!r} dispatched {recorded!r} to a wrapper "
        f"despite --profile bogus. This is the exact Phase 1.6 leak."
    )
    # The error message names --profile / bogus somewhere.
    output = (result.output or "") + (result.stderr or "")
    assert "bogus" in output or "profile" in output.lower(), (
        f"error should name the failing profile: {output!r}"
    )


# ---------------------------------------------------------------------------
# `docs from-html` is special-cased: the verb runs pandoc BEFORE the
# gog-firewall upload, so the wrapper mock alone would not prove
# side-effect absence. Assert that a bogus profile fails loud without
# even invoking pandoc (patched to a recorder). This is the same
# fail-loud invariant, just proven at the pandoc-subprocess seam.
# ---------------------------------------------------------------------------


def test_docs_from_html_rejects_bogus_profile_before_pandoc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`docs from-html` must fail loud before pandoc runs."""
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)

    html = tmp_path / "src.html"
    html.write_text("<p>hi</p>", encoding="utf-8")

    subprocess_calls: List[list] = []
    wrapper_calls: List[list] = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        subprocess_calls.append(list(cmd))
        class _Rc:
            returncode = 0
        return _Rc()

    def fake_wrapper(argv, **kwargs):
        wrapper_calls.append(list(argv))
        return 0

    with patch(
        "mineru_cli.verbs.docs.subprocess.run", fake_subprocess_run
    ), patch(
        "mineru_cli.verbs.docs.run_gog_firewall", fake_wrapper
    ):
        result = runner.invoke(
            app,
            [
                "--profile", "bogus",
                "docs", "from-html", str(html),
                "--name", "out",
            ],
        )

    assert result.exit_code != 0, (
        f"docs from-html: bogus --profile must fail loud; "
        f"rc={result.exit_code}, output={result.output!r}"
    )
    assert subprocess_calls == [], (
        f"docs from-html: pandoc must NOT run on bogus profile; got {subprocess_calls!r}"
    )
    assert wrapper_calls == [], (
        f"docs from-html: gog-firewall must NOT run on bogus profile; got {wrapper_calls!r}"
    )


# ---------------------------------------------------------------------------
# HELP-STILL-RENDERS INVARIANT. Verb-body placement of `get_profile(ctx)`
# only holds if the callback for each subapp doesn't accidentally invoke
# it. If a maintainer moved the check up into a `@sub_app.callback()`,
# `mineru <noun> --help` on a fresh clone would once again fail loud —
# the exact F3 shape we just eliminated.
# ---------------------------------------------------------------------------


_CONNECTOR_NOUNS: List[str] = [
    "gmail", "drive", "docs", "sheets", "contacts",
    "tasks", "finance", "amazon", "imessage",
]

# One representative sub-verb per noun that we know is registered.
# Duplication with the catalogue is intentional — the catalogue is the
# fail-loud fan-out; this list is the help-render smoke.
_REPRESENTATIVE_VERBS: List[Tuple[str, str]] = [
    ("gmail",    "search"),
    ("drive",    "ls"),
    ("docs",     "info"),
    ("sheets",   "get"),
    ("contacts", "list"),
    ("tasks",    "lists"),
    ("finance",  "recurring"),
    ("amazon",   "history"),
    ("imessage", "chats"),
]

# Nested sub-groups that render their own `--help`. If a nested subapp
# grew a group callback that hydrates the profile, its `--help` would
# regress here on a fresh clone.
_NESTED_SUBAPP_HELP: List[List[str]] = [
    ["gmail", "labels", "--help"],
    ["gmail", "batch", "--help"],
    ["gmail", "drafts", "--help"],
    ["finance", "auth", "--help"],
    ["finance", "accounts", "--help"],
    ["finance", "tx", "--help"],
    ["finance", "budgets", "--help"],
    ["finance", "cashflow", "--help"],
    ["finance", "categories", "--help"],
    ["finance", "tags", "--help"],
    ["finance", "institutions", "--help"],
    ["imessage", "chat", "--help"],
]


@pytest.mark.parametrize("noun", _CONNECTOR_NOUNS)
def test_noun_help_renders_on_empty_workspace(
    noun: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mineru <noun> --help` must render on a fresh clone."""
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, [noun, "--help"])
    assert result.exit_code == 0, (
        f"`mineru {noun} --help` failed on empty workspace: {result.output!r}"
    )
    # Sanity: not just a bare exit-0.
    assert noun in result.stdout or "Usage" in result.stdout, (
        f"`mineru {noun} --help` output looks empty: {result.stdout!r}"
    )


@pytest.mark.parametrize("noun,verb", _REPRESENTATIVE_VERBS)
def test_noun_verb_help_renders_on_empty_workspace(
    noun: str, verb: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mineru <noun> <verb> --help` must render on a fresh clone."""
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, [noun, verb, "--help"])
    assert result.exit_code == 0, (
        f"`mineru {noun} {verb} --help` failed on empty workspace: "
        f"{result.output!r}"
    )


@pytest.mark.parametrize("argv", _NESTED_SUBAPP_HELP, ids=lambda a: " ".join(a))
def test_nested_subapp_help_renders_on_empty_workspace(
    argv: List[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested sub-group `--help` must render on a fresh clone.

    A group callback that hydrates the profile would fail here even
    though the leaf verbs still work. Verb-body placement keeps every
    `--help` frame safe.
    """
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, (
        f"`mineru {' '.join(argv)}` failed on empty workspace: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# Helper: patch several dotted paths at once and yield the recorded fake.
# Written as a context manager so `with _patch_many(...)` reads like a
# single patch across many targets.
# ---------------------------------------------------------------------------


class _patch_many:
    """Context manager that patches every dotted `target` with `replacement`.

    Cleans up in reverse order on exit; each `patch()` handles its own
    exception path.
    """

    def __init__(self, targets: Sequence[str], replacement) -> None:
        self._patchers = [patch(t, replacement) for t in targets]

    def __enter__(self) -> None:
        for p in self._patchers:
            p.start()
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        for p in reversed(self._patchers):
            p.stop()
