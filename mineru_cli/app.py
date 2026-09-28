"""Root `mineru` Typer app.

Registers the seven foundation nouns as sub-apps so the full spelled-out verb
tree renders under `mineru --help`. Global flags (--json / --pretty / --profile)
live on the root callback and are propagated to sub-apps via `ctx.obj`. Wired
verbs (memory search, gmail search) forward the resolved --json / --pretty
flag to the underlying engine so root-level and trailing forms behave the same.

Profile hydration: LAZY (2026-08-28 rev). The root callback stashes the
`--profile <name>` flag value on `ctx.obj["profile"]` and does NOT touch the
profile loader. Every verb that requires a profile calls
`mineru_cli.profile.get_profile(ctx)` at its point of use, which on first
call resolves + loads + validates the active profile and caches both the
`Profile` and its derived `SecretsConfig` on `ctx.obj` under the keys
`profile_obj` and `secrets_config`.

Why lazy: eager hydration in the callback required a heuristic to skip
loading on `--help` invocations, and that heuristic could not distinguish a
help FLAG from an option VALUE — `mineru --profile bogus telegram photo x
--caption "--help"` silently skipped profile validation and fell through to
the default profile, a real cross-profile isolation leak. Under lazy
hydration, Click renders `--help` BEFORE the verb callback runs, so a
verb's `get_profile(ctx)` is never reached on a help path (by construction)
and a real command with a bogus profile fails loud at the first verb-side
`get_profile(ctx)` call.

Naming discipline: full spelled-out nouns only (memory, telegram, calendar,
imessage) per §0 of the 2026-07-25 capability spec. Abbreviated forms
(mem, tg, cal, imsg) are explicitly forbidden as command names.

Root --help layering (2026-09-16 CLI naming-consolidation audit §D):
sub-apps render in registration order, and we cluster them in two bands so
a first-time reader sees the lifecycle + infra surface first (what you use
to onboard / operate the agent) and the third-party connectors second.

  Band 1 — lifecycle + infra (audit's exact ordering):
    profile, setup, access, people, secrets, cron, custom, memory
  Band 2 — connectors (Google Workspace grouped, then messaging, then
  finance / shopping, then browser + brevity utilities at the tail):
    gmail, drive, docs, sheets, contacts, tasks, directory, groups, calendar,
    telegram, imessage, slack, finance, amazon, browser, brevity

  (2026-09-16 audit §2A + §2B: the Google Workspace directory sub-app
  moved from `people` to `directory` first, so the `people` name could
  be re-used for the machine-level human registry. The registry sub-app
  used to be `humans` and is now `people`; the old `mineru humans`
  spelling remains a HIDDEN 90-day compat alias — see
  `mineru_cli/verbs/people.py::humans_alias_app`. The transient hidden
  `people` alias for the Google-connector rename was DROPPED here
  because the canonical `people` name is now taken by the registry.)
"""

from __future__ import annotations

import typer

from mineru_cli.profile import resolve_assistant_name_for_help
from mineru_cli.verbs import (
    access as access_verb,
    amazon,
    brevity,
    browser,
    calendar,
    contacts,
    cron as cron_verb,
    custom as custom_verb,
    docs,
    drive,
    finance,
    directory as directory_verb,
    gmail,
    groups,
    imessage,
    memory,
    people as people_verb,
    profile as profile_verb,
    secrets,
    setup as setup_verb,
    sheets,
    slack,
    tasks,
    telegram,
)

# Persona-name genericization: the root `--help` string surfaces the active
# profile's `assistant_name` instead of a hardcoded "Mineru", so a profile
# with `assistant_name: Alfred` renders "every Alfred capability". Typer
# renders the root help before the callback runs, so `MINERU_PROFILE` +
# the default profile participate but `--profile <name>` does not; the
# fallback keeps `mineru --help` working on a fresh clone with no
# profile.yaml. Subcommand invocations still hydrate the profile per-call.
_ASSISTANT_NAME_FOR_HELP = resolve_assistant_name_for_help()

