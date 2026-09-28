"""`mineru secrets` sub-app.

Foundation status: `get`, `audit`, `set`, and `list` are wired.
`rotate` remains a hidden stub (needs per-backend logic that ships once
the 1Password backend lands).

`set` writes into the ACTIVE profile's Keychain namespace via
`mineru_cli.secrets.writer.write_keychain_secret`. Value input never
crosses argv: it comes from an interactive prompt (getpass, echo
suppressed) OR from stdin when `--from-stdin` is passed.

`list` enumerates the secret NAMES the active profile declares in
`<profile_root>/connectors.yaml` under `KEYCHAIN_SERVICES` (each entry
is a mapping with `service:` and an optional `purpose:`). It NEVER
reads or prints values — that would violate the whole read-permission
model that makes `secrets audit` safe to log.

Naming note: the enumeration verb was called `ls` in the P1 skeleton.
The 2026-09-16 audit §2C standardized every enumeration verb in the
tree on the spelled-out `list` (only `drive ls` keeps the filesystem
metaphor); renamed here on 2026-09-16, with `ls` kept as a hidden
alias that emits a one-line stderr deprecation notice for ~90 days.

Security invariants (see mineru_cli/secrets/*.py for enforcement):
  - `get` writes ONLY the resolved value to stdout on success. On miss
    it exits 2 with a hint on stderr; the value is never logged.
  - `audit` never emits values, ever — the row shape is
    `{name, resolved_by, present}`. `--json` produces that structure
    verbatim; without `--json` we render a two-column presence table.
  - `set` never echoes the value. The prompt path uses `getpass`; the
    stdin path reads and immediately hands the string to the writer.
    Failure paths print `stderr_snippet` (which itself never carries
    the value; see `writer.write_keychain_secret`).
  - `list` never reads values. It reports names + presence only.
"""

from __future__ import annotations

import getpass
import json
import sys
from typing import List, Optional

import typer

from mineru_cli._deprecation import emit_rename_notice
from mineru_cli._stub import not_yet_implemented
from mineru_cli.secrets import (
    KeychainWriteError,
    SecretsConfig,
    build_resolver,
    write_keychain_secret,
)

