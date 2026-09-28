"""Smoke tests for every `mineru <noun> [<verb>] --help` in the tree (F7).

Purpose (§0 #13 of the capability spec, restated in the F7 done-criteria):
help works at every level — root, noun, and verb — so `mineru --help`,
`mineru gmail --help`, and `mineru gmail search --help` all exit 0 with
non-empty output. A change that renames a noun, drops a sub-verb, or
regresses a Typer callback signature fails a test here immediately, before
it can reach a live invocation.

Coverage:
  - Root `mineru --help` exits 0, mentions the 7 registered nouns.
  - `mineru <noun> --help` for every noun exits 0 with non-empty output.
  - `mineru <noun> <verb> --help` for every registered sub-verb exits 0
    with non-empty output. The (noun, verb) list is derived from Typer's
    own introspection (`app.registered_groups[*].typer_instance.registered_commands`),
    so a new sub-verb added later is automatically covered on the next run.
  - Sanity: the current tree exposes exactly the 7 nouns the F1 skeleton
    registered. Explicit hardcoded list guards against an accidental
    add/remove that Typer introspection alone wouldn't catch.
  - Sanity: the two foundation-implemented sub-verbs (`memory search`,
    `gmail search`) plus the wired-through-F3 `profile show` and the
    F2-backed `secrets get` / `secrets audit` all render help, so the
    "foundation-implemented sub-verbs" clause of the F7 done-criteria is
    explicitly asserted, not just implicitly covered by the sweep.

Discipline:
  - In-process via `typer.testing.CliRunner`. No subprocess spawns,
    since we're not asserting on real engine output — just that the help
    text renders without a Click / Typer crash.
  - No live engines needed. `mineru <noun> --help` and
    `mineru <noun> <verb> --help` are `--help` short-circuits that never
    touch the profile loader, msearch, gog-firewall, iMessage, or any
    other external system.
"""

from __future__ import annotations

from typing import Iterator, Tuple

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app


# Foundation-registered nouns (F1). Written out so a rename/removal fails
# loudly — the introspection-driven test below would silently accept a
# renamed noun.
EXPECTED_NOUNS = frozenset(
    {
        "profile",
        # Phase 1 (2026-08-28) multi-profile framework revival: people
        # (machine-level human registry) + access (per-profile
        # allowlist). Registered right after `profile` so the three
        # related nouns cluster at the top of the verb tree.
        # 2026-09-16 audit §2A F3: `humans` renamed to `people` (plain
        # English). The old `humans` spelling remains a HIDDEN alias
        # (registered via `add_typer(hidden=True)` in `mineru_cli/app.py`)
        # so it is NOT in EXPECTED_NOUNS — that set drives the
        # visible-noun invariant only.
        "people",
        "access",
        "secrets",
        "memory",
        "gmail",
        "drive",
        "docs",
        "sheets",
        # P2-05: Contacts + Tasks + People + Groups (all four gog-firewall
        # backed Workspace nouns landed together per the task's grouping).
        # 2026-09-16 audit §2B: `people` renamed to `directory` to free the
        # `people` name for the human-registry rename that chains right
        # after this one. `people` remains a HIDDEN Typer alias (registered
        # via `add_typer(hidden=True)` in `mineru_cli/app.py`) so it does
        # NOT appear in EXPECTED_NOUNS — that set drives the visible-noun
        # invariant.
        "contacts",
        "tasks",
        "directory",
        "groups",
        "telegram",
        # P2-09: Finance (Monarch Money) wraps `$MINERU_HOME/bin/monarch`.
        # Sits after telegram per the task's explicit placement.
        "finance",
        "calendar",
        "imessage",
        # P2-07: Slack (READ-ONLY observer).
        "slack",
        # P2-10: Amazon order history (via amazon-orders CLI). `brevity`
        # from the same P2-10 task is a bare command on the root app
        # (not a group -- see verbs/brevity.py Shape choice), so it does
        # NOT appear in NOUN_NAMES; it's exercised via test_brevity_verbs.py
        # and a direct root-help mention check below.
        "amazon",
        # P3-06: Browser (READ-ONLY window over the browser automation
        # server at 127.0.0.1:9471). Sits right AFTER slack per §7 of
        # the capability spec.
        "browser",
        # P3-07: user-defined custom verbs. The `custom` sub-app owns
        # add / list / show / remove; individual registered verbs are
        # added dynamically at root-callback time, not as a static
        # sub-app entry.
        "custom",
        # P4-03: cron (launchd surface) — READ-ONLY today (list, status,
        # logs, edit, plist, diff). install / run land in P4-04 / P4-05
        # with hard --dry-run defaults. Registered right after `custom`
        # per §7 of the capability spec.
        "cron",
    }
)

