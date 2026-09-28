"""Interactive + non-interactive onboarding for a new agent profile.

Phase 1.5 (2026-08-28). Sits on top of the Phase 1 multi-agent-profile
foundation (`Profile` / humans registry / access allowlist / active-profile
symlink) and gives operators the "spin up a new agent" entry point.

Terminology (locked):
  - PROFILE NAME (`spec.name`)   — the agent's dir + identity (kebab-case,
                                    `[a-z0-9-]+`, matches the containing
                                    `<workspace_root>/profiles/<name>/`).
  - PERSONA NAME (`spec.persona`) — the assistant's display name; lands on
                                    `assistant_name` in profile.yaml.

Atomic-scaffold contract:

  1. Every field is validated BEFORE any filesystem write.
  2. Uniqueness is enforced by `os.makedirs(<profile_root>, exist_ok=False)`
     — one syscall, no TOCTOU gap between "does it exist?" and "create it".
  3. Any failure after the dir is created (an OSError, an interrupt, a
     mid-scaffold exception) triggers a `shutil.rmtree(profile_root)`
     rollback so the workspace never carries a half-built profile.
  4. If a new human is being registered inline, the canonical
     `people.yaml` (with a legacy `humans.yaml` mirror refresh, when
     one pre-existed) is written AFTER the profile scaffold succeeds
     — a partial profile scaffold never leaves an orphan human hanging.

Google-account walkthrough (guarded, optional):

  - The onboarding flow can capture a `google_account` email and store it
    on the profile's `google_account` field (goes to `Profile.extras`
    since it is not a foundation field).
  - When a TTY is attached AND the operator opts in, we shell out to
    `gog auth add <email>` (verified against `/opt/homebrew/bin/gog auth
    add --help` on 2026-08-28 — the CLI form is `gog auth add <email>`).
  - When the caller is scripted (`--no-input`, `--skip-integrations`,
    or `stdin` not a TTY), we PRINT the exact command instead so the
    operator can run it themselves later.
  - NO SECRET IS EVER WRITTEN TO DISK by this module. Bot-token setup
    is intentionally deferred to a follow-up `mineru profile set-bot`
    pointer (documented at the end of the summary).

FUTURE hooks (not built here — extension points for later increments):
  - Monarch (`monarch auth login`)
  - Telegram bot token → Keychain via the secrets seam
  - Slack workspace bearer token
  - Landline daemon bootstrap

Design invariants (must-hold):
  - Never `rm` outside the just-created profile dir.
  - Never write the human registry non-atomically; use tmp+replace.
  - Never activate a half-built profile — activation is the LAST step.
  - Never hardcode the workspace path; the workspace root comes from the
    profile layer (the `MINERU_HOME` seam, default `~/.mineru`).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from mineru_cli.humans import (
    HumansError,
    HumansRegistry,
    default_people_yaml_path,
    legacy_humans_yaml_path,
    load_humans_registry,
    resolve_registry_yaml_path,
)
from mineru_cli.humans.loader import (
    HUMANS_YAML_FILENAME,
    PEOPLE_YAML_FILENAME,
    _HANDLE_PATTERN,
)
from mineru_cli.humans.schema import Human
from mineru_cli.profile.loader import (
    CURRENT_SYMLINK_NAME,
    ProfileError,
    _PROFILE_NAME_PATTERN,
    current_symlink_path,
    default_profiles_base_dir,
    default_workspace_root,
)
from mineru_cli.profile.switching import switch_active_profile


# --- Constants ------------------------------------------------------------

# Profile-name character class. Kebab-case only for new profiles — the
# lower-case restriction is tighter than the loader's `[A-Za-z0-9_-]+`
# safe-char class on purpose: existing profiles keep working, but new
# ones are steered toward one visual style so the profiles list stays
# uniform (sam, gemini-scout, alfred, not Alfred_v2).
_NEW_PROFILE_NAME_PATTERN = re.compile(r"[a-z0-9-]+")

# Profile-dir uniqueness policy. The dir basename defaults to the requested
# profile name; when that dir already exists, a 6-hex-char suffix is drawn
# (`mineru` -> `mineru-3fa9c2`) so many agents can share a display name.
# Flip SUFFIX_ONLY_ON_COLLISION to False to suffix EVERY new profile dir.
PROFILE_DIR_SUFFIX_HEX_LEN = 6
PROFILE_DIR_SUFFIX_ONLY_ON_COLLISION = True
PROFILE_DIR_SUFFIX_MAX_ATTEMPTS = 5

# Names that would collide with workspace-level artifacts or make the
# loader ambiguous. `active` is the active-profile pointer symlink
# itself (renamed from `current` on 2026-09-16 — both names stay
# reserved so a hand-guessed `mineru profile init --name current` can't
# shadow either the new symlink or the legacy fallback path). `engine`
# is reserved for engine-level dirs per §0 of the capability spec.
# `profiles`, `humans`, and `people` are pseudo-reserved so a hand-guessed
# `mineru profile init --name people` cannot shadow the people.yaml
# concept (nor its legacy `humans.yaml` fallback — both filenames
# reserve their bare basenames as of the 2026-09-16 audit §2A F3 file
# rename).
RESERVED_PROFILE_NAMES = frozenset({
    "active",
    "current",
    "engine",
    "profiles",
    "humans",
    "people",
})

# The default assistant-notes folder used by the journal-export job.
# Same as the seed profile so a new operator doesn't have to think
# about this on day one.
DEFAULT_JOURNAL_APPLE_NOTES_FOLDER = "Daily Journals"

# Standard secrets backend order for a new profile: env first so CI can
# shadow with `MINERU_SECRET_*` env vars, keychain second for long-lived
# host secrets. The 1password backend is documented separately and can
# be inserted by the operator later.
DEFAULT_SECRETS_BACKENDS: Tuple[str, ...] = ("env", "keychain")

# Location of the gog CLI used for the optional Google walkthrough. Same
# absolute path the GMAIL.md reference doc / firewall wrapper uses.
GOG_BIN = "/opt/homebrew/bin/gog"

# Loose email shape check for the Google walkthrough. The gog CLI does the
# authoritative validation (it's the one running OAuth), so we only reject
# obviously-malformed values before the shell-out — nothing whitespace-y,
# nothing missing an `@`, nothing missing a dot in the domain half. Local-
# part rules (RFC 5321) are broad; a strict regex here would reject legal
# addresses and cause more support pain than it prevents.
_EMAIL_SHAPE_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")

# TODO(phase2): render IDENTITY.md / SOUL.md / AGENTS.md from templates.
# The `charter/` template tree does not exist yet as of Phase 1.5, so the
# scaffold intentionally does NOT emit those files — the operator's
# next-steps summary calls this out explicitly.
_CHARTER_TEMPLATES_TODO_MARKER = (
    "TODO(phase2): render IDENTITY.md / SOUL.md / AGENTS.md from templates."
)


# --- Errors ---------------------------------------------------------------


class OnboardingError(RuntimeError):
    """Raised on any onboarding-time failure.

    Distinct from `ProfileError` / `HumansError` so callers can catch
    the onboarding-specific ones and print them under `mineru profile
    init: <msg>` without swallowing a genuine loader failure.

    The `__str__` always names the offending field / path so a Typer
    handler can render it verbatim.
    """


# --- Value dataclasses ----------------------------------------------------


@dataclass(frozen=True)
class NewHuman:
    """A new human to register in the machine-level registry (people.yaml,
    or legacy humans.yaml when one pre-exists) as part of onboarding.

    Attributes:
        handle: safe short identifier (`[A-Za-z0-9_-]+`).
        display_name: free-form human label.
        telegram_id: numeric Telegram user ID; the ONLY identity Landline
            uses to authorize inbound messages.
    """

    handle: str
    display_name: str
    telegram_id: int


@dataclass(frozen=True)
class ProfileSpec:
    """Everything needed to scaffold a new profile atomically.

    All fields are already validated (name shape, reserved-name check,
    owner presence, timezone non-empty) by the time this dataclass is
    handed to `create_profile`.

    Attributes:
        name: REQUESTED PROFILE NAME. When the corresponding dir already
            exists, `create_profile` reserves a suffixed name
            (`mineru` -> `mineru-3fa9c2`) so many agents can share a
            display name; the final dir basename lands on
            `ScaffoldResult.profile_name`.
        persona: PERSONA NAME (`assistant_name` in profile.yaml).
        owner_handle: handle that MUST exist in the machine-level human
            registry (people.yaml, or the legacy humans.yaml fallback)
            at the moment of scaffold, either pre-existing or freshly
            added via `new_human`.
        timezone: IANA zone (e.g. `America/Los_Angeles`).
        google_account: optional Google Workspace email to record on the
            profile (goes to profile.yaml's `google_account` field, which
            lands on `Profile.extras` since it is not a foundation field).
        new_human: when the operator wants to register the owner inline
            (i.e. no matching registry entry exists yet), this holds
            the fields to append. `owner_handle` must equal
            `new_human.handle`.
    """

    name: str
    persona: str
    owner_handle: str
    timezone: str
    google_account: Optional[str] = None
    new_human: Optional[NewHuman] = None


@dataclass(frozen=True)
class ScaffoldResult:
    """What `create_profile` produced.

    Attributes:
        profile_root: absolute path of the created profile dir.
        profile_name: the reserved dir basename; may carry a collision
            suffix (`mineru` -> `mineru-3fa9c2`) when the requested name
            was already taken. This is what the profile.yaml `name:`,
            `keychain_account:`, `launchd_label_prefix:`, and
            `secrets.env_prefix` all derive from — not `spec.name`.
        profile_yaml_path: absolute path of the emitted profile.yaml.
        access_yaml_path: absolute path of the emitted access.yaml.
        connectors_yaml_path: absolute path of the emitted starter
            connectors.yaml (Sep 2026 gap-close). Every value in the
            scaffold is a `REPLACE_ME__*` placeholder — edit in place
            and re-run `mineru profile install --apply` with real
            values. `None` for pre-Sep-2026 callers that constructed
            the dataclass directly.
        memory_root: created memory dir.
        briefs_root: created briefs dir.
        cache_root: created cache dir.
        logs_root: created logs dir.
        humans_yaml_written: True iff the machine-level human registry
            file was newly written or appended to during this scaffold.
            Field name kept for pre-2026-09-16 back-compat; the actual
            file written is now `people.yaml` (with a legacy
            `humans.yaml` mirror refresh when one pre-existed).
        activated: True iff the `current` symlink now points at this
            profile (bootstrap case OR `activate=True`).
        integration_command: when the caller opted OUT of running the
            Google walkthrough live (or no TTY), the ready-to-copy
            command they should run themselves. `None` when either no
            google_account was set or the walkthrough already ran.
    """

    profile_root: Path
    profile_name: str
    profile_yaml_path: Path
    access_yaml_path: Path
    memory_root: Path
    briefs_root: Path
    cache_root: Path
    logs_root: Path
    humans_yaml_written: bool
    activated: bool
    integration_command: Optional[str] = None
    # `connectors_yaml_path` is populated by `_write_scaffold_files` when
    # the scaffold writes a starter connectors.yaml (Sep 2026, F2 gap).
    # Optional to keep back-compat with any test that constructed the
    # dataclass directly before the field was added.
    connectors_yaml_path: Optional[Path] = None


# --- Validation -----------------------------------------------------------


def machine_timezone() -> str:
    """Best-effort read of the machine's IANA timezone.

    Falls back to `"America/Los_Angeles"` (the operator's canonical zone; matches
    the seed profile) on any failure so a fresh checkout has a working
    default that is clearly regional rather than opaque like `UTC`.
    """
    try:
        tz = datetime.now().astimezone().tzinfo
        name = getattr(tz, "key", None)
        if isinstance(name, str) and name:
            return name
        # tzinfo may only expose `tzname()` (a shorthand like "PDT"),
        # which is NOT a valid IANA zone. Fall through.
    except Exception:  # noqa: BLE001 — timezone read must never crash init
        pass
    return "America/Los_Angeles"


def default_persona_from_name(name: str) -> str:
    """Suggest a persona from the profile name (title-cased, hyphens -> spaces).

    `sam` -> `"Sam"`; `gemini-scout` -> `"Gemini Scout"`. The operator
    can (and usually will) override this in the prompt.
    """
    return " ".join(part.capitalize() for part in name.split("-") if part)


def default_env_prefix_from_name(name: str) -> str:
    """Build a `secrets.env_prefix` from the profile name (upper, underscores).

    `sam` -> `"SAM_SECRET_"`; `gemini-scout` -> `"GEMINI_SCOUT_SECRET_"`.
    Uppercase + underscore-joined is the convention every existing
    env-shadowed secret uses (`MINERU_SECRET_*` in the seed).
    """
    return name.replace("-", "_").upper() + "_SECRET_"


def validate_profile_name(name: str, workspace_root: Path) -> None:
    """Fail-loud check for a new-profile name; run BEFORE any dir creation.

    Enforced:
      - Non-empty, matches `[a-z0-9-]+` (lowercase kebab-case only for new
        profiles — tighter than the loader's `[A-Za-z0-9_-]+`).
      - Not a leading `-` (would be parsed as a CLI flag by any callback).
      - Not in `RESERVED_PROFILE_NAMES`.
      - Does NOT collide with an existing top-level entry under
        `<workspace_root>/`. Prevents `--name people` from shadowing
        `people.yaml` (or `--name humans` from shadowing the legacy
        `humans.yaml` fallback), and `--name profiles` from colliding
        with the base dir. The `<workspace_root>/profiles/<name>/`
        collision itself is enforced ATOMICALLY at scaffold time via
        `os.makedirs(exist_ok=False)` — this pre-check only guards
        against name shapes that would break BEFORE we reach that gate.

    Raises `OnboardingError` on any failure; the message is safe to
    render straight to the terminal.
    """
    if not isinstance(name, str) or not name:
        raise OnboardingError(
            "profile name is empty. Pass --name <name> or provide one "
            "at the prompt."
        )
    if name.startswith("-"):
        raise OnboardingError(
            f"profile name {name!r} cannot start with '-' — it would be "
            "parsed as a CLI flag by every subsequent invocation."
        )
    if not _NEW_PROFILE_NAME_PATTERN.fullmatch(name):
        raise OnboardingError(
            f"profile name {name!r} is not valid: use lowercase kebab-case "
            "matching [a-z0-9-]+ (no spaces, no path separators, no "
            "uppercase, no underscores)."
        )
    if name in RESERVED_PROFILE_NAMES:
        raise OnboardingError(
            f"profile name {name!r} is reserved. Reserved names: "
            f"{sorted(RESERVED_PROFILE_NAMES)}."
        )
    # Refuse to collide with any existing top-level workspace entry
    # (people.yaml, legacy humans.yaml, current, profiles/ itself,
    # cache/, ...). The
    # `<workspace_root>/profiles/<name>/` collision is handled at the
    # atomic scaffold gate.
    if workspace_root.exists():
        for entry in os.listdir(workspace_root):
            if entry == name:
                raise OnboardingError(
                    f"profile name {name!r} collides with existing "
                    f"workspace entry {workspace_root / entry}. Pick a "
                    "different name."
                )


def validate_persona(persona: str) -> None:
    """Persona (`assistant_name`) must be a non-empty, printable string."""
    if not isinstance(persona, str) or not persona.strip():
        raise OnboardingError(
            "persona name is empty. Pass --persona <name> or provide "
            "one at the prompt (defaults to the title-cased profile name)."
        )


def validate_timezone(tz: str) -> None:
    """Timezone must be a non-empty string; deeper IANA validation is
    the loader's job (zoneinfo lookups elsewhere in the CLI)."""
    if not isinstance(tz, str) or not tz.strip():
        raise OnboardingError(
            "timezone is empty. Pass --timezone <IANA zone> "
            "(e.g. America/Los_Angeles) or provide one at the prompt."
        )


def validate_new_human(new: NewHuman) -> None:
    """Validate an inline-added human entry.

    Same handle rules the humans loader enforces, plus a strict integer
    check on `telegram_id` (bools rejected explicitly since Python's
    `bool` subclasses `int`).
    """
    if not isinstance(new.handle, str) or not new.handle:
        raise OnboardingError("new human handle is empty.")
    if not _HANDLE_PATTERN.fullmatch(new.handle):
        raise OnboardingError(
            f"new human handle {new.handle!r} must match [A-Za-z0-9_-]+ "
            "(no spaces, no path separators)."
        )
    if not isinstance(new.display_name, str) or not new.display_name.strip():
        raise OnboardingError(
            f"new human {new.handle!r}: display_name must be a non-empty "
            "string."
        )
    if isinstance(new.telegram_id, bool) or not isinstance(new.telegram_id, int):
        raise OnboardingError(
            f"new human {new.handle!r}: telegram_id must be an integer "
            f"(got {type(new.telegram_id).__name__})."
        )


def check_persona_collision(
    persona: str, base_dir: Path, exclude_profile_name: Optional[str] = None
) -> List[str]:
    """Return profile names that already use `persona` as assistant_name.

    Best-effort scan of `<base_dir>/*/profile.yaml`. A YAML error or
    permission error on a peer profile is silently ignored — we're only
    warning about a soft duplication, not gating on it.
    """
    if not base_dir.is_dir():
        return []
    hits: List[str] = []
    for child in sorted(base_dir.iterdir()):
        if not child.is_dir():
            continue
        if exclude_profile_name is not None and child.name == exclude_profile_name:
            continue
        candidate = child / "profile.yaml"
        if not candidate.exists():
            continue
        try:
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        other = data.get("assistant_name")
        if isinstance(other, str) and other.strip() == persona.strip():
            hits.append(child.name)
    return hits


# --- Atomic scaffold ------------------------------------------------------


def compute_profile_paths(
    name: str,
    *,
    workspace_root: Optional[Path] = None,
    profiles_base_dir: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """Return `(workspace_root, profile_dir)` for a new profile name.

    Uses the same resolution as the loader (env-var overrides participate)
    so onboarding + subsequent `mineru profile use` land on the same paths.
    """
    ws = workspace_root or default_workspace_root()
    base = profiles_base_dir or default_profiles_base_dir()
    return ws, base / name


def reserve_profile_root(name: str, base_dir: Path) -> Tuple[str, Path]:
    """Atomically reserve a fresh dir under `base_dir` for a new profile.

    Uniqueness policy (`PROFILE_DIR_SUFFIX_ONLY_ON_COLLISION`):
      - ONLY_ON_COLLISION (default): first try `<base_dir>/<name>` bare.
        If that dir already exists, retry with a 6-hex-char suffix
        (`mineru` -> `mineru-3fa9c2`) up to
        `PROFILE_DIR_SUFFIX_MAX_ATTEMPTS` times.
      - Always-suffix: skip the bare attempt and start with a suffixed
        candidate. Useful when the operator wants every profile dir to
        carry a per-instance suffix (multi-agent farms).

    Every attempt is exactly one `os.makedirs(exist_ok=False)` syscall,
    so the TOCTOU-free property of the previous atomic gate is preserved
    per attempt: a second `create_profile` racing us can only lose on
    the SAME candidate we just claimed, and its retry moves it to a
    different suffix.

    Returns `(final_name, profile_root)`. The `final_name` equals `name`
    on the collision-free path and `f"{name}-{hex6}"` after a bump.

    Raises `OnboardingError` if every attempt collides (exhaustion). The
    caller's `validate_profile_name` has already vetted the bare `name`,
    and the suffix charset (`[0-9a-f]`) stays inside the
    new-profile-name character class by construction, so a suffixed
    candidate is still a legal profile name.
    """
    base_dir.mkdir(parents=True, exist_ok=True)

    if PROFILE_DIR_SUFFIX_ONLY_ON_COLLISION:
        candidate_path = base_dir / name
        try:
            os.makedirs(candidate_path, exist_ok=False)
            return name, candidate_path
        except FileExistsError:
            pass  # Fall through to the suffixed retry loop.

    # Random hex, split evenly so `PROFILE_DIR_SUFFIX_HEX_LEN=6` maps to
    # `secrets.token_hex(3)` (each byte renders as two hex chars).
    hex_bytes = PROFILE_DIR_SUFFIX_HEX_LEN // 2
    for _ in range(PROFILE_DIR_SUFFIX_MAX_ATTEMPTS):
        suffix = secrets.token_hex(hex_bytes)
        candidate_name = f"{name}-{suffix}"
        # Defense in depth: re-run the suffixed name through the new-
        # profile-name validator. Safe TODAY because the suffix charset
        # (`[0-9a-f]`) stays inside `[a-z0-9-]+` and the bare `name` was
        # already validated by `create_profile`, so a suffixed candidate
        # is legal by construction. This lock catches a future edit that
        # loosens the suffix format (e.g. base64, uppercase) BEFORE the
        # bad name lands on disk.
        validate_profile_name(candidate_name, base_dir.parent)
        candidate_path = base_dir / candidate_name
        try:
            os.makedirs(candidate_path, exist_ok=False)
            return candidate_name, candidate_path
        except FileExistsError:
            continue

    raise OnboardingError(
        f"could not reserve a unique profile directory for {name!r} under "
        f"{base_dir} after {PROFILE_DIR_SUFFIX_MAX_ATTEMPTS} attempts."
    )


def create_profile(
    spec: ProfileSpec,
    *,
    workspace_root: Optional[Path] = None,
    profiles_base_dir: Optional[Path] = None,
    activate: bool = False,
    fail_after_scaffold_for_test: Optional[Callable[[Path], None]] = None,
) -> ScaffoldResult:
    """Scaffold a new profile atomically; roll back on any failure.

    Args:
        spec: fully-validated `ProfileSpec` (see `ProfileSpec` docstring).
        workspace_root: override for the workspace root (mostly tests).
        profiles_base_dir: override for the profiles base dir (mostly tests).
        activate: when True, atomically re-point the `current` symlink at
            the new profile after successful scaffold. The bootstrap
            case (zero pre-existing profiles) triggers this automatically
            regardless of the flag.
        fail_after_scaffold_for_test: test hook. When set, invoked with
            the just-created profile dir immediately after every file is
            written but BEFORE humans.yaml is touched. Raising from the
            hook exercises the atomic-rollback path.

    Returns:
        A `ScaffoldResult` describing every file that landed on disk.

    Raises:
        OnboardingError: any validation miss (name shape, reserved name,
            unknown owner, duplicate handle inside `new_human`, etc.).
        OnboardingError: raised when `reserve_profile_root` exhausts
            `PROFILE_DIR_SUFFIX_MAX_ATTEMPTS` suffixed candidates — every
            reservation attempt lost to a concurrent create. Very rare.
    """
    ws, _requested_root = compute_profile_paths(
        spec.name,
        workspace_root=workspace_root,
        profiles_base_dir=profiles_base_dir,
    )

    # --- Validate again (defense in depth; the verb front-end already
    # --- called these, but scripted callers may reach create_profile
    # --- directly with hand-built specs).
    validate_profile_name(spec.name, ws)
    validate_persona(spec.persona)
    validate_timezone(spec.timezone)
    if spec.new_human is not None:
        validate_new_human(spec.new_human)
        if spec.owner_handle != spec.new_human.handle:
            raise OnboardingError(
                f"owner_handle {spec.owner_handle!r} does not match "
                f"new_human.handle {spec.new_human.handle!r}."
            )

    # --- Load-or-empty the humans registry so we can cross-check the
    # --- owner. Bootstrap case: the registry file may be absent; that's
    # --- fine. The reader prefers `people.yaml` and falls back to the
    # --- legacy `humans.yaml` so pre-2026-09-16 workspaces keep working
    # --- without operator intervention.
    canonical_registry_yaml = default_people_yaml_path(ws)
    resolved_registry_yaml = resolve_registry_yaml_path(ws)
    existing_registry: Optional[HumansRegistry]
    if resolved_registry_yaml.exists():
        try:
            existing_registry = load_humans_registry(path=resolved_registry_yaml)
        except HumansError as exc:
            raise OnboardingError(
                f"human registry at {resolved_registry_yaml} is present "
                f"but invalid: {exc}. Fix the file (or delete it to "
                "bootstrap fresh) and retry `mineru profile init`."
            ) from exc
    else:
        existing_registry = None

    # --- Owner presence check.
    if spec.new_human is None:
        if existing_registry is None or spec.owner_handle not in existing_registry:
            known = (
                existing_registry.handles() if existing_registry is not None else []
            )
            raise OnboardingError(
                f"owner {spec.owner_handle!r} not found in human registry "
                f"at {resolved_registry_yaml}. Known handles: {known}. "
                "Either pick one of those, or provide new_human=... to "
                "register them inline (interactive) or pass --owner-new "
                "to the init verb."
            )
    else:
        # Adding inline: reject if the handle already exists.
        if (
            existing_registry is not None
            and spec.new_human.handle in existing_registry
        ):
            raise OnboardingError(
                f"cannot add human {spec.new_human.handle!r} inline: "
                f"handle already registered in human registry at "
                f"{resolved_registry_yaml}. Drop --owner-new-* and pass "
                "--owner <handle> instead."
            )

    # --- Reserve a fresh profile dir atomically. When the requested
    # --- `spec.name` is free, we get it verbatim; when it collides, a
    # --- 6-hex suffix disambiguates so many agents can share a persona.
    # --- Every attempt is one `os.makedirs(exist_ok=False)` syscall
    # --- (TOCTOU-free, matches the previous single-attempt gate).
    base_for_reservation = profiles_base_dir or default_profiles_base_dir()
    final_name, profile_root = reserve_profile_root(spec.name, base_for_reservation)

    # From here on, EVERY name-derived field (profile.yaml `name:`,
    # `keychain_account:`, `launchd_label_prefix:`, `secrets.env_prefix`,
    # the `_no_prior_profiles` exclusion, the `switch_active_profile`
    # activation target) must use `final_name`. Rebinding the frozen
    # dataclass keeps that invariant enforced by one line: no downstream
    # site can accidentally reach through to the pre-reservation name.
    if final_name != spec.name:
        spec = replace(spec, name=final_name)

    # Compute the bootstrap flag BEFORE scaffolding so the rendered
    # profile.yaml can carry `imessage_enabled: false` for every NON-owner
    # (i.e. non-bootstrap) profile. iMessage reads the ONE shared
    # `~/Library/Messages/chat.db` for this macOS user, so a 2nd profile
    # invoking any `mineru imessage` verb would silently read the primary
    # user's messages — a real privacy leak. The gate lives on the profile
    # (see `Profile.imessage_enabled`) and every `mineru imessage <verb>`
    # calls `_require_imessage_profile(ctx)` at its point of use.
    #
    # `_no_prior_profiles` runs the same check the `bootstrap` variable
    # below uses for the `current` symlink; the two must agree, so we
    # compute it once here and reuse below.
    bootstrap = existing_registry is None and _no_prior_profiles(
        profiles_base_dir or default_profiles_base_dir(),
        exclude=spec.name,
    )

    # --- Scaffold, with rollback on any failure inside this block.
    scaffold_written = False
    humans_yaml_written = False
    activated = False
    try:
        result_paths = _write_scaffold_files(
            profile_root, ws, spec, is_bootstrap=bootstrap
        )
        scaffold_written = True

        # Test hook: exercise the rollback path AFTER files are on disk
        # but BEFORE the humans.yaml or activation steps commit.
        if fail_after_scaffold_for_test is not None:
            fail_after_scaffold_for_test(profile_root)

        # --- Commit the registry file if we're bootstrapping it or
        # --- adding a new human. The canonical write target is
        # --- `people.yaml` (2026-09-16 audit §2A F3 file rename).
        # --- Mirror pattern (from the `active`/`current` symlink rename
        # --- in commit `c318df6`): if a pre-existing legacy
        # --- `humans.yaml` sits at the workspace root, refresh IT too
        # --- with the same contents, so a downstream reader that
        # --- resolves the fallback path cannot return a stale answer.
        # --- Fresh workspaces get only `people.yaml` — no `humans.yaml`
        # --- planted.
        need_humans_write = spec.new_human is not None or existing_registry is None
        if need_humans_write:
            updated_registry = _humans_registry_with_addition(
                existing_registry, spec
            )
            _atomic_write_humans_yaml(canonical_registry_yaml, updated_registry)
            legacy_registry_yaml = legacy_humans_yaml_path(ws)
            if (
                legacy_registry_yaml.exists()
                and legacy_registry_yaml != canonical_registry_yaml
            ):
                # Only refresh when the legacy file already exists on
                # this workspace (never plant one). The rewrite keeps
                # both files byte-identical so the loader picks the
                # canonical path on the next read and downstream
                # tooling that hardcoded the legacy path (unlikely, but
                # possible pre-rename) still sees the fresh contents.
                _atomic_write_humans_yaml(
                    legacy_registry_yaml, updated_registry
                )
            humans_yaml_written = True

        # --- Activate: bootstrap case fires unconditionally so a fresh
        # --- machine has a `current` symlink after `mineru profile init`.
        # `bootstrap` was already computed above so the rendered
        # `imessage_enabled` field and the `current` symlink activation
        # agree on the same "is this the owner / first profile?" answer.
        if activate or bootstrap:
            switch_active_profile(
                spec.name,
                workspace_root=ws,
                profiles_base_dir=profiles_base_dir,
            )
            activated = True

        return ScaffoldResult(
            profile_root=profile_root,
            profile_name=spec.name,
            profile_yaml_path=result_paths["profile_yaml"],
            access_yaml_path=result_paths["access_yaml"],
            connectors_yaml_path=result_paths.get("connectors_yaml"),
            memory_root=result_paths["memory"],
            briefs_root=result_paths["briefs"],
            cache_root=result_paths["cache"],
            logs_root=result_paths["logs"],
            humans_yaml_written=humans_yaml_written,
            activated=activated,
        )
    except BaseException:
        # Rollback: remove the just-created profile dir. Never touches
        # anything outside `profile_root`. `shutil.rmtree` on a dir we
        # own is safe here — the tree only contains files this call
        # just wrote.
        _cleanup_partial_dir(profile_root)
        # If humans.yaml was already committed we intentionally leave
        # it — the added human is thin and harmless, and re-running
        # `profile init` with the same handle will find them.
        raise


def _cleanup_partial_dir(path: Path) -> None:
    """Best-effort rmtree of a half-scaffolded profile dir. Never raises.

    Used only for rolling back a scaffold this call just created.
    `shutil.rmtree` is deliberate — we OWN the dir (it did not exist
    when this call started, per the atomic uniqueness gate), so no
    unrelated data can live under it. A cleanup failure is surfaced on
    stderr so an orphaned half-scaffolded dir is visible, but the
    warning never masks the original scaffold error the caller is about
    to re-raise.
    """
    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError as exc:
        # Warn but never raise — the caller is about to re-raise the
        # original scaffold error, and swallowing a cleanup failure
        # would silently leave an orphan profile dir on disk.
        print(
            f"warning: could not clean up partial profile dir {path}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _no_prior_profiles(profiles_base_dir: Path, *, exclude: str) -> bool:
    """Return True iff no profile dirs exist under `profiles_base_dir`
    besides the one we just created (`exclude`)."""
    if not profiles_base_dir.exists():
        return True
    for child in profiles_base_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name == exclude:
            continue
        if (child / "profile.yaml").exists():
            return False
    return True


# --- File emitters --------------------------------------------------------


def _write_scaffold_files(
    profile_root: Path,
    workspace_root: Path,
    spec: ProfileSpec,
    *,
    is_bootstrap: bool = True,
) -> Dict[str, Path]:
    """Materialize every file + subdir the scaffold owns.

    Order matters only for readability — the caller (`create_profile`)
    already reserved the parent dir atomically. If any write here raises,
    the caller's `except BaseException` triggers the rollback.

    Args:
        is_bootstrap: True iff this is the FIRST profile on the machine
            (the owner profile). Non-bootstrap profiles render
            `imessage_enabled: false` into profile.yaml because iMessage
            reads the ONE shared macOS chat.db and a second profile that
            tried to read it would leak the owner's message history.

    Note on `workspace_root`: kept in the signature for backward-compat
    with any test that constructs the scaffold directly, but the rendered
    `workspace_absolute` is `profile_root` now, NOT `workspace_root`. Each
    profile is its OWN workspace (memory tree, cache, inject-queue,
    cloakbrowser-profile all live under `profile_root`); collapsing every
    profile to the shared engine root was the Sep-4-2026 step-5 audit's
    Cat-B critical (see reports/2026-09-04-mineru-step5-security-audit.md).
    """
    memory_root = profile_root / "memory"
    briefs_root = profile_root / "briefs"
    cache_root = profile_root / "cache"
    logs_root = profile_root / "logs"
    for d in (memory_root, briefs_root, cache_root, logs_root):
        d.mkdir(parents=True, exist_ok=False)

    profile_yaml_path = profile_root / "profile.yaml"
    profile_yaml_path.write_text(
        _render_profile_yaml(
            spec,
            memory_root,
            briefs_root,
            profile_root,
            is_bootstrap=is_bootstrap,
        ),
        encoding="utf-8",
    )

    access_yaml_path = profile_root / "access.yaml"
    access_yaml_path.write_text(_render_access_yaml(spec), encoding="utf-8")

    cron_yaml_path = profile_root / "cron.yaml"
    cron_yaml_path.write_text(_render_cron_yaml_starter(spec), encoding="utf-8")

    # Connectors overlay — a working starter file with `REPLACE_ME__*`
    # placeholders. Wired in Sep 2026 to close the gap where a fresh
    # `profile init` → `profile install --apply` died on the first
    # `{{DAEMON_PERSONA_NAME}}` / `{{TAILSCALE_HOSTNAME}}` template
    # reference because no `connectors.yaml` existed under `profile_root`
    # (see 2026-09-16 CLI naming-consolidation audit §F2). The install
    # loader already fails loud on an absent file; now the scaffold
    # writes a real working version an operator can fill in place.
    connectors_yaml_path = profile_root / "connectors.yaml"
    connectors_yaml_path.write_text(
        _render_connectors_yaml_starter(spec), encoding="utf-8"
    )

    # Charter templates deferred to Phase 2 (see module docstring). Leave
    # a marker file so a future increment can `git grep` for it.
    (profile_root / "TODO_CHARTER_TEMPLATES.md").write_text(
        _CHARTER_TEMPLATES_TODO_MARKER + "\n"
        "\n"
        "Phase 1.5 onboarding does NOT render IDENTITY.md / SOUL.md / "
        "AGENTS.md because the template tree does not exist yet. Phase 2 "
        "will add them from prompts/templates/*.md.\n",
        encoding="utf-8",
    )

    return {
        "profile_yaml": profile_yaml_path,
        "access_yaml": access_yaml_path,
        "cron_yaml": cron_yaml_path,
        "connectors_yaml": connectors_yaml_path,
        "memory": memory_root,
        "briefs": briefs_root,
        "cache": cache_root,
        "logs": logs_root,
    }


def _render_profile_yaml(
    spec: ProfileSpec,
    memory_root: Path,
    briefs_root: Path,
    profile_root: Path,
    *,
    is_bootstrap: bool = True,
) -> str:
    """Serialize a fully-populated profile.yaml body.

    Uses PyYAML with `sort_keys=False` so the emitted key order matches
    the seed profile — the loader tolerates any order, but a consistent
    order makes diffs across profiles boring instead of visually noisy.
    Comments cannot round-trip through yaml.safe_dump, so we hand-splice
    a short header block up top.

    `workspace_absolute` is `profile_root`, NOT the shared workspace root:
    each profile owns its own workspace (memory, cache, inject-queue,
    cloakbrowser-profile) under `profiles/<name>/`. The Sep-4-2026 step-5
    audit's Cat-B critical (`onboarding.py:852`) flagged the earlier
    default (workspace_root) as collapsing every per-profile consumer to
    the OWNER's tree; the loader now hard-rejects any non-seed profile
    that still points at `default_workspace_root()`.

    `is_bootstrap` gates the `imessage_enabled` field. See
    `_write_scaffold_files` and the imessage-verb gate in
    `mineru_cli/verbs/imessage.py` for the full rationale (macOS binds
    iMessage to one Apple ID per user, so multi-profile isolation must
    be a CLI-level gate). Bootstrap (first) profile: field omitted,
    loader defaults to `True`. Non-bootstrap profiles: rendered as
    `imessage_enabled: false`, and the operator can flip it back on
    intentionally if they own the Apple ID.
    """
    body: Dict[str, Any] = {
        "name": spec.name,
        # display_name mirrors the owner's human display (readable in
        # `mineru profile show`). Fall back to the persona if we cannot
        # resolve it (bootstrap + inline-added human sets it explicitly).
        "display_name": spec.persona,
        "assistant_name": spec.persona,
        "timezone": spec.timezone,
        "keychain_account": spec.name,
        "launchd_label_prefix": f"com.{spec.name}",
        "workspace_absolute": str(profile_root),
        "memory_root": str(memory_root),
        "briefs_root": str(briefs_root),
        "journal_apple_notes_folder": DEFAULT_JOURNAL_APPLE_NOTES_FOLDER,
        "secrets": {
            "backends": list(DEFAULT_SECRETS_BACKENDS),
            "env_prefix": default_env_prefix_from_name(spec.name),
        },
    }
    if spec.google_account:
        # Now a first-class field on `Profile.google_account` — the
        # loader lifts it out of the top-level YAML into
        # `Profile.google_account` and the gog wrapper injects
        # `--account=<email>` on every subprocess call so a multi-tenant
        # Mac addresses two Google accounts cleanly.
        body["google_account"] = spec.google_account

    if not is_bootstrap:
        # Non-owner (non-bootstrap) profiles opt OUT of iMessage. macOS
        # has one Apple ID per user account and one shared chat.db, so a
        # second profile invoking `mineru imessage <verb>` would silently
        # read the primary profile's message history. See
        # `mineru_cli/verbs/imessage.py::_require_imessage_profile` for
        # the fail-loud gate the CLI applies.
        body["imessage_enabled"] = False

    header = (
        "# Profile scaffolded by `mineru profile init`.\n"
        "# See `mineru profile show` to inspect the loaded shape.\n"
        "\n"
    )
    payload = yaml.safe_dump(body, default_flow_style=False, sort_keys=False)
    return header + payload


def _render_access_yaml(spec: ProfileSpec) -> str:
    """Serialize the owner-only access.yaml.

    Guest-tier enforcement is DEFERRED per Phase-1 access module. The
    `authorized:` list explicitly names the owner so `mineru access
    show` renders the intended shape without leaning on the loader's
    implicit-owner materialization.
    """
    body = (
        f"# Per-profile access allowlist for the {spec.name!r} agent.\n"
        "#\n"
        "# Guest-tier enforcement is DEFERRED to a later phase. When it\n"
        "# lands, guest entries will be added here with `tier: guest`\n"
        "# and Landline will scope their toolset accordingly.\n"
        "\n"
        f"owner: {spec.owner_handle}\n"
        "authorized:\n"
        f"  - {{human: {spec.owner_handle}, tier: owner}}\n"
    )
    return body


def _render_cron_yaml_starter(spec: ProfileSpec) -> str:
    """Emit a minimal starter cron.yaml.

    Intentionally leaves `jobs: []` empty. Any `mineru cron ...` verb
    will fail loud with "add at least one job" until the operator wires
    real jobs — that fail-loud is the right UX, since a scheduled job
    is never something a new profile should acquire silently.
    """
    return (
        f"# Cron schedule for the {spec.name!r} profile.\n"
        "#\n"
        "# `mineru cron install` reads this file and materializes one\n"
        "# launchd plist per entry. Add jobs under `jobs:` when you are\n"
        "# ready — the empty list below will cause any `mineru cron`\n"
        "# verb to fail loud with a clear message until you do.\n"
        "\n"
        "defaults:\n"
        "  default_model: claude-opus-4-6\n"
        "  default_timeout: 1800\n"
        "\n"
        "jobs: []\n"
    )


# Every UPPERCASE placeholder value written by `_render_connectors_yaml_starter`
# starts with this token, so `mineru setup` (and any future audit) can grep for
# unedited placeholders after an install and warn the operator to fill them in.
CONNECTORS_PLACEHOLDER_TOKEN = "REPLACE_ME"


def _render_connectors_yaml_starter(spec: ProfileSpec) -> str:
    """Emit a working starter connectors.yaml with clearly-flagged placeholders.

    Every shipped engine template hard-references a small set of UPPERCASE
    connector keys (`{{DAEMON_PERSONA_NAME}}`, `{{TAILSCALE_HOSTNAME}}`,
    `{{PERSONAL_CALENDAR_ID}}`, `{{TELEGRAM_BOT_SERVICE}}`, ...), and
    `render_template` fails LOUD on any missing scalar. Before Sep 2026
    `profile init` scaffolded `profile.yaml` + `access.yaml` + `cron.yaml`
    but NOT this file, so every fresh install died on the first template
    connector reference with a message pointing the operator at a file
    that did not exist.

    We now write a real working connectors.yaml at scaffold time — every
    required key present, each value a `REPLACE_ME__*` placeholder — so
    `mineru profile install --apply` renders successfully on a fresh
    profile. The rendered workspace obviously contains fake values; the
    operator's fill-in job is to edit this file in place and re-install.

    Which keys we include, and why:
      - Required (no `{{#if}}` guard) — every value must be present or
        render bombs: DAEMON_PERSONA_NAME, TAILSCALE_HOSTNAME,
        TELEGRAM_BOT_SERVICE, PERSONAL_CALENDAR_ID, FAMILY_CALENDAR_ID,
        CHURCH_NAME, USER_PRIMARY_EMAIL, FINANCE_CLI_PRODUCT,
        FINANCE_CLI_BINARY_NAME.
      - Required list (iterated with `{{#each}}`): FINANCE_CLI_SUBCOMMANDS.
      - Guarded (`{{#if}}` around every use) — included so the rendered
        docs match a real install: CHURCH_CALENDAR_ID.

    Derived from templates via
    `grep -rhoE '\\{\\{[A-Z_]+\\}\\}' engine/` + the templating spec §2.5
    (`~/.mineru/reports/2026-08-28-genericize-templating-spec.md`).
    Kept in one place so a new engine template that references a new
    connector adds ONE line here and to the connectors context group.
    """
    return (
        f"# Connectors overlay for the {spec.name!r} profile.\n"
        "#\n"
        "# Flat UPPERCASE keys — each maps 1:1 to a `{{KEY}}` variable that\n"
        "# `mineru profile install` substitutes into engine templates.\n"
        "# `profile init` scaffolded this file with placeholders so a\n"
        "# fresh `mineru profile install --apply` succeeds end-to-end.\n"
        "# Every value tagged `REPLACE_ME__*` is a placeholder; edit them\n"
        "# in place and re-run `mineru profile install --apply --target\n"
        "# <workspace>` when you have real values.\n"
        "#\n"
        f"# The full connector schema (spec §2.5) lives in the 2026-08-28\n"
        "# genericize-templating report.\n"
        "\n"
        "# --- Google Workspace ---------------------------------------------\n"
        "\n"
        "USER_PRIMARY_EMAIL: REPLACE_ME__you@example.com\n"
        "\n"
        "PERSONAL_CALENDAR_ID: REPLACE_ME__personal@group.calendar.google.com\n"
        "FAMILY_CALENDAR_ID:   REPLACE_ME__family@group.calendar.google.com\n"
        "# CHURCH_CALENDAR_ID is guarded by `{{#if}}` in engine templates;\n"
        "# leave it set to the placeholder and it renders inside the guard,\n"
        "# or comment the line out to have the guard omit the block.\n"
        "CHURCH_CALENDAR_ID:   REPLACE_ME__community@group.calendar.google.com\n"
        "\n"
        "# --- Networking ----------------------------------------------------\n"
        "\n"
        "TAILSCALE_HOSTNAME: REPLACE_ME__mineru.tailXXXXXX.ts.net\n"
        "\n"
        "# --- Telegram / daemon --------------------------------------------\n"
        "\n"
        "TELEGRAM_BOT_SERVICE: REPLACE_ME__telegram-bot-token\n"
        "DAEMON_PERSONA_NAME:  REPLACE_ME__Landline\n"
        "\n"
        "# --- Community -----------------------------------------------------\n"
        "\n"
        "CHURCH_NAME: REPLACE_ME__Community Name\n"
        "\n"
        "# --- Finance CLI ---------------------------------------------------\n"
        "\n"
        "FINANCE_CLI_PRODUCT:     REPLACE_ME__Finance Product\n"
        "FINANCE_CLI_BINARY_NAME: REPLACE_ME__finance-binary\n"
        "FINANCE_CLI_SUBCOMMANDS:\n"
        "  - cmd:     REPLACE_ME__finance-binary transactions --days 7\n"
        "    comment: Recent transactions\n"
        "  - cmd:     REPLACE_ME__finance-binary accounts\n"
        "    comment: All linked accounts\n"
    )


def _humans_registry_with_addition(
    existing: Optional[HumansRegistry], spec: ProfileSpec
) -> HumansRegistry:
    """Return a new registry containing existing entries plus, if any,
    the freshly-added human from `spec.new_human`."""
    from mineru_cli.humans.schema import Human, HumansRegistry as _R

    entries: Dict[str, Human] = {}
    if existing is not None:
        for h in existing:
            entries[h.handle] = h
    if spec.new_human is not None:
        entries[spec.new_human.handle] = Human(
            handle=spec.new_human.handle,
            telegram_id=spec.new_human.telegram_id,
            display_name=spec.new_human.display_name,
        )
    return _R(entries_by_handle=entries)


def _atomic_write_humans_yaml(path: Path, registry: HumansRegistry) -> None:
    """Serialize `registry` to `path` via tmp+rename (atomic on POSIX).

    A concurrent reader (e.g. `mineru people list` on another shell)
    sees either the pre-swap contents or the post-swap contents, never
    a half-written file.

    Callers pass the CANONICAL path (`default_people_yaml_path`) and,
    when refreshing a pre-existing legacy file, the LEGACY path
    (`legacy_humans_yaml_path`). The helper itself is filename-agnostic
    — it just writes the given path atomically — so both call sites
    share the same tmp+replace guarantee.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    text = _render_humans_yaml(registry)
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _render_humans_yaml(registry: HumansRegistry) -> str:
    """Serialize a `HumansRegistry` to the canonical humans.yaml body.

    Preserves insertion order (YAML file order) so re-writing a registry
    that only added a new tail entry produces a clean append-only diff.
    """
    header = (
        "# Machine-level human registry.\n"
        "# Thin: handle -> {telegram_id, display_name}. No per-human dirs.\n"
        "\n"
    )
    body: Dict[str, Any] = {"humans": {}}
    for h in registry:
        body["humans"][h.handle] = {
            "telegram_id": h.telegram_id,
            "display_name": h.display_name,
        }
    payload = yaml.safe_dump(body, default_flow_style=False, sort_keys=False)
    return header + payload


# --- Google walkthrough ---------------------------------------------------


def build_gog_auth_command(google_account: str) -> List[str]:
    """Return the exact argv for the optional Google walkthrough.

    Verified against `/opt/homebrew/bin/gog auth add --help` on
    2026-08-28: the form is `gog auth add <email>`. `<email>` is a
    positional argument, NOT a flag. Extra `gog` flags (--services,
    --readonly, --json) are the operator's business; we keep the
    command minimal so the operator sees the exact same UX they would
    see running `gog auth add` from a shell.

    Two defensive tweaks:
      - A loose shape-check (`_EMAIL_SHAPE_PATTERN`) rejects obviously-
        malformed values (empty, no `@`, whitespace, missing dot in the
        domain). gog does the authoritative validation; this only
        catches typos before the shell-out.
      - A `--` separator between `auth add` and the email keeps a value
        that happens to start with `-` from being parsed as a gog flag.
        `gog auth add -- <email>` is unambiguous.
    """
    if not isinstance(google_account, str) or not _EMAIL_SHAPE_PATTERN.fullmatch(
        google_account.strip()
    ):
        raise OnboardingError(
            f"google_account {google_account!r} is not a well-formed email "
            "address (expected `local@domain.tld`)."
        )
    return [GOG_BIN, "auth", "add", "--", google_account.strip()]


def format_gog_command_string(google_account: str) -> str:
    """Human-copyable one-liner rendering of `build_gog_auth_command`."""
    return " ".join(build_gog_auth_command(google_account))


def run_gog_auth_add(
    google_account: str,
    *,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> int:
    """Shell out to `gog auth add <email>` and return the exit code.

    Streams output directly (no capture) so the operator sees the same
    OAuth flow they would see in a bare shell: the browser opens, they
    consent, gog writes the token to its own store. This module NEVER
    touches the token itself — the gog CLI is the authority.

    Args:
        google_account: the email to authorize.
        runner: injection point for tests. When omitted, resolved at
            call time via `subprocess.run` so a
            `patch("mineru_cli.profile.onboarding.subprocess.run", ...)`
            in a test takes effect (a default-argument capture would
            snapshot the real `subprocess.run` at function-definition
            time and defeat the patch, which we learned the hard way
            when a smoke test opened the real browser once).

    Returns:
        The subprocess exit code.
    """
    cmd = build_gog_auth_command(google_account)
    if runner is None:
        # Resolved via the module attribute so tests that patch
        # `mineru_cli.profile.onboarding.subprocess.run` take effect.
        completed = subprocess.run(cmd)
    else:
        completed = runner(cmd)
    rc = getattr(completed, "returncode", 0)
    return int(rc) if rc is not None else 1


__all__ = [
    "CONNECTORS_PLACEHOLDER_TOKEN",
    "DEFAULT_JOURNAL_APPLE_NOTES_FOLDER",
    "DEFAULT_SECRETS_BACKENDS",
    "GOG_BIN",
    "NewHuman",
    "OnboardingError",
    "ProfileSpec",
    "RESERVED_PROFILE_NAMES",
    "ScaffoldResult",
    "build_gog_auth_command",
    "check_persona_collision",
    "compute_profile_paths",
    "create_profile",
    "default_env_prefix_from_name",
    "default_persona_from_name",
    "format_gog_command_string",
    "machine_timezone",
    "run_gog_auth_add",
    "validate_new_human",
    "validate_persona",
    "validate_profile_name",
    "validate_timezone",
]