secrets_app = typer.Typer(
    name="secrets",
    help=(
        "Secret access (env then macOS Keychain; 1Password later behind the same "
        "seam). Read + write: get, audit, set, list."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _resolver_for_ctx(ctx: typer.Context):
    """Build the resolver from the active profile's SecretsConfig.

    Lazy hydration (2026-08-28 rev): the F3 profile layer no longer
    hydrates the profile in the root callback. Calling `get_profile(ctx)`
    here on the first `mineru secrets` invocation loads the active
    profile, validates it, and caches both the `Profile` and its derived
    `SecretsConfig` on `ctx.obj`. Any loader failure (no active profile,
    `--profile bogus`, malformed yaml) fails loud with a clean CLI frame.

    After hydration, `ctx.obj["secrets_config"]` holds the config; we
    read it back and build the resolver so `mineru secrets get / audit`
    walks the profile's configured backend chain (not the module
    defaults).

    Fallback to `SecretsConfig()` is only for callsites that construct a
    Typer context without going through the root callback (unit tests)
    where `get_profile` might not populate the config.
    """
    from mineru_cli.profile import get_profile

    get_profile(ctx)
    obj = ctx.obj or {}
    config = obj.get("secrets_config") or SecretsConfig()
    return build_resolver(config)


@secrets_app.command("get")
def get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name to read."),
) -> None:
    """Resolve a secret via the backend chain (env then keychain).

    On hit: writes ONLY the value to stdout (with one trailing newline
    for shell-pipe ergonomics). On miss: exits with code 2 and prints a
    short hint on stderr — never the secret material.
    """
    resolver = _resolver_for_ctx(ctx)
    result = resolver.resolve(name)
    if not result.present:
        typer.echo(
            f"mineru secrets get: no backend resolved {name!r}. "
            f"Run `mineru secrets audit {name}` to see the chain.",
            err=True,
        )
        raise typer.Exit(code=2)
    # Value on stdout, nothing else. The trailing newline matches how
    # `security -w` (and most CLI secret readers) behave; downstream
    # consumers typically strip it before use.
    sys.stdout.write(result.value + "\n")


@secrets_app.command("audit")
def audit(
    ctx: typer.Context,
    names: Optional[List[str]] = typer.Argument(
        None, help="Secret names to audit (0 or more)."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the audit report as JSON."
    ),
) -> None:
    """Per-name presence report — never emits values.

    Foundation-increment shape: pass one or more secret names as
    positional arguments. A future increment will hydrate the expected
    list from the profile's `connectors.yaml` so `mineru secrets audit`
    (no args) becomes the full missing-item report described in §2.11.
    """
    resolver = _resolver_for_ctx(ctx)
    names = names or []
    # Use `to_audit_row()` (explicit projection) rather than
    # `dataclasses.asdict(...)` so the serialized shape is
    # `{name, present, resolved_by}` — the `value` key is not merely
    # `None` but structurally absent. This is the last line of defence
    # against a future seam change silently including a value key.
    rows = [r.to_audit_row() for r in resolver.audit(names)]
    if json_out:
        typer.echo(json.dumps(rows))
        return
    if not rows:
        typer.echo(
            "mineru secrets audit: no names given. Pass one or more secret "
            "names (e.g. `mineru secrets audit telegram-bot-token`)."
        )
        return
    for row in rows:
        status = "present" if row["present"] else "MISSING"
        source = row["resolved_by"] or "-"
        typer.echo(f"  {row['name']:32s}  {status:8s}  {source}")


@secrets_app.command("set")
def set_(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name to write."),
    from_stdin: bool = typer.Option(
        False,
        "--from-stdin",
        help=(
            "Read the value from STDIN instead of prompting. The whole "
            "stdin stream (minus a single trailing newline) becomes the "
            "value. Handy for piped writes: `pbpaste | mineru secrets "
            "set foo --from-stdin`. Value NEVER crosses argv."
        ),
    ),
) -> None:
    """Write a named secret to the active profile's macOS Keychain slot.

    Value input:
      - Default (interactive): `getpass.getpass()` reads the value with
        terminal echo off, so the value never renders on the screen.
      - `--from-stdin`: read the entire stdin stream, strip ONE trailing
        newline, and use that as the value. This is the sanctioned
        piped-write path (`echo -n VALUE | mineru secrets set foo
        --from-stdin`).

    Writes go to the profile's `keychain_account` namespace under
    service name `<name>`, using `security add-generic-password -U`
    (upsert: creates on first write, updates in place after). The
    argument list handed to `security` carries the value in `-w
    <value>` — argv is unavoidable for `security`, but no other tool
    in this codepath ever sees the value, nor does any log or file.

    Fails loud (exit 2) on: bad-shape name, empty value, unwritable
    Keychain (locked, timeout, missing `security` binary, non-zero
    exit). Never prints the value on any path, success or failure.
    """
    active = _profile_for_ctx(ctx)
    if from_stdin:
        raw = sys.stdin.read()
        # `.read()` returns everything through EOF. Strip exactly ONE
        # trailing newline so `echo -n VALUE` and `echo VALUE` both do
        # the intuitive thing (both write VALUE, not VALUE + '\n').
        value = raw[:-1] if raw.endswith("\n") else raw
    else:
        # `getpass` echoes nothing; empty entry raises loudly below
        # (never proceed with an empty value).
        try:
            value = getpass.getpass(f"Value for secret {name!r} (hidden): ")
        except (EOFError, KeyboardInterrupt):
            typer.echo(
                "mineru secrets set: no value entered — aborting without "
                "writing anything.",
                err=True,
            )
            raise typer.Exit(code=2)

    if not value:
        typer.echo(
            "mineru secrets set: refusing to write an EMPTY value. "
            "Pass a non-empty value on stdin or at the prompt.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        result = write_keychain_secret(
            name=name,
            value=value,
            account=active.keychain_account,
        )
    except (KeychainWriteError, ValueError) as exc:
        # Caller-bug errors (bad name shape, empty value that slipped
        # past the guard). The exception message never carries the
        # value — see `writer.write_keychain_secret` invariants.
        typer.echo(f"mineru secrets set: {exc}", err=True)
        raise typer.Exit(code=2)

    if not result.ok:
        typer.echo(
            f"mineru secrets set: failed to write {name!r} to Keychain "
            f"(rc={result.rc}, account={active.keychain_account!r}). "
            f"stderr: {result.stderr_snippet or '<empty>'}",
            err=True,
        )
        raise typer.Exit(code=2)

    typer.echo(
        f"secrets set: wrote {name!r} to Keychain "
        f"(account={active.keychain_account!r}). "
        f"Verify with `mineru secrets audit {name}`."
    )


@secrets_app.command("rotate", hidden=True)
def rotate(name: str = typer.Argument(..., help="Secret name to rotate.")) -> None:
    """Rotate a secret (revoke old value at source, write new value to backend)."""
    not_yet_implemented(f"secrets rotate {name}")


@secrets_app.command("list")
def list_(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False, "--json", help="Emit the configured-secret list as JSON."
    ),
) -> None:
    """List the secret NAMES the active profile is configured to use.

    Reads `<profile_root>/connectors.yaml` and enumerates the
    `KEYCHAIN_SERVICES` block — a list of `{service, purpose?}`
    mappings that names every Keychain item this profile depends on.
    Presence (found in the profile's account namespace or not) is
    reported alongside the name, so `secrets list` doubles as the
    discovery half of `secrets set`: the operator sees which slots
    exist, which need filling, and which purpose each serves.

    Values are NEVER read or printed — this verb is a discovery listing,
    not a dump. Use `mineru secrets get <name>` to read one value on
    demand.

    Exit codes:
      - 0 always (an empty list is still a valid output).
      - Missing / malformed `connectors.yaml` is reported on stderr
        (non-fatal so a fresh profile can still enumerate — the printed
        list is just empty).
    """
    from mineru_cli.profile import secrets_config_from_profile

    active = _profile_for_ctx(ctx)
    entries = _load_configured_secret_names(active.profile_root)

    if entries:
        resolver = build_resolver(secrets_config_from_profile(active))
        audit_rows = {r.name: r for r in resolver.audit([e["name"] for e in entries])}
        for entry in entries:
            row = audit_rows.get(entry["name"])
            entry["present"] = bool(row and row.present)
            entry["resolved_by"] = row.resolved_by if row else None

    if json_out:
        typer.echo(json.dumps(entries))
        return

    if not entries:
        typer.echo(
            "mineru secrets list: no secrets configured for this profile. "
            "Populate a `KEYCHAIN_SERVICES:` list in "
            f"{active.profile_root / 'connectors.yaml'} (each entry: "
            "`service: <name>` and optional `purpose: <text>`)."
        )
        return

    name_width = max(len(e["name"]) for e in entries)
    for entry in entries:
        status = "present" if entry["present"] else "MISSING"
        purpose = entry.get("purpose") or ""
        typer.echo(f"  {entry['name']:{name_width}}  {status:8s}  {purpose}")