app = typer.Typer(
    name="mineru",
    help=(
        f"mineru: unified CLI front door over every {_ASSISTANT_NAME_FOR_HELP} capability.\n\n"
        "Foundation increment covers profile, secrets, memory, gmail, telegram, "
        "calendar, imessage. Only `memory search` and `gmail search` are wired "
        "to live engines today; other verbs are discoverable stubs."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    # `cls=CustomVerbTyperGroup` wires the P3-07 dynamic-dispatch machinery
    # onto the ROOT app: it overrides `list_commands` / `get_command` so
    # every registered custom verb (from <profile_root>/custom_verbs.yaml)
    # appears alongside the built-in noun tree and dispatches through the
    # subprocess.run path in `verbs/custom.py`. The subclass FIRST tries the
    # built-in tree and only falls back to the registry, so a built-in
    # never gets shadowed. See `mineru_cli.verbs.custom` module docstring
    # for the full rationale (why-not-in-callback, collision policy, etc.).
    cls=custom_verb.CustomVerbTyperGroup,
)
# Belt-and-braces: `install_custom_group(app)` also mutates
# `app.info.cls` in case a future Typer version handles the `cls=` kwarg
# differently on `add_typer`-registered sub-apps. This is a no-op today
# but keeps the machinery discoverable in one place.
custom_verb.install_custom_group(app)


@app.callback()
def root(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON output where a verb supports it.",
    ),
    pretty: bool = typer.Option(
        False,
        "--pretty",
        help="Human-readable pretty-printed output where a verb supports it.",
    ),
    profile_name: str = typer.Option(
        None,
        "--profile",
        metavar="NAME",
        help=(
            "Active profile name. Resolution order (highest first): "
            "--profile > MINERU_PROFILE > `active` symlink "
            "at the workspace root (legacy `current` symlink as fallback) "
            "> fail loud (no silent default). "
            "Switch the persistent default with `mineru profile use <name>`."
        ),
    ),
) -> None:
    """Stash global flags for every mineru verb; do NOT load the profile here.

    Stores `--json`, `--pretty`, and the `--profile <name>` value on
    `ctx.obj` so sub-apps can read them without re-declaring the flags.
    Profile hydration is LAZY: verbs that need a profile call
    `mineru_cli.profile.get_profile(ctx)` at their point of use. On the
    first such call, the profile is loaded, validated, and cached on
    `ctx.obj` under `profile_obj` and `secrets_config`; subsequent calls
    reuse the cache.

    Doing NO loading here is deliberate. An earlier eager design tried to
    skip profile hydration on `--help` invocations via a heuristic that
    scanned argv for the token `--help`, but that heuristic could not
    tell a help FLAG from an option VALUE — a caption or label whose
    value happened to be `--help` bypassed profile validation entirely
    and fell through to the default profile, silently leaking writes
    across profiles. Under lazy hydration Click renders `--help` BEFORE
    the verb callback runs, so `--help` at any level works on a fresh
    clone by construction, and a real command with a bogus profile
    fails loud the first time its verb calls `get_profile(ctx)`.
    """
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_out
    ctx.obj["pretty"] = pretty
    ctx.obj["profile"] = profile_name


# Rich help panels — Typer / rich groups commands under a labeled panel
# in `mineru --help`. Within a panel Typer sorts alphabetically, but the
# panels themselves render in first-encountered order, so putting Band 1
# BEFORE Band 2 in the file below is what the operator sees on the terminal
# (2026-09-16 CLI naming-consolidation audit §D). Bare commands (`setup`,
# `brevity`) carry the same panel name on their own decorator so they
# cluster with their band.
_PANEL_LIFECYCLE = "Lifecycle & infra"
_PANEL_CONNECTORS = "Connectors"


# --- Band 1: lifecycle + infra --------------------------------------------
#
# Audit-mandated grouping (2026-09-16 §D):
#   profile, setup, access, people, secrets, cron, custom, memory
# (The audit's original ordering said `humans` in the fourth slot; that
# noun was renamed to `people` on the same day — see audit §2A F3.)
# Within the panel Typer sorts alphabetically; the panel LABEL is what
# clusters the lifecycle surface visually ahead of the connector surface.