# Foundation-implemented sub-verbs the F7 done-criteria explicitly names.
# These are the ones actually wired to a live engine (or to F2/F3 in-process
# logic); they're a subset of the full introspection sweep, called out here
# so a regression that hid one from help would fail loudly.
FOUNDATION_IMPLEMENTED_VERBS: Tuple[Tuple[str, str], ...] = (
    ("profile", "show"),
    ("secrets", "get"),
    ("secrets", "audit"),
    ("memory", "search"),
    ("memory", "tags"),
    ("memory", "query"),
    ("gmail", "search"),
)


runner = CliRunner()


# --- Introspection helpers -------------------------------------------------


def _iter_noun_verb_pairs() -> Iterator[Tuple[str, str]]:
    """Yield (noun, sub_verb) for every registered sub-app command.

    Derives the list from Typer's own `app.registered_groups` so a
    sub-verb added later gets exercised automatically. If a Typer version
    reshapes this attribute, this helper is the one place to update.
    """
    for group in app.registered_groups:
        subapp = group.typer_instance
        assert subapp is not None, f"noun {group.name!r} has no typer instance"
        for cmd in subapp.registered_commands:
            yield group.name, cmd.name


def _iter_noun_names() -> Iterator[str]:
    for group in app.registered_groups:
        yield group.name


def _iter_visible_noun_names() -> Iterator[str]:
    """Yield only the noun sub-apps that render in `mineru --help`.

    Hidden aliases (e.g. `people` after the 2026-09-16 audit §2B rename to
    `directory`) are registered via `add_typer(..., hidden=True)` so they
    stay callable for muscle-memory continuity but do NOT appear in
    parent `--help`. The visible-surface invariant (`EXPECTED_NOUNS`)
    compares against the visible set only; the sweep tests below still
    cover hidden aliases (each hidden group must still render its own
    `--help`), so hiding is a discovery choice, not a testing gap.
    """
    for group in app.registered_groups:
        if group.hidden is True:
            continue
        yield group.name


# Enumerate at collection time so pytest -v prints one test per (noun, verb)
# pair — makes "which help entry regressed" obvious in a red run.
NOUN_VERB_PAIRS = tuple(sorted(_iter_noun_verb_pairs()))
NOUN_NAMES = tuple(sorted(_iter_noun_names()))


# --- Structural sanity: the tree matches F1 --------------------------------


def test_registered_nouns_match_f1_skeleton() -> None:
    """The F1 skeleton nouns plus P2-added nouns are exactly what the root app exposes.

    Any silent add/remove between the skeleton and the current app fails
    here. The introspection sweep below would happily accept extras or
    a rename, so this test is the pin. P2 additions (drive at P2-03)
    are added to EXPECTED_NOUNS as they land.

    Compares only the VISIBLE surface (`_iter_visible_noun_names`). Hidden
    aliases (e.g. `people` after the 2026-09-16 audit §2B rename to
    `directory`) are excluded because they exist for muscle-memory
    continuity, not for CLI discovery.
    """
    visible_nouns = set(_iter_visible_noun_names())
    assert visible_nouns == EXPECTED_NOUNS, (
        f"root app exposes {visible_nouns}, "
        f"F1 skeleton + P2 additions expected {EXPECTED_NOUNS}"
    )


