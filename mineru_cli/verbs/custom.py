"""`mineru custom` sub-app + dynamic dispatch registration (P3-07).

⚠️ SAFETY POSTURE — READ THIS TWICE ⚠️

  `mineru custom add` writes to disk (custom_verbs.yaml at 0600) but does
  NOT execute the shell command being registered. The command runs later,
  when the user (or a scheduled job) invokes `mineru <name>`.

  The dispatcher (`_dispatch_custom_verb`) does `subprocess.run` on the
  entry's `command` list. That runs whatever the user configured, so
  a compromised registry file is a compromised shell — the registry
  file's 0600 permission bit is the load-bearing security control.

  Tests MOCK the dispatcher's subprocess.run to capture argv/env without
  actually executing anything, so no real shell command fires during
  test / dev.

  ⚠️ SECRETS BELONG IN THE KEYCHAIN, NOT `entry.env` ⚠️
  Operators MUST NOT hand-embed API keys, tokens, or passwords in the
  `env:` block of `custom_verbs.yaml`. Use the secrets seam (Keychain)
  from the invoked script instead. `custom show` masks every env VALUE
  by default (only the key names render) and `custom show --json`
  masks env values unless the operator opts in with `--reveal-env`,
  precisely so a shoulder-surf or a shared terminal recording can't
  leak the plaintext. Additionally, an entry may set `env_inherit:
  false` to run its subprocess with a MINIMAL base env (PATH, HOME,
  USER, LANG) instead of a full `os.environ.copy()` — recommended for
  third-party scripts that shouldn't see the parent shell's tokens.

Phase 3 status (P3-07):

  - `custom add`:     interactive prompts (name, description, command,
                      cwd, schedule, deploy_notes, env). Validates the
                      name against the built-in verb set BEFORE
                      prompting for the rest of the fields (so a
                      collision on `gmail` fails immediately). Writes
                      the registry atomically at 0600. Prints a
                      summary at the end explaining how to invoke the
                      new verb + how to schedule it (points at Phase 4's
                      cron install).
  - `custom list`:    table of registered verbs (name, description,
                      schedule if any). `--json` for scripted use.
  - `custom show <name>`: pretty-prints the full entry (command, cwd,
                      env, deploy_notes) plus a synthesized invocation
                      preview.
  - `custom remove <name>`: confirmation prompt (`--yes` to skip),
                      atomic rewrite of the registry without the entry.

Dynamic dispatch:

  `register_custom_dispatch(app)` is called from `mineru_cli.app.root`
  after the profile is hydrated. It reads the registry and, for each
  entry, calls `app.command(entry.name)(...)` to add a leaf command
  whose implementation shells out to `entry.command`. Collisions with
  built-in verb names are SKIPPED WITH A STDERR WARNING at load time
  (not raised): an operator who added `mygmail` before a hypothetical
  future built-in `mygmail` shouldn't have their CLI break silently on
  a version bump; they should see the warning and rename.

  Registration is skipped when `ctx.invoked_subcommand == 'custom'` so
  the management subcommands can add/remove entries without a stale
  in-process dispatch table interfering. It is also skipped when
  `ctx.invoked_subcommand is None` (the discovery path in `app.root`,
  which returns before this point anyway) — belt-and-braces.

Wire-up rules (mirror memory.py / telegram.py):

  - The dispatcher is the ONLY subprocess call site in this file.
  - Registry writes always go through `CustomVerbRegistry.save_to`.
  - Neither the verb nor the dispatcher imports `mineru_cli.app` at
    module import time — the collision-check helper does a lazy import
    to avoid the cycle. See `mineru_cli.custom.registry.builtin_verb_names`.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from typer._click.core import Command as _ClickCommand
from typer._click.core import Context as _ClickContext
from typer._click.globals import get_current_context as _click_get_current_context
from typer.core import TyperCommand, TyperGroup

from mineru_cli.custom import (
    CustomVerbEntry,
    CustomVerbError,
    CustomVerbRegistry,
    builtin_verb_names,
    registry_path_for_profile,
)
from mineru_cli.profile import ProfileError, load_active_profile

# Naming note: we route through Typer's VENDORED click (`typer._click.*`)
# rather than an external `click` package. Two reasons:
#   1. The `mineru-cli` install has no external `click` in its declared deps
#      (typer 0.27+ ships its click as a private vendor); adding one risks
#      loading TWO Command class hierarchies at import time (`click.Command`
#      vs `typer._click.core.Command`) that are NOT interchangeable — a
#      `click.Command` returned from `get_command` would look wrong to a
#      TyperGroup that only speaks the vendored dialect and the dispatcher
#      would silently 404.
#   2. TyperGroup itself subclasses `typer._click.core.Command`, so a
#      dynamically-built subcommand must be that same class to slot in.
# The rest of this module refers to it as `_ClickCommand` / `_ClickContext`
# to make the vendoring intentional and to keep the dispatcher legible.


# ---------------------------------------------------------------------------
# Top-level `mineru custom` app.
# ---------------------------------------------------------------------------
#
# Historical note (2026-08-28 rev): an earlier design stashed the
# pending sub-command argv on `ctx.meta` from a `CustomVerbTyperGroup.invoke`
# override so the root callback could scan it for `--help` before deciding
# whether to load the active profile. That heuristic could not distinguish
# a help FLAG from an option VALUE — an option whose value happened to be
# the string `--help` silently skipped profile validation. The redesign
# moves profile hydration off the callback entirely (see
# `mineru_cli.profile.get_profile`), so no argv snapshot is needed and
# the `invoke` override is gone.


custom_app = typer.Typer(
    name="custom",
    help=(
        "User-defined custom verbs. Register your own `mineru <verb>` "
        "that shells out to any command, with optional schedule metadata. "
        "Stored in <profile_root>/custom_verbs.yaml at 0600."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile_for_ctx(ctx: typer.Context):
    """Return the active profile, hydrating on first call.

    The custom-verb file lives at `<profile_root>/custom_verbs.yaml`, so
    writing against the wrong profile would silently orphan the entry.
    `get_profile(ctx)` fails loud with a clean CLI frame on any loader
    error (no active profile, `--profile bogus`, malformed yaml).
    """
    from mineru_cli.profile import get_profile

    return get_profile(ctx)


def _load_registry(ctx: typer.Context) -> CustomVerbRegistry:
    """Load the registry for the active profile, wrapping loader errors."""
    profile = _profile_for_ctx(ctx)
    try:
        return CustomVerbRegistry.load(profile)
    except CustomVerbError as exc:
        typer.echo(f"mineru custom: {exc}", err=True)
        raise typer.Exit(code=2)


def _default_cwd(profile) -> str:
    """The default cwd for a subprocess dispatch: the profile's workspace."""
    return str(getattr(profile, "workspace_absolute", Path.cwd()))