app.add_typer(
    profile_verb.profile_app,
    name="profile",
    help="Profile management (show, use, active, init, install, validate, export, import).",
    rich_help_panel=_PANEL_LIFECYCLE,
)
# Guided first-run setup verb — thin orchestration over `profile init`
# + `profile install` + a Keychain-secrets checklist. Bare command (not
# a group), registered here (not via `add_typer`) via `register_setup`
# so it lands right after `profile` in the lifecycle band.
setup_verb.register_setup(app)
app.add_typer(
    access_verb.access_app,
    name="access",
    help=(
        "Per-profile access allowlist (owner + authorized humans): show, "
        "add, remove, sync, status. `sync` / `add --apply` / `remove --apply` "
        "write the Landline daemon's Keychain slot (telegram-allowed-chat-ids) "
        "under the profile's keychain_account. Guest-tier enforcement DEFERRED."
    ),
    rich_help_panel=_PANEL_LIFECYCLE,
)
app.add_typer(
    people_verb.people_app,
    name="people",
    help=(
        "Machine-level human registry (thin Telegram identities): "
        "list, path. Renamed from `humans` on 2026-09-16 (audit §2A F3)."
    ),
    rich_help_panel=_PANEL_LIFECYCLE,
)
# HIDDEN DEPRECATED alias — `mineru humans ...` (pre-2026-09-16 spelling).
# Both verbs under the old name fire a one-line DEPRECATED notice on stderr
# and delegate through the canonical verb body. Standard 90-day compat
# window (see `mineru_cli/_deprecation.py`). Sits under the same
# Lifecycle & infra panel; `hidden=True` keeps it out of `mineru --help`.
app.add_typer(
    people_verb.humans_alias_app,
    name="humans",
    help=(
        "DEPRECATED alias for `mineru people`. Kept as a hidden 90-day "
        "compat window; will be removed after the deprecation cycle."
    ),
    hidden=True,
    rich_help_panel=_PANEL_LIFECYCLE,
)
app.add_typer(
    secrets.secrets_app,
    name="secrets",
    help="Secrets (env / macOS Keychain / 1Password later): get, audit, set, list.",
    rich_help_panel=_PANEL_LIFECYCLE,
)
# Cron promoted into Band 1 by the audit — infra-adjacent noun for launchd
# job management. Read verbs (list / status / logs / edit / plist / diff)
# landed in P4-03; `run` in P4-04; `install` / `uninstall` in P4-05 with
# the LIVE path gated behind `--live-flip` + MINERU_CRON_ALLOW_LIVE=1 in
# this SAFE build.
app.add_typer(
    cron_verb.cron_app,
    name="cron",
    help=(
        "Scheduled jobs (launchd) surface. Read verbs: list, status, "
        "logs, edit, plist, diff. Write verbs: run, install, uninstall. "
        "Install / uninstall LIVE paths gated behind --live-flip AND "
        "MINERU_CRON_ALLOW_LIVE=1; use --dry-run to preview."
    ),
    rich_help_panel=_PANEL_LIFECYCLE,
)
# Custom user-defined verbs (P3-07). Individual registered verbs (from
# custom_verbs.yaml) are added dynamically at root-callback time, not here.
app.add_typer(
    custom_verb.custom_app,
    name="custom",
    help=(
        "User-defined custom verbs: add / list / show / remove. "
        "Registered verbs become root-level `mineru <name>` commands."
    ),
    rich_help_panel=_PANEL_LIFECYCLE,
)
app.add_typer(
    memory.memory_app,
    name="memory",
    help=(
        "Memory & search: msearch keyword/tag/query, plus warm-resume, "
        "tree, reindex, consolidate, backup."
    ),
    rich_help_panel=_PANEL_LIFECYCLE,
)

# --- Band 2: connectors ---------------------------------------------------
#
# Google Workspace first (gmail, drive, docs, sheets, contacts, tasks,
# people, groups, calendar), then messaging (telegram, imessage, slack),
# then finance / shopping (finance, amazon), then the browser + brevity
# utilities at the tail.