def test_foundation_implemented_verbs_are_registered() -> None:
    """The wired-in-foundation sub-verbs must all appear in the tree.

    Guards against silently dropping a foundation-implemented sub-verb
    (memory search, gmail search, profile show, secrets get/audit,
    memory tags, memory query). The sweep below would still test them
    via introspection, but a missing entry means the sweep silently
    covered less; this test flips that into a loud failure.
    """
    all_pairs = set(NOUN_VERB_PAIRS)
    missing = [p for p in FOUNDATION_IMPLEMENTED_VERBS if p not in all_pairs]
    assert not missing, (
        f"foundation-implemented sub-verbs missing from the app tree: {missing}"
    )


# --- Help renders at every level ------------------------------------------


def test_root_help_exits_zero_and_mentions_every_noun() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, (
        f"`mineru --help` failed (rc={result.exit_code}):\n{result.output}"
    )
    assert result.stdout.strip(), "`mineru --help` produced empty output"
    # Every VISIBLE noun should appear in the root help's Commands panel.
    # Hidden aliases (e.g. `people` after the 2026-09-16 audit §2B rename)
    # deliberately do NOT surface in root help, so EXPECTED_NOUNS is the
    # right assertion set.
    for noun in EXPECTED_NOUNS:
        assert noun in result.stdout, (
            f"root help missing noun {noun!r}. Full output:\n{result.output}"
        )


def test_root_short_help_flag_also_works() -> None:
    """`-h` is aliased on the root callback; it must render help too."""
    result = runner.invoke(app, ["-h"])
    assert result.exit_code == 0
    assert result.stdout.strip()


@pytest.mark.parametrize("noun", NOUN_NAMES)
def test_noun_help_exits_zero_and_prints_nonempty(noun: str) -> None:
    """`mineru <noun> --help` renders for every registered noun.

    Catches any noun sub-app whose callback signature stops matching
    Typer's expectations (a common regression when a shared option is
    added incorrectly).
    """
    result = runner.invoke(app, [noun, "--help"])
    assert result.exit_code == 0, (
        f"`mineru {noun} --help` failed (rc={result.exit_code}):\n{result.output}"
    )
    assert result.stdout.strip(), (
        f"`mineru {noun} --help` produced empty output"
    )
    # The noun's own name should render somewhere in its own help text
    # (Typer includes it in Usage / Commands).
    assert noun in result.stdout


@pytest.mark.parametrize(("noun", "verb"), NOUN_VERB_PAIRS)
def test_noun_verb_help_exits_zero_and_prints_nonempty(
    noun: str, verb: str
) -> None:
    """`mineru <noun> <verb> --help` renders for every registered sub-verb.

    This is the anti-regression net that F7's done-criteria call for:
    catches signature drift on any future change. A verb whose Typer
    signature stops parsing (bad Argument type, unresolved Callable)
    fails here loud instead of at first live use.
    """
    result = runner.invoke(app, [noun, verb, "--help"])
    assert result.exit_code == 0, (
        f"`mineru {noun} {verb} --help` failed (rc={result.exit_code}):\n"
        f"{result.output}"
    )
    assert result.stdout.strip(), (
        f"`mineru {noun} {verb} --help` produced empty output"
    )


# --- Help never triggers the profile loader --------------------------------


