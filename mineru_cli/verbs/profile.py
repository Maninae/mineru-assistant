"""`mineru profile` sub-app (Phase 1 + 1.5 multi-profile framework).

Wired verbs:
  - `show`      — render the active profile's foundation fields.
  - `use <name>` — atomically re-point the `active` active-profile
                   symlink at the target profile. Revives the
                   multi-tenant switching machinery dropped in §0 of
                   the 2026-07-25 capability spec. (Was `current`
                   before 2026-09-16 — a legacy `current` symlink is
                   still READ as a fallback.)
  - `active`    — print the currently-active profile name (symlink
                   read; falls back to a legacy `current` symlink for
                   pre-2026-09-16 workspaces). Was `current` before
                   the rename; the old spelling is a hidden alias.
  - `init`      — Phase 1.5: interactive + non-interactive onboarding
                   for a NEW agent profile. Validates every field
                   BEFORE any filesystem write, reserves the profile
                   dir atomically via `os.makedirs(exist_ok=False)`,
                   rolls back on any mid-scaffold failure. Optional
                   guarded Google-account walkthrough (`gog auth add
                   <email>`); NEVER writes a secret to disk.
  - `install`   — render templates + lay symlinks into --target from
                   the active profile (was `hydrate` before 2026-09-16;
                   the `hydrate` name still works as a hidden alias for
                   ~90 days, with a stderr deprecation notice).
                   Preview by default; pass `--apply` to write.
                   `--no-dry-run` remains as a hidden flag alias.

Wired verbs (continued):
  - `validate`  — schema + required-secrets + connectors placeholder
                  check against the active profile. Exit 2 on any miss.
  - `export`    — bundle the active profile dir into a portable
                  `.tar.gz` archive at `--out <path>`, EXCLUDING any
                  file that could carry a secret plus cache/log noise.
  - `import`    — extract such a bundle into `<profiles_base>/<name>/`
                  as a NEW profile (name uniqueness + schema validation
                  before writing anything).

Hidden stubs (still callable, off the parent --help):
  - `edit`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import typer
import yaml

from mineru_cli._deprecation import emit_rename_notice
from mineru_cli._stub import not_yet_implemented
from mineru_cli.install import (
    HydrationError,
    apply_plan,
    build_plan,
    build_render_context,
)
from mineru_cli.humans import (
    HumansError,
    HumansRegistry,
    default_humans_yaml_path,
    load_humans_registry,
)
from mineru_cli.profile import (
    ProfileError,
    active_symlink_path,
    default_engine_root,
    default_profiles_base_dir,
    default_workspace_root,
    get_profile,
    legacy_current_symlink_path,
)
from mineru_cli.profile.onboarding import (
    DEFAULT_JOURNAL_APPLE_NOTES_FOLDER,
    NewHuman,
    OnboardingError,
    ProfileSpec,
    RESERVED_PROFILE_NAMES,
    ScaffoldResult,
    build_gog_auth_command,
    check_persona_collision,
    create_profile,
    default_persona_from_name,
    format_gog_command_string,
    machine_timezone,
    run_gog_auth_add,
    validate_new_human,
    validate_persona,
    validate_profile_name,
    validate_timezone,
)
from mineru_cli.profile.switching import switch_active_profile

profile_app = typer.Typer(
    name="profile",
    help=(
        "Profile management. Multiple profiles co-exist under "
        "<workspace_root>/profiles/; `profile use <name>` atomically "
        "re-points the `active` active-profile symlink (a pre-2026-09-16 "
        "`current` symlink is still READ as a fallback)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@profile_app.command("show")
def show(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False, "--json", help="Emit the resolved profile as JSON."
    ),
    pretty: bool = typer.Option(
        False, "--pretty", help="Human-readable table (default when neither flag)."
    ),
) -> None:
    """Show the active profile's resolved foundation fields.

    Lazy hydration (2026-08-28 rev): calls `get_profile(ctx)` to load +
    validate + cache the active profile from `<base>/<name>/profile.yaml`.
    Any loader failure (`--profile bogus`, missing profile.yaml, malformed
    yaml, or `no active profile: --profile flag not passed, env vars not
    set, and no 'current' symlink...`) surfaces as a clean CLI frame.
    Root-level `--json` / `--pretty` propagate through.
    """
    active = get_profile(ctx)
    obj = ctx.obj or {}

    use_json = bool(json_out or obj.get("json"))
    use_pretty = bool(pretty or obj.get("pretty"))

    payload = _profile_to_display_dict(active)

    if use_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=False))
        return

    _print_pretty_table(payload, force_wide=use_pretty)


@profile_app.command("use")
def use(
    name: str = typer.Argument(
        ...,
        help="Profile name to activate (must exist under <workspace_root>/profiles/).",
    ),
) -> None:
    """Atomically re-point the `current` symlink at profile `<name>`.

    Fails loud if the target profile does not exist (missing
    profile.yaml) or if a regular file sits where the symlink should
    live. Writes are atomic (tmp symlink + `os.replace`) so a concurrent
    reader never sees a partial state.
    """
    try:
        current_path = switch_active_profile(name)
    except ProfileError as exc:
        typer.echo(f"mineru profile use: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"active profile: {name}  ({current_path})")


@profile_app.command("active")
def active_verb() -> None:
    """Print the name of the currently active profile (via the symlink).

    Handy shell-composable read that does not require loading the whole
    profile.yaml — reads just the `active` symlink at the workspace root
    (falling back to a legacy `current` symlink for pre-2026-09-16
    workspaces) and prints the pointed-at basename.

    Renamed from `profile current` on 2026-09-16 (audit §2A F4) to match
    the on-disk symlink rename. The old `profile current` spelling still
    works as a hidden alias for ~90 days.
    """
    _print_active_profile_name(verb_label="mineru profile active")


@profile_app.command("current", hidden=True)
def current_alias() -> None:
    """DEPRECATED alias for `profile active`. Kept for ~90 days."""
    emit_rename_notice("mineru profile current", "mineru profile active")
    _print_active_profile_name(verb_label="mineru profile current")


def _print_active_profile_name(*, verb_label: str) -> None:
    """Print the pointer's basename, with legacy-`current` fallback.

    Both `profile active` (canonical) and `profile current` (hidden
    alias) dispatch through this shared body so the read semantics are
    identical: prefer the new `active` symlink; on absence, fall back
    to the pre-rename `current` symlink; on neither, exit 1 with a
    verb-labeled hint that points at the fix (`mineru profile use`).
    """
    import os

    ws = default_workspace_root()
    active_path = active_symlink_path(ws)
    if os.path.islink(active_path):
        typer.echo(Path(os.readlink(active_path)).name)
        return
    legacy = legacy_current_symlink_path(ws)
    if os.path.islink(legacy):
        typer.echo(Path(os.readlink(legacy)).name)
        return
    typer.echo(
        f"{verb_label}: no `active` symlink at {active_path} "
        f"(nor a legacy `current` symlink at {legacy}). "
        "Run `mineru profile use <name>` to set one.",
        err=True,
    )
    raise typer.Exit(code=1)


@profile_app.command("init")
def init(
    name: Optional[str] = typer.Option(
        None,
        "--name",
        metavar="NAME",
        help=(
            "PROFILE NAME — the agent's identifier and directory basename "
            "(lowercase kebab-case, [a-z0-9-]+). Prompted for if omitted."
        ),
    ),
    persona: Optional[str] = typer.Option(
        None,
        "--persona",
        metavar="NAME",
        help=(
            "PERSONA NAME — the assistant's display name (lands on "
            "`assistant_name` in profile.yaml). Defaults to the "
            "title-cased profile name."
        ),
    ),
    owner: Optional[str] = typer.Option(
        None,
        "--owner",
        metavar="HANDLE",
        help=(
            "Handle of an EXISTING human in humans.yaml to own this profile. "
            "Mutually exclusive with --owner-new-*."
        ),
    ),
    owner_new_handle: Optional[str] = typer.Option(
        None,
        "--owner-new-handle",
        metavar="HANDLE",
        help=(
            "Register a NEW human inline as the owner. Requires "
            "--owner-new-display and --owner-new-telegram."
        ),
    ),
    owner_new_display: Optional[str] = typer.Option(
        None,
        "--owner-new-display",
        metavar="NAME",
        help="Display name for the inline-added owner (see --owner-new-handle).",
    ),
    owner_new_telegram: Optional[int] = typer.Option(
        None,
        "--owner-new-telegram",
        metavar="INT",
        help="Numeric Telegram user ID for the inline-added owner.",
    ),
    timezone: Optional[str] = typer.Option(
        None,
        "--timezone",
        metavar="ZONE",
        help=(
            "IANA timezone (e.g. America/Los_Angeles). Defaults to the "
            "machine timezone."
        ),
    ),
    google_account: Optional[str] = typer.Option(
        None,
        "--google-account",
        metavar="EMAIL",
        help=(
            "Google Workspace email to record on the profile "
            "(profile.yaml `google_account`)."
        ),
    ),
    activate: bool = typer.Option(
        False,
        "--activate",
        help=(
            "Atomically re-point the `current` active-profile symlink at "
            "the new profile after successful scaffold. Bootstrap case "
            "(zero pre-existing profiles) does this automatically."
        ),
    ),
    skip_integrations: bool = typer.Option(
        False,
        "--skip-integrations",
        help=(
            "Do NOT offer the Google walkthrough at the end. --no-input "
            "implies this."
        ),
    ),
    no_input: bool = typer.Option(
        False,
        "--no-input",
        help=(
            "Never prompt. Any missing required field (--name, --persona, "
            "and either --owner or a complete --owner-new-* trio) FAILS "
            "instead of prompting. Useful for scripting."
        ),
    ),
) -> None:
    """Bootstrap a new agent profile on disk (interactive or scripted).

    Flow:
      1. Resolve every field (from flags, then prompts unless --no-input).
      2. Validate name shape, reserved names, workspace collisions,
         owner presence, timezone, and (when inline-added) new-human shape.
      3. Reserve `<workspace_root>/profiles/<name>/` ATOMICALLY via
         `os.makedirs(exist_ok=False)` — one syscall, no TOCTOU gap.
      4. Scaffold `profile.yaml`, `access.yaml`, `cron.yaml`, and
         `memory/`, `briefs/`, `cache/`, `logs/` subdirs. Any failure
         rolls back the entire dir.
      5. If a new human was added inline, atomically append them to
         `humans.yaml` (tmp+replace).
      6. Optionally offer the Google walkthrough (`gog auth add
         <email>`) or print the command for later.
      7. Print a summary + next-steps pointer.

    Never writes a secret to disk. Bot-token setup is deferred to the
    forthcoming `mineru profile set-bot` pointer.
    """
    obj = ctx_or_empty()

    # --- Resolve the workspace root + humans.yaml eagerly so we can
    # --- offer live prompts backed by the actual registry.
    ws = default_workspace_root()
    base = default_profiles_base_dir()
    humans_yaml_path = default_humans_yaml_path(ws)

    interactive = not no_input and _stdin_is_tty()

    # --- Welcome header — interactive only (a scripted caller piping
    # --- output gets a clean plain-text log without a decorative
    # --- header line). Matches the cli-ux-patterns §1 rustup pattern:
    # --- show where we're operating BEFORE asking anything.
    if interactive:
        _emit_section_header(
            "mineru profile init",
            subtitle=f"scaffolding a new agent under {ws}",
        )

    # --- Resolve name ------------------------------------------------------
    resolved_name = _resolve_or_prompt(
        name, "profile name (lowercase kebab-case, e.g. gemini-scout)",
        interactive=interactive, no_input=no_input, field_label="--name",
    )
    try:
        validate_profile_name(resolved_name, ws)
    except OnboardingError as exc:
        _die(f"profile init: {exc}")

    # --- Resolve persona ---------------------------------------------------
    persona_default = default_persona_from_name(resolved_name)
    if persona is not None and persona.strip():
        resolved_persona = persona.strip()
    elif interactive:
        resolved_persona = typer.prompt(
            "persona name (assistant's display name)",
            default=persona_default,
        ).strip()
    elif no_input:
        # No prompt allowed. Fall back to the derived default so the
        # operator can pass just --name in scripted flows without
        # having to also compute the persona.
        resolved_persona = persona_default
    else:
        resolved_persona = persona_default
    try:
        validate_persona(resolved_persona)
    except OnboardingError as exc:
        _die(f"profile init: {exc}")

    # --- Warn on persona duplication across profiles (soft check, not
    # --- a gate). Never fatal — an operator may legitimately reuse a
    # --- persona across two experimental profiles.
    persona_dups = check_persona_collision(resolved_persona, base)
    if persona_dups:
        typer.echo(
            f"note: persona {resolved_persona!r} is also used by "
            f"profile(s) {persona_dups}. Continuing.",
            err=True,
        )

    # --- Resolve owner -----------------------------------------------------
    # Two paths:
    #   (a) --owner <handle> : existing human in humans.yaml.
    #   (b) --owner-new-* trio : register a new human inline.
    # In interactive mode, if neither is provided we prompt and offer both.
    existing_registry: Optional[HumansRegistry] = None
    if humans_yaml_path.exists():
        try:
            existing_registry = load_humans_registry(path=humans_yaml_path)
        except HumansError as exc:
            _die(
                f"profile init: humans.yaml at {humans_yaml_path} is "
                f"invalid ({exc}). Fix it before running init."
            )

    resolved_owner_handle: Optional[str] = None
    resolved_new_human: Optional[NewHuman] = None

    if owner_new_handle is not None or owner_new_display is not None or owner_new_telegram is not None:
        # Inline-add path: all three fields must be present together.
        if owner_new_handle is None or owner_new_display is None or owner_new_telegram is None:
            _die(
                "profile init: --owner-new-handle / --owner-new-display / "
                "--owner-new-telegram must all be provided together."
            )
        if owner is not None:
            _die(
                "profile init: --owner and --owner-new-* are mutually "
                "exclusive. Pick one."
            )
        resolved_new_human = NewHuman(
            handle=owner_new_handle,
            display_name=owner_new_display,
            telegram_id=int(owner_new_telegram),
        )
        resolved_owner_handle = owner_new_handle
    elif owner is not None:
        resolved_owner_handle = owner
    elif interactive:
        resolved_owner_handle, resolved_new_human = _prompt_for_owner(
            existing_registry
        )
    else:
        # no_input or non-TTY without owner selection.
        if no_input:
            _die(
                "profile init: --no-input requires either --owner <handle> "
                "or the full --owner-new-* trio."
            )
        # Non-interactive fallback: fail loud instead of silently guessing.
        _die(
            "profile init: no --owner and no TTY for interactive prompt. "
            "Pass --owner <handle> or --owner-new-* trio."
        )

    if resolved_new_human is not None:
        try:
            validate_new_human(resolved_new_human)
        except OnboardingError as exc:
            _die(f"profile init: {exc}")

    # --- Resolve timezone --------------------------------------------------
    tz_default = machine_timezone()
    if timezone is not None and timezone.strip():
        resolved_tz = timezone.strip()
    elif interactive:
        resolved_tz = typer.prompt(
            "timezone (IANA zone)", default=tz_default
        ).strip()
    else:
        resolved_tz = tz_default
    try:
        validate_timezone(resolved_tz)
    except OnboardingError as exc:
        _die(f"profile init: {exc}")

    # --- Resolve google_account -------------------------------------------
    resolved_google: Optional[str] = None
    if google_account is not None and google_account.strip():
        resolved_google = google_account.strip()
    elif interactive and not skip_integrations:
        raw = typer.prompt(
            "Google Workspace email for this agent (blank to skip)",
            default="",
            show_default=False,
        ).strip()
        if raw:
            resolved_google = raw

    # --- Build the spec + call the scaffold -------------------------------
    spec = ProfileSpec(
        name=resolved_name,
        persona=resolved_persona,
        owner_handle=resolved_owner_handle,
        timezone=resolved_tz,
        google_account=resolved_google,
        new_human=resolved_new_human,
    )
    if interactive:
        _emit_step("start", f"scaffolding profile {spec.name!r}...")
    try:
        result = create_profile(spec, activate=activate)
    except OnboardingError as exc:
        if interactive:
            _emit_step("fail", f"scaffold aborted for {spec.name!r}")
        _die(f"profile init: {exc}")
    except FileExistsError as exc:
        # Rare — create_profile already translates the atomic-uniqueness
        # gate into OnboardingError. This is a belt-and-braces catch.
        if interactive:
            _emit_step("fail", f"scaffold aborted for {spec.name!r}")
        _die(f"profile init: {exc}")
    if interactive:
        _emit_step(
            "ok", f"scaffolded {result.profile_name!r} at {result.profile_root}"
        )
    # A suffixed reservation happens when the requested name was taken.
    # Surface it plainly so the operator sees the actual dir basename that
    # was created — persona / display_name are unchanged.
    if result.profile_name != spec.name:
        typer.secho(
            f"note: {spec.name!r} was taken; created {result.profile_name!r} "
            "(persona/display name unchanged).",
            fg=typer.colors.YELLOW,
            err=True,
        )

    # --- Optional Google walkthrough --------------------------------------
    integration_note: Optional[str] = None
    if resolved_google and not skip_integrations:
        integration_note = _run_or_defer_google_walkthrough(
            resolved_google, interactive=interactive
        )

    # --- Post-onboarding: preview the Landline Keychain allowlist -----------
    # Arc 2 step 4 wire-up. A freshly-scaffolded profile carries an owner-
    # only access.yaml, so the resolved allowlist is a single Telegram id
    # (the owner). We PLAN the sync here (dry-run only) so the operator
    # sees what `mineru access sync --apply` will do next — never write
    # the live Keychain from inside `profile init`. The write is
    # explicit: `mineru access sync --apply` (or `--apply` on any of
    # `access add`/`access remove`) is the one call site that mutates
    # the Keychain.
    keychain_plan_note = _preview_allowlist_sync(
        result.profile_name, result.profile_root
    )

    # --- Summary ----------------------------------------------------------
    _print_init_summary(spec, result, integration_note, keychain_plan_note)


def _stdin_is_tty() -> bool:
    """Return True iff stdin is attached to a TTY.

    Wrapped as a module-level helper (rather than an inline
    `sys.stdin.isatty()` call) so tests can mock it cleanly — the
    CliRunner replaces `sys.stdin` with a `StringIO`, which would
    otherwise defeat a `mineru_cli.verbs.profile.sys.stdin.isatty`
    patch that snapshots the original stdin object.
    """
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# Presentation-layer helpers (styling + framed panels + step markers).
#
# TTY-aware: EVERY styling helper below no-ops to plain text when
# `stdout` is not a TTY OR `NO_COLOR` is set OR `TERM=dumb`. Applies to
# both color and Unicode box-drawing — a scripted caller piping our
# output gets the same words with zero ANSI cruft.
#
# Patterns applied (from the `cli-ux-patterns` + `interactive-python-
# terminal-ux` skills, cited at their point of use):
#   - Module-level color/box detection with graceful ASCII fallback
#     (cli-ux-patterns §6 tables, §11 accessibility; interactive skill §4).
#   - Section header + framed panels for the intro + final summary
#     (cli-ux-patterns §1 rustup pattern, §9 done screens).
#   - `step()` marker with `... / ok / fail / warn` states
#     (cli-ux-patterns §7 step-by-step status).
#   - Stderr-only styled `[ERROR]` frames (cli-ux-patterns §8).
#   - Full plain-text degrade when non-TTY / `--no-input`
#     (cli-ux-patterns §10 headless, §11 accessibility).
# ---------------------------------------------------------------------------


def _stdout_is_tty() -> bool:
    """Return True iff stdout is attached to a TTY (safe on odd streams)."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _color_enabled() -> bool:
    """Decide once whether we may emit ANSI color escapes.

    Rules (respected in order):
      - `NO_COLOR` env var set (any value) → no color (https://no-color.org).
      - `TERM=dumb` → no color.
      - `stdout` is not a TTY (piped, redirected, `--no-input` runner) → no color.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return _stdout_is_tty()


def _unicode_enabled() -> bool:
    """Decide once whether to draw with Unicode box-drawing.

    Falls back to ASCII when the locale doesn't advertise UTF-8 or when
    stdout is not a TTY. The output stays intelligible either way; this
    just prevents `?`-glyph noise on legacy locales and keeps scripted
    output free of decorative bytes.
    """
    if not _stdout_is_tty():
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    encoding = (sys.stdout.encoding or "").lower()
    if "utf" in encoding:
        return True
    lang = (
        os.environ.get("LC_ALL", "")
        or os.environ.get("LC_CTYPE", "")
        or os.environ.get("LANG", "")
    ).lower()
    return "utf-8" in lang or "utf8" in lang


# Snapshotted at import time so we don't recompute per line. Every helper
# below reads through these two module-level flags.
_COLOR = _color_enabled()
_UNICODE = _unicode_enabled()

# ANSI escapes (empty strings when color is disabled). Small, curated
# palette — bold + one accent + green/red/yellow for status, matching
# the cli-ux-patterns skill's palette.
_BOLD = "\033[1m" if _COLOR else ""
_DIM = "\033[2m" if _COLOR else ""
_RESET = "\033[0m" if _COLOR else ""
_GREEN = "\033[32m" if _COLOR else ""
_YELLOW = "\033[33m" if _COLOR else ""
_RED = "\033[31m" if _COLOR else ""
_CYAN = "\033[36m" if _COLOR else ""

# Box-drawing chars with ASCII fallback (cli-ux-patterns §6).
if _UNICODE:
    _BOX_TL, _BOX_TR, _BOX_BL, _BOX_BR = "╭", "╮", "╰", "╯"
    _BOX_H, _BOX_V = "─", "│"
    _BULLET_OK = "✓"
    _BULLET_FAIL = "✗"
    _BULLET_DOT = "•"
else:
    _BOX_TL, _BOX_TR, _BOX_BL, _BOX_BR = "+", "+", "+", "+"
    _BOX_H, _BOX_V = "-", "|"
    _BULLET_OK = "+"
    _BULLET_FAIL = "x"
    _BULLET_DOT = "*"


def _panel_width() -> int:
    """Return a reasonable interior width for framed panels.

    Reads the terminal width when possible, clamps to `[52, 78]` so tables
    stay legible on tiny windows AND don't run edge-to-edge on wide ones.
    """
    try:
        cols = os.get_terminal_size(sys.stdout.fileno()).columns
    except (OSError, ValueError, AttributeError):
        cols = 80
    return max(52, min(78, cols - 2))


def _visible_len(text: str) -> int:
    """Character count with ANSI escape sequences stripped.

    Needed so a colored cell still aligns inside a fixed-width panel.
    Only recognizes the CSI (`ESC[...m`) form our palette uses.
    """
    if not _COLOR or "\033" not in text:
        return len(text)
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\033" and i + 1 < len(text) and text[i + 1] == "[":
            j = i + 2
            while j < len(text) and text[j] != "m":
                j += 1
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return len(out)


def _pad_right(text: str, width: int) -> str:
    """Right-pad `text` to `width` visible columns (ignoring ANSI cruft)."""
    pad = max(0, width - _visible_len(text))
    return text + (" " * pad)


def _panel_line(text: str, *, width: int) -> str:
    """One row of a framed panel, padded to `width` interior columns."""
    return f"  {_BOX_V} {_pad_right(text, width)} {_BOX_V}"


def _panel_top(width: int) -> str:
    return f"  {_BOX_TL}{_BOX_H * (width + 2)}{_BOX_TR}"


def _panel_bot(width: int) -> str:
    return f"  {_BOX_BL}{_BOX_H * (width + 2)}{_BOX_BR}"


def _emit_panel(title: str, rows: List[str]) -> None:
    """Print a framed panel with a bold header and one row per string.

    Panel width auto-sizes to the widest visible row (with the title
    counted) so long file paths never bulge past the right edge — the
    alternative (a fixed clamp) produces a ragged frame on rows whose
    content overshoots the clamp. Empty rows render as a blank spacer
    line. Non-TTY callers get the same lines as plain text (the
    framing chars auto-degrade to ASCII per `_UNICODE`).
    """
    min_w = _panel_width()
    content_w = max(
        [min_w, _visible_len(title)]
        + [_visible_len(row) for row in rows]
    )
    typer.echo(_panel_top(content_w))
    typer.echo(_panel_line(f"{_BOLD}{title}{_RESET}", width=content_w))
    typer.echo(_panel_line("", width=content_w))
    for row in rows:
        typer.echo(_panel_line(row, width=content_w))
    typer.echo(_panel_bot(content_w))


def _emit_section_header(title: str, subtitle: Optional[str] = None) -> None:
    """Emit a lightweight section header the operator's eye lands on.

    Kept small — just a blank line, a bold title with a leading accent
    bullet, and an optional dim subtitle. Same shape scripted or not
    (the bold/dim/color codes empty-string out when non-TTY).
    """
    typer.echo("")
    typer.echo(f"  {_CYAN}{_BULLET_DOT}{_RESET} {_BOLD}{title}{_RESET}")
    if subtitle:
        typer.echo(f"    {_DIM}{subtitle}{_RESET}")


def _emit_step(state: str, msg: str) -> None:
    """`step()`-style status marker (cli-ux-patterns §7).

    `state` is one of `start`, `ok`, `skip`, `fail`, `warn`. Non-TTY
    callers get a plain-text left-margin ("ok / skip / fail / warn / ..")
    so the log stays scannable without ANSI.
    """
    if state == "start":
        prefix = f"  {_DIM}..{_RESET} "
    elif state == "ok":
        prefix = f"  {_GREEN}{_BULLET_OK}{_RESET}  "
    elif state == "skip":
        prefix = f"  {_DIM}--{_RESET} "
    elif state == "warn":
        prefix = f"  {_YELLOW}!!{_RESET} "
    elif state == "fail":
        prefix = f"  {_RED}{_BULLET_FAIL}{_RESET}  "
    else:
        prefix = "     "
    typer.echo(f"{prefix}{msg}")


def _emit_error_frame(message: str) -> None:
    """Styled `[ERROR] ...` block on stderr (cli-ux-patterns §8).

    Always prefixed with `[ERROR]` in caps so the meaning survives when
    color is off (never rely on color alone — accessibility rule).
    """
    typer.echo("", err=True)
    typer.echo(f"  {_RED}{_BOLD}[ERROR]{_RESET} {message}", err=True)
    typer.echo("", err=True)


def ctx_or_empty() -> Dict[str, Any]:
    """Return a mutable dict for context accessors that expect one.

    The init verb runs without touching a hydrated profile (`get_profile`),
    so we don't need `typer.Context` here at all — but we keep a
    placeholder so future context-aware branches can plumb it in
    without a signature change.
    """
    return {}


def _resolve_or_prompt(
    value: Optional[str],
    prompt_text: str,
    *,
    interactive: bool,
    no_input: bool,
    field_label: str,
) -> str:
    """Return `value` if non-empty, else prompt (interactive) or die (no_input).

    Extracted so each required-field resolution reads the same way.
    Only used for fields with no reasonable default — persona/timezone/
    google_account get their own inline resolution because they have
    derived defaults.
    """
    if value is not None and value.strip():
        return value.strip()
    if no_input:
        _die(f"profile init: {field_label} is required with --no-input.")
    if not interactive:
        _die(
            f"profile init: {field_label} is required and stdin is not a "
            "TTY (add --no-input to opt into scripted-only mode)."
        )
    resolved = typer.prompt(prompt_text).strip()
    if not resolved:
        _die(f"profile init: {field_label} cannot be empty.")
    return resolved


def _prompt_for_owner(
    existing_registry: Optional[HumansRegistry],
) -> tuple[str, Optional[NewHuman]]:
    """Interactive owner selection.

    If `existing_registry` has humans, offer to pick one (with a "new"
    escape hatch); otherwise go straight to the inline-add path.
    Returns `(owner_handle, new_human_or_None)`.
    """
    if existing_registry is not None and len(existing_registry) > 0:
        typer.echo("existing humans:")
        for h in existing_registry:
            typer.echo(f"  - {h.handle}  ({h.display_name}, tg={h.telegram_id})")
        choice = typer.prompt(
            "owner handle (from the list above; type 'new' to register a new human)"
        ).strip()
        if choice.lower() != "new":
            if choice not in existing_registry:
                _die(
                    f"profile init: {choice!r} is not in humans.yaml. "
                    "Rerun and pick one of the listed handles or 'new'."
                )
            return choice, None
    # Inline-add path.
    handle = typer.prompt("new human handle (e.g. alice)").strip()
    display = typer.prompt("new human display name (e.g. \"Alice Smith\")").strip()
    tg_raw = typer.prompt("new human numeric Telegram user ID").strip()
    try:
        tg_id = int(tg_raw)
    except ValueError:
        _die(
            f"profile init: Telegram user ID {tg_raw!r} is not an integer. "
            "Retry with a numeric value."
        )
    return handle, NewHuman(handle=handle, display_name=display, telegram_id=tg_id)


def _run_or_defer_google_walkthrough(
    google_account: str, *, interactive: bool
) -> str:
    """Run `gog auth add <email>` if the operator says yes; otherwise
    return the command string for the summary."""
    command_str = format_gog_command_string(google_account)
    if not interactive:
        return (
            f"Google auth deferred (non-TTY). Run this yourself:\n"
            f"    {command_str}"
        )
    typer.echo("")
    run_now = typer.confirm(
        f"Run `{command_str}` now to authorize the Google account?",
        default=False,
    )
    if not run_now:
        return (
            f"Google auth deferred. Run this yourself when ready:\n"
            f"    {command_str}"
        )
    rc = run_gog_auth_add(google_account)
    if rc != 0:
        return (
            f"`gog auth add` exited {rc}. Retry when ready:\n"
            f"    {command_str}"
        )
    return f"Google auth completed via `{command_str}`."


def _print_init_summary(
    spec: ProfileSpec,
    result: ScaffoldResult,
    integration_note: Optional[str],
    keychain_plan_note: Optional[str] = None,
) -> None:
    """Print the human-facing summary at the end of a successful init.

    Renders as a framed "what was created" panel + a "next steps" block
    with copy-pasteable commands (cli-ux-patterns §9 done screens).
    Non-TTY / NO_COLOR / dumb-TERM callers get the same LINES with
    ASCII framing and no color codes — every field the operator can
    grep for is present in either mode.

    `keychain_plan_note` is the (dry-run) preview of the Landline
    Keychain allowlist sync — rendered above the next-steps block so
    the operator sees the exact `mineru access sync --apply` command
    they need to run to activate the daemon-side authorization.
    """
    # --- Green checkmark headline. Always includes the word "created" so
    # --- a scripted caller can grep for it whether color is on or off.
    typer.echo("")
    typer.echo(
        f"  {_GREEN}{_BOLD}{_BULLET_OK} created profile: "
        f"{result.profile_name}{_RESET}"
    )

    # --- Panel: what landed on disk + resolved identity fields.
    rows: List[str] = _build_summary_rows(spec, result)
    _emit_panel("What was created", rows)

    # --- Integration walkthrough note (rendered plainly so a copyable
    # --- command line stays paste-safe).
    if integration_note is not None:
        typer.echo("")
        for line in integration_note.splitlines():
            typer.echo(f"  {line}" if not line.startswith("    ") else line)

    # --- Landline Keychain allowlist preview (dry-run; the `--apply`
    # --- landing zone is a separate `mineru access sync --apply` call
    # --- so we never write the live Keychain from inside `profile init`).
    if keychain_plan_note is not None:
        typer.echo("")
        for line in keychain_plan_note.splitlines():
            typer.echo(f"  {line}" if not line.startswith("    ") else line)

    # --- Next steps: literal commands. Bold header, cyan command bodies
    # --- (no color when not a TTY). Charter + bot-token pointers stay
    # --- as `#` comments so the block reads as a paste-safe shell script.
    typer.echo("")
    typer.echo(f"  {_BOLD}Next steps:{_RESET}")
    typer.echo("")
    typer.echo(
        f"    {_CYAN}mineru profile show --profile {result.profile_name}{_RESET}"
    )
    typer.echo(
        f"    {_CYAN}mineru access show --profile {result.profile_name}{_RESET}"
    )
    typer.echo(
        f"    {_CYAN}mineru access sync --profile {result.profile_name} --apply{_RESET}"
        f"   {_DIM}# refresh Landline daemon Keychain allowlist{_RESET}"
    )
    if not result.activated:
        typer.echo(
            f"    {_CYAN}mineru profile use {result.profile_name}{_RESET}"
            f"       {_DIM}# activate this profile{_RESET}"
        )
    typer.echo("")
    typer.echo(
        f"    {_DIM}# Charter (IDENTITY.md / SOUL.md / AGENTS.md) "
        f"rendering is Phase 2 —{_RESET}"
    )
    typer.echo(
        f"    {_DIM}# see TODO_CHARTER_TEMPLATES.md in the new profile dir.{_RESET}"
    )
    typer.echo(
        f"    {_DIM}# Bot-token setup will land as "
        f"`mineru profile set-bot` (never write secrets to a file).{_RESET}"
    )
    typer.echo("")


def _preview_allowlist_sync(
    profile_name: str, profile_root: Path
) -> Optional[str]:
    """Preview the Landline Keychain allowlist for a freshly-scaffolded profile.

    DOES NOT WRITE the Keychain. Loads the just-scaffolded access.yaml +
    the machine-level humans.yaml, resolves the owner-only allowlist to
    a Telegram user id, and returns a short human-readable note the
    summary printer inlines above the next-steps block.

    Returns `None` (silently skips the preview) when either yaml pair is
    unloadable — a scaffold-time YAML error is already surfaced by
    downstream `mineru access show`/`sync`; we don't want to make
    `profile init` fail loudly for a preview-only step.
    """
    try:
        # Deferred imports keep the top-of-file import graph clean —
        # onboarding runs on a fresh clone where these subsystems may
        # not have been touched yet, but they are guaranteed to be
        # importable inside this repo layout.
        from mineru_cli.access import (
            AllowlistExportError,
            format_allowlist_value,
            load_access_config,
            resolve_allowlist_ids,
        )
        from mineru_cli.humans import HumansError, load_humans_registry
        from mineru_cli.profile import load_active_profile

        # We just scaffolded this profile; load it fresh so the note
        # reads the on-disk YAML (avoids stale state if the operator's
        # active profile is a DIFFERENT profile they were working on).
        loaded_profile = load_active_profile(
            profile_name, base_dir=profile_root.parent
        )
        humans = load_humans_registry()
        access = load_access_config(loaded_profile, humans)
        resolved = resolve_allowlist_ids(access, humans)
    except (
        AllowlistExportError,
        HumansError,
    ) as exc:  # noqa: BLE001 — preview must never crash init
        return (
            f"Landline Keychain allowlist NOT previewable "
            f"(access/humans join failed: {type(exc).__name__}). Fix "
            f"with `mineru access show --profile {profile_name}` and "
            f"then `mineru access sync --profile {profile_name} --apply`."
        )
    except Exception:  # noqa: BLE001 — preview must never crash init
        return None
    canonical = format_allowlist_value(resolved)
    return (
        "Landline Keychain allowlist preview (DRY-RUN, nothing written):\n"
        f"    keychain account: {loaded_profile.keychain_account}\n"
        f"    keychain service: telegram-allowed-chat-ids\n"
        f"    resolved ids:     {resolved}\n"
        f"    canonical value:  {canonical!r}\n"
        f"    apply with:       mineru access sync --profile "
        f"{profile_name} --apply"
    )


def _build_summary_rows(
    spec: ProfileSpec, result: ScaffoldResult
) -> List[str]:
    """Compose the aligned key/value rows for the summary panel.

    Kept as its own helper so the panel width + alignment logic lives
    in one place and the tests that check for specific tokens
    ("profile.yaml", "memory/", etc.) find them regardless of framing.
    """
    label_w = len("google account")
    def kv(label: str, value: str, *, accent: str = "") -> str:
        return (
            f"{_pad_right(label, label_w)}  {accent}{value}{_RESET if accent else ''}"
        )

    rows: List[str] = [
        kv("profile root",  str(result.profile_root),      accent=_BOLD),
        kv("profile.yaml",  str(result.profile_yaml_path)),
        kv("access.yaml",   str(result.access_yaml_path)),
        kv("memory/",       str(result.memory_root)),
        kv("briefs/",       str(result.briefs_root)),
        kv("cache/",        str(result.cache_root)),
        kv("logs/",         str(result.logs_root)),
        "",
        kv("persona",       spec.persona,                  accent=_BOLD),
        kv("owner",         spec.owner_handle),
        kv("timezone",      spec.timezone),
    ]
    if spec.google_account:
        rows.append(kv("google account", spec.google_account))
    if result.humans_yaml_written:
        rows.append(kv("humans.yaml", f"updated  {_GREEN}{_BULLET_OK}{_RESET}"))
    if result.activated:
        rows.append(
            kv(
                "active profile",
                f"{result.profile_name}  {_DIM}(active symlink re-pointed){_RESET}",
                accent=_GREEN,
            )
        )
    else:
        rows.append(
            kv(
                "active profile",
                f"unchanged  {_DIM}(run `mineru profile use "
                f"{result.profile_name}` to activate){_RESET}",
            )
        )
    return rows


def _die(message: str) -> None:
    """Print `message` to stderr in a styled `[ERROR]` frame and exit 2.

    Non-TTY / --no-input / NO_COLOR: styling degrades to a plain
    `  [ERROR] <message>` line (still scannable), so `mineru profile
    init ... 2>err.log` produces the same words a human sees on a
    terminal.
    """
    _emit_error_frame(message)
    raise typer.Exit(code=2)


@profile_app.command("install")
def install_cmd(
    ctx: typer.Context,
    target: Path = typer.Option(
        ...,
        "--target",
        metavar="DIR",
        help=(
            "Target directory to install into. REQUIRED for now — the "
            "verb refuses to default to a real workspace so an accidental "
            "invocation cannot clobber `$MINERU_HOME`."
        ),
    ),
    profile_name: Optional[str] = typer.Option(
        None,
        "--profile",
        metavar="NAME",
        help=(
            "Optional profile override for this invocation. When omitted, "
            "resolution falls through to the root-level `--profile` flag / "
            "MINERU_PROFILE env / `current` symlink."
        ),
    ),
    engine_root_override: Optional[Path] = typer.Option(
        None,
        "--engine-root",
        metavar="DIR",
        help=(
            "Override the engine root (the source tree that ships charter/, "
            "prompts/, recurring/, launchd/, app-deploy/). When omitted, "
            "resolution falls through to MINERU_ENGINE_ROOT env, then "
            "<workspace_root>/engine/. Handy for pointing at a sibling "
            "checkout during development."
        ),
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help=(
            "DEFAULT dry-run. Prints the plan and mutates nothing. Pass "
            "--apply to actually write. Apply is safe to re-run; it refuses "
            "up front (writing nothing) when the target holds unrelated "
            "entries, or a real file/dir sits where a link is planned."
        ),
    ),
    no_dry_run_alias: bool = typer.Option(
        False,
        "--no-dry-run",
        hidden=True,
        help=(
            "DEPRECATED alias for --apply. Kept for 90 days so operator "
            "scripts and muscle memory keep working."
        ),
    ),
) -> None:
    """Render templates + lay symlinks into --target from the active profile.

    Phase-2 foundation (chunk 1). Loads the active profile, assembles a
    `{{VAR}}` render context (persona + user identity + filesystem
    roots + system namespacing), walks the ENGINE tree (NOT the workspace
    root — the engine clone lives at `<workspace_root>/engine/` in
    production; see `default_engine_root()`), and either prints the
    resulting plan (default dry-run) or applies it into --target.

    `--target` is REQUIRED — the verb refuses to default to a real
    workspace this round so an accidental `mineru profile install` on
    the operator's laptop cannot overwrite `$MINERU_HOME`. Executing a real
    (non-dry-run) install into `$MINERU_HOME` is out of scope for this
    chunk; the required --target plus the non-empty-target guard keep
    it sandbox-only.
    """
    _run_profile_install(
        ctx,
        target=target,
        profile_name=profile_name,
        engine_root_override=engine_root_override,
        dry_run=dry_run,
        no_dry_run_alias=no_dry_run_alias,
    )


@profile_app.command("hydrate", hidden=True)
def hydrate_alias(
    ctx: typer.Context,
    target: Path = typer.Option(
        ...,
        "--target",
        metavar="DIR",
        help="See `profile install --help`.",
    ),
    profile_name: Optional[str] = typer.Option(
        None, "--profile", metavar="NAME", help="See `profile install --help`."
    ),
    engine_root_override: Optional[Path] = typer.Option(
        None, "--engine-root", metavar="DIR", help="See `profile install --help`."
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="See `profile install --help`.",
    ),
    no_dry_run_alias: bool = typer.Option(
        False, "--no-dry-run", hidden=True, help="See `profile install --help`."
    ),
) -> None:
    """DEPRECATED alias for `profile install`. Kept for 90 days."""
    emit_rename_notice("mineru profile hydrate", "mineru profile install")
    _run_profile_install(
        ctx,
        target=target,
        profile_name=profile_name,
        engine_root_override=engine_root_override,
        dry_run=dry_run,
        no_dry_run_alias=no_dry_run_alias,
    )


def _run_profile_install(
    ctx: typer.Context,
    *,
    target: Path,
    profile_name: Optional[str],
    engine_root_override: Optional[Path],
    dry_run: bool,
    no_dry_run_alias: bool,
) -> None:
    """Shared body for `profile install` and the deprecated `profile hydrate` alias.

    Kept as a plain module-level function (not a Typer callback) so both
    the canonical verb and the hidden `hydrate` shim dispatch through
    the same code path — one place to change when the underlying
    hydrate machinery evolves.

    Flag-alias translation: `--no-dry-run` was the original opt-in
    write flag; the audit's flag-ergonomics fix (2026-09-16) renamed it
    to `--apply`. The hidden `no_dry_run_alias` boolean below preserves
    the old spelling; when set it emits one deprecation line and forces
    `dry_run=False` (the same effect the old flag had).
    """
    if no_dry_run_alias:
        emit_rename_notice("--no-dry-run", "--apply")
        dry_run = False

    # Verb-level --profile override: stash it on ctx.obj so
    # `get_profile(ctx)` sees the newer value. Bust the cache too so a
    # prior `show` invocation in the same process doesn't leak.
    if profile_name is not None:
        obj = ctx.ensure_object(dict)
        obj["profile"] = profile_name
        obj.pop("profile_obj", None)
        obj.pop("secrets_config", None)

    # Resolve engine_root BEFORE `get_profile`: loading the profile exports
    # MINERU_HOME = workspace_absolute (often `profiles/<name>`), which
    # would otherwise redirect the `<MINERU_HOME>/engine` default into the
    # profile dir. Resolved in the verb body (never a Typer default) so the
    # env seam is read at call time. `--engine-root` > MINERU_ENGINE_ROOT >
    # `<workspace_root>/engine`.
    if engine_root_override is not None:
        engine_root = engine_root_override.expanduser().resolve()
    else:
        engine_root = default_engine_root()

    active = get_profile(ctx)
    # Rehearsal must-fix #1 (2026-09-16): load `<profile_root>/connectors.yaml`
    # and pass it to `build_render_context`. The prior `connectors=None`
    # was the CLI-shipped bug that aborted every real install on the
    # first connector-derived variable (`TAILSCALE_HOSTNAME`,
    # `PERSONAL_CALENDAR_ID`, ...). Shipped engine templates hard-reference
    # connector keys, so a missing / unreadable / malformed / non-mapping
    # connectors.yaml fails loud (_die) rather than rendering a broken
    # workspace; only an empty file is tolerated (yields {}).
    connectors = _load_profile_connectors(active.profile_root)

    context = build_render_context(
        active,
        connectors=connectors,
        env=dict(os.environ),
    )
    try:
        plan = build_plan(
            engine_root=engine_root,
            target_root=target,
            context=context,
            profile_root=active.profile_root,
        )
        _warn_if_target_is_not_runtime_root(target, active.workspace_absolute)
        report = apply_plan(plan, dry_run=dry_run)
        if not dry_run:
            typer.echo(f"profile install: applied ({report.summary()}).")
    except HydrationError as exc:
        # `build_plan` raises HydrationError when engine_root is missing
        # or not a directory; `apply_plan` raises when a template read fails
        # or the non-empty-target guard trips. Both surface as one CLI frame.
        _die(f"profile install: {exc}")
    except ValueError as exc:
        # `render_template` raises ValueError for unknown scalar keys,
        # malformed/unbalanced section tags, and non-list operands to
        # `{{#each}}`. Only bites on `--apply` (dry-run skips rendering),
        # but wire the branch now so a bad template surfaces as a clean
        # CLI frame instead of an uncaught traceback.
        _die(f"profile install: template error: {exc}")


def _warn_if_target_is_not_runtime_root(target: Path, workspace_absolute: Path) -> None:
    """Warn on stderr when --target differs from the profile's runtime root.

    Rendered plists and recipes bake `{{MINERU_HOME}}` from
    `workspace_absolute`, not from --target, so an install elsewhere points
    every job at `<workspace_absolute>/scripts/...`, which this install did
    not lay down. A sandbox rehearsal does this on purpose, hence a warning.
    """
    if target.expanduser().resolve() == Path(workspace_absolute).resolve():
        return
    typer.echo(
        f"warning: install target {target} differs from the profile's "
        f"workspace_absolute {workspace_absolute}; rendered paths "
        "({{MINERU_HOME}}) point at workspace_absolute, so jobs will not "
        "find this install's bin/ and scripts/.",
        err=True,
    )


def _load_profile_connectors(profile_root: Path) -> Dict[str, Any]:
    """Load `<profile_root>/connectors.yaml` into a dict — fail loud on absence.

    Absent file → `_die` with an operator-facing message. Shipped engine
    templates HARD-REFERENCE connector keys (`{{DAEMON_PERSONA_NAME}}`,
    `{{TAILSCALE_HOSTNAME}}`, `{{PERSONAL_CALENDAR_ID}}`, ...), so a
    missing overlay guarantees the render bombs deep inside
    `render_template` with an "unknown variable" error that does not
    guide the operator toward the fix. Failing loud here surfaces the
    exact missing file and points at the required schema instead.

    Present-but-empty → empty dict (a valid connectors overlay is
    allowed to be empty; template refs will still fail loud at render).
    Present-and-malformed or unreadable → `_die` with a clear
    operator-facing message.

    Keys are copied through verbatim, per the locked design in
    `memory/projects/mineru-tooling.md`: connectors.yaml uses UPPERCASE
    keys matching template variable names (`TAILSCALE_HOSTNAME`,
    `PERSONAL_CALENDAR_ID`, ...), and the render context takes them as-is.

    Follow-up (2026-09-16 review): `profile init` today scaffolds
    `profile.yaml`, `access.yaml`, `cron.yaml` and `humans.yaml`, but
    NOT `connectors.yaml`, and no `connectors.example.yaml` ships in
    the repo either. That gap means every fresh profile trips this
    branch on the first `profile install`. The right fix is to have
    `profile init` scaffold a minimal `connectors.yaml` stub (or ship
    a `connectors.example.yaml` alongside `humans.yaml`); tracked
    separately.
    """
    connectors_path = profile_root / "connectors.yaml"
    if not connectors_path.exists():
        _die(
            f"profile install: {connectors_path} does not exist, but shipped "
            "engine templates reference connector keys "
            "(`{{DAEMON_PERSONA_NAME}}`, `{{TAILSCALE_HOSTNAME}}`, "
            "`{{PERSONAL_CALENDAR_ID}}`, ...). Without this file the render "
            "bombs on the first connector reference. Create it as a YAML "
            "mapping of UPPERCASE keys (`KEY: value` pairs) matching the "
            "connector variables your templates use. `profile init` does not "
            "scaffold this file yet — populate it by hand from your "
            "connector settings (Tailscale hostname, Google calendar IDs, "
            "Telegram bot token service, etc.) before re-running install."
        )
    try:
        raw_text = connectors_path.read_text(encoding="utf-8")
    except OSError as exc:
        _die(
            f"profile install: could not read {connectors_path}: "
            f"{type(exc).__name__}. Check permissions or move the file "
            "out of the way and re-run."
        )
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        _die(
            f"profile install: {connectors_path} is not valid YAML "
            f"({type(exc).__name__}). Fix the file and retry."
        )
    if data is None:
        return {}
    if not isinstance(data, dict):
        _die(
            f"profile install: {connectors_path} must be a YAML mapping "
            f"at the top level; got {type(data).__name__}. Rewrite as "
            "`KEY: value` pairs (UPPERCASE keys)."
        )
    return data


@profile_app.command("validate")
def validate(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False, "--json", help="Emit the validation report as JSON."
    ),
) -> None:
    """Validate the active profile: schema, required secrets, and connectors.

    Three-part check:
      1. `profile.yaml` schema — delegated to `load_active_profile`,
         which enforces every required field, type coercion, and
         path/prefix safety class. A ProfileError surfaces here as one
         schema problem row.
      2. Required secrets — for every entry in the profile's
         `<profile_root>/connectors.yaml` `KEYCHAIN_SERVICES` list, run
         the secrets resolver's audit and flag any that are absent
         from every backend on the chain.
      3. Connectors placeholders — walk every string in
         `connectors.yaml` and flag values containing the literal
         `REPLACE_ME` substring (case-insensitive). These are the
         unfilled slots a fresh scaffold leaves behind.

    Exit code:
      - 0 iff EVERY check passed.
      - 2 if ANY problem was reported. The report enumerates every
        problem — one pass, not first-hit.
    """
    from mineru_cli.profile.loader import ProfileError

    problems: List[Dict[str, Any]] = []
    active = None
    try:
        active = get_profile(ctx)
    except typer.BadParameter as exc:
        # `get_profile` wraps `ProfileError` as `BadParameter` for the
        # help/usage frame. Unwrap the message so the validate report
        # carries the original loader message.
        problems.append(
            {"kind": "schema", "message": str(exc.message)}
        )
    except ProfileError as exc:
        problems.append({"kind": "schema", "message": str(exc)})

    connectors: Dict[str, Any] = {}
    if active is not None:
        connectors = _validate_connectors_placeholders(active, problems)
        _validate_required_secrets(active, problems)

    if json_out:
        payload = {
            "ok": not problems,
            "profile": active.name if active is not None else None,
            "problems": problems,
        }
        typer.echo(json.dumps(payload))
    else:
        if not problems:
            profile_label = active.name if active is not None else "<unknown>"
            typer.echo(
                f"profile validate: OK — profile {profile_label!r} passes "
                "schema, required-secrets, and connectors checks."
            )
        else:
            typer.echo(
                f"profile validate: {len(problems)} problem"
                f"{'s' if len(problems) != 1 else ''} found:",
                err=True,
            )
            for problem in problems:
                typer.echo(
                    f"  [{problem['kind']}] {problem['message']}", err=True
                )
    if problems:
        raise typer.Exit(code=2)


def _validate_connectors_placeholders(
    active: Any, problems: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Load connectors.yaml and flag REPLACE_ME placeholders.

    Missing connectors.yaml is itself a problem (shipped engine
    templates hard-reference connector keys — see `_load_profile_connectors`).
    Malformed YAML is a problem. Present-but-empty is fine (returns {}).

    Every string value (nested lists/dicts included) is walked; any
    value containing the case-insensitive substring `replace_me`
    contributes ONE problem row naming its dotted-path key.
    """
    connectors_path = active.profile_root / "connectors.yaml"
    if not connectors_path.exists():
        problems.append(
            {
                "kind": "connectors",
                "message": (
                    f"connectors.yaml missing at {connectors_path}; every "
                    "shipped engine template references connector keys "
                    "(TAILSCALE_HOSTNAME, PERSONAL_CALENDAR_ID, ...). "
                    "Create the file as a YAML mapping (KEY: value) or "
                    "run `profile install` to see the required refs."
                ),
            }
        )
        return {}
    try:
        data = yaml.safe_load(connectors_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        problems.append(
            {
                "kind": "connectors",
                "message": (
                    f"connectors.yaml at {connectors_path} could not be "
                    f"parsed ({type(exc).__name__}). Fix and rerun."
                ),
            }
        )
        return {}
    if not isinstance(data, dict):
        problems.append(
            {
                "kind": "connectors",
                "message": (
                    f"connectors.yaml at {connectors_path} must be a "
                    f"mapping at the top level; got {type(data).__name__}."
                ),
            }
        )
        return {}
    for dotted_key, offending_value in _find_replace_me_leaves(data):
        problems.append(
            {
                "kind": "connectors",
                "message": (
                    f"connectors.yaml key {dotted_key} still carries an "
                    f"unfilled placeholder value {offending_value!r} — "
                    "replace REPLACE_ME with the real connector value."
                ),
            }
        )
    return data


def _find_replace_me_leaves(node: Any, prefix: str = "") -> List[Tuple[str, str]]:
    """Walk `node` (dict/list/scalar), return `[(dotted_key, value)]` hits.

    A value hits if it's a string containing `replace_me` (case-insensitive).
    Numeric list indices are rendered as `[0]`, `[1]`, ... in the
    dotted key so the report points to the exact offender.
    """
    hits: List[Tuple[str, str]] = []
    if isinstance(node, dict):
        for key, child in node.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            hits.extend(_find_replace_me_leaves(child, child_prefix))
    elif isinstance(node, list):
        for i, child in enumerate(node):
            hits.extend(_find_replace_me_leaves(child, f"{prefix}[{i}]"))
    elif isinstance(node, str):
        if "replace_me" in node.lower():
            hits.append((prefix or "<root>", node))
    return hits


def _validate_required_secrets(
    active: Any, problems: List[Dict[str, Any]]
) -> None:
    """For every declared secret name, add a problem row if it's absent.

    "Declared" = an entry in `KEYCHAIN_SERVICES` inside `connectors.yaml`.
    Uses the same resolver every other `mineru secrets` verb uses
    (env then Keychain by default, per the profile's `secrets.backends`
    config). Never reads or prints values — only presence flags.
    """
    from mineru_cli.profile import secrets_config_from_profile
    from mineru_cli.secrets import build_resolver
    from mineru_cli.verbs.secrets import _load_configured_secret_names

    entries = _load_configured_secret_names(active.profile_root)
    if not entries:
        return
    resolver = build_resolver(secrets_config_from_profile(active))
    for row in resolver.audit([e["name"] for e in entries]):
        if not row.present:
            problems.append(
                {
                    "kind": "secret",
                    "message": (
                        f"secret {row.name!r} is declared under "
                        f"KEYCHAIN_SERVICES but is absent from every "
                        "configured backend. Run "
                        f"`mineru secrets set {row.name}` to populate it."
                    ),
                }
            )


@profile_app.command("export")
def export(
    ctx: typer.Context,
    out: Path = typer.Option(
        ...,
        "--out",
        metavar="PATH",
        help=(
            "Output path for the portable bundle (`.tar.gz`). Parent "
            "dirs are created if missing. An existing file is overwritten."
        ),
    ),
) -> None:
    """Export the active profile as a portable `.tar.gz` bundle.

    The bundle EXCLUDES anything that could carry a secret (see
    `mineru_cli.profile.bundle.EXCLUDED_*` constants — `secrets.yaml`,
    `secrets/`, `.env*`, `*.pem`, `id_*`, etc.) plus common noise
    (`cache/`, `logs/`, `__pycache__/`, `.git/`, `.DS_Store`).

    The bundle IS the profile directory itself (`profile.yaml`,
    `connectors.yaml`, `access.yaml`, `cron.yaml`, `custom_verbs.yaml`,
    any charter markdown, etc.) — the operator can inspect it with
    `tar tzf <bundle>` before importing anywhere.

    Prints one line per included file (grouped) and a short excluded
    summary; then the archive path. Exits 0 on success, 2 on IO error.
    """
    from mineru_cli.profile.bundle import (
        BundleError,
        build_export_plan,
        write_export_archive,
    )

    active = get_profile(ctx)
    plan = build_export_plan(
        profile_name=active.name,
        profile_root=active.profile_root,
        out_path=out.expanduser(),
    )
    try:
        write_export_archive(plan)
    except (BundleError, OSError) as exc:
        _die(f"profile export: {exc}")

    typer.echo(
        f"profile export: wrote {len(plan.included)} file"
        f"{'s' if len(plan.included) != 1 else ''} to {plan.out_path}"
    )
    typer.echo(f"  profile: {plan.profile_name}")
    typer.echo(f"  source:  {plan.profile_root}")
    typer.echo("  included:")
    for rel in plan.included:
        typer.echo(f"    {rel}")
    if plan.excluded:
        typer.echo(
            f"  excluded ({len(plan.excluded)} entr"
            f"{'ies' if len(plan.excluded) != 1 else 'y'} — never in bundle):"
        )
        for entry in plan.excluded:
            typer.echo(f"    {entry['path']}  ({entry['reason']})")


@profile_app.command("import")
def import_bundle(
    path: Path = typer.Argument(
        ...,
        help=(
            "Path to a `.tar.gz` profile bundle produced by "
            "`mineru profile export --out ...`."
        ),
    ),
) -> None:
    """Install a profile bundle as a NEW profile on this machine.

    Extraction is safe:
      - Bundle unpacks into a temp dir OUTSIDE the workspace so a
        malformed archive can never partial-write into `profiles/`.
      - Every member is checked for path traversal (absolute paths,
        `..` segments, unsafe symlinks) BEFORE any file is extracted.
      - The archive's top-level directory name and its `profile.yaml`
        `name:` field must match — the loader keys profiles by that
        single canonical name.
      - Target `<profiles_base>/<name>/` must NOT already exist. Name
        uniqueness is the whole point of this check; a duplicate would
        silently shadow the operator's existing profile.

    On success, prints the installed path. Does NOT activate the new
    profile — the operator runs `mineru profile use <name>` when ready.
    """
    from mineru_cli.profile import load_active_profile
    from mineru_cli.profile.bundle import (
        BundleError,
        install_bundle_contents,
        open_bundle,
    )
    from mineru_cli.profile.loader import ProfileError
    import tempfile

    bundle_path = path.expanduser().resolve()
    profiles_base = default_profiles_base_dir()
    with tempfile.TemporaryDirectory(prefix="mineru-import-") as tmp:
        tmp_root = Path(tmp)
        try:
            contents = open_bundle(bundle_path, tmp_root)
        except BundleError as exc:
            _die(str(exc))
        # Name-uniqueness check BEFORE any write — same "reserve target"
        # discipline `profile init` uses via `os.makedirs(exist_ok=False)`.
        dest = profiles_base / contents.profile_name
        if dest.exists():
            _die(
                f"profile import: a profile named "
                f"{contents.profile_name!r} already exists at {dest}. "
                "Remove it (or rename inside the bundle) and retry."
            )
        # Schema check BEFORE install — same fail-loud pattern as the
        # loader's own `_build_profile`. Uses the temp dir as the
        # profiles_base so the load reads the bundle-contained YAML
        # instead of hitting the workspace tree.
        try:
            load_active_profile(
                explicit_name=contents.profile_name, base_dir=tmp_root
            )
        except ProfileError as exc:
            _die(f"profile import: bundled profile.yaml is invalid: {exc}")
        try:
            installed_at = install_bundle_contents(contents, profiles_base)
        except BundleError as exc:
            _die(str(exc))

    typer.echo(
        f"profile import: installed profile {contents.profile_name!r} at "
        f"{installed_at}"
    )
    typer.echo(
        f"  activate with: mineru profile use {contents.profile_name}"
    )


# --- Rendering helpers ----------------------------------------------------


def _profile_to_display_dict(profile_obj: Any) -> Dict[str, Any]:
    """Build the ordered dict used by both JSON and table renderings."""
    if not is_dataclass(profile_obj):
        raise TypeError(
            f"expected Profile dataclass, got {type(profile_obj).__name__}"
        )
    raw = asdict(profile_obj)
    ordered: Dict[str, Any] = {}
    for key in (
        "name",
        "display_name",
        "assistant_name",
        "timezone",
        "keychain_account",
        "launchd_label_prefix",
        "workspace_absolute",
        "memory_root",
        "briefs_root",
        "journal_apple_notes_folder",
        "secrets_backends",
        "secrets_env_prefix",
        "profile_root",
        "profile_yaml_path",
        "extras",
    ):
        value = raw.get(key)
        if isinstance(value, Path):
            ordered[key] = str(value)
        else:
            ordered[key] = value
    return ordered


def _print_pretty_table(payload: Dict[str, Any], *, force_wide: bool) -> None:
    """Two-column key/value table, aligned to the longest key."""
    del force_wide
    if not payload:
        typer.echo("(empty profile)")
        return
    key_width = max(len(k) for k in payload)
    for key, value in payload.items():
        if isinstance(value, (dict, list)) and value:
            rendered = json.dumps(value, sort_keys=False)
        elif isinstance(value, (dict, list)):
            rendered = "(empty)"
        else:
            rendered = str(value)
        typer.echo(f"  {key.ljust(key_width)}  {rendered}")
