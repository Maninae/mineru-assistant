"""Tests for the Phase-2 Slack verbs (P2-07).

Covers every verb the P2-07 task requires:

  READS wired to live engines (safe to run live vs. the connected Slack workspace):
    - `slack read <channel> [--limit N]`        -> `slack-read <channel> [N]`
    - `slack users refresh`                     -> `slack-refresh-users`
    - `slack thread <channel> <ts>`             -> `slack-thread <channel> <ts>`
    - `slack channels [--include-private]`      -> `slack-channels [--include-private]`
    - `slack search public <query>`             -> `slack-search-public <query>`

  READ stubs (discoverable, no live-tool backing yet - later increment):
    - `slack profile <userId>`
    - `slack file <fileId> --out <path>`
    - `slack search {channels|public-and-private} <query>`

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every test patches the
    wrapper functions (`run_slack_read`, `run_slack_thread`,
    `run_slack_channels`, `run_slack_search_public`,
    `run_slack_refresh_users`) with a recorder, asserts the argv the
    wrapper WOULD send to the shell script, and verifies the exit-code
    plumbing. Read verbs are safe to run live, but the tests use mocks
    so the suite stays hermetic and CI-friendly (no live Slack calls,
    no Keychain access).
  - Grep invariant: the verb source contains NO write-command names
    (`send`, `post`, `write`, `slack_send`, `send_message`, `react`,
    `schedule`, `create_canvas`, `update_canvas`). This is the
    defense-in-depth guard on the observer-mode policy - a regression
    that added a write verb would fail here BEFORE anyone could type
    it and hit Slack.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List
from unittest.mock import patch

from typer.testing import CliRunner

from mineru_cli._stub import STUB_EXIT_CODE
from mineru_cli.app import app
from mineru_cli.verbs import slack as slack_verb


runner = CliRunner()


# --------------------------------------------------------------------- helpers


def _stub_stderr(result) -> str:
    """Return the stub notice text captured from a CliRunner result.

    `not_yet_implemented` writes to stderr and raises `typer.Exit(2)`. In
    newer Click/Typer versions CliRunner exposes stderr separately via
    `result.stderr`; older versions merged everything into `result.output`.
    We try `stderr` first and fall back to `output` so the assertion works
    across both.
    """
    try:
        stderr = result.stderr
    except (AttributeError, ValueError):
        stderr = ""
    if stderr:
        return stderr
    return result.output or ""


def _record(recorded: List[List[str]], returncode: int = 0):
    """Return a fake run_* recorder that captures argv and returns `returncode`.

    Shallow-copies the argv list so a later mutation of the recorded list
    cannot retroactively rewrite what we recorded.
    """

    def fake(args, **kwargs):
        recorded.append(list(args))
        return returncode

    return fake


def _invoke_read(args: List[str], returncode: int = 0):
    """Run CLI with the slack-read wrapper patched. Returns (result, recorded)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.slack.run_slack_read",
        _record(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


def _invoke_refresh(args: List[str], returncode: int = 0):
    """Run CLI with the slack-refresh-users wrapper patched. Returns (result, recorded)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.slack.run_slack_refresh_users",
        _record(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


def _invoke_thread(args: List[str], returncode: int = 0):
    """Run CLI with the slack-thread wrapper patched. Returns (result, recorded)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.slack.run_slack_thread",
        _record(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


def _invoke_channels(args: List[str], returncode: int = 0):
    """Run CLI with the slack-channels wrapper patched. Returns (result, recorded)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.slack.run_slack_channels",
        _record(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


def _invoke_search_public(args: List[str], returncode: int = 0):
    """Run CLI with the slack-search-public wrapper patched. Returns (result, recorded)."""
    recorded: List[List[str]] = []
    with patch(
        "mineru_cli.verbs.slack.run_slack_search_public",
        _record(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS (wired to live engines)
# ============================================================================


# --- read (wraps slack-read) ---------------------------------------------


def test_slack_read_positional_channel_only() -> None:
    """`mineru slack read <channel>` -> `slack-read <channel>` (no limit)."""
    result, recorded = _invoke_read(["slack", "read", "C02RAQRC10T"])
    assert result.exit_code == 0
    assert recorded == [["C02RAQRC10T"]]


def test_slack_read_with_limit_emits_second_positional() -> None:
    """`mineru slack read <channel> --limit 20` -> `slack-read <channel> 20`.

    slack-read's positional shape is `<channel_id> [limit]`. The verb
    layer translates `--limit N` into the second positional. This is
    the small mineru->engine argument-shape translation called out in
    the verb docstring.
    """
    result, recorded = _invoke_read(
        ["slack", "read", "C02RAQRC10T", "--limit", "20"]
    )
    assert result.exit_code == 0
    assert recorded == [["C02RAQRC10T", "20"]]


def test_slack_read_short_limit_flag() -> None:
    """`-n` is the documented short form of `--limit`."""
    result, recorded = _invoke_read(
        ["slack", "read", "C02RAQRC10T", "-n", "5"]
    )
    assert result.exit_code == 0
    assert recorded == [["C02RAQRC10T", "5"]]


def test_slack_read_root_json_propagates() -> None:
    """Root-level `--json` folds into the trailing extras list."""
    result, recorded = _invoke_read(
        ["--json", "slack", "read", "C02RAQRC10T"]
    )
    assert result.exit_code == 0
    assert recorded == [["C02RAQRC10T", "--json"]]


def test_slack_read_root_pretty_propagates() -> None:
    result, recorded = _invoke_read(
        ["--pretty", "slack", "read", "C02RAQRC10T"]
    )
    assert result.exit_code == 0
    assert recorded == [["C02RAQRC10T", "--pretty"]]


def test_slack_read_flag_not_duplicated_when_present_twice() -> None:
    """Root --json + trailing --json should still produce a single --json."""
    result, recorded = _invoke_read(
        ["--json", "slack", "read", "C02RAQRC10T", "--json"]
    )
    assert result.exit_code == 0
    assert recorded == [["C02RAQRC10T", "--json"]]


def test_slack_read_extras_pass_through_after_limit() -> None:
    """Unknown extras land after the positional limit, preserving order."""
    result, recorded = _invoke_read(
        ["slack", "read", "C02RAQRC10T", "--limit", "20", "--future-flag"]
    )
    assert result.exit_code == 0
    assert recorded == [["C02RAQRC10T", "20", "--future-flag"]]


def test_slack_read_propagates_nonzero_exit() -> None:
    """A non-zero rc from the wrapper (e.g. missing Keychain token) surfaces."""
    result, _ = _invoke_read(["slack", "read", "C02RAQRC10T"], returncode=1)
    assert result.exit_code == 1


def test_slack_read_help_smoke() -> None:
    result = runner.invoke(app, ["slack", "read", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    # Help must surface the observer / read-only intent.
    assert "read" in lowered
    assert "slack-read" in lowered or "channel" in lowered


# --- users refresh (wraps slack-refresh-users) ---------------------------


def test_slack_users_refresh_no_args_forwards_empty_argv() -> None:
    """`mineru slack users refresh` -> `slack-refresh-users` (no args)."""
    result, recorded = _invoke_refresh(["slack", "users", "refresh"])
    assert result.exit_code == 0
    assert recorded == [[]]


def test_slack_users_refresh_root_json_propagates() -> None:
    result, recorded = _invoke_refresh(
        ["--json", "slack", "users", "refresh"]
    )
    assert result.exit_code == 0
    assert recorded == [["--json"]]


def test_slack_users_refresh_propagates_nonzero_exit() -> None:
    result, _ = _invoke_refresh(
        ["slack", "users", "refresh"], returncode=1
    )
    assert result.exit_code == 1


def test_slack_users_refresh_help_smoke() -> None:
    result = runner.invoke(app, ["slack", "users", "refresh", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "refresh" in lowered or "user" in lowered


def test_slack_users_help_smoke_lists_refresh() -> None:
    """`mineru slack users --help` renders the `refresh` sub-verb."""
    result = runner.invoke(app, ["slack", "users", "--help"])
    assert result.exit_code == 0
    assert "refresh" in result.stdout.lower()


# ============================================================================
# READ STUB VERBS (discoverable, no live backing yet)
# ============================================================================


# --- thread (WIRED, uses run_slack_thread wrapper) ------------------------


def test_slack_thread_help_smoke() -> None:
    result = runner.invoke(app, ["slack", "thread", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "thread" in lowered
    # Both required positionals must appear in usage or arguments block.
    assert "channel" in lowered
    assert "ts" in lowered


def test_slack_thread_positionals_forwarded_in_order() -> None:
    """`mineru slack thread <C> <TS>` -> `slack-thread <C> <TS>`."""
    result, recorded = _invoke_thread(
        ["slack", "thread", "C02RAQRC10T", "1723050000.123456"]
    )
    assert result.exit_code == 0
    assert recorded == [["C02RAQRC10T", "1723050000.123456"]]


def test_slack_thread_root_json_propagates() -> None:
    """Root --json folds into the wrapper argv tail."""
    result, recorded = _invoke_thread(
        ["--json", "slack", "thread", "C02RAQRC10T", "1723050000.123456"]
    )
    assert result.exit_code == 0
    assert recorded == [["C02RAQRC10T", "1723050000.123456", "--json"]]


def test_slack_thread_extras_pass_through_after_ts() -> None:
    """Unknown extras land after both positionals (e.g. a trailing limit)."""
    result, recorded = _invoke_thread(
        ["slack", "thread", "C02RAQRC10T", "1723050000.123456", "50"]
    )
    assert result.exit_code == 0
    assert recorded == [
        ["C02RAQRC10T", "1723050000.123456", "50"],
    ]


def test_slack_thread_propagates_nonzero_exit() -> None:
    """A non-zero rc from the wrapper surfaces (missing Keychain / cache)."""
    result, _ = _invoke_thread(
        ["slack", "thread", "C02RAQRC10T", "1723050000.123456"],
        returncode=1,
    )
    assert result.exit_code == 1


def test_slack_thread_is_not_hidden_from_parent_help() -> None:
    """The wired thread verb must be visible in `mineru slack --help`."""
    result = runner.invoke(app, ["slack", "--help"])
    assert result.exit_code == 0
    assert "thread" in result.stdout.lower()


# --- profile --------------------------------------------------------------


def test_slack_profile_help_smoke() -> None:
    result = runner.invoke(app, ["slack", "profile", "--help"])
    assert result.exit_code == 0
    assert "profile" in result.stdout.lower()


def test_slack_profile_stub_runs_cleanly() -> None:
    result = runner.invoke(app, ["slack", "profile", "U08H3ABCDEF"])
    assert result.exit_code == STUB_EXIT_CODE
    assert "not yet implemented" in _stub_stderr(result).lower()


# --- channels (WIRED, uses run_slack_channels wrapper) --------------------


def test_slack_channels_help_smoke() -> None:
    result = runner.invoke(app, ["slack", "channels", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "channels" in lowered
    assert "include-private" in lowered or "private" in lowered


def test_slack_channels_default_public_only_forwards_empty_argv() -> None:
    """`mineru slack channels` -> `slack-channels` (no --include-private)."""
    result, recorded = _invoke_channels(["slack", "channels"])
    assert result.exit_code == 0
    assert recorded == [[]]


def test_slack_channels_include_private_flag_forwarded() -> None:
    """`--include-private` translates 1:1 to the shell script flag."""
    result, recorded = _invoke_channels(
        ["slack", "channels", "--include-private"]
    )
    assert result.exit_code == 0
    assert recorded == [["--include-private"]]


def test_slack_channels_root_json_propagates() -> None:
    result, recorded = _invoke_channels(["--json", "slack", "channels"])
    assert result.exit_code == 0
    assert recorded == [["--json"]]


def test_slack_channels_include_private_plus_root_pretty() -> None:
    """Both flags round-trip in the argv the wrapper receives."""
    result, recorded = _invoke_channels(
        ["--pretty", "slack", "channels", "--include-private"]
    )
    assert result.exit_code == 0
    assert recorded == [["--include-private", "--pretty"]]


def test_slack_channels_propagates_nonzero_exit() -> None:
    result, _ = _invoke_channels(["slack", "channels"], returncode=1)
    assert result.exit_code == 1


def test_slack_channels_is_not_hidden_from_parent_help() -> None:
    """The wired channels verb must be visible in `mineru slack --help`."""
    result = runner.invoke(app, ["slack", "--help"])
    assert result.exit_code == 0
    assert "channels" in result.stdout.lower()


# --- file -----------------------------------------------------------------


def test_slack_file_help_smoke() -> None:
    result = runner.invoke(app, ["slack", "file", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "file" in lowered
    assert "--out" in result.stdout or "-out" in lowered or "out" in lowered


def test_slack_file_stub_runs_cleanly_with_out() -> None:
    result = runner.invoke(
        app, ["slack", "file", "F0ABCDEF", "--out", "/tmp/downloaded.png"]
    )
    assert result.exit_code == STUB_EXIT_CODE
    assert "not yet implemented" in _stub_stderr(result).lower()


def test_slack_file_requires_out_flag() -> None:
    """`--out` is required per the task spec."""
    result = runner.invoke(app, ["slack", "file", "F0ABCDEF"])
    # Missing required option -> non-zero.
    assert result.exit_code != 0


# --- search sub-app -------------------------------------------------------


def test_slack_search_help_smoke_lists_all_scopes() -> None:
    """`mineru slack search --help` renders all three scopes."""
    result = runner.invoke(app, ["slack", "search", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "channels" in lowered
    assert "public" in lowered
    assert "public-and-private" in lowered


def test_slack_search_channels_stub_runs_cleanly() -> None:
    result = runner.invoke(
        app, ["slack", "search", "channels", "worship set list"]
    )
    assert result.exit_code == STUB_EXIT_CODE
    assert "not yet implemented" in _stub_stderr(result).lower()


def test_slack_search_public_positional_query_forwarded() -> None:
    """`mineru slack search public <q>` -> `slack-search-public <q>`."""
    result, recorded = _invoke_search_public(
        ["slack", "search", "public", "sabbath"]
    )
    assert result.exit_code == 0
    assert recorded == [["sabbath"]]


def test_slack_search_public_multi_word_query_arrives_as_one_arg() -> None:
    """A quoted multi-word query stays a single argv element."""
    result, recorded = _invoke_search_public(
        ["slack", "search", "public", "worship set list"]
    )
    assert result.exit_code == 0
    assert recorded == [["worship set list"]]


def test_slack_search_public_extras_pass_through() -> None:
    """Unknown extras (e.g. `--count 5`) pass through opaquely."""
    result, recorded = _invoke_search_public(
        ["slack", "search", "public", "prayer", "--count", "5"]
    )
    assert result.exit_code == 0
    assert recorded == [["prayer", "--count", "5"]]


def test_slack_search_public_root_json_propagates() -> None:
    result, recorded = _invoke_search_public(
        ["--json", "slack", "search", "public", "sabbath"]
    )
    assert result.exit_code == 0
    assert recorded == [["sabbath", "--json"]]


def test_slack_search_public_propagates_nonzero_exit() -> None:
    result, _ = _invoke_search_public(
        ["slack", "search", "public", "sabbath"], returncode=1
    )
    assert result.exit_code == 1


def test_slack_search_public_and_private_stub_runs_cleanly() -> None:
    result = runner.invoke(
        app, ["slack", "search", "public-and-private", "prayer request"]
    )
    assert result.exit_code == STUB_EXIT_CODE
    assert "not yet implemented" in _stub_stderr(result).lower()


def test_slack_search_channels_help_smoke() -> None:
    result = runner.invoke(app, ["slack", "search", "channels", "--help"])
    assert result.exit_code == 0


def test_slack_search_public_help_smoke() -> None:
    result = runner.invoke(app, ["slack", "search", "public", "--help"])
    assert result.exit_code == 0


def test_slack_search_public_and_private_help_smoke() -> None:
    result = runner.invoke(
        app, ["slack", "search", "public-and-private", "--help"]
    )
    assert result.exit_code == 0


# ============================================================================
# TOP-LEVEL DISCOVERY + OBSERVER-POLICY HELP
# ============================================================================


def test_slack_top_level_help_smoke_mentions_observer_mode() -> None:
    """`mineru slack --help` surfaces the READ-ONLY observer policy.

    Defense-in-depth: anyone tab-completing into the slack sub-app should
    see the observer guarantee in the first paragraph, not only in the
    module docstring.
    """
    result = runner.invoke(app, ["slack", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "read-only" in lowered or "read only" in lowered or "observer" in lowered


def test_slack_top_level_help_lists_read_and_users_refresh() -> None:
    """The two live-wired verbs must be discoverable at the top level."""
    result = runner.invoke(app, ["slack", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "read" in lowered
    assert "users" in lowered


# ============================================================================
# GREP-STYLE OBSERVER-POLICY INVARIANT (defense in depth)
# ============================================================================


VERB_SRC = Path(slack_verb.__file__).read_text()


# Allowlist of the READ commands the observer policy permits at
# module scope. Every `@<sub>_app.command(...)` registration and every
# top-level `def <name>(` MUST be in this set. Adding a new verb (with
# ANY name — a small denylist is fatally leaky because Slack's mutating
# surface is enormous: broadcast, dm, announce, upload, reply, respond,
# postMessage, postEphemeral, update, delete, pin, unpin, star, unstar,
# invite, kick, setPurpose, setTopic, dnd, usergroups.create, etc.)
# means editing this allowlist AND the spec, so a slip cannot land silently.
_READ_ONLY_COMMAND_NAMES = frozenset(
    {
        # slack_app
        "read",
        "thread",
        "profile",
        "channels",
        "file",
        # users_app (users refresh -> refresh sub-command on users_app)
        "refresh",
        # search_app
        "public",
        "public-and-private",
        # `channels` also appears under search_app; membership is a set.
    }
)

# The Python function names the verbs are registered as. Same allowlist
# discipline as the command-name set — a new module-scope `def` must
# match one of these OR the whole test fires. This catches a slip
# where someone adds `def broadcast(` even without a matching
# `@slack_app.command("broadcast")`.
_READ_ONLY_FUNCTION_NAMES = frozenset(
    {
        "read",
        "users_refresh",
        "thread",
        "profile",
        "channels",
        "file",
        "search_channels",
        "search_public",
        "search_public_and_private",
    }
)


def test_verb_source_defines_no_write_command() -> None:
    """Allowlist-guard: only the known READ verbs may register commands.

    The load-bearing observer-mode invariant. A denylist of hand-picked
    write tokens (`send`, `post`, `write`, ...) can never cover Slack's
    real mutating surface — someone could add
    `@slack_app.command("broadcast")` or `@slack_app.command("dm")` and
    the denylist would happily allow it. We instead extract every
    `@..._app.command("<name>")` registration and every module-scope
    `def <name>(` and assert each one is in the READ-only allowlist
    at the top of this file. Adding a verb means editing this test,
    which forces a spec-level review.
    """
    # Every @<some>_app.command("<name>") or @<some>_app.command('<name>')
    # in the module source. Use a permissive regex so both quote styles
    # + whitespace variants are caught.
    command_pattern = re.compile(
        r"""@\w+_app\.command\(\s*['"]([^'"]+)['"]""",
        re.MULTILINE,
    )
    registered_commands = set(command_pattern.findall(VERB_SRC))
    unexpected_commands = registered_commands - _READ_ONLY_COMMAND_NAMES
    assert not unexpected_commands, (
        f"verbs/slack.py registered command(s) NOT on the READ-only allowlist: "
        f"{sorted(unexpected_commands)}. Adding a verb requires updating the "
        "allowlist in tests/test_slack_verbs.py AND a spec-level review."
    )

    # Every module-scope `def <name>(` (four-space-indented defs
    # inside classes are ignored — the slack verb file has none, but
    # this way a future helper class wouldn't false-fire this test).
    function_pattern = re.compile(r"^def (\w+)\(", re.MULTILINE)
    module_functions = set(function_pattern.findall(VERB_SRC))
    unexpected_functions = module_functions - _READ_ONLY_FUNCTION_NAMES
    assert not unexpected_functions, (
        f"verbs/slack.py defined function(s) NOT on the READ-only allowlist: "
        f"{sorted(unexpected_functions)}. Adding a verb requires updating the "
        "allowlist in tests/test_slack_verbs.py AND a spec-level review."
    )


def test_verb_source_does_not_import_write_wrappers() -> None:
    """The verb file imports only READ wrappers.

    Allowlist-shaped: extract every `from mineru_cli.wrappers.* import ...`
    and assert only the two known READ wrappers appear. A future write
    wrapper (`run_slack_broadcast`, `run_slack_dm`, ...) would slip a
    small denylist; an allowlist can't.
    """
    wrapper_import_pattern = re.compile(
        r"from mineru_cli\.wrappers\.(\w+) import (\w+)",
    )
    imported_wrappers = set(wrapper_import_pattern.findall(VERB_SRC))
    # (module, name) tuples. Only the known READ wrappers may appear.
    allowed_wrappers = {
        ("slack_channels", "run_slack_channels"),
        ("slack_read", "run_slack_read"),
        ("slack_refresh_users", "run_slack_refresh_users"),
        ("slack_search_public", "run_slack_search_public"),
        ("slack_thread", "run_slack_thread"),
    }
    unexpected = imported_wrappers - allowed_wrappers
    assert not unexpected, (
        f"verbs/slack.py imports non-READ wrapper(s): {sorted(unexpected)}. "
        "Adding a write wrapper requires a spec-level review + updating "
        "the allowlist in tests/test_slack_verbs.py."
    )


def test_verb_source_docstring_states_read_only_observer() -> None:
    """The module docstring must document the observer policy at the top.

    A future maintainer must not be able to skim the file and miss the
    policy. The word `READ-ONLY` is required in the module-level
    docstring (verified by loading the module).
    """
    docstring = slack_verb.__doc__ or ""
    assert (
        "READ-ONLY" in docstring or "read-only" in docstring.lower()
    ), "verbs/slack.py module docstring must declare the READ-ONLY observer policy"
    # And it must call out that adding a write verb requires a spec-level change.
    assert "spec" in docstring.lower(), (
        "verbs/slack.py docstring must reference the spec-level gate on adding writes"
    )