def test_root_help_works_without_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mineru --help` on a fresh machine with no profile.yaml still works.

    The root callback deliberately skips profile loading when the invoked
    subcommand is None (bare `mineru` / bare `mineru --help`). If someone
    regresses that guard, the fresh-checkout `mineru --help` would fail —
    this test catches it.
    """
    # Point the profile base at an empty tmp dir so the default `mineru`
    # profile is guaranteed absent, then confirm --help still exits 0.
    monkeypatch.setenv("MINERU_PROFILE_ROOT", "/nonexistent/mineru-profiles-empty")
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, (
        f"root --help must work without any profile on disk:\n{result.output}"
    )


@pytest.mark.parametrize("noun", NOUN_NAMES)
def test_noun_help_works_on_fresh_clone_without_active_profile(
    noun: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`mineru <noun> --help` renders on a fresh clone with no `current` symlink.

    Fable adversarial review Finding 3: the pre-fix root callback tried
    to hydrate the active profile for every non-bootstrap noun, so
    `mineru gmail --help` on a fresh public clone (no `current` symlink,
    no seed profile) exited 2 with an "Invalid value for --profile" error.
    The fix short-circuits active-profile loading when `--help` / `-h`
    appears in the pending sub-command argv. This test walks EVERY
    registered noun and asserts help still exits 0 against a completely
    empty workspace root.
    """
    # Empty workspace root: no `profiles/`, no `current` symlink,
    # nothing at all. Simulates a fresh clone.
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, [noun, "--help"])
    assert result.exit_code == 0, (
        f"`mineru {noun} --help` must render on an empty workspace root:\n"
        f"exit={result.exit_code}\n{result.output}"
    )
    assert result.stdout.strip(), (
        f"`mineru {noun} --help` produced empty output on empty workspace"
    )


def test_dash_h_short_form_also_skips_profile_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`-h` is aliased to `--help`; the profile-load skip must recognize both."""
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["gmail", "-h"])
    assert result.exit_code == 0, (
        f"`mineru gmail -h` must render on empty workspace: {result.output}"
    )


# --- Stub verbs are hidden from parent --help ------------------------------
#
# 2026-09-16 CLI naming-consolidation audit §3A: every `not_yet_implemented`
# stub carries `hidden=True` on its Typer decorator so parent `--help` lists
# only working verbs. The stubs stay CALLABLE (so whoever's building them
# can exercise the surface) — the hide only affects discovery. This test
# pins the contract by naming each stub explicitly, so a regression that
# drops `hidden=True` fails here loud.


# (noun, verb) pairs known to be `not_yet_implemented` stubs. Kept as an
# explicit list rather than derived from `grep not_yet_implemented`
# because the failure mode we're guarding against is "someone added a
# new stub and forgot hidden=True" — the guard has to name what SHOULD
# be hidden, not what happens to invoke `not_yet_implemented` today.
_HIDDEN_STUBS: Tuple[Tuple[str, str], ...] = (
    # NOTE (2026-09-16): the onboarding-lifecycle verbs `profile validate /
    # export / import` and `secrets set / list` were WIRED as of this
    # increment, so they're no longer stubs and no longer hidden. The
    # `secrets ls` alias is still hidden (renamed → `secrets list`), but
    # it now dispatches into the REAL `list` body (not the stub), so it
    # exits 0 — see the deprecation-alias test in test_secrets.py.
    ("secrets", "rotate"),
    ("secrets", "ls"),    # deprecated alias (hidden by definition)
    # NOTE (2026-09-16): the five memory maintenance verbs (`warm-resume`,
    # `tree`, `reindex`, `consolidate`, `backup`) were WIRED in the
    # follow-on increment — see `mineru_cli/memory_ops/` and
    # `tests/test_memory_maintenance_verbs.py`. They are no longer stubs
    # and no longer hidden.
    ("calendar", "freebusy"),
    ("calendar", "conflicts"),
    # NOTE (2026-09-16): `slack thread` and `slack channels` were WIRED
    # in the stub-triage BUILD slice (see
    # `reports/2026-09-16-mineru-stub-verbs-triage.md` §C). They are no
    # longer stubs and no longer hidden. `slack profile` and `slack
    # file` remain hidden stubs pending live-tool backing.
    ("slack", "profile"),
    ("slack", "file"),
)