ENV_REDACTED_VALUE = "***"


def _render_entry_summary(entry: CustomVerbEntry, *, invocation: str) -> str:
    """Return a multi-line pretty rendering of one entry.

    Env VALUES are always masked to `***` in the pretty renderer —
    only the KEY names surface, so an operator can confirm the shape
    of the injected env without exposing any secret they hand-embedded
    in `custom_verbs.yaml`. Secrets should live in the Keychain via
    the secrets seam, not here (see the module docstring); the
    redaction is defense-in-depth for the case where they don't.
    """
    lines: List[str] = []
    lines.append(f"name:         {entry.name}")
    lines.append(f"description:  {entry.description}")
    lines.append(f"command:      {shlex.join(entry.command)}")
    if entry.cwd:
        lines.append(f"cwd:          {entry.cwd}")
    if entry.schedule:
        lines.append(f"schedule:     {entry.schedule}")
    if not entry.env_inherit:
        lines.append("env_inherit:  false  (minimal base env: PATH, HOME, USER, LANG)")
    if entry.env:
        for key in sorted(entry.env):
            lines.append(f"env[{key}]     {ENV_REDACTED_VALUE}")
    if entry.deploy_notes:
        lines.append("deploy_notes:")
        for note_line in entry.deploy_notes.splitlines():
            lines.append(f"  {note_line}")
    lines.append(f"invocation:   {invocation}")
    return "\n".join(lines)


def _synthesize_invocation(entry: CustomVerbEntry) -> str:
    """Show the operator what `mineru <name> ...` will translate to.

    Presents `{args}` as `[...args]` for clarity if the entry uses the
    pass-through placeholder; a straight-through command shows as
    `<cmd> [...args]` since Typer will forward extras regardless.
    """
    rendered = shlex.join(entry.command)
    if "{args}" not in rendered:
        rendered = f"{rendered} [...args]"
    else:
        rendered = rendered.replace("{args}", "[...args]")
    return f"mineru {entry.name} [...args]  ->  {rendered}"


# Day-of-week map for `humanize_schedule`. Cron accepts both 0 and 7 for
# Sunday; we normalize both to "Sunday" so `MM HH * * 0` and `MM HH * * 7`
# render identically. Numeric-only DoW is deliberate: word-form DoW
# (`MON`, `TUE`, ...) is not universally supported across cron flavors,
# so we keep the humanizer strict on the numeric form and fall back to
# the raw string when the input strays.
_DAY_OF_WEEK_NAMES: Dict[int, str] = {
    0: "Sunday",
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
    7: "Sunday",
}


def _ordinal_suffix(n: int) -> str:
    """Return the English ordinal suffix ('st', 'nd', 'rd', 'th') for `n`."""
    if 10 <= (n % 100) <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def humanize_schedule(schedule: str) -> str:
    """Best-effort English phrase for a 5-field cron string.

    Recognizes three patterns and falls back to the raw string on
    anything else. This text is INFORMATIONAL only (shown in the
    `custom add` summary block so the operator can eyeball their input
    against a human phrase); the load-bearing consumer is the operator's
    own eyeballs plus, later, Phase 4's `mineru cron install`.

    Recognized patterns:
      * `MM HH * * *`        -> "daily at HH:MM"
      * `MM HH * * <dow>`    -> "weekly on <Day> at HH:MM"
      * `MM HH <dom> * *`    -> "monthly on the <N>{st,nd,rd,th} at HH:MM"

    Everything else (ranges, lists, step values, non-numeric fields)
    returns the input verbatim so the operator sees exactly what they
    typed without a misleading "translation" hiding a syntax error.
    """
    if not isinstance(schedule, str) or not schedule.strip():
        return schedule
    parts = schedule.split()
    if len(parts) != 5:
        return schedule
    minute_field, hour_field, dom_field, month_field, dow_field = parts
    try:
        minute = int(minute_field)
        hour = int(hour_field)
    except ValueError:
        return schedule
    if not (0 <= minute < 60 and 0 <= hour < 24):
        return schedule
    time_str = f"{hour:02d}:{minute:02d}"
    if dom_field == "*" and month_field == "*" and dow_field == "*":
        return f"daily at {time_str}"
    if dom_field == "*" and month_field == "*":
        try:
            dow = int(dow_field)
        except ValueError:
            return schedule
        name = _DAY_OF_WEEK_NAMES.get(dow)
        if name is None:
            return schedule
        return f"weekly on {name} at {time_str}"
    if month_field == "*" and dow_field == "*":
        try:
            dom = int(dom_field)
        except ValueError:
            return schedule
        if not (1 <= dom <= 31):
            return schedule
        return f"monthly on the {dom}{_ordinal_suffix(dom)} at {time_str}"
    return schedule