app.add_typer(
    gmail.gmail_app,
    name="gmail",
    help="Gmail: firewall-preserving reads and writes via the injection firewall; search, get, thread, label, labels, batch, drafts, send.",
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    drive.drive_app,
    name="drive",
    help=(
        "Google Drive: firewall-preserving reads and writes via gog-firewall; "
        "ls, search, get, perms, drives, download, url, upload, copy, mkdir, "
        "mv, rename, rm, share, unshare."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    docs.docs_app,
    name="docs",
    help=(
        "Google Docs: firewall-preserving reads and writes via gog-firewall; "
        "export, info, cat, create, copy, from-html (pandoc + drive upload)."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    sheets.sheets_app,
    name="sheets",
    help=(
        "Google Sheets: firewall-preserving reads and writes via gog-firewall; "
        "get, metadata, update, append, clear, format."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    contacts.contacts_app,
    name="contacts",
    help=(
        "Google Contacts: firewall-preserving reads and writes via gog-firewall; "
        "lookup, search, list, get, directory, other, create, update, delete."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    tasks.tasks_app,
    name="tasks",
    help=(
        "Google Tasks: firewall-preserving reads and writes via gog-firewall; "
        "lists, items, get, add, update, done, undo, delete, clear. "
        "`lists` (plural) enumerates task LISTS; `items` enumerates TASKS in a list."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    directory_verb.directory_app,
    name="directory",
    help=(
        "Google People (Workspace directory profiles): firewall-preserving reads "
        "via gog-firewall; me, get, search, relations. Renamed from `people` on "
        "2026-09-16 (audit §2B) to free `people` for the human registry."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
# NOTE: the transient hidden `people` alias for the Google directory
# connector (added in the preceding commit of this rename chain) was
# DROPPED here. Rationale: the canonical `people` sub-app now belongs to
# the machine-level human registry (audit §2A F3, the rename this file
# just made). Typer cannot register two sub-apps under the same name;
# keeping both would be structurally impossible AND semantically
# confusing (two things called `people`, one hidden). The
# `directory_verb.people_alias_app` symbol stays defined in
# `verbs/directory.py` (its module docstring flags the transient nature)
# for callers that already imported it; nothing in the CLI mounts it any
# more. Muscle-memory continuity for `mineru people {me,get,search,
# relations}` was one commit long; the canonical `mineru directory ...`
# has been the visible name since the preceding commit landed.
app.add_typer(
    groups.groups_app,
    name="groups",
    help=(
        "Google Groups: firewall-preserving reads via gog-firewall; "
        "list, members."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    calendar.calendar_app,
    name="calendar",
    help=(
        "Google Calendar: firewall-preserving reads and writes via gog-firewall; "
        "list, get, search, calendars, create, update, delete, respond. "
        "MCP is agent-preferred; CLI uses the gog-firewall fallback."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    telegram.telegram_app,
    name="telegram",
    help="Telegram (Landline delivery + inject): send, deliver, photo, inject, allowlist.",
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    imessage.imessage_app,
    name="imessage",
    help=(
        "iMessage: firewall-preserving reads via imsg-firewall; direct outbound "
        "sends via imsg. chats, history, group, search, watch, whois, nickname, "
        "status; send, react, edit, unsend, delete, mark-read, typing, notify, "
        "chat (create/rename/photo/add/remove/leave/delete), launch, rpc."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
# Slack: READ-ONLY observer by SECURITY policy — no send/post/write verb
# is exposed. Reads wrap live shell scripts in `$MINERU_HOME/bin/`
# (`slack-read`, `slack-refresh-users`, `slack-thread`, `slack-channels`,
# `slack-search-public`). The remaining Web-API-backed reads (profile,
# file, search channels, search public-and-private) are discoverable
# stubs pending a live-tool backing.
app.add_typer(
    slack.slack_app,
    name="slack",
    help=(
        "Slack (READ-ONLY observer): read, thread, channels, search public, "
        "users refresh. NO send/post/write verb exists."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
# Finance (Monarch Money) + Amazon: connector-adjacent utilities wrapping
# thin `$MINERU_HOME/bin/` CLIs. Reads safe live; the two amazon writes
# (login / logout) are MOCK-ONLY in tests. NOTE: `artifact-detect` and
# `artifact-remove` in `$MINERU_HOME/bin/` are DEAD per spec §7 — no verbs
# for them.
app.add_typer(
    finance.finance_app,
    name="finance",
    help=(
        "Finance (Monarch Money) via the `monarch` CLI: auth, accounts, tx, "
        "budgets, cashflow, categories, tags, recurring, institutions. Reads "
        "safe live; writes MOCK-ONLY in tests."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
app.add_typer(
    amazon.amazon_app,
    name="amazon",
    help=(
        "Amazon order history (via amazon-orders CLI): history, order, invoice, "
        "transactions, check-session (READ); login, logout (WRITE, MOCK-ONLY in tests)."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
# Browser (P3-06). READ-ONLY window over the browser automation server at
# 127.0.0.1:9471 today (`tabs` verb: snapshot + SSE stream + on-disk snapshot
# fallback). open/act/snapshot/screenshot still go through the raw HTTP surface
# until a wrapper is wired for them.
app.add_typer(
    browser.browser_app,
    name="browser",
    help=(
        "Browser automation server surface (READ-ONLY today): "
        "tabs (snapshot + --watch SSE + --grep filter, with on-disk fallback)."
    ),
    rich_help_panel=_PANEL_CONNECTORS,
)
# brevity is a BARE COMMAND, not a group -- see verbs/brevity.py's Shape
# choice docstring. A Click group would treat `mineru brevity <url>
# --extended` (natural human order) as "route --extended to a subcommand"
# and fail; the bare-command registration takes the positional + option
# cleanly. Uses register_brevity(app) instead of add_typer.
brevity.register_brevity(app)


def main() -> None:
    """Console-script entry point declared in pyproject.toml."""
    app()


if __name__ == "__main__":
    main()
