"""`mineru setup` bare verb — guided first-run onboarding.

Sep 2026 audit §F2 wire-up. This is a thin orchestration layer over the
already-shipped `profile init` + `profile install` verbs — it does NOT
reinvent either. The job: turn "read the docs, then run four commands in
the right order and remember the flags" into ONE command.

Flow:

  1. `profile init` — scaffold `profile.yaml`, `access.yaml`, `cron.yaml`,
     `connectors.yaml` (the Sep-2026 gap-close), and the per-profile dir
     tree. Skip if the target profile already exists (idempotent).
  2. `profile install --apply` — render every engine template + lay every
     symlink into the target workspace. Preview-first is not useful here
     since the operator already asked for a fresh setup; `setup` runs
     `--apply` directly.
  3. Print the Keychain secrets checklist. `setup` NEVER writes a secret
     to disk (policy — see `SECURITY.md` "Never Store Plaintext Secrets
     on Disk"). It prints the `security add-generic-password` commands
     the operator should run themselves.

Design notes:

  - `setup` reuses `profile init` and `profile install` INTERNALLY via
    the CliRunner-invocation pattern used by the existing tests. That
    is deliberate: it means every future fix / feature / bug landing on
    `profile init` or `profile install` shows up under `setup` for
    free, and the setup verb itself stays small.
  - The Sep 2026 audit §F2 fallback: even after `connectors.yaml` gets
    scaffolded, install can fail for OTHER reasons (a template that
    references a profile-extra the scaffold does not populate). `setup`
    catches any non-zero exit from `profile install` and prints a
    clear next-steps block naming the exact failure and the exact
    file the operator should edit, rather than letting the raw
    stderr traceback surface.
  - Secrets checklist keys are derived from the connectors overlay:
    every `*_KEYCHAIN_SERVICE` / `TELEGRAM_BOT_SERVICE` value pulled
    from the just-scaffolded connectors.yaml becomes one line in the
    checklist. Values never leave the file — `setup` reads the file
    and prints `security add-generic-password` commands with the
    service name filled in and `-w '<PROMPT_FOR_VALUE>'` as the
    payload sentinel so the operator sees they have to supply the
    real value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

import typer
import yaml

from mineru_cli.profile import (
    active_symlink_path,
    default_workspace_root,
    legacy_current_symlink_path,
)
from mineru_cli.profile.onboarding import CONNECTORS_PLACEHOLDER_TOKEN


def register_setup(app: typer.Typer) -> None:
    """Register the bare `setup` command on the root Typer app.

    Called once from `app.py` at import time. Bare-command shape (not a
    group) matches `brevity` — `mineru setup` is a top-level guided
    verb, not a namespace with sub-verbs.
    """

    @app.command(
        "setup",
        help=(
            "Guided first-run setup: profile init + install + secrets checklist. "
            "Idempotent — skips init when the target profile already exists."
        ),
        # Panel matches the Band-1 (lifecycle + infra) grouping in `app.py`
        # so the root `--help` clusters `setup` with `profile`, `access`,
        # `people`, `secrets`, `cron`, `custom`, `memory` (2026-09-16
        # audit §D). Within a panel Typer still sorts alphabetically, but
        # the two-panel split is what the audit calls for. (The
        # machine-level human registry sub-app was renamed from `humans`
        # to `people` on the same day; see audit §2A F3.)
        rich_help_panel="Lifecycle & infra",
        context_settings={"help_option_names": ["-h", "--help"]},
    )
    def setup(
        ctx: typer.Context,
        name: Optional[str] = typer.Option(
            None,
            "--name",
            metavar="NAME",
            help=(
                "Profile name to init (lowercase kebab-case). Forwarded to "
                "`profile init --name`. Prompted for when omitted."
            ),
        ),
        persona: Optional[str] = typer.Option(
            None,
            "--persona",
            metavar="NAME",
            help="Persona display name. Forwarded to `profile init --persona`.",
        ),
        owner: Optional[str] = typer.Option(
            None,
            "--owner",
            metavar="HANDLE",
            help=(
                "Existing owner handle in humans.yaml. Forwarded to "
                "`profile init --owner`. Mutually exclusive with the "
                "--owner-new-* trio."
            ),
        ),
        owner_new_handle: Optional[str] = typer.Option(
            None,
            "--owner-new-handle",
            metavar="HANDLE",
            help="Register a NEW human inline as the owner (needs the trio).",
        ),
        owner_new_display: Optional[str] = typer.Option(
            None,
            "--owner-new-display",
            metavar="NAME",
            help="Display name for the inline-added owner.",
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
            help="IANA timezone. Defaults to the machine timezone.",
        ),
        target: Optional[Path] = typer.Option(
            None,
            "--target",
            metavar="DIR",
            help=(
                "Target workspace to install into. Defaults to the workspace "
                "root discovered by the profile loader (MINERU_HOME env / "
                "~/.mineru). Forwarded to `profile install --target`."
            ),
        ),
        no_input: bool = typer.Option(
            False,
            "--no-input",
            help=(
                "Never prompt. Forwarded to `profile init --no-input`. "
                "Every required init field must be provided as a flag."
            ),
        ),
    ) -> None:
        """Guided first-run setup: profile init + install + secrets checklist."""
        # Two distinct paths that operators (and this verb) routinely
        # confuse:
        #   - `workspace_root` = where `profiles/`, `humans.yaml`, and
        #     the `active` symlink live (env-controlled via
        #     `MINERU_WORKSPACE_ROOT` / `MINERU_HOME`, resolved by the
        #     profile loader). This is what `profile init` writes into.
        #   - `install_target` = where `profile install` renders the
        #     workspace shape (symlinks + rendered templates). Defaults
        #     to `workspace_root`, overridable via `--target`.
        # The active symlink lookup below always uses `workspace_root`
        # because that is where init put it. Reading from
        # `install_target` was the Sep-16 verify-time bug that made the
        # secrets checklist print "no active profile resolved" when
        # setup was pointed at a target dir separate from the workspace.
        workspace_root = default_workspace_root()
        install_target = target if target is not None else workspace_root

        typer.echo("mineru setup — guided first-run onboarding")
        typer.echo(f"  workspace: {workspace_root}")
        if install_target != workspace_root:
            typer.echo(f"  install target: {install_target}")
        typer.echo("")

        # --- Step 1: profile init (idempotent) ---------------------------
        # `setup` skips init when an `active` (or legacy `current`)
        # symlink already resolves to a real profile dir; that avoids
        # clobbering an existing profile on a re-run.
        if _active_profile_exists(workspace_root):
            typer.echo(
                "[1/3] profile init: skipped — an active profile already exists."
            )
        else:
            typer.echo("[1/3] profile init: scaffolding new profile...")
            init_argv = _build_init_argv(
                name=name,
                persona=persona,
                owner=owner,
                owner_new_handle=owner_new_handle,
                owner_new_display=owner_new_display,
                owner_new_telegram=owner_new_telegram,
                timezone=timezone,
                no_input=no_input,
            )
            init_rc = _dispatch_verb(init_argv)
            if init_rc != 0:
                typer.echo(
                    "\n[setup] profile init failed. Fix the error above and "
                    "re-run `mineru setup`.",
                    err=True,
                )
                raise typer.Exit(code=init_rc)
        typer.echo("")

        # --- Step 2: profile install --apply -----------------------------
        typer.echo("[2/3] profile install --apply: rendering engine templates...")
        install_argv = ["profile", "install", "--target", str(install_target), "--apply"]
        install_rc = _dispatch_verb(install_argv)
        install_failed = install_rc != 0
        if install_failed:
            # Sep 2026 audit §F2 fallback clause: even with the
            # connectors.yaml scaffold in place (closes the specific
            # missing-file gap), install can still fail on unrelated
            # template variables (profile extras like user_pronouns
            # that `profile init` does not populate today). Do NOT let
            # the raw stderr trace be the last thing the operator sees
            # — surface a clear next-steps block naming the file(s)
            # they need to edit.
            _print_install_failure_guidance(workspace_root, install_target)
        typer.echo("")

        # --- Step 3: secrets checklist -----------------------------------
        # Always printed. Even on install failure the operator will need
        # the checklist eventually; printing it now saves a second run.
        typer.echo("[3/3] Keychain secrets checklist (nothing written to disk):")
        _print_secrets_checklist(workspace_root)

        # Exit rc propagates install's rc so scripts / CI can gate on it.
        # `setup` does not fail on install failure so the operator sees
        # the checklist too — the guidance block above already surfaced
        # the failure with the clear next steps.
        if install_failed:
            raise typer.Exit(code=install_rc)


def _active_profile_exists(workspace_root: Path) -> bool:
    """True iff `<workspace_root>/active` (or legacy `current`) resolves.

    A dangling symlink counts as absent — the profile it points at was
    removed and setup should re-scaffold rather than die on the missing
    profile dir. Any resolution error is treated as "not present" to
    keep this check cheap; `profile init` will fail loud downstream if
    the workspace itself is unreadable.
    """
    for symlink in (
        active_symlink_path(workspace_root),
        legacy_current_symlink_path(workspace_root),
    ):
        try:
            if symlink.is_symlink() and symlink.resolve(strict=True).is_dir():
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _build_init_argv(
    *,
    name: Optional[str],
    persona: Optional[str],
    owner: Optional[str],
    owner_new_handle: Optional[str],
    owner_new_display: Optional[str],
    owner_new_telegram: Optional[int],
    timezone: Optional[str],
    no_input: bool,
) -> List[str]:
    """Assemble `profile init` argv, forwarding only the flags the operator set.

    Omitting a `None` flag matters — passing `--name ""` (empty string)
    to `profile init` fails a validation check that would not fire if
    the flag were left out entirely. Same for the owner-new-* trio,
    which must be all-present or all-absent per the init verb.
    """
    argv: List[str] = ["profile", "init"]
    if name is not None:
        argv += ["--name", name]
    if persona is not None:
        argv += ["--persona", persona]
    if owner is not None:
        argv += ["--owner", owner]
    if owner_new_handle is not None:
        argv += ["--owner-new-handle", owner_new_handle]
    if owner_new_display is not None:
        argv += ["--owner-new-display", owner_new_display]
    if owner_new_telegram is not None:
        argv += ["--owner-new-telegram", str(owner_new_telegram)]
    if timezone is not None:
        argv += ["--timezone", timezone]
    if no_input:
        # `--no-input` implies `--skip-integrations` on `profile init`
        # (that verb's own convention). Forward both so setup does not
        # accidentally open a browser during a scripted `--no-input`
        # run.
        argv += ["--no-input", "--skip-integrations"]
    return argv


def _dispatch_verb(argv: List[str]) -> int:
    """Invoke the top-level `mineru` app with argv IN-PROCESS.

    In-process (via `CliRunner`) is deliberate: it keeps setup a single
    Python process (no shell spawn overhead, no PATH resolution to
    worry about, no `.venv` activation confusion), and the invoked
    verb inherits the exact same env — every `MINERU_*` env var setup
    already resolved.

    Returns the exit code. Typer's `CliRunner` (0.20+) separates stdout
    and stderr by default; both are relayed straight back to the
    surrounding terminal so the operator sees the same output they
    would running the verb by hand — no swallowed error frames.
    Any uncaught exception (`catch_exceptions=False`) surfaces the
    real traceback rather than a swallowed Click-error-frame.
    """
    from typer.testing import CliRunner

    from mineru_cli.app import app

    runner = CliRunner()
    result = runner.invoke(app, argv, catch_exceptions=False)
    # Stream both channels back to the caller so the operator sees the
    # embedded verb's [ERROR] frames, prompts (echoed as inputs),
    # summary panels, etc. Empty strings guarded so we do not emit a
    # blank trailing newline.
    if result.stdout:
        typer.echo(result.stdout, nl=False)
    stderr_text = _safe_stderr(result)
    if stderr_text:
        typer.echo(stderr_text, err=True, nl=False)
    return int(result.exit_code)


def _safe_stderr(result: Any) -> str:
    """Read `result.stderr` without exploding on older Typer wrappers.

    Older `click.testing.Result` raised `ValueError` on `.stderr` when
    stdout/stderr were mixed. Newer Typer versions separate them by
    default and always expose `.stderr`; this helper is the belt so a
    Typer bump does not silently break the setup verb.
    """
    try:
        return result.stderr or ""
    except (AttributeError, ValueError):
        return ""


def _print_install_failure_guidance(
    workspace_root: Path, install_target: Path
) -> None:
    """Emit the audit-mandated clear-next-steps block when install fails.

    Any of these is possible on a `setup` install failure:
      1. `connectors.yaml` scaffold contains unedited `REPLACE_ME__*`
         placeholders (setup treats these as fine for the render — they
         are real strings — but the operator will want to edit).
      2. A template references a profile extra the current init flow
         does not scaffold (e.g. `user_pronouns` -> `USER_POSSESSIVE`).
      3. Some other install error — surfaced by the install verb's own
         stderr frame above; the guidance here still points at the
         two files the operator can hand-edit to make progress.

    The block is deliberately terse: install's own error frame already
    named the specific missing variable / bad path; this block adds
    the "which file do I edit" pointer install cannot give (because
    the file lives on the operator's disk, not in the engine tree).
    """
    profile_root = _resolve_active_profile_root(workspace_root)
    typer.echo("", err=True)
    typer.echo("[setup] profile install did not complete. Next steps:", err=True)
    if profile_root is not None:
        typer.echo(
            f"  1. Edit {profile_root}/connectors.yaml — every value tagged "
            f"'{CONNECTORS_PLACEHOLDER_TOKEN}__*' is a placeholder that "
            "needs your real Google/Tailscale/Telegram values.",
            err=True,
        )
        typer.echo(
            f"  2. Edit {profile_root}/profile.yaml — add profile extras "
            "the shipped templates reference (`user_pronouns`, "
            "`user_full_name`, `user_location`, `user_dev_root`, "
            "`user_claude_home`, `persona_emoji`, `persona_origin`, "
            "`persona_kind`). Any unknown-variable error above names "
            "the exact key.",
            err=True,
        )
        typer.echo(
            f"  3. Re-run `mineru profile install --target {install_target} --apply`.",
            err=True,
        )
    else:
        typer.echo(
            "  1. Check the profile dir under "
            f"{workspace_root}/profiles/ and fix any file the "
            "install error above named.",
            err=True,
        )
        typer.echo(
            f"  2. Re-run `mineru profile install --target {install_target} --apply`.",
            err=True,
        )


def _resolve_active_profile_root(workspace_root: Path) -> Optional[Path]:
    """Return the `profiles/<active>` dir if a symlink resolves; else None.

    Mirrors `_active_profile_exists` but returns the dir (not just a
    boolean) so the failure-guidance block can name the file paths
    the operator needs to edit.
    """
    for symlink in (
        active_symlink_path(workspace_root),
        legacy_current_symlink_path(workspace_root),
    ):
        try:
            if symlink.is_symlink():
                resolved = symlink.resolve(strict=True)
                if resolved.is_dir():
                    return resolved
        except (OSError, RuntimeError):
            continue
    return None


def _print_secrets_checklist(workspace_root: Path) -> None:
    """Emit `security add-generic-password` commands for every Keychain slot.

    Reads the scaffolded `connectors.yaml` and pulls every value that
    reads as a Keychain SERVICE name (the `TELEGRAM_BOT_SERVICE` key
    plus anything ending in `_KEYCHAIN_SERVICE`). Prints one shell
    line per service with `-w '<PROMPT_FOR_VALUE>'` as the payload
    sentinel — the operator's own hands supply the real value.

    Never touches the Keychain, never reads a secret from anywhere.
    This is a printout, not an action. Matches SECURITY.md's "Never
    Store Plaintext Secrets on Disk" — the disk is the operator's
    Keychain, populated by their own hand-typed values.
    """
    profile_root = _resolve_active_profile_root(workspace_root)
    if profile_root is None:
        typer.echo(
            "  (no active profile resolved — skipping checklist; "
            "`mineru profile show` after fixing setup)"
        )
        return
    connectors_path = profile_root / "connectors.yaml"
    if not connectors_path.exists():
        typer.echo(f"  (no connectors.yaml at {connectors_path} — nothing to print)")
        return

    try:
        body = yaml.safe_load(connectors_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        typer.echo(
            f"  (could not read {connectors_path} — fix the file and re-run setup)"
        )
        return
    if not isinstance(body, dict):
        typer.echo(f"  (unexpected shape in {connectors_path} — skipping checklist)")
        return

    # Pull keychain-shaped service names. `TELEGRAM_BOT_SERVICE` is the
    # canonical daemon slot; `*_KEYCHAIN_SERVICE` matches the pattern
    # in `KEYCHAIN_SERVICES` list entries too. Order preserved so the
    # operator sees the same layout as the file.
    services: List[str] = []
    for key, value in body.items():
        if not isinstance(value, str):
            continue
        if key == "TELEGRAM_BOT_SERVICE" or key.endswith("_KEYCHAIN_SERVICE"):
            services.append(value)

    if not services:
        typer.echo(
            "  (no Keychain service names found in connectors.yaml — "
            "add `TELEGRAM_BOT_SERVICE: <name>` after filling placeholders)"
        )
        return

    typer.echo(
        "  Run each `security add-generic-password` command below in a "
        "terminal, then re-run `mineru setup` (or `mineru access sync --apply`)."
    )
    typer.echo("  Setup NEVER writes secret values — you type each real value yourself.")
    typer.echo("")
    for service in services:
        typer.echo(
            f"    security add-generic-password -a mineru -s {service} "
            "-w '<PROMPT_FOR_VALUE>' -U"
        )


__all__ = ["register_setup"]