# Stable prefixes for the `custom add` summary block. Bumped to
# module-level so tests can pin against copy tweaks WITHOUT re-writing the
# full sentence — a tweak that keeps the prefix but changes the trailing
# text stays green, while a rename ("invoke" -> "run") fails loudly.
SUMMARY_INVOKE_PREFIX = "  * invoke:   "
SUMMARY_SCHEDULE_PREFIX = "  * schedule: "
SUMMARY_FILE_PREFIX = "  * file:     "
SUMMARY_SCHEDULE_HEADER = "  * to schedule automatically:"
SUMMARY_SCHEDULE_OPTION_A_PREFIX = "      - option A "
SUMMARY_SCHEDULE_OPTION_B_PREFIX = "      - option B "


def render_add_summary(
    entry: CustomVerbEntry,
    *,
    target_path: str,
    profile_name: str,
    launchd_label_prefix: Optional[str] = None,
) -> List[str]:
    """Return the `custom add` summary block as a list of lines.

    Broken out from the verb body so the exact text is unit-testable
    against a fixed CustomVerbEntry input WITHOUT going through
    CliRunner — copy tweaks that shift a leading indent or a stopword
    fail here immediately.

    `launchd_label_prefix` is the resolved profile-level plist label
    prefix (e.g. `com.mineru`) so the manual-plist path in option B
    lands on a valid, fully-qualified filename instead of a placeholder
    the operator has to hand-substitute. When None (e.g. a callsite
    that legitimately doesn't have a profile), we fall back to a
    generic `mineru` prefix so the printed path is still a legal
    filename rather than a `{placeholder}` literal.

    Content contract (locked by the P3-08 task):
      (1) The invocation the operator can run RIGHT NOW: `mineru <name>`
          (no `[...args]` decoration here — that's what the entry-summary
          block above already shows).
      (2) The recorded schedule string PLUS a human phrase in parens
          (e.g. `45 0 * * *  (daily at 00:45)`). Missing schedule shows
          a "no schedule set" line so the operator sees the branch.
      (3) The two follow-up options for actually MAKING the schedule
          fire: option A points at the per-profile cron.yaml that
          Phase 4's `mineru cron install` will materialize; option B
          references the entry's own `deploy_notes` free-text (typically
          "drop a plist at ~/Library/LaunchAgents/<label>.plist").
      (4) The absolute path to the registry file we just wrote — so an
          operator inspecting the summary can `cat` it and confirm the
          write landed where they expected.
    """
    resolved_label_prefix = launchd_label_prefix or "mineru"
    lines: List[str] = ["next steps:"]
    lines.append(f"{SUMMARY_INVOKE_PREFIX}mineru {entry.name}")
    if entry.schedule:
        phrase = humanize_schedule(entry.schedule)
        if phrase == entry.schedule:
            # Humanizer punted (unrecognized pattern) -- don't tack on a
            # redundant parenthetical, but still surface the raw string.
            lines.append(f"{SUMMARY_SCHEDULE_PREFIX}{entry.schedule}")
        else:
            lines.append(
                f"{SUMMARY_SCHEDULE_PREFIX}{entry.schedule}  ({phrase})"
            )
        lines.append(SUMMARY_SCHEDULE_HEADER)
        lines.append(
            f"{SUMMARY_SCHEDULE_OPTION_A_PREFIX}(recommended): add this "
            f"schedule to profiles/{profile_name}/cron.yaml, then run "
            "`mineru cron install` when Phase 4 lands."
        )
        if entry.deploy_notes:
            lines.append(
                f"{SUMMARY_SCHEDULE_OPTION_B_PREFIX}(manual): drop a launchd "
                "plist yourself; see the entry's `deploy_notes` for the "
                "target path and label."
            )
        else:
            lines.append(
                f"{SUMMARY_SCHEDULE_OPTION_B_PREFIX}(manual): drop a launchd "
                "plist at ~/Library/LaunchAgents/"
                f"{resolved_label_prefix}.{entry.name}.plist and back-fill "
                "the entry's `deploy_notes` so future you knows where it "
                "lives."
            )
    else:
        lines.append(
            f"{SUMMARY_SCHEDULE_PREFIX}(none) -- re-run `custom add` with "
            "`--schedule '<cron>'` or edit the YAML to add one."
        )
    lines.append(f"{SUMMARY_FILE_PREFIX}{target_path}")
    return lines


# ---------------------------------------------------------------------------
# `custom add` — interactive registration.
# ---------------------------------------------------------------------------