def _find_command_info(path: Tuple[str, ...]):
    """Return the Typer `CommandInfo` for `<path...>`, or None if absent.

    `path` is the full noun-chain plus terminal verb, so a top-level
    `secrets ls` is `("secrets", "ls")` and the nested `slack search
    users` is `("slack", "search", "users")`.
    """
    # Descend from the root app through each `add_typer`-registered group.
    current_groups = app.registered_groups
    current_commands = app.registered_commands
    for segment in path[:-1]:
        subapp = None
        for group in current_groups:
            if group.name == segment:
                subapp = group.typer_instance
                break
        if subapp is None:
            return None
        current_groups = subapp.registered_groups
        current_commands = subapp.registered_commands
    terminal = path[-1]
    for cmd in current_commands:
        if cmd.name == terminal:
            return cmd
    return None


# Nested `slack search` stubs live one level deeper — the sub-group is
# added via `slack_app.add_typer(search_app, name="search")`, so the full
# path is `slack search <scope>`. Kept as its own list so the flat
# _HIDDEN_STUBS above stays uncluttered.
_HIDDEN_NESTED_STUBS: Tuple[Tuple[str, ...], ...] = (
    ("slack", "search", "channels"),
    # NOTE (2026-09-16): `slack search public` was WIRED in the stub-triage
    # BUILD slice — no longer hidden. `channels` and `public-and-private`
    # remain hidden stubs.
    ("slack", "search", "public-and-private"),
)


@pytest.mark.parametrize(("noun", "verb"), _HIDDEN_STUBS)
def test_stub_verb_carries_hidden_true(noun: str, verb: str) -> None:
    """Every stub's Typer `CommandInfo` has `hidden=True`.

    Guards against a regression where someone lands a new stub (or drops
    `hidden=True` off an existing one) and the verb resurfaces in the
    noise-heavy `--help` output. Uses Typer introspection rather than
    scraping the rendered help text — the latter false-positives on
    common English words that also appear in parent help strings
    (`tree`, `channels`, `edit`).
    """
    cmd = _find_command_info((noun, verb))
    assert cmd is not None, f"stub `{noun} {verb}` is not registered"
    assert cmd.hidden is True, (
        f"stub `{noun} {verb}` must carry `hidden=True` on its Typer "
        "decorator (2026-09-16 audit §3A). Add `hidden=True` to the "
        "`@..._app.command(...)` call, or remove the stub if the verb has "
        "been wired."
    )
    # Own --help still works (hidden means invisible in the parent, not disabled).
    own_help = runner.invoke(app, [noun, verb, "--help"])
    assert own_help.exit_code == 0, (
        f"`mineru {noun} {verb} --help` must still render:\n{own_help.output}"
    )


@pytest.mark.parametrize("path", _HIDDEN_NESTED_STUBS)
def test_nested_stub_verb_carries_hidden_true(path: Tuple[str, ...]) -> None:
    """Nested stubs (e.g. `slack search users`) also carry `hidden=True`."""
    cmd = _find_command_info(path)
    label = " ".join(path)
    assert cmd is not None, f"stub `{label}` is not registered"
    assert cmd.hidden is True, (
        f"stub `{label}` must carry `hidden=True` on its Typer "
        "decorator (2026-09-16 audit §3A)."
    )
    own_help = runner.invoke(app, [*path, "--help"])
    assert own_help.exit_code == 0, (
        f"`mineru {label} --help` must still render:\n{own_help.output}"
    )


# --- Root --help panel ordering (2026-09-16 audit §D) ---------------------
#
# The audit calls for lifecycle + infra to cluster BEFORE the connector
# groups in `mineru --help`. The implementation uses Typer's rich_help_panel
# (two labeled panels: "Lifecycle & infra" and "Connectors"). Within each
# panel Typer sorts alphabetically; the panels themselves render in
# first-encountered order.
#
# Both these tests are cosmetic pins — a regression here would silently
# reshuffle the first-time-user impression back to a single alphabetical
# blob (or worse, swap the two panels).


