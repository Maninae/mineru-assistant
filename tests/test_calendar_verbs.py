"""Tests for the Phase-2 Calendar verbs (P2-02).

Covers the eight verbs the P2-02 task requires:
  READ  : list, get, search, calendars
  WRITE : create (OUTBOUND), update, delete, respond (OUTBOUND)

Test discipline (P2 hard safety rule):

  - No verb in this file is ever executed live. Every test patches
    `mineru_cli.verbs.calendar.run_gog_firewall` with a recorder, asserts
    the argv the wrapper WOULD send to the firewall, and verifies the
    exit code plumbing.
  - Every verb also gets a `--help` smoke test to confirm it renders
    without a crash and stays discoverable from the CLI surface.
  - The firewall-preservation invariant (argv[0] basename == `gog-firewall`,
    no `--raw`, no `--unsafe-strip-invisible`, no `/opt/homebrew/bin/gog`)
    already has dedicated tests in `test_gmail_wrapper.py`; the wrapper is
    the same, so we don't re-derive those here. A pair of belt-and-braces
    tests re-check the invariant end-to-end for the calendar surface —
    one read (list) and one write (respond) — so a P2 regression is
    caught here too.
  - Firewall exit codes 0 / 77 / 78 propagate through the new READ verbs
    unchanged (verified via patched wrapper; a live fake binary is
    already exercised by the gmail tests).

Why patch at `mineru_cli.verbs.calendar.run_gog_firewall`:

  Same pattern as `test_gmail_write_verbs.py::_invoke`. Patching the
  verb-module binding lets the CliRunner drive the real Typer callback
  (including root-flag propagation, `--calendar` option parsing, and
  the alias-resolution helper) without ever spawning a subprocess.
  This is the ONLY safe way to test the write verbs.

MCP-vs-CLI context (why gog-firewall calls, not MCP):

  Per `prompts/GCALENDAR.md`, MCP is the agent-preferred calendar path.
  A Python CLI cannot invoke MCP tools, so the wrapper uses
  `gog-firewall calendar` — the documented fallback. The verb file's
  module docstring is load-bearing for this rationale; these tests
  assert the docstring survives (regression guard).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.verbs import calendar as calendar_verb


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
        "mineru_cli.verbs.calendar.run_gog_firewall",
        _record_run_gog_firewall(recorded, returncode),
    ):
        result = runner.invoke(app, args)
    return result, recorded


# ============================================================================
# READ VERBS
# ============================================================================


# --- list ------------------------------------------------------------------


def test_calendar_list_without_calendar_omits_positional() -> None:
    """`--calendar` absent → no calendarId positional (gog-firewall uses primary)."""
    result, recorded = _invoke(["calendar", "list", "--from", "2026-07-27", "--to", "2026-08-03"])
    assert result.exit_code == 0
    assert recorded == [
        ["calendar", "events", "--from", "2026-07-27", "--to", "2026-08-03"]
    ]


def test_calendar_list_with_calendar_raw_id_passes_through() -> None:
    """A raw calendar ID (no alias in profile) is emitted as the positional."""
    result, recorded = _invoke(
        [
            "calendar", "list",
            "--calendar", "personal-cal-id@group.calendar.google.com",
            "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "events",
            "personal-cal-id@group.calendar.google.com",
            "--json",
        ]
    ]


def test_calendar_list_with_calendar_primary_shorthand() -> None:
    """`--calendar primary` passes through verbatim."""
    result, recorded = _invoke(["calendar", "list", "--calendar", "primary", "--today"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "events", "primary", "--today"]]


def test_calendar_list_root_json_propagates() -> None:
    """Root-level `--json` folds into the extras list."""
    result, recorded = _invoke(["--json", "calendar", "list"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "events", "--json"]]


def test_calendar_list_root_pretty_propagates() -> None:
    result, recorded = _invoke(["--pretty", "calendar", "list"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "events", "--pretty"]]


def test_calendar_list_root_json_not_duplicated_when_also_trailing() -> None:
    """Root + trailing `--json` must yield exactly one `--json` in argv."""
    result, recorded = _invoke(["--json", "calendar", "list", "--json"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "events", "--json"]]


def test_calendar_list_extras_pass_through() -> None:
    """Every gog-firewall events flag passes through opaquely."""
    result, recorded = _invoke(
        [
            "calendar", "list",
            "--calendar", "primary",
            "--week", "--max", "50", "--query", "dentist", "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "events", "primary",
            "--week", "--max", "50", "--query", "dentist", "--json",
        ]
    ]


def test_calendar_list_help_smoke() -> None:
    result = runner.invoke(app, ["calendar", "list", "--help"])
    assert result.exit_code == 0
    assert "--calendar" in result.stdout
    assert "firewall" in result.stdout.lower()


def test_calendar_list_propagates_exit_77() -> None:
    result, _ = _invoke(["calendar", "list"], returncode=77)
    assert result.exit_code == 77


def test_calendar_list_propagates_exit_78() -> None:
    result, _ = _invoke(["calendar", "list"], returncode=78)
    assert result.exit_code == 78


# --- get -------------------------------------------------------------------


def test_calendar_get_forwards_event_id_with_default_calendar() -> None:
    """--calendar absent → defaults to `primary` (gog-firewall requires positional)."""
    result, recorded = _invoke(["calendar", "get", "EV123"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "event", "primary", "EV123"]]


def test_calendar_get_forwards_with_explicit_calendar() -> None:
    result, recorded = _invoke(["calendar", "get", "EV123", "--calendar", "some@x.com", "--json"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "event", "some@x.com", "EV123", "--json"]]


def test_calendar_get_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "calendar", "get", "EV123"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "event", "primary", "EV123", "--json"]]


def test_calendar_get_root_pretty_propagates() -> None:
    result, recorded = _invoke(["--pretty", "calendar", "get", "EV123"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "event", "primary", "EV123", "--pretty"]]


def test_calendar_get_help_smoke() -> None:
    result = runner.invoke(app, ["calendar", "get", "--help"])
    assert result.exit_code == 0
    assert "--calendar" in result.stdout


def test_calendar_get_propagates_exit_77() -> None:
    result, _ = _invoke(["calendar", "get", "EV1"], returncode=77)
    assert result.exit_code == 77


# --- search ----------------------------------------------------------------


def test_calendar_search_forwards_query() -> None:
    result, recorded = _invoke(["calendar", "search", "dentist"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "search", "dentist"]]


def test_calendar_search_extras_pass_through() -> None:
    """Engine flags on search pass through: `--from`, `--to`, `--all`, `--json`."""
    result, recorded = _invoke(
        [
            "calendar", "search", "dentist",
            "--from", "2026-06-01T00:00:00-07:00",
            "--to", "2026-09-01T00:00:00-07:00",
            "--all", "--json",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "search", "dentist",
            "--from", "2026-06-01T00:00:00-07:00",
            "--to", "2026-09-01T00:00:00-07:00",
            "--all", "--json",
        ]
    ]


def test_calendar_search_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "calendar", "search", "dentist"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "search", "dentist", "--json"]]


def test_calendar_search_help_smoke() -> None:
    result = runner.invoke(app, ["calendar", "search", "--help"])
    assert result.exit_code == 0
    assert "search" in result.stdout.lower()


def test_calendar_search_propagates_exit_77_all_blocked() -> None:
    result, _ = _invoke(["calendar", "search", "x"], returncode=77)
    assert result.exit_code == 77


# --- calendars -------------------------------------------------------------


def test_calendar_calendars_argv() -> None:
    result, recorded = _invoke(["calendar", "calendars"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "calendars"]]


def test_calendar_calendars_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "calendar", "calendars"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "calendars", "--json"]]


def test_calendar_calendars_extras_pass_through() -> None:
    result, recorded = _invoke(["calendar", "calendars", "--min-access-role", "owner"])
    assert result.exit_code == 0
    assert recorded == [["calendar", "calendars", "--min-access-role", "owner"]]


def test_calendar_calendars_help_smoke() -> None:
    result = runner.invoke(app, ["calendar", "calendars", "--help"])
    assert result.exit_code == 0


# ============================================================================
# WRITE VERBS — PATCHED, NEVER EXECUTED LIVE
# ============================================================================


# --- create (WRITE, OUTBOUND) ---------------------------------------------


def test_calendar_create_defaults_to_primary_and_passes_engine_flags() -> None:
    """`create` without --calendar defaults to `primary`; every engine flag passes through opaquely."""
    result, recorded = _invoke(
        [
            "calendar", "create",
            "--summary", "Dinner",
            "--from", "2026-08-02T18:30:00-07:00",
            "--to", "2026-08-02T20:30:00-07:00",
            "--location", "1 Ferry Building, San Francisco, CA 94111",
            "--send-updates", "none",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "create", "primary",
            "--summary", "Dinner",
            "--from", "2026-08-02T18:30:00-07:00",
            "--to", "2026-08-02T20:30:00-07:00",
            "--location", "1 Ferry Building, San Francisco, CA 94111",
            "--send-updates", "none",
            "--no-input",
        ]
    ]


def test_calendar_create_with_explicit_calendar_raw_id() -> None:
    """Raw calendar ID passes through as positional."""
    raw_id = "some-random@group.calendar.google.com"
    result, recorded = _invoke(
        [
            "calendar", "create",
            "--calendar", raw_id,
            "--summary", "Test",
            "--from", "2026-08-02T18:30:00-07:00",
            "--to", "2026-08-02T19:30:00-07:00",
            "--location", "Somewhere",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded[0][:3] == ["calendar", "create", raw_id]
    # Rest of argv is the extras verbatim.
    assert recorded[0][3:] == [
        "--summary", "Test",
        "--from", "2026-08-02T18:30:00-07:00",
        "--to", "2026-08-02T19:30:00-07:00",
        "--location", "Somewhere",
        "--no-input",
    ]


def test_calendar_create_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "calendar", "create", "--summary", "X",
         "--from", "2026-08-02T00:00:00-07:00",
         "--to", "2026-08-02T01:00:00-07:00",
         "--location", "somewhere",
         "--no-input"]
    )
    assert result.exit_code == 0
    # --json appears exactly once, appended by the propagator.
    assert recorded[0].count("--json") == 1
    assert recorded[0][-1] == "--json"


def test_calendar_create_help_smoke() -> None:
    result = runner.invoke(app, ["calendar", "create", "--help"])
    assert result.exit_code == 0
    # The help text should surface the WRITE/OUTBOUND intent and the
    # location-required GCALENDAR.md rule.
    combined = result.stdout.lower()
    assert "outbound" in combined or "write" in combined
    assert "location" in combined


def test_calendar_create_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        ["calendar", "create", "--summary", "x",
         "--from", "2026-08-02T00:00:00-07:00",
         "--to", "2026-08-02T01:00:00-07:00",
         "--location", "loc"],
        returncode=2,
    )
    assert result.exit_code == 2


# --- update (WRITE) --------------------------------------------------------


def test_calendar_update_forwards_event_id_and_default_calendar() -> None:
    result, recorded = _invoke(
        [
            "calendar", "update", "EV999",
            "--location", "New Location",
            "--send-updates", "none",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "update", "primary", "EV999",
            "--location", "New Location",
            "--send-updates", "none",
            "--no-input",
        ]
    ]


def test_calendar_update_with_explicit_calendar() -> None:
    result, recorded = _invoke(
        [
            "calendar", "update", "EV999",
            "--calendar", "some@x.com",
            "--summary", "Updated",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "update", "some@x.com", "EV999",
            "--summary", "Updated",
            "--no-input",
        ]
    ]


def test_calendar_update_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "calendar", "update", "EV999", "--summary", "x", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "update", "primary", "EV999",
            "--summary", "x", "--no-input", "--json",
        ]
    ]


def test_calendar_update_help_smoke() -> None:
    result = runner.invoke(app, ["calendar", "update", "--help"])
    assert result.exit_code == 0
    assert "--calendar" in result.stdout


def test_calendar_update_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        ["calendar", "update", "EV1", "--summary", "x", "--no-input"], returncode=3
    )
    assert result.exit_code == 3


# --- delete (WRITE, DESTRUCTIVE) ------------------------------------------


def test_calendar_delete_forwards_event_id_with_default_calendar() -> None:
    """`delete` without --calendar defaults to `primary`. Never runs live."""
    result, recorded = _invoke(["calendar", "delete", "EV_DEL", "--no-input"])
    assert result.exit_code == 0
    assert recorded == [
        ["calendar", "delete", "primary", "EV_DEL", "--no-input"]
    ]


def test_calendar_delete_with_explicit_calendar_and_scope() -> None:
    """Recurring-event scope flags pass through opaquely (single/future/all)."""
    result, recorded = _invoke(
        [
            "calendar", "delete", "EV_DEL",
            "--calendar", "some@x.com",
            "--scope", "single",
            "--original-start", "2026-08-02T18:30:00-07:00",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "delete", "some@x.com", "EV_DEL",
            "--scope", "single",
            "--original-start", "2026-08-02T18:30:00-07:00",
            "--no-input",
        ]
    ]


def test_calendar_delete_root_json_propagates() -> None:
    result, recorded = _invoke(["--json", "calendar", "delete", "EV1", "--no-input"])
    assert result.exit_code == 0
    assert recorded == [
        ["calendar", "delete", "primary", "EV1", "--no-input", "--json"]
    ]


def test_calendar_delete_help_smoke() -> None:
    result = runner.invoke(app, ["calendar", "delete", "--help"])
    assert result.exit_code == 0
    # DESTRUCTIVE wording surfaces so operators know what they're invoking.
    assert "destructive" in result.stdout.lower() or "write" in result.stdout.lower()


def test_calendar_delete_propagates_engine_exit_code() -> None:
    result, _ = _invoke(["calendar", "delete", "EV1", "--no-input"], returncode=4)
    assert result.exit_code == 4


# --- respond (WRITE, OUTBOUND) --------------------------------------------


def test_calendar_respond_forwards_event_id_and_status() -> None:
    result, recorded = _invoke(
        ["calendar", "respond", "EV_INVITE", "--status", "accepted", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "respond", "primary", "EV_INVITE",
            "--status", "accepted", "--no-input",
        ]
    ]


def test_calendar_respond_with_explicit_calendar_and_comment() -> None:
    """`--comment` passes through opaquely alongside `--status`."""
    result, recorded = _invoke(
        [
            "calendar", "respond", "EV_INVITE",
            "--calendar", "some@x.com",
            "--status", "tentative",
            "--comment", "Might be late",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "respond", "some@x.com", "EV_INVITE",
            "--status", "tentative",
            "--comment", "Might be late",
            "--no-input",
        ]
    ]


def test_calendar_respond_root_json_propagates() -> None:
    result, recorded = _invoke(
        ["--json", "calendar", "respond", "EV1", "--status", "declined", "--no-input"]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "respond", "primary", "EV1",
            "--status", "declined", "--no-input", "--json",
        ]
    ]


def test_calendar_respond_help_smoke() -> None:
    result = runner.invoke(app, ["calendar", "respond", "--help"])
    assert result.exit_code == 0
    combined = result.stdout.lower()
    assert "outbound" in combined or "write" in combined
    assert "--calendar" in result.stdout


def test_calendar_respond_propagates_engine_exit_code() -> None:
    result, _ = _invoke(
        ["calendar", "respond", "EV1", "--status", "accepted", "--no-input"],
        returncode=5,
    )
    assert result.exit_code == 5


# ============================================================================
# ALIAS RESOLUTION VIA PROFILE.extras
# ============================================================================


class _StubProfile:
    """Minimal Profile-shaped stub with just the `extras` attribute the resolver reads.

    The alias-resolution helper only reads
    `profile_obj.extras.connectors.google.calendars`; a real `Profile` is not
    required. Using a stub keeps this test focused on the resolver logic
    without hydrating the whole profile-loader stack.
    """

    def __init__(self, extras: dict) -> None:
        self.extras = extras


def _invoke_with_stub_profile(args: List[str], profile: "_StubProfile"):
    """Invoke the CLI with the profile loader patched to return `profile`.

    Lazy-hydration rev (2026-08-28): `get_profile(ctx)` now runs at the
    top of every calendar verb (not the root callback) and calls
    `load_active_profile(explicit_name)` under the hood. Patching the
    loader in the profile package makes both the verb-level hydration
    and any downstream `_resolve_calendar_id` read see the same stub
    profile, without spinning up a real `profile.yaml` on disk.
    """
    from mineru_cli import app as app_module
    from mineru_cli.profile import loader as profile_loader

    recorded: List[List[str]] = []

    with patch.object(
        profile_loader, "load_active_profile", lambda name=None: profile,
    ), patch.object(
        profile_loader, "secrets_config_from_profile", lambda p: None,
    ), patch(
        "mineru_cli.verbs.calendar.run_gog_firewall",
        _record_run_gog_firewall(recorded, 0),
    ):
        result = runner.invoke(app_module.app, args)
    return result, recorded


def test_calendar_alias_resolved_from_profile_extras() -> None:
    """A `--calendar personal-cal` value with a matching profile alias expands to the raw ID.

    End-to-end via the real `_resolve_calendar_id` (no stub). Injects the
    profile via a patched app root callback, so the ctx.obj plumbing that
    the resolver actually reads is exercised.
    """
    profile = _StubProfile(
        extras={
            "connectors": {
                "google": {
                    "calendars": {
                        "personal-cal": "personal-cal-id@group.calendar.google.com",
                        "church": "church-cal-id@group.calendar.google.com",
                    }
                }
            }
        }
    )

    result, recorded = _invoke_with_stub_profile(
        ["calendar", "list", "--calendar", "personal-cal", "--today"],
        profile,
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "events",
            "personal-cal-id@group.calendar.google.com",
            "--today",
        ]
    ]


def test_calendar_alias_primary_shorthand_passes_through_with_profile() -> None:
    """`primary` is a reserved Google shorthand — passes through even when a profile exists."""
    profile = _StubProfile(
        extras={"connectors": {"google": {"calendars": {"personal-cal": "expanded-id"}}}}
    )
    result, recorded = _invoke_with_stub_profile(
        ["calendar", "get", "EV1", "--calendar", "primary"],
        profile,
    )
    assert result.exit_code == 0
    assert recorded == [["calendar", "event", "primary", "EV1"]]


def test_calendar_alias_email_form_passes_through_with_profile() -> None:
    """Email-form calendar IDs pass through (they're valid raw IDs, no alias needed)."""
    profile = _StubProfile(
        extras={"connectors": {"google": {"calendars": {"personal-cal": "expanded-id"}}}}
    )
    result, recorded = _invoke_with_stub_profile(
        ["calendar", "get", "EV1", "--calendar", "some@x.com"],
        profile,
    )
    assert result.exit_code == 0
    assert recorded == [["calendar", "event", "some@x.com", "EV1"]]


def test_calendar_alias_unknown_bare_word_fails_loud_with_profile() -> None:
    """A bare-word alias not in the profile is a BadParameter, not a silent pass-through.

    The old resolver returned the unknown alias unchanged and let
    gog-firewall reject it opaquely three layers down ("Invalid calendar
    ID"). The fixed resolver surfaces a clean CLI error naming where to
    configure the alias.
    """
    profile = _StubProfile(
        extras={"connectors": {"google": {"calendars": {"personal-cal": "expanded-id"}}}}
    )
    result, recorded = _invoke_with_stub_profile(
        ["calendar", "list", "--calendar", "typo-alias"],
        profile,
    )
    assert result.exit_code != 0, (
        "Unknown bare-word alias must fail loud; got exit 0"
    )
    assert recorded == [], (
        "gog-firewall must never run for an unknown alias"
    )
    # The BadParameter message points the user at where to configure it.
    assert "typo-alias" in result.stdout or "typo-alias" in (result.stderr or "")


class _StubCtx:
    """Minimal ctx-shaped stub the resolver can read `.obj` off of.

    The resolver only touches `ctx.obj` (a dict-like) and pulls
    `"profile_obj"` off it — no other Typer/Click Context surface is
    exercised. Using a plain stub keeps this test decoupled from Click's
    Context constructor (which requires a Command instance).
    """

    def __init__(self, obj):
        self.obj = obj


def test_resolve_calendar_id_helper_direct_expansion() -> None:
    """Unit test the helper directly: matched aliases expand; unmatched pass through."""
    profile = _StubProfile(
        extras={
            "connectors": {
                "google": {
                    "calendars": {"personal-cal": "expanded-cal-id"},
                }
            }
        }
    )
    ctx = _StubCtx({"profile_obj": profile})

    assert (
        calendar_verb._resolve_calendar_id(ctx, "personal-cal")
        == "expanded-cal-id"
    )
    # Unknown alias → pass-through.
    assert calendar_verb._resolve_calendar_id(ctx, "primary") == "primary"
    assert (
        calendar_verb._resolve_calendar_id(ctx, "some@x.com") == "some@x.com"
    )
    # None → None.
    assert calendar_verb._resolve_calendar_id(ctx, None) is None


def test_resolve_calendar_id_helper_without_profile_passes_through() -> None:
    """No profile on ctx.obj → resolver passes the value through unchanged."""
    ctx = _StubCtx({})  # no profile_obj key set

    assert (
        calendar_verb._resolve_calendar_id(ctx, "any-alias") == "any-alias"
    )
    assert calendar_verb._resolve_calendar_id(ctx, None) is None

    # Also cover the ctx.obj-is-falsy branch.
    ctx_empty = _StubCtx(None)
    assert (
        calendar_verb._resolve_calendar_id(ctx_empty, "any-alias") == "any-alias"
    )


# ============================================================================
# FIREWALL-PRESERVATION BELT-AND-BRACES (already covered by test_gmail_wrapper,
# but re-checked here so a calendar-specific regression is caught in this file too).
# ============================================================================


VERB_SRC = Path(calendar_verb.__file__).read_text()


def test_calendar_verb_source_has_no_bypass_flags() -> None:
    """The calendar verb file must not inject firewall-bypassing flags anywhere."""
    assert '"--raw"' not in VERB_SRC
    assert "'--raw'" not in VERB_SRC
    assert '"--unsafe-strip-invisible"' not in VERB_SRC
    assert "'--unsafe-strip-invisible'" not in VERB_SRC
    assert '"/opt/homebrew/bin/gog"' not in VERB_SRC
    assert "'/opt/homebrew/bin/gog'" not in VERB_SRC


def test_calendar_module_docstring_notes_mcp_vs_cli_rationale() -> None:
    """LOAD-BEARING: the module docstring must document why CLI uses gog-firewall (not MCP).

    Future maintainers who read this file need to see that MCP is the
    agent-preferred path per prompts/GCALENDAR.md, but the Python CLI
    cannot invoke MCP tools, so gog-firewall calendar is the fallback.
    Deleting that context is a regression this test catches.
    """
    doc = calendar_verb.__doc__ or ""
    lowered = doc.lower()
    assert "mcp" in lowered, "module docstring must reference MCP explicitly"
    assert "gog-firewall" in lowered, (
        "module docstring must name gog-firewall as the CLI's path"
    )
    assert "gcalendar.md" in lowered or "prompts/gcalendar" in lowered, (
        "module docstring must cite prompts/GCALENDAR.md as the source of truth"
    )
    assert "fallback" in lowered, (
        "module docstring must call the CLI path the MCP fallback"
    )


def test_calendar_list_end_to_end_argv_shape_matches_gog_firewall_expectation() -> None:
    """Belt-and-braces: the recorded argv is exactly what an operator would type.

    A regression that reordered arguments (e.g. put the calendarId after
    the flags) or dropped the `events` sub-verb would be caught here in
    addition to the more focused tests above.
    """
    result, recorded = _invoke(
        [
            "calendar", "list",
            "--calendar", "some@x.com",
            "--from", "2026-08-01T00:00:00-07:00",
            "--to", "2026-08-02T00:00:00-07:00",
            "--json",
        ]
    )
    assert result.exit_code == 0
    # Order: calendar → events → calendarId positional → extras
    assert recorded == [
        [
            "calendar", "events", "some@x.com",
            "--from", "2026-08-01T00:00:00-07:00",
            "--to", "2026-08-02T00:00:00-07:00",
            "--json",
        ]
    ]


def test_calendar_respond_end_to_end_argv_shape_write_side() -> None:
    """Belt-and-braces on the write side: respond argv is verb → calId → eventId → extras."""
    result, recorded = _invoke(
        [
            "calendar", "respond", "EV1",
            "--calendar", "some@x.com",
            "--status", "accepted",
            "--comment", "See you there",
            "--no-input",
        ]
    )
    assert result.exit_code == 0
    assert recorded == [
        [
            "calendar", "respond", "some@x.com", "EV1",
            "--status", "accepted",
            "--comment", "See you there",
            "--no-input",
        ]
    ]


# ============================================================================
# ROOT `--help` REGRESSION GUARD
# ============================================================================


def test_calendar_help_lists_the_eight_wired_verbs() -> None:
    """`mineru calendar --help` surfaces the eight wired sub-verbs."""
    result = runner.invoke(app, ["calendar", "--help"])
    assert result.exit_code == 0
    for verb in ("list", "get", "search", "calendars", "create", "update", "delete", "respond"):
        assert verb in result.stdout, (
            f"`mineru calendar --help` missing {verb!r}. Output:\n{result.stdout}"
        )


def test_calendar_help_mentions_firewall_and_gog_firewall() -> None:
    """The noun-level help surfaces the firewall preservation + gog-firewall fallback."""
    result = runner.invoke(app, ["calendar", "--help"])
    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert "gog-firewall" in lowered
    assert "firewall" in lowered