@custom_app.command("add", help="Register a new user-defined custom verb (interactive).")
def add(
    ctx: typer.Context,
    name: Optional[str] = typer.Option(
        None,
        "--name",
        help="Verb name (^[a-z][a-z0-9-]*$). Omit to be prompted.",
    ),
    description: Optional[str] = typer.Option(
        None,
        "--description",
        help="One-line description for --help. Omit to be prompted.",
    ),
    command: Optional[str] = typer.Option(
        None,
        "--command",
        help=(
            "Shell command line, parsed with shlex.split. "
            "Supports {args} for pass-through. Omit to be prompted."
        ),
    ),
    cwd: Optional[str] = typer.Option(
        None,
        "--cwd",
        help="Absolute working directory (default = profile workspace).",
    ),
    schedule: Optional[str] = typer.Option(
        None,
        "--schedule",
        help="Cron-ish schedule string (informational until Phase 4).",
    ),
    deploy_notes: Optional[str] = typer.Option(
        None,
        "--deploy-notes",
        help="Multi-line free text shown by `custom show`.",
    ),
    env_inherit: Optional[bool] = typer.Option(
        None,
        "--env-inherit/--no-env-inherit",
        help=(
            "Whether the shelled-out subprocess inherits the parent env. "
            "Default: True (full os.environ + entry.env), matches historical "
            "behavior. Pass --no-env-inherit for third-party scripts that "
            "shouldn't see MINERU_SECRET_* / OAuth tokens / any other parent "
            "env vars — the subprocess then gets only PATH, HOME, USER, LANG "
            "plus whatever the entry itself declares in `env:`. Omit to be "
            "prompted (interactive) or accept the True default (scripted)."
        ),
    ),
) -> None:
    """Interactive onboarding for a new custom verb.

    Flow:
      1. Prompt for a name; validate the regex + collision against the
         built-in verb set BEFORE anything else (fail early if the name
         is bad — don't waste the operator's typing).
      2. Prompt for the description, command line, optional cwd,
         schedule, deploy_notes.
      3. Build the CustomVerbEntry, add to registry, save atomically.
      4. Print the final summary + next-steps for scheduling.

    Any prompt can be skipped via the corresponding flag (useful for
    scripted `mineru custom add --name foo --command 'bar'`). We do NOT
    ship a full non-interactive mode with implicit defaults — a missing
    required field always drops us into a prompt so the operator can
    confirm.
    """
    profile = _profile_for_ctx(ctx)
    registry = _load_registry(ctx)
    builtin = builtin_verb_names()

    # Mode selection: if all three REQUIRED fields (name, description,
    # command) were provided via flags, treat this as fully non-interactive
    # -- optional fields stay at whatever the flags provided (default
    # None) without dropping into a prompt. If any required field is
    # missing, we're in interactive mode and prompt for every unset field,
    # including the optional ones. This split keeps `mineru custom add
    # --name x --description y --command z` scriptable while preserving
    # the guided onboarding flow when a user runs bare `mineru custom add`.
    interactive = not (name and description and command)

    resolved_name = (
        name if name else typer.prompt("verb name (a-z, 0-9, -)")
    ).strip()
    try:
        # Same validation function `add_entry` will run later -- surface
        # the error NOW instead of after description prompting so the
        # operator doesn't waste typing on a bad name.
        from mineru_cli.custom.registry import _validate_name_shape, validate_no_collision

        _validate_name_shape(resolved_name)
        validate_no_collision(resolved_name, builtin)
        if registry.get_entry(resolved_name) is not None:
            raise CustomVerbError(
                f"custom verb {resolved_name!r} already registered. Run "
                f"`mineru custom remove {resolved_name}` first to replace it."
            )
    except CustomVerbError as exc:
        typer.echo(f"mineru custom add: {exc}", err=True)
        raise typer.Exit(code=2)

    resolved_description = (
        description if description else typer.prompt("one-line description")
    ).strip()
    if not resolved_description:
        typer.echo(
            "mineru custom add: description must be non-empty.", err=True
        )
        raise typer.Exit(code=2)

    resolved_command_str = (
        command
        if command
        else typer.prompt(
            "shell command (use {args} for pass-through)",
        )
    ).strip()
    if not resolved_command_str:
        typer.echo(
            "mineru custom add: command must be non-empty.", err=True
        )
        raise typer.Exit(code=2)
    try:
        parsed_command = shlex.split(resolved_command_str)
    except ValueError as exc:
        typer.echo(
            f"mineru custom add: could not parse command with shlex: {exc}",
            err=True,
        )
        raise typer.Exit(code=2)
    if not parsed_command:
        typer.echo(
            "mineru custom add: command parsed to an empty argv list.",
            err=True,
        )
        raise typer.Exit(code=2)

    default_cwd = _default_cwd(profile)
    if cwd is not None:
        resolved_cwd_raw = cwd.strip()
    elif interactive:
        resolved_cwd_raw = typer.prompt(
            f"cwd (absolute path; blank to use profile workspace: {default_cwd})",
            default="",
            show_default=False,
        ).strip()
    else:
        resolved_cwd_raw = ""
    resolved_cwd_final: Optional[str] = resolved_cwd_raw or None

    if schedule is not None:
        resolved_schedule_raw = schedule.strip()
    elif interactive:
        resolved_schedule_raw = typer.prompt(
            "schedule (cron string, blank for none)",
            default="",
            show_default=False,
        ).strip()
    else:
        resolved_schedule_raw = ""
    resolved_schedule_final: Optional[str] = resolved_schedule_raw or None

    if deploy_notes is not None:
        resolved_deploy_notes_raw = deploy_notes.strip()
    elif interactive:
        resolved_deploy_notes_raw = typer.prompt(
            "deploy notes (blank for none)",
            default="",
            show_default=False,
        ).strip()
    else:
        resolved_deploy_notes_raw = ""
    resolved_deploy_notes_final: Optional[str] = (
        resolved_deploy_notes_raw or None
    )

    # env_inherit resolution: explicit CLI flag wins; otherwise prompt in
    # interactive mode (default True); otherwise fall back to True to
    # preserve historical behavior for scripted callers that pre-date
    # the flag.
    if env_inherit is not None:
        resolved_env_inherit = env_inherit
    elif interactive:
        resolved_env_inherit = typer.confirm(
            "inherit parent env vars? "
            "(No = isolate to PATH/HOME/USER/LANG + entry env)",
            default=True,
        )
    else:
        resolved_env_inherit = True

    entry = CustomVerbEntry(
        name=resolved_name,
        description=resolved_description,
        command=tuple(parsed_command),
        cwd=resolved_cwd_final,
        schedule=resolved_schedule_final,
        deploy_notes=resolved_deploy_notes_final,
        env={},
        env_inherit=resolved_env_inherit,
    )

    try:
        new_registry = registry.add_entry(entry, builtin_names=builtin)
        target = registry_path_for_profile(profile)
        new_registry.save_to(target)
    except CustomVerbError as exc:
        typer.echo(f"mineru custom add: {exc}", err=True)
        raise typer.Exit(code=2)

    invocation = _synthesize_invocation(entry)
    typer.echo("registered custom verb.")
    typer.echo(_render_entry_summary(entry, invocation=invocation))
    typer.echo("")
    for line in render_add_summary(
        entry,
        target_path=str(target),
        profile_name=getattr(profile, "name", "mineru"),
        launchd_label_prefix=getattr(profile, "launchd_label_prefix", None),
    ):
        typer.echo(line)


