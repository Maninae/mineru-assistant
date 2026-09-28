"""`mineru access` sub-app (Phase 1 multi-profile framework + Arc 2 step 4).

Per-profile access allowlist. Wired verbs today:
  - `show`   — render the access.yaml for the active profile.
  - `add`    — add an authorized human to access.yaml (dry-run default;
               with `--apply` writes YAML + refreshes Keychain slot).
  - `remove` — remove an authorized human from access.yaml (dry-run
               default; with `--apply` writes YAML + refreshes Keychain).
  - `sync`   — resolve the allowlist to Telegram user ids and export
               the canonical comma-string into the macOS Keychain slot
               `telegram-allowed-chat-ids` under the active profile's
               `keychain_account`. DRY-RUN by default; `--apply` runs
               `security add-generic-password -U`. NEVER writes the
               live Keychain in tests (subprocess runner is injectable
               and every test mocks it).
  - `status` — read the Keychain slot, parse it with the daemon's
               semantics (`_parse_int_set` in landline.runtime.guard),
               diff against the resolved allowlist, print the drift.

Keychain contract (must match `landline.runtime.guard._parse_int_set`):

  - SERVICE = "telegram-allowed-chat-ids" (fixed, matches the daemon)
  - ACCOUNT = <Profile.keychain_account> (per-profile — "landline",
              "mineru", "gemini-scout", etc.)
  - FORMAT  = comma-separated decimal integer Telegram USER ids
              (`from.id` on each inbound message; equals `chat.id` on a
              1:1 owner bot, which is why existing single-value slots
              keep working). Empty string parses to empty set — fail-
              closed.

Zero-import seam: this module NEVER imports from `landline.*`. The
Landline daemon reads the Keychain slot the CLI writes; the daemon
never reciprocally imports engine code. Preserving the one-way seam
means the daemon can ship independently of the engine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer
import yaml

from mineru_cli.access import (
    AccessConfig,
    AccessEntry,
    AccessError,
    AccessTier,
    AllowlistExportError,
    default_access_yaml_path,
    diff_allowlist,
    format_allowlist_value,
    load_access_config,
    read_keychain_allowlist,
    resolve_allowlist_ids,
    write_keychain_allowlist,
)
from mineru_cli.humans import HumansError, load_humans_registry
from mineru_cli.profile import get_profile

access_app = typer.Typer(
    name="access",
    help=(
        "Per-profile access allowlist. Shows who can reach the active "
        "agent (owner + authorized humans), and syncs the resolved "
        "Telegram-id set into the macOS Keychain slot the Landline "
        "daemon reads."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_profile_and_access(ctx: typer.Context):
    """Load active profile + humans registry + access config, or die.

    Every verb below opens with this pair of loads; extracting it means
    error framing stays identical across `show` / `sync` / `status` /
    `add` / `remove`.
    """
    active = get_profile(ctx)
    try:
        humans = load_humans_registry()
    except HumansError as exc:
        typer.echo(f"mineru access: {exc}", err=True)
        raise typer.Exit(code=2)
    try:
        access = load_access_config(active, humans)
    except AccessError as exc:
        typer.echo(f"mineru access: {exc}", err=True)
        raise typer.Exit(code=2)
    return active, humans, access


def _render_access_yaml(access: AccessConfig) -> str:
    """Serialize an `AccessConfig` back to canonical access.yaml text.

    Preserves the shape the onboarding scaffold + `mineru access show`
    both render, so a diff between a hand-edited file and one this
    writer produced is minimal (only entries change).
    """
    lines: List[str] = []
    lines.append(
        f"# Per-profile access allowlist for the {access.profile_name!r} agent.\n"
        "#\n"
        "# Guest-tier enforcement is DEFERRED to a later phase. When it\n"
        "# lands, guest entries will be added here with `tier: guest`\n"
        "# and Landline will scope their toolset accordingly.\n"
        "\n"
    )
    lines.append(f"owner: {access.owner}\n")
    lines.append("authorized:\n")
    for entry in access.authorized:
        lines.append(
            f"  - {{human: {entry.human}, tier: {entry.tier.value}}}\n"
        )
    return "".join(lines)


def _write_access_yaml_atomic(path: Path, body: str) -> None:
    """Serialize `body` to `path` via tmp+rename (atomic on POSIX).

    A concurrent reader sees either the pre-swap contents or the
    post-swap contents, never a half-written file. Mirrors the humans
    loader's `_atomic_write_humans_yaml` shape.
    """
    import os
    import time as _time
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{_time.time_ns()}"
    tmp.write_text(body, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _print_sync_plan(
    profile_name: str,
    keychain_account: str,
    resolved_ids: List[int],
    canonical: str,
    diff,
    *,
    dry_run: bool,
    applied: bool = False,
    write_rc: Optional[int] = None,
) -> None:
    """Human-readable render of a sync plan / result.

    Same layout for dry-run and applied so the operator can eyeball the
    plan first, then `--apply` and see the same shape confirming the
    write landed.
    """
    typer.echo(f"profile:           {profile_name}")
    typer.echo(f"keychain account:  {keychain_account}")
    typer.echo(f"keychain service:  telegram-allowed-chat-ids")
    typer.echo(f"resolved ids:      {resolved_ids or '(none)'}")
    typer.echo(f"canonical value:   {canonical!r}")
    if diff.in_sync:
        typer.echo("state:             in sync — Keychain already matches")
    elif not resolved_ids:
        typer.echo(
            "state:             DRIFT — empty allowlist would FAIL-CLOSE "
            "the daemon (block every sender)."
        )
    else:
        parts: List[str] = []
        if diff.missing_from_keychain:
            parts.append(f"missing_from_keychain={diff.missing_from_keychain}")
        if diff.extra_in_keychain:
            parts.append(f"extra_in_keychain={diff.extra_in_keychain}")
        if not parts and not diff.in_sync:
            parts.append(
                "keychain payload is non-canonical (extra whitespace, "
                "junk tokens, or unsorted); apply rewrites to the canonical form."
            )
        typer.echo(f"state:             DRIFT — {'; '.join(parts)}")
    if dry_run and not applied:
        typer.echo(
            "action:            DRY-RUN — nothing written. Pass --apply "
            "to update the Keychain slot."
        )
    elif applied:
        typer.echo(
            f"action:            APPLIED — Keychain updated (rc={write_rc})."
        )


# ---------------------------------------------------------------------------
# READ: show + status
# ---------------------------------------------------------------------------


@access_app.command("show")
def show(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False, "--json", help="Emit the access config as JSON."
    ),
) -> None:
    """Show the access allowlist for the active profile."""
    _, humans, access = _load_profile_and_access(ctx)
    active = get_profile(ctx)
    obj = ctx.obj or {}
    use_json = bool(json_out or obj.get("json"))

    if use_json:
        payload = {
            "profile_name": access.profile_name,
            "owner": access.owner,
            "authorized": [
                {"human": e.human, "tier": e.tier.value}
                for e in access.authorized
            ],
            "access_yaml_path": str(default_access_yaml_path(active)),
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=False))
        return

    typer.echo(f"profile:    {access.profile_name}")
    typer.echo(f"owner:      {access.owner}")
    typer.echo("authorized:")
    for e in access.authorized:
        typer.echo(f"  - {e.human}  ({e.tier.value})")
    guest_count = len(access.guest_entries())
    typer.echo(
        f"({guest_count} guest{'s' if guest_count != 1 else ''}; "
        "guest-tier enforcement DEFERRED to a later phase)"
    )


@access_app.command("status")
def status(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False, "--json", help="Emit the diff as JSON."
    ),
) -> None:
    """Read the Keychain allowlist slot and diff it against the resolved
    access.yaml.

    Reports the concrete drift: which resolved Telegram ids are absent
    from the daemon-visible slot, which ids the daemon sees that no
    longer resolve from access.yaml (usually a revoked human), and
    whether the slot's raw string is in canonical form.

    Never writes the Keychain. `sync --apply` is the write path.
    """
    active, humans, access = _load_profile_and_access(ctx)
    obj = ctx.obj or {}
    use_json = bool(json_out or obj.get("json"))

    try:
        resolved_ids = resolve_allowlist_ids(access, humans)
    except AllowlistExportError as exc:
        typer.echo(f"mineru access status: {exc}", err=True)
        raise typer.Exit(code=2)

    kc_account = active.keychain_account
    kc_read = read_keychain_allowlist(kc_account)
    drift = diff_allowlist(resolved_ids, kc_read)

    if use_json:
        payload = {
            "profile_name": access.profile_name,
            "keychain_account": kc_account,
            "keychain_service": "telegram-allowed-chat-ids",
            "keychain_present": kc_read.present,
            "keychain_rc": kc_read.rc,
            "resolved_ids": drift.resolved_ids,
            "keychain_ids": drift.keychain_ids,
            "missing_from_keychain": drift.missing_from_keychain,
            "extra_in_keychain": drift.extra_in_keychain,
            "in_sync": drift.in_sync,
            "canonical_value": drift.canonical_value,
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=False))
        return

    typer.echo(f"profile:            {access.profile_name}")
    typer.echo(f"keychain account:   {kc_account}")
    typer.echo(f"keychain service:   telegram-allowed-chat-ids")
    typer.echo(f"keychain present:   {kc_read.present} (rc={kc_read.rc})")
    if kc_read.stderr_snippet and not kc_read.present:
        typer.echo(f"keychain note:      {kc_read.stderr_snippet}")
    typer.echo(f"resolved (yaml):    {drift.resolved_ids or '(none)'}")
    typer.echo(f"keychain (daemon):  {drift.keychain_ids or '(none)'}")
    if drift.missing_from_keychain:
        typer.echo(
            f"missing on daemon:  {drift.missing_from_keychain}  "
            "(sender would be blocked)"
        )
    if drift.extra_in_keychain:
        typer.echo(
            f"extra on daemon:    {drift.extra_in_keychain}  "
            "(unauthorized sender still allowed)"
        )
    if drift.in_sync:
        typer.echo("state:              in sync")
    else:
        typer.echo(
            "state:              DRIFT — run `mineru access sync --apply` "
            "to write the canonical value."
        )


# ---------------------------------------------------------------------------
# WRITE: sync (dry-run default; --apply writes Keychain)
# ---------------------------------------------------------------------------


@access_app.command("sync")
def sync(
    ctx: typer.Context,
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Actually write the Keychain slot. Without this flag, `sync` "
            "runs DRY-RUN and prints the plan without touching anything."
        ),
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the plan/result as JSON."
    ),
) -> None:
    """Export the resolved allowlist to the macOS Keychain slot.

    Writes `security add-generic-password -U -a <keychain_account> -s
    telegram-allowed-chat-ids -w <canonical>` when `--apply` is passed.
    Without `--apply`, prints the plan (resolved ids, canonical string,
    drift vs current slot, next action) and exits 0.

    Exit codes:
      0 — plan printed (dry-run) OR apply succeeded.
      2 — resolve failure (bad humans.yaml / access.yaml), or, with
          `--apply`, the `security` write returned non-zero.
    """
    active, humans, access = _load_profile_and_access(ctx)
    obj = ctx.obj or {}
    use_json = bool(json_out or obj.get("json"))

    try:
        resolved_ids = resolve_allowlist_ids(access, humans)
    except AllowlistExportError as exc:
        typer.echo(f"mineru access sync: {exc}", err=True)
        raise typer.Exit(code=2)

    canonical = format_allowlist_value(resolved_ids)
    kc_account = active.keychain_account
    kc_read = read_keychain_allowlist(kc_account)
    drift = diff_allowlist(resolved_ids, kc_read)

    if not apply:
        if use_json:
            payload = {
                "profile_name": access.profile_name,
                "keychain_account": kc_account,
                "keychain_service": "telegram-allowed-chat-ids",
                "resolved_ids": resolved_ids,
                "canonical_value": canonical,
                "in_sync": drift.in_sync,
                "missing_from_keychain": drift.missing_from_keychain,
                "extra_in_keychain": drift.extra_in_keychain,
                "action": "dry-run",
            }
            typer.echo(json.dumps(payload, indent=2, sort_keys=False))
            return
        _print_sync_plan(
            access.profile_name, kc_account, resolved_ids, canonical, drift,
            dry_run=True,
        )
        return

    # --apply path.
    write = write_keychain_allowlist(kc_account, canonical)
    if use_json:
        payload = {
            "profile_name": access.profile_name,
            "keychain_account": kc_account,
            "keychain_service": "telegram-allowed-chat-ids",
            "resolved_ids": resolved_ids,
            "canonical_value": canonical,
            "action": "applied" if write.ok else "failed",
            "rc": write.rc,
            "stderr": write.stderr_snippet,
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=False))
        if not write.ok:
            raise typer.Exit(code=2)
        return

    if not write.ok:
        typer.echo(
            f"mineru access sync: keychain write failed (rc={write.rc}). "
            f"{write.stderr_snippet}",
            err=True,
        )
        raise typer.Exit(code=2)
    _print_sync_plan(
        access.profile_name, kc_account, resolved_ids, canonical, drift,
        dry_run=False, applied=True, write_rc=write.rc,
    )


# ---------------------------------------------------------------------------
# WRITE: add / remove (edits access.yaml; refreshes Keychain when --apply)
# ---------------------------------------------------------------------------


@access_app.command("add")
def add(
    ctx: typer.Context,
    handle: str = typer.Argument(
        ..., help="humans.yaml handle to authorize on this profile."
    ),
    tier: str = typer.Option(
        AccessTier.GUEST.value,
        "--tier",
        help=(
            "Tier for the new entry. `guest` is the only sensible value "
            "here (owner is set on profile init and cannot be reassigned "
            "via `add`). Guest-tier enforcement is DEFERRED to a later "
            "phase — the entry lands in access.yaml today but is not yet "
            "gated by Landline."
        ),
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Actually mutate access.yaml AND refresh the Keychain slot "
            "(one `security add-generic-password -U`). Without this flag "
            "the verb runs DRY-RUN, printing the plan without touching "
            "the filesystem or the Keychain."
        ),
    ),
) -> None:
    """Add an authorized human to the active profile's access.yaml.

    Dry-run default. With `--apply`:
      1. Rewrite <profile_root>/access.yaml atomically with the new
         `{human, tier}` entry appended.
      2. Refresh the Keychain slot via `sync --apply` semantics so the
         Landline daemon sees the new sender on next-cycle cache expiry.

    Rejects unknown handles, duplicate handles already in `authorized:`,
    and `--tier owner` (the owner is set at `profile init` time and is
    not reassignable here).
    """
    active, humans, access = _load_profile_and_access(ctx)

    try:
        tier_enum = AccessTier(tier)
    except ValueError:
        typer.echo(
            f"mineru access add: unknown tier {tier!r}. Legal tiers: "
            f"{[t.value for t in AccessTier]}.",
            err=True,
        )
        raise typer.Exit(code=2)
    if tier_enum == AccessTier.OWNER:
        typer.echo(
            "mineru access add: `--tier owner` is not allowed here. Owner "
            "is set at `mineru profile init` time and cannot be "
            "reassigned via `access add`.",
            err=True,
        )
        raise typer.Exit(code=2)
    if handle not in humans:
        typer.echo(
            f"mineru access add: handle {handle!r} not found in humans.yaml. "
            f"Register the human first (or pick one of {humans.handles()}).",
            err=True,
        )
        raise typer.Exit(code=2)
    if any(e.human == handle for e in access.authorized):
        typer.echo(
            f"mineru access add: handle {handle!r} is already authorized on "
            f"profile {access.profile_name!r}.",
            err=True,
        )
        raise typer.Exit(code=2)

    new_authorized: List[AccessEntry] = list(access.authorized) + [
        AccessEntry(human=handle, tier=tier_enum)
    ]
    new_config = AccessConfig(
        profile_name=access.profile_name,
        owner=access.owner,
        authorized=new_authorized,
    )

    # Show the operator both what would land in access.yaml AND what
    # would land on the Keychain slot — the sync happens in the same
    # apply pass so a dry-run shows the full downstream effect.
    try:
        resolved_after = resolve_allowlist_ids(new_config, humans)
    except AllowlistExportError as exc:
        typer.echo(f"mineru access add: {exc}", err=True)
        raise typer.Exit(code=2)
    canonical_after = format_allowlist_value(resolved_after)

    typer.echo(f"profile:           {access.profile_name}")
    typer.echo(f"access.yaml:       {default_access_yaml_path(active)}")
    typer.echo(f"add:               {handle}  (tier={tier_enum.value})")
    typer.echo(f"resolved (after):  {resolved_after}")
    typer.echo(f"canonical value:   {canonical_after!r}")

    if not apply:
        typer.echo(
            "action:            DRY-RUN — access.yaml NOT written and "
            "Keychain NOT refreshed. Pass --apply to commit."
        )
        return

    # Apply: write access.yaml atomically, then refresh Keychain.
    _write_access_yaml_atomic(
        default_access_yaml_path(active), _render_access_yaml(new_config)
    )
    write = write_keychain_allowlist(active.keychain_account, canonical_after)
    if not write.ok:
        typer.echo(
            f"mineru access add: access.yaml updated but Keychain write "
            f"failed (rc={write.rc}). {write.stderr_snippet}",
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(
        f"action:            APPLIED — access.yaml updated, Keychain "
        f"refreshed (rc={write.rc})."
    )


@access_app.command("remove")
def remove(
    ctx: typer.Context,
    handle: str = typer.Argument(
        ..., help="Authorized human handle to revoke on this profile."
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Actually rewrite access.yaml AND refresh the Keychain slot. "
            "Without this flag the verb runs DRY-RUN."
        ),
    ),
) -> None:
    """Remove an authorized human from the active profile's access.yaml.

    Dry-run default. Refuses to remove the owner (owner is set at
    `profile init` time; delete + re-init if you want to hand a profile
    over to a different owner). With `--apply`:
      1. Rewrite <profile_root>/access.yaml with the entry gone.
      2. Refresh the Keychain slot so the daemon stops accepting that
         sender on the next cache-expiry cycle.
    """
    active, humans, access = _load_profile_and_access(ctx)

    if handle == access.owner:
        typer.echo(
            f"mineru access remove: cannot remove the profile owner "
            f"({handle!r}). Owner is set at `mineru profile init` time; "
            "delete + re-init the profile to change owners.",
            err=True,
        )
        raise typer.Exit(code=2)

    matching = [e for e in access.authorized if e.human == handle]
    if not matching:
        typer.echo(
            f"mineru access remove: handle {handle!r} is not currently "
            f"authorized on profile {access.profile_name!r}.",
            err=True,
        )
        raise typer.Exit(code=2)

    new_authorized: List[AccessEntry] = [
        e for e in access.authorized if e.human != handle
    ]
    new_config = AccessConfig(
        profile_name=access.profile_name,
        owner=access.owner,
        authorized=new_authorized,
    )

    try:
        resolved_after = resolve_allowlist_ids(new_config, humans)
    except AllowlistExportError as exc:
        typer.echo(f"mineru access remove: {exc}", err=True)
        raise typer.Exit(code=2)
    canonical_after = format_allowlist_value(resolved_after)

    typer.echo(f"profile:           {access.profile_name}")
    typer.echo(f"access.yaml:       {default_access_yaml_path(active)}")
    typer.echo(f"remove:            {handle}")
    typer.echo(f"resolved (after):  {resolved_after}")
    typer.echo(f"canonical value:   {canonical_after!r}")

    if not apply:
        typer.echo(
            "action:            DRY-RUN — access.yaml NOT written and "
            "Keychain NOT refreshed. Pass --apply to commit."
        )
        return

    _write_access_yaml_atomic(
        default_access_yaml_path(active), _render_access_yaml(new_config)
    )
    write = write_keychain_allowlist(active.keychain_account, canonical_after)
    if not write.ok:
        typer.echo(
            f"mineru access remove: access.yaml updated but Keychain write "
            f"failed (rc={write.rc}). {write.stderr_snippet}",
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(
        f"action:            APPLIED — access.yaml updated, Keychain "
        f"refreshed (rc={write.rc})."
    )