@secrets_app.command("ls", hidden=True)
def ls_alias(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """DEPRECATED alias for `secrets list`. Kept for ~90 days."""
    emit_rename_notice("mineru secrets ls", "mineru secrets list")
    # Same body as the canonical verb — call it directly rather than
    # forwarding via Click so the shared code path is one function.
    list_(ctx, json_out=json_out)


# ---- Helpers ------------------------------------------------------------


def _profile_for_ctx(ctx: typer.Context):
    """Lazy-hydrate the active profile — same seam `_resolver_for_ctx` uses.

    Kept as its own helper so verbs that only need the profile (not the
    resolver) skip building the resolver.
    """
    from mineru_cli.profile import get_profile

    return get_profile(ctx)


def _load_configured_secret_names(profile_root) -> List[dict]:
    """Return `[{name, purpose}]` from `<profile_root>/connectors.yaml`.

    Schema expectation (matches the synthetic + real profiles' shape):

        KEYCHAIN_SERVICES:
          - service: some-bot-token
            purpose: Chat bot API token
          - service: some-webapp-passphrase-hash
            purpose: Web app passphrase gate

    Missing file / missing block: return `[]` (fresh profile — legitimate).
    Malformed block (not a list, entries not mappings, missing `service`):
    return `[]` and print one hint on stderr so the operator learns the
    shape without the verb failing loud.

    NEVER reads secret VALUES — this is a metadata-only inventory.
    """
    from pathlib import Path

    import yaml

    connectors_path = Path(profile_root) / "connectors.yaml"
    if not connectors_path.exists():
        return []
    try:
        raw = connectors_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
    except (OSError, yaml.YAMLError) as exc:
        typer.echo(
            f"mineru secrets list: could not parse {connectors_path} "
            f"({type(exc).__name__}); returning an empty list.",
            err=True,
        )
        return []
    if not isinstance(data, dict):
        return []
    services = data.get("KEYCHAIN_SERVICES")
    if not isinstance(services, list):
        return []
    out: List[dict] = []
    for entry in services:
        if not isinstance(entry, dict):
            continue
        name = entry.get("service")
        if not isinstance(name, str) or not name:
            continue
        purpose = entry.get("purpose")
        out.append(
            {
                "name": name,
                "purpose": purpose if isinstance(purpose, str) else None,
            }
        )
    return out