# ---------------------------------------------------------------------------
# `custom list` — table / JSON view.
# ---------------------------------------------------------------------------


@custom_app.command("list", help="List all registered custom verbs.")
def list_verbs(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the raw list of entries as JSON.",
    ),
) -> None:
    """List registered verbs — table by default, JSON on --json.

    Empty registry prints a friendly note explaining how to get started
    (`mineru custom add`) so a fresh install is a smooth first-run
    experience instead of a blank stare.
    """
    registry = _load_registry(ctx)
    entries = registry.list_entries()

    if json_out:
        # Env values are ALWAYS redacted in the list view. The list
        # command has no `--reveal-env` opt-in (unlike `show`); an
        # operator who legitimately needs the raw value can call
        # `mineru custom show <name> --json --reveal-env` for that
        # single entry, which prints a stderr warning per call.
        payload = [
            {
                "name": e.name,
                "description": e.description,
                "command": list(e.command),
                "cwd": e.cwd,
                "schedule": e.schedule,
                "deploy_notes": e.deploy_notes,
                "env": {key: ENV_REDACTED_VALUE for key in e.env},
                "env_inherit": e.env_inherit,
            }
            for e in entries
        ]
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if not entries:
        typer.echo(
            "(no custom verbs registered — run `mineru custom add`)"
        )
        return
    name_w = max(len(e.name) for e in entries)
    sched_w = max((len(e.schedule or "") for e in entries), default=0)
    for e in entries:
        sched = e.schedule or ""
        typer.echo(
            f"  {e.name.ljust(name_w)}  {sched.ljust(sched_w)}  {e.description}"
        )


# ---------------------------------------------------------------------------
# `custom show` — pretty-print one entry.
# ---------------------------------------------------------------------------