_LIFECYCLE_NOUNS = frozenset(
    # 2026-09-16 audit §2A F3: `humans` renamed to `people`. The hidden
    # `humans` alias sub-app is skipped by the panel-label test (see
    # `test_lifecycle_and_connector_nouns_carry_correct_panel_label`),
    # so only the canonical name lives in this set.
    {"profile", "access", "people", "secrets", "cron", "custom", "memory"}
)
_CONNECTOR_NOUNS = frozenset(
    {
        "gmail",
        "drive",
        "docs",
        "sheets",
        "contacts",
        "tasks",
        # 2026-09-16 audit §2B: `directory` is the canonical Workspace
        # directory noun (renamed from `people`). The hidden `people`
        # alias is registered under this same panel but never surfaces in
        # `mineru --help`; it's out of scope for the visible-noun invariant.
        "directory",
        "groups",
        "calendar",
        "telegram",
        "imessage",
        "slack",
        "finance",
        "amazon",
        "browser",
    }
)


def test_root_help_renders_lifecycle_panel_before_connectors_panel() -> None:
    """`mineru --help` shows the Lifecycle & infra panel above the Connectors panel.

    Rich/Typer wraps grouped commands in `╭─ <panel title> ─...─╮` box
    lines; the panel that appears first in the file's registration is
    what the operator sees first on the terminal. The two panel titles
    below are what `_PANEL_LIFECYCLE` / `_PANEL_CONNECTORS` in
    `mineru_cli/app.py` set — if that renames, this test must too.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    lifecycle_idx = result.stdout.find("Lifecycle & infra")
    connectors_idx = result.stdout.find("Connectors")
    assert lifecycle_idx >= 0, (
        "root --help missing the 'Lifecycle & infra' panel label"
    )
    assert connectors_idx >= 0, (
        "root --help missing the 'Connectors' panel label"
    )
    assert lifecycle_idx < connectors_idx, (
        "Lifecycle & infra panel must appear before Connectors panel in "
        f"root --help.\nOutput:\n{result.output}"
    )


def test_lifecycle_and_connector_nouns_carry_correct_panel_label() -> None:
    """Every noun's registered_groups entry has the audit-mandated panel label.

    Introspects Typer's `TyperInfo.rich_help_panel` rather than scraping
    the rendered `--help`. That way a Typer / rich version bump that
    changes the box-drawing characters still passes, and the pin fires
    on the semantic contract (which noun belongs in which band) rather
    than the visual layer.
    """
    for group in app.registered_groups:
        # Hidden aliases (e.g. `people` after the 2026-09-16 audit §2B
        # rename to `directory`) are registered under both a visible and a
        # hidden name; only the visible name needs to sort into a panel.
        # Skip the hidden registrations so this test stays a semantic
        # invariant on the operator-visible surface.
        if group.hidden is True:
            continue
        info = getattr(group, "typer_instance", None)
        # Panel label lives on the group registration (TyperInfo), not the
        # sub-app itself. Newer Typer versions expose it as
        # `group.rich_help_panel`; older versions as
        # `group.typer_info.rich_help_panel`. Fall back gracefully.
        panel = getattr(group, "rich_help_panel", None)
        if panel is None:
            panel = getattr(getattr(group, "typer_info", None), "rich_help_panel", None)
        if group.name in _LIFECYCLE_NOUNS:
            assert panel == "Lifecycle & infra", (
                f"noun {group.name!r} must live in the 'Lifecycle & infra' "
                f"panel (got {panel!r}). See 2026-09-16 audit §D."
            )
        elif group.name in _CONNECTOR_NOUNS:
            assert panel == "Connectors", (
                f"noun {group.name!r} must live in the 'Connectors' panel "
                f"(got {panel!r}). See 2026-09-16 audit §D."
            )
        else:
            # Any noun that isn't in either set should trip loudly so a
            # future noun addition MUST pick a band explicitly, not
            # inherit the alphabetical default.
            pytest.fail(
                f"noun {group.name!r} is neither in the lifecycle nor "
                "connector set. Add it to _LIFECYCLE_NOUNS or "
                "_CONNECTOR_NOUNS in this test AND set "
                "`rich_help_panel=` on its `add_typer` call in "
                "`mineru_cli/app.py`."
            )