@custom_app.command("show", help="Show the full details of one custom verb.")
def show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Verb name to show."),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the entry as JSON instead of the pretty rendering.",
    ),
    reveal_env: bool = typer.Option(
        False,
        "--reveal-env",
        help=(
            "Only with --json: emit the RAW env values instead of the "
            "'***' redaction. Prints a stderr warning that plaintext "
            "secrets are being echoed. Secrets should live in the "
            "Keychain via the secrets seam; if this flag is needed, "
            "the entry is probably misconfigured."
        ),
    ),
) -> None:
    """Print the full entry for `name`, exit 2 on miss.

    Env values are redacted by default in both output modes. The
    pretty renderer masks unconditionally (there is no way to unmask
    from a shoulder-surfable terminal render). The `--json` mode
    accepts `--reveal-env` for the rare scripted-recovery path (e.g.
    an operator dumping the registry into a migration tool) — that
    unmask always prints a stderr warning so the risky mode leaves a
    trace in logs.
    """
    if reveal_env and not json_out:
        typer.echo(
            "mineru custom show: --reveal-env only applies with --json "
            "(pretty rendering always masks env values).",
            err=True,
        )
        raise typer.Exit(code=2)
    registry = _load_registry(ctx)
    entry = registry.get_entry(name)
    if entry is None:
        typer.echo(
            f"mineru custom show: no verb named {name!r}. "
            "Run `mineru custom list` to see registered verbs.",
            err=True,
        )
        raise typer.Exit(code=2)
    if json_out:
        if reveal_env and entry.env:
            typer.echo(
                "mineru custom show: WARNING: --reveal-env is emitting "
                f"{len(entry.env)} plaintext env value(s) for verb "
                f"{name!r}. Prefer the Keychain (secrets seam) over "
                "hand-embedded env values.",
                err=True,
            )
            env_out: Dict[str, str] = dict(entry.env)
        else:
            env_out = {key: ENV_REDACTED_VALUE for key in entry.env}
        typer.echo(
            json.dumps(
                {
                    "name": entry.name,
                    "description": entry.description,
                    "command": list(entry.command),
                    "cwd": entry.cwd,
                    "schedule": entry.schedule,
                    "deploy_notes": entry.deploy_notes,
                    "env": env_out,
                    "env_inherit": entry.env_inherit,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    typer.echo(_render_entry_summary(entry, invocation=_synthesize_invocation(entry)))


# ---------------------------------------------------------------------------
# `custom remove` — confirmation + atomic rewrite.
# ---------------------------------------------------------------------------


@custom_app.command("remove", help="Remove a registered custom verb.")
def remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Verb name to remove."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Remove `name` from the registry after a confirmation prompt.

    A `--yes` flag skips the confirm — useful for scripted teardown but
    NOT the default, per SECURITY.md's destructive-actions posture (list
    what you'll do, then confirm before doing it).
    """
    profile = _profile_for_ctx(ctx)
    registry = _load_registry(ctx)
    entry = registry.get_entry(name)
    if entry is None:
        typer.echo(
            f"mineru custom remove: no verb named {name!r}.",
            err=True,
        )
        raise typer.Exit(code=2)
    if not yes:
        confirmed = typer.confirm(
            f"remove custom verb {name!r} (command: {shlex.join(entry.command)!r})?",
            default=False,
        )
        if not confirmed:
            typer.echo("aborted — no changes written.")
            raise typer.Exit(code=0)
    try:
        new_registry = registry.remove_entry(name)
        new_registry.save_to(registry_path_for_profile(profile))
    except CustomVerbError as exc:
        typer.echo(f"mineru custom remove: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"removed custom verb {name!r}.")


# ---------------------------------------------------------------------------
# Dynamic dispatch — TyperGroup subclass consults the registry at parse time.
# ---------------------------------------------------------------------------
#
# Design note: we tried to register custom verbs from `mineru_cli.app.root`
# (i.e. from the root callback), but Typer resolves the target command
# BEFORE the root callback runs — so a fresh `mineru <custom-name>` invocation
# hits "No such command" before dispatch. The correct hook point is the group
# class itself: Click / Typer call `get_command(name)` and `list_commands(ctx)`
# on the group when parsing / rendering --help. Overriding those lets us
# consult the on-disk registry lazily and synthesize a Click command per
# entry on demand.
#
# Registry loads are best-effort here: a malformed file emits a stderr
# warning ONCE and disables custom verbs for that invocation, but does NOT
# break the built-in verb tree. Collisions with built-in verb names are also
# warn-and-skip (the built-in wins). This is the runtime-surprise safety
# posture: an upgrade that adds a new built-in name matching a custom verb
# must not break the CLI; the operator sees the warning and renames.


# Process-scoped memoization for the resolved registry. TyperGroup consults
# `_load_registry_best_effort` on EVERY `get_command` / `list_commands`, and
# every `mineru <anything>` invocation triggers at least a `get_command`. A
# naive load re-parses two YAML files (profile.yaml + custom_verbs.yaml) on
# every call. The cache keys on (registry-file path, mtime_ns) so a fresh
# `custom add` in the same process (writes the file, changes mtime)
# invalidates the entry and the next call re-reads. When the file does not
# yet exist, we cache the "empty registry" answer under a sentinel mtime so
# repeated `--help` renders on a fresh checkout still hit the cache. The
# tuple value is (registry, warned_names) — `warned_names` remembers which
# shadow-collision warnings we already emitted for this snapshot so we
# don't re-warn on cache hits.
_REGISTRY_CACHE: Dict[Path, Tuple[int, "CustomVerbRegistry | None", frozenset]] = {}
# Sentinel mtime for a not-yet-existing registry file. `-1` never appears as
# a real `stat().st_mtime_ns`, so a subsequent `Path.exists() == True` (real
# mtime > 0) forces a cache miss and a fresh load.
_MISSING_FILE_MTIME_SENTINEL = -1

# Process-scoped memo for `load_active_profile(None)` inside the dispatch
# path. Before this cache the TyperGroup re-parsed profile.yaml on every
# `get_command` / `list_commands`, which happens on essentially every CLI
# invocation (a `mineru gmail --help` triggered TWO profile-YAML reads:
# one from the group, one from the root callback). The cache key is a
# fingerprint of every input `load_active_profile(None)` actually
# consults: the resolved profile name (MINERU_PROFILE env + default), the
# resolved base dir (MINERU_PROFILE_ROOT env + default), and the target
# profile.yaml's mtime. A change to any of them invalidates the cache;
# a `custom add` in the same process does not touch profile.yaml so the
# memo survives.
_PROFILE_CACHE: Dict[Tuple[str, str], Tuple[int, "Profile"]] = {}


def _profile_cache_key() -> Tuple[Tuple[str, str], Path]:
    """Return ((profile_name, base_dir_str), profile_yaml_path).

    Kept alongside `_load_active_profile_cached` so the invalidation
    contract has one home.
    """
    from mineru_cli.profile.loader import (
        default_profiles_base_dir,
        resolve_profile_name,
    )

    profile_name = resolve_profile_name(None)
    base_dir = default_profiles_base_dir()
    profile_yaml = base_dir / profile_name / "profile.yaml"
    return (profile_name, str(base_dir)), profile_yaml


def _load_active_profile_cached() -> "Profile":
    """Cached `load_active_profile(None)`; raises `ProfileError` on miss.

    Called from `_load_registry_best_effort` (which the TyperGroup calls
    on every `get_command` / `list_commands`) so the cost matters. The
    cache is invalidated when either the resolved profile name, resolved
    base dir, or the profile.yaml's mtime changes. A missing profile.yaml
    is treated as an uncached miss so the loader's own fail-loud path
    fires exactly as it would without the cache.
    """
    key, profile_yaml = _profile_cache_key()
    try:
        current_mtime = profile_yaml.stat().st_mtime_ns
    except OSError:
        # File missing / unreadable — do not cache; let the loader raise.
        return load_active_profile(None)

    cached = _PROFILE_CACHE.get(key)
    if cached is not None and cached[0] == current_mtime:
        return cached[1]

    profile = load_active_profile(None)
    _PROFILE_CACHE[key] = (current_mtime, profile)
    return profile


def reset_registry_cache() -> None:
    """Clear the process-scoped registry + profile memo. Test-only helper."""
    _REGISTRY_CACHE.clear()
    _PROFILE_CACHE.clear()


def _load_registry_best_effort() -> "CustomVerbRegistry | None":
    """Resolve the active profile + load the registry, or None on any failure.

    Best-effort: never raises. Called from the TyperGroup on every
    `get_command` / `list_commands`, so the cost matters. Warnings are
    written to stderr ONCE per failure mode via the `_warn_once` gate.

    Also runs the built-in collision check on every successful load so
    that a `mineru <built-in>` invocation — which doesn't iterate custom
    entries in `list_commands` — still surfaces the "you have a custom
    verb that shadows a built-in, and the built-in wins" warning.
    Without this hook the operator would only see the warning while
    rendering `--help`, which they might never do.

    Perf: BOTH the profile parse (via `_load_active_profile_cached`) and
    the registry parse (via `_REGISTRY_CACHE`) are memoized. A `custom
    add` in the same process changes the registry mtime and invalidates
    only the registry entry; the profile memo survives because
    profile.yaml wasn't touched.
    """
    try:
        # Same env-var resolution the root callback uses; no ctx here
        # (get_command runs before Click builds one for the subcommand).
        # Memoized: repeat calls in the same process do NOT re-parse
        # profile.yaml unless its mtime changes.
        profile = _load_active_profile_cached()
    except ProfileError:
        # No profile.yaml -> no custom verbs. Silent: on a fresh checkout
        # the CLI must still discover its own built-in tree.
        return None

    try:
        registry_path = registry_path_for_profile(profile)
    except CustomVerbError as exc:
        _warn_once(
            f"mineru: warning: custom verb registry unusable ({exc}). "
            "Custom verbs disabled until fixed."
        )
        return None

    try:
        current_mtime = registry_path.stat().st_mtime_ns
    except FileNotFoundError:
        current_mtime = _MISSING_FILE_MTIME_SENTINEL
    except OSError:
        # stat() failed for some other reason (permission etc.) — force
        # a fresh load and let the loader surface any error.
        current_mtime = _MISSING_FILE_MTIME_SENTINEL

    cached = _REGISTRY_CACHE.get(registry_path)
    if cached is not None and cached[0] == current_mtime:
        cached_registry = cached[1]
        # Re-emit the shadow warnings on cache hits too, so the operator
        # still sees them on every `mineru <built-in>` invocation. The
        # `_warn_once` gate keeps them to one per process regardless.
        if cached_registry is not None:
            _reemit_shadow_warnings(cached[2])
        return cached_registry

    try:
        registry = CustomVerbRegistry.load(profile)
    except CustomVerbError as exc:
        _warn_once(
            f"mineru: warning: custom verb registry unusable ({exc}). "
            "Custom verbs disabled until fixed."
        )
        # Cache the None result too so a busy `--help` doesn't retry the
        # loader (and re-emit the warning) on every subsequent call.
        _REGISTRY_CACHE[registry_path] = (current_mtime, None, frozenset())
        return None

    # Collision probe on every load. `builtin_verb_names()` reaches into
    # the root `app` lazily; if a downstream engine has added a NEW
    # built-in named after a previously-registered custom verb, this is
    # where the operator learns about it. `_warn_once` dedupes so a busy
    # command that hits `get_command` several times only warns once.
    try:
        builtin = builtin_verb_names()
    except Exception:  # noqa: BLE001 — a broken root app can't kill custom-verb loading
        builtin = frozenset()
    shadow_names: set[str] = set()
    for entry in registry.list_entries():
        if entry.name in builtin:
            shadow_names.add(entry.name)
    _reemit_shadow_warnings(frozenset(shadow_names))
    _REGISTRY_CACHE[registry_path] = (current_mtime, registry, frozenset(shadow_names))
    return registry


def _reemit_shadow_warnings(shadow_names: frozenset) -> None:
    """Re-issue the shadow-collision warnings for the given names.

    `_warn_once` gates repeat prints, so calling this on every cache hit
    is cheap AND still lands the warning on the first `mineru <built-in>`
    invocation of the process.
    """
    for name in shadow_names:
        _warn_once(
            f"mineru: warning: custom verb {name!r} shadows a "
            "built-in and was NOT registered. Rename it via "
            f"`mineru custom remove {name}` and re-add with a "
            "different name."
        )


_WARNED_MESSAGES: set[str] = set()


def _warn_once(message: str) -> None:
    """Print `message` to stderr the first time it's seen this process.

    The TyperGroup consults the registry on both `list_commands` (help)
    and `get_command` (dispatch), so a repeat warning within a single
    invocation would double-print. The set is process-scoped; tests
    exercising warn behavior across multiple invocations reset it via
    the `reset_warnings` fixture.
    """
    if message in _WARNED_MESSAGES:
        return
    _WARNED_MESSAGES.add(message)
    print(message, file=sys.stderr)


def reset_warnings() -> None:
    """Clear the once-per-process warning gate + registry + profile memos.

    Test-only helper. Drops `_REGISTRY_CACHE` AND `_PROFILE_CACHE` alongside
    the warning gate because all three pieces of state are always reset
    together: a cached "None" registry (or a stale profile) from a
    previous test could hide a fresh file the current test just wrote,
    which is exactly the kind of pytest state-leak we are trying to
    prevent.
    """
    _WARNED_MESSAGES.clear()
    _REGISTRY_CACHE.clear()
    _PROFILE_CACHE.clear()


# Env vars carried over from the parent process when an entry has
# `env_inherit: false`. Kept intentionally small: anything more would
# defeat the purpose of the opt-out (isolating a third-party script
# from the parent shell's tokens). PATH so the subprocess can find its
# own tool chain; HOME so tools that stash caches under $HOME still
# work; USER for tools that need the invoking user's name; LANG for
# locale-sensitive output. Notably ABSENT: any MINERU_SECRET_*, any
# OAuth / TELEGRAM_* / API_KEY / TOKEN env vars a parent job might
# have set. If a specific extra is required, the operator sets it
# explicitly via `entry.env`.
MINIMAL_INHERITED_ENV_KEYS: Tuple[str, ...] = ("PATH", "HOME", "USER", "LANG")


def _build_subprocess_env(entry: CustomVerbEntry) -> Dict[str, str]:
    """Return the env dict handed to `subprocess.run` for `entry`.

    `entry.env_inherit=True` (default, back-compat): full os.environ
    copy + entry.env overlay.
    `entry.env_inherit=False` (opt-in isolation): a minimal base copied
    from os.environ (PATH, HOME, USER, LANG) + entry.env overlay. The
    minimal base intentionally excludes MINERU_SECRET_*, OAuth tokens,
    and any other environment secret the parent might carry — the
    entry must declare what it needs.
    """
    if entry.env_inherit:
        merged_env = os.environ.copy()
    else:
        merged_env = {
            key: os.environ[key]
            for key in MINIMAL_INHERITED_ENV_KEYS
            if key in os.environ
        }
    merged_env.update(entry.env)
    return merged_env


def _synthesize_click_command(entry: CustomVerbEntry, default_cwd: str) -> _ClickCommand:
    """Build a `typer._click.core.Command` for one custom verb entry.

    Must be typer's vendored Command class — see the module-import note.
    A plain external `click.Command` would slot into `get_command`
    silently and then fail dispatch because TyperGroup only recognizes
    its own hierarchy. The command:
      - Accepts arbitrary trailing args (`allow_extra_args=True`,
        `ignore_unknown_options=True`) so any downstream flag / arg
        passes through cleanly.
      - Substitutes `{args}` in the entry's argv with the extras if the
        placeholder is present; otherwise appends them.
      - Seeds the subprocess env per `_build_subprocess_env` (honors
        `entry.env_inherit`) and overlays `entry.env`.
      - Exits with the subprocess's returncode.

    Note: we do NOT use a `pass_context`-style decorator (typer's
    vendored click does not export one). Instead the callback grabs the
    current click context via `get_current_context()` — which is the
    documented modern-click alternative and works identically here.
    """
    resolved_cwd = entry.cwd or default_cwd

    def _callback(**_kwargs: Any) -> None:
        ctx = _click_get_current_context()
        extras: List[str] = list(ctx.args)
        argv: List[str] = []
        substituted = False
        for token in entry.command:
            if token == "{args}":
                argv.extend(extras)
                substituted = True
            else:
                argv.append(token)
        if not substituted:
            argv.extend(extras)

        merged_env = _build_subprocess_env(entry)
        completed = subprocess.run(
            argv, cwd=resolved_cwd, env=merged_env, check=False
        )
        ctx.exit(completed.returncode)

    return TyperCommand(
        name=entry.name,
        callback=_callback,
        help=entry.description,
        context_settings={
            "allow_extra_args": True,
            "ignore_unknown_options": True,
            "help_option_names": ["-h", "--help"],
        },
    )


class CustomVerbTyperGroup(TyperGroup):
    """TyperGroup that exposes on-disk custom verbs alongside the built-ins.

    Overrides two Click hooks:
      * `list_commands(ctx)` — the union of built-in commands + custom-verb
        names not shadowed by a built-in. Powers `--help`.
      * `get_command(ctx, name)` — returns a built-in command first;
        falls back to a dynamically-built Click Command for the matching
        custom verb; returns None if nothing matches (Click then renders
        the standard "No such command" error).

    Load failures emit ONE stderr warning per process and disable custom
    verbs for that invocation without breaking the built-in tree.
    Collisions with a built-in emit ONE warning per name and let the
    built-in win.

    Historical note (2026-08-28 rev): this group used to also override
    `invoke()` to snapshot the pending sub-command argv onto `ctx.meta`
    so the root callback could detect a downstream `--help` and skip
    profile loading. Profile loading moved off the callback (see
    `mineru_cli.profile.get_profile`), so the snapshot + `invoke`
    override were both retired — `--help` now works on a fresh clone by
    construction because Click renders it before the verb callback runs.
    """

    def _custom_registry(self) -> "CustomVerbRegistry | None":
        return _load_registry_best_effort()

    def list_commands(self, ctx: _ClickContext) -> List[str]:
        base = list(super().list_commands(ctx))
        registry = self._custom_registry()
        if registry is None:
            return sorted(base)
        builtin_set = set(base)
        extras: List[str] = []
        for entry in registry.list_entries():
            if entry.name in builtin_set:
                _warn_once(
                    f"mineru: warning: custom verb {entry.name!r} shadows a "
                    "built-in and was NOT registered. Rename it via "
                    f"`mineru custom remove {entry.name}` and re-add with a "
                    "different name."
                )
                continue
            extras.append(entry.name)
        return sorted(base + extras)

    def get_command(self, ctx: _ClickContext, name: str) -> "_ClickCommand | None":
        # ALWAYS probe the registry first — not to override anything, but
        # to trigger the shadow-collision warning inside
        # `_load_registry_best_effort` so that even a straight
        # `mineru <built-in>` invocation surfaces the "you have a custom
        # verb that shadows a built-in" alert. The built-in still wins
        # (we consult it right after), so probing here is pure
        # observability, not a dispatch override.
        registry = self._custom_registry()
        # Built-in wins on every name, no matter what a custom entry says.
        base = super().get_command(ctx, name)
        if base is not None:
            return base
        if registry is None:
            return None
        entry = registry.get_entry(name)
        if entry is None:
            return None
        # Resolve default cwd from the active profile so the subprocess
        # lands in the operator-expected workspace when the entry didn't
        # override cwd. Any resolution failure falls back to the CWD.
        # `_load_active_profile_cached` is process-memoized against
        # profile.yaml's mtime, so this call is a dict lookup on the
        # happy path — no redundant YAML parse per dispatch.
        try:
            profile = _load_active_profile_cached()
            default_cwd = _default_cwd(profile)
        except ProfileError:
            default_cwd = str(Path.cwd())
        return _synthesize_click_command(entry, default_cwd)


def install_custom_group(app: typer.Typer) -> None:
    """Bind `CustomVerbTyperGroup` onto `app` so custom verbs render + dispatch.

    Called once from `mineru_cli.app` at import time, immediately after
    the `app = typer.Typer(...)` construction. This is a mutation of the
    Typer object's constructor default (Typer stashes the desired group
    class on `_add_completion` -> `TyperInfo` -> `cls`); calling this
    helper AFTER Typer is built keeps app.py readable while the class
    logic stays here.

    Behavior: replaces the app's `_add_completion.info.cls` (Typer's
    private stash for the group class) with our subclass. If the
    private API changes in a future Typer, this raises loudly (better
    than silently regressing to the built-in TyperGroup and having
    every custom verb disappear).
    """
    # Typer stashes the app-level configuration on `info` (a TyperInfo
    # dataclass); mutating `.cls` on it is the supported way to inject
    # a custom TyperGroup subclass.
    typer_info = getattr(app, "info", None)
    if typer_info is None:  # pragma: no cover — sanity guard for future Typer
        raise RuntimeError(
            "install_custom_group: Typer app has no `.info`; internal "
            "Typer layout changed. Update install_custom_group to match."
        )
    typer_info.cls = CustomVerbTyperGroup
