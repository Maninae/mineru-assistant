"""Loader for the active profile (Phase 1 multi-profile framework).

Phase 1 (2026-08-28) revives the multi-tenant machinery that was dropped
in §0 of the 2026-07-25 capability spec. Multiple profiles can now
co-exist under `<workspace_root>/profiles/` and be switched with
`mineru profile use <name>`, which atomically re-points an `active`
symlink at the chosen profile.

Resolution order for the active profile name:

  1. Explicit `--profile <name>` flag on the root callback.
  2. `MINERU_PROFILE` env var (canonical name, back-compat).
  3. `active` symlink at the workspace root (`active -> profiles/<name>`).
     If absent, the legacy `current` symlink (same target semantics) is
     read as a fallback so pre-2026-09-16 workspaces keep resolving.
  4. Fail loud — there is NO silent default. The old machine-wide default fallback is
     gone; a machine with no active profile MUST say so.

Also exposes `secrets_config_from_profile(profile)`, the bridge from a
loaded `Profile` to F2's `SecretsConfig`.

Path model (workspace-root-relative). The default workspace root is the
`MINERU_HOME` env seam (default `~/.mineru`); a downstream user with a
custom `MINERU_HOME` gets that path instead:

  <workspace_root>/
    profiles/<name>/profile.yaml   (one dir per agent profile)
    people.yaml                    (machine-level human registry; was
                                    `humans.yaml` before 2026-09-16 —
                                    the legacy filename is still read
                                    as a fallback)
    active -> profiles/<name>      (active-profile pointer symlink;
                                    was `current` before 2026-09-16 —
                                    a legacy `current` symlink is
                                    still read as a fallback)
    profiles/<name>/access.yaml    (per-agent access allowlist)

Overrides for tests + dev:

  * `MINERU_WORKSPACE_ROOT` — set the workspace root directly. When set,
    `profiles/`, the registry file, and `active` all live under it.
  * `MINERU_PROFILE_ROOT` — legacy override for the profiles base dir.
    When set (and `MINERU_WORKSPACE_ROOT` is NOT), workspace root is
    treated as the SAME path (so the registry file and `active` sit
    directly next to the per-profile directories). This keeps the
    existing test fixtures that write to `tmp_path` working without
    every test having to know about a workspace-root/profiles-base
    split.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

# Simple email-shape check. Real validation is deferred to the gog CLI
# when it actually authenticates; we just want to catch obviously-broken
# values before threading them into a subprocess argv.
_EMAIL_SHAPE_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

import typer
import yaml

from mineru_cli.profile.schema import Profile
from mineru_cli.secrets import (
    DEFAULT_BACKENDS,
    DEFAULT_ENV_PREFIX,
    OnePasswordBackend,
    SecretsConfig,
)


# --- Constants -------------------------------------------------------------

# Env var callers can set to override the active profile without touching
# `--profile` every invocation.
PROFILE_NAME_ENV_VAR = "MINERU_PROFILE"

# Env var (mostly tests + dev) that overrides the workspace root — the
# directory that holds `profiles/`, `humans.yaml`, and the `active`
# active-profile symlink. See module docstring for the layout.
WORKSPACE_ROOT_ENV_VAR = "MINERU_WORKSPACE_ROOT"

# Legacy env var that overrides the profiles base directory directly
# (kept for backward compatibility with the F3/F7 test suite that pointed
# it at `tmp_path`). When set without `MINERU_WORKSPACE_ROOT`, workspace
# root is treated as the same path.
PROFILE_BASE_DIR_ENV_VAR = "MINERU_PROFILE_ROOT"

# Env var naming the framework workspace root — the directory that holds
# `profiles/`, `humans.yaml`, and the `active` active-profile symlink.
# Default `~/.mineru`. This is the master `MINERU_HOME` seam every
# subsystem shares; a downstream user with a custom `MINERU_HOME` gets
# that path everywhere.
MINERU_HOME_ENV_VAR = "MINERU_HOME"

# Env var (mostly tests, dev, and sibling-clone dev layouts) that overrides
# the engine root — the directory that ships the public engine tree
# (charter/, prompts/, recurring/, launchd/, app-deploy/, config/, ...).
# See `default_engine_root()` for the resolution order.
ENGINE_ROOT_ENV_VAR = "MINERU_ENGINE_ROOT"


def _default_home_workspace_root() -> Path:
    """The `MINERU_HOME`-seam default workspace root (default `~/.mineru`).

    Read at call time (not import time) so a test or a caller that sets
    `MINERU_HOME` in the environment gets the override without a reload.
    """
    home = os.environ.get(MINERU_HOME_ENV_VAR)
    if home is not None and is_env_framework_managed(MINERU_HOME_ENV_VAR):
        # Our own profile export (`MINERU_HOME = workspace_absolute`, often
        # `profiles/<name>`) is for subprocesses; in-process lookups of the
        # SHARED root use the value the operator had before the export.
        home = _OPERATOR_ENV_BEFORE_EXPORT.get(MINERU_HOME_ENV_VAR)
    return Path(home or str(Path.home() / ".mineru")).expanduser()

# Opt-in that lets `workspace_absolute` equal the shared workspace root.
# Meant for the one operator whose runtime tree IS the install root (the
# owner's `~/.mineru`); every other profile keeps its own
# `profiles/<name>/` dir so per-profile state cannot leak across profiles.
RUNTIME_ROOT_KEY = "runtime_root"
RUNTIME_ROOT_SHARED = "shared"

# Profile names come from an untrusted-ish source (--profile flag,
# the `MINERU_PROFILE` env var, `active` symlink target) and
# are joined onto the base dir to reach `<base>/<name>/profile.yaml`.
# Restrict to a safe character class so a path-traversal payload
# (`../evil`, `foo/bar`) cannot punch through the workspace boundary.
_PROFILE_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

# `launchd_label_prefix` is substituted VERBATIM into rendered filenames
# under `launchd/` (via the hydrator's LABEL_PREFIX filename-token map),
# so a payload like `../../../Library/LaunchAgents/com.evil` would let a
# `--no-dry-run` hydrate write a plist OUTSIDE the sandbox target. The
# hydrator's dest-inside-target guard catches the escape; the loader
# rejects it earlier so the operator sees a clean "your profile is
# malformed" message rather than a plan-time traceback. Pattern:
#   starts with a letter, then any of [a-zA-Z0-9._-].
# Onboarding writes `com.<name>` which matches trivially; the constraint
# targets HAND-EDITED malicious values only.
_LAUNCHD_LABEL_PREFIX_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]*$")

# Filename of the active-profile pointer symlink at the workspace root.
# Renamed from `current` on 2026-09-16 (audit §2A F4) — self-documenting
# under `ls ~/.mineru/` ("active -> profiles/<name>"). The legacy `current`
# spelling is retained under `CURRENT_SYMLINK_NAME` and read as a
# fallback (see `_resolve_name_from_active_symlink`) so pre-2026-09-16
# workspaces keep resolving without a re-run of `mineru profile use`.
# Both `switch_active_profile` and `active_symlink_path` operate on the
# NEW name; only the reader falls back.
ACTIVE_SYMLINK_NAME = "active"
CURRENT_SYMLINK_NAME = "current"  # legacy fallback; still readable

# Foundation scalar fields that must be YAML strings. Non-string scalars
# used to silently coerce via `str(...)`, pushing the failure downstream
# to zoneinfo lookups or Keychain queries. Fail loud at the boundary.
_REQUIRED_STRING_FIELDS = (
    "display_name",
    "assistant_name",
    "timezone",
    "keychain_account",
    "launchd_label_prefix",
    "workspace_absolute",
    "memory_root",
    "briefs_root",
    "journal_apple_notes_folder",
)

# Absolute-path fields whose YAML value binds a filesystem root the CLI
# writes into. If any of these is empty or relative, the loader used to
# silently resolve against the process CWD — under a cron/launchd job
# whose working directory is arbitrary, that would silently point the
# memory tree or briefs output at the wrong root.
_ABSOLUTE_PATH_FIELDS = (
    "workspace_absolute",
    "memory_root",
    "briefs_root",
)

# The known secret-backend names `build_resolver` recognizes. A typo in
# `secrets.backends` used to load cleanly and only blow up later at
# `build_resolver` call time. Reject unknown names at load time.
_KNOWN_SECRETS_BACKENDS = frozenset({"env", "keychain", OnePasswordBackend.CONFIG_KEY})

# Required top-level fields (or dotted paths for nested keys). A missing
# one triggers a fail-loud error message that names the field. Kept as a
# module constant so tests can assert against the same list the loader
# enforces.
REQUIRED_FIELDS = (
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
    "secrets.backends",
    "secrets.env_prefix",
)


# --- Errors ----------------------------------------------------------------


class ProfileError(RuntimeError):
    """Raised on any loader failure (missing file, missing field, bad YAML).

    The `__str__` always names the exact path or field that failed so a
    downstream Typer handler can render it verbatim without wrapping.
    """


class NoActiveProfileError(ProfileError):
    """Raised when NO active profile is selectable at all.

    Distinct from `ProfileError` so the root callback can tolerate this
    specific case for bootstrap verbs (`profile use`, `profile current`,
    `humans list`) that exist to establish or query the pointer, while
    every other loader miss (explicit `--profile bogus`, dangling env
    var, malformed yaml) still fails loud.
    """


# --- Workspace-root + base-dir + symlink resolution ----------------------


def default_workspace_root() -> Path:
    """Return the workspace root — where `profiles/`, `humans.yaml`, and
    the `current` symlink live.

    Order:
      1. `MINERU_WORKSPACE_ROOT` env var (explicit override).
      2. `MINERU_PROFILE_ROOT` env var (legacy override; when set without
         `MINERU_WORKSPACE_ROOT`, workspace root == profiles base — so
         `humans.yaml` and `current` sit directly next to the per-profile
         directories, which is exactly what the existing `tmp_path`-based
         test fixtures expect).
      3. `MINERU_HOME` env seam, default `~/.mineru` (the framework
         workspace root).
    """
    override = os.environ.get(WORKSPACE_ROOT_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    legacy = os.environ.get(PROFILE_BASE_DIR_ENV_VAR)
    if legacy:
        return Path(legacy).expanduser().resolve()
    return _default_home_workspace_root()


def default_profiles_base_dir() -> Path:
    """Return the base directory under which `<name>/profile.yaml` lives.

    Order:
      1. `MINERU_PROFILE_ROOT` env var (legacy override used by tests + dev).
      2. `<workspace_root>/profiles/` (the target layout).
    """
    override = os.environ.get(PROFILE_BASE_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    return default_workspace_root() / "profiles"


def default_engine_root() -> Path:
    """Return the engine root — where the public engine tree lives.

    The engine tree ships `charter/`, `prompts/`, `recurring/`, `launchd/`,
    `app-deploy/`, `config/` — the source tree hydration walks. In production
    the engine clone lives at `<workspace_root>/engine/` (a sibling of
    `profiles/`, NOT the workspace root itself); in a sibling-clone dev
    layout an operator can point `MINERU_ENGINE_ROOT` at any other checkout.

    Order (mirrors the `default_workspace_root()` env-seam pattern):
      1. `MINERU_ENGINE_ROOT` env var (explicit override) — expanduser +
         resolve (follows symlinks, normalizes any `..`).
      2. `<workspace_root>/engine/` (the target production layout).

    This helper returns the path WITHOUT checking for existence. The
    fail-loud on a missing engine tree happens at `build_plan` time
    (which raises `HydrationError` when `engine_root` does not exist),
    so `--help` and non-hydrate verbs never trip on a missing tree, while
    an actual hydrate against a bogus root surfaces a clean error.

    Read at call time (not import time) so a test or a caller that sets
    `MINERU_ENGINE_ROOT` / `MINERU_HOME` / `MINERU_WORKSPACE_ROOT` in the
    environment gets the override without a reload.
    """
    override = os.environ.get(ENGINE_ROOT_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    return (default_workspace_root() / "engine").resolve()


def active_symlink_path(workspace_root: Optional[Path] = None) -> Path:
    """Return the absolute path of the `active` active-profile symlink.

    The symlink itself may not exist yet on a fresh workspace; callers
    that need to check existence should `os.path.islink(...)` and handle
    the miss explicitly.
    """
    root = workspace_root or default_workspace_root()
    return root / ACTIVE_SYMLINK_NAME


def legacy_current_symlink_path(workspace_root: Optional[Path] = None) -> Path:
    """Return the absolute path of the legacy `current` active-profile symlink.

    Retained for the 90-day compat window: `switch_active_profile` writes
    `active`, but a pre-2026-09-16 workspace may still have a `current`
    symlink from an older `profile use` — the loader reads it as a
    fallback via this helper.
    """
    root = workspace_root or default_workspace_root()
    return root / CURRENT_SYMLINK_NAME


def current_symlink_path(workspace_root: Optional[Path] = None) -> Path:
    """DEPRECATED public alias for `active_symlink_path`.

    Pre-2026-09-16 callers imported `current_symlink_path` when the
    symlink was actually named `current`. The physical symlink has since
    been renamed to `active`, so returning the `active` path is now the
    right behavior — this alias exists so external tooling that grabs
    the pointer path by the old name still lands on the currently-used
    symlink. New code should call `active_symlink_path` directly.
    """
    return active_symlink_path(workspace_root)


def resolve_profile_name(explicit: Optional[str]) -> str:
    """Pick the active profile name per the Phase-1 resolution order.

    Order:
      1. Explicit `--profile <name>` flag.
      2. `MINERU_PROFILE` env var.
      3. `active` symlink at the workspace root (`active -> profiles/<name>`),
         falling back to a legacy `current` symlink (same target shape)
         if `active` is absent — the 90-day compat window for the
         2026-09-16 rename.
      4. `ProfileError` — no silent default.

    `explicit` may be `None` (flag omitted) or the empty string (flag
    passed with no value); both fall through to the env vars / symlink.

    The returned name is validated against a safe character class
    (`[A-Za-z0-9_-]+`) so it can be joined onto the profiles base dir
    without opening a path-traversal hole.
    """
    if explicit:
        return _validated_profile_name(explicit, source="--profile flag")
    from_env = os.environ.get(PROFILE_NAME_ENV_VAR)
    if from_env:
        return _validated_profile_name(
            from_env, source=f"{PROFILE_NAME_ENV_VAR} env var"
        )
    resolved = _resolve_name_from_active_symlink()
    if resolved is not None:
        return resolved
    workspace_root = default_workspace_root()
    active = active_symlink_path(workspace_root)
    raise NoActiveProfileError(
        "no active profile: --profile flag not passed, "
        f"{PROFILE_NAME_ENV_VAR} env var not set, "
        f"and no {ACTIVE_SYMLINK_NAME!r} symlink at {active} "
        f"(nor a legacy {CURRENT_SYMLINK_NAME!r} symlink). Create one "
        "with `mineru profile use <name>`, or point --profile at an existing "
        f"profile under {default_profiles_base_dir()}."
    )


def _resolve_name_from_active_symlink() -> Optional[str]:
    """Read the `active` symlink (with `current` fallback) and return the
    pointed-at profile name.

    Preference order: the new canonical `active` name, then the legacy
    `current` name for workspaces created before the 2026-09-16 rename.

    Returns `None` when neither symlink is present (fresh workspace) so
    the caller can raise a fail-loud "no active profile" error naming
    the missing symlink path.

    Raises `ProfileError` when a candidate symlink path exists but is
    not a symlink (a regular file / dir was placed there by hand), or
    when the target basename fails the safe-name check. Silent fallback
    would leave the operator debugging why their `use` never took effect.
    """
    active = active_symlink_path()
    resolved = _read_pointer_symlink(active)
    if resolved is not None:
        return resolved
    # Legacy fallback: pre-2026-09-16 workspaces still have `current`.
    legacy = legacy_current_symlink_path()
    return _read_pointer_symlink(legacy)


def _read_pointer_symlink(path: Path) -> Optional[str]:
    """Read one pointer-symlink candidate at `path`. Return name or None.

    Shared body for `active` and legacy `current` reads. `None` means
    "this candidate is absent" (the caller may try the next one);
    ProfileError means "this candidate exists but is invalid" (fail loud).
    """
    if not os.path.islink(path):
        if path.exists():
            # A regular file/dir sits at the symlink path — refuse loudly
            # rather than treating its basename as a profile name (that
            # path would go straight into `<base>/<basename>/profile.yaml`).
            raise ProfileError(
                f"active-profile pointer at {path} is not a symlink; "
                "refusing to guess. Remove it or replace it with "
                "`mineru profile use <name>`."
            )
        return None
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise ProfileError(
            f"could not read active-profile symlink at {path}: "
            f"{type(exc).__name__}"
        ) from exc
    # Extract the last path component. A symlink target of
    # `profiles/mineru` -> name `mineru`; `mineru` -> name `mineru`;
    # `/abs/path/foo` -> name `foo`. This is safe because we then
    # validate the name and reload from `<profiles_base>/<name>/`.
    name = Path(target).name
    if not name:
        raise ProfileError(
            f"active-profile symlink at {path} has empty target basename "
            f"({target!r}); expected e.g. `profiles/mineru` or `mineru`."
        )
    return _validated_profile_name(
        name, source=f"{path.name!r} symlink at {path}"
    )


# Backward-compatible private alias so any lingering in-repo/import-star
# reference keeps working during the rename cycle. New code should call
# `_resolve_name_from_active_symlink` directly.
_resolve_name_from_current_symlink = _resolve_name_from_active_symlink


def _validated_profile_name(name: str, *, source: str) -> str:
    """Enforce the safe-character class on a resolved profile name.

    A profile name is joined onto the base dir to reach
    `<base>/<name>/profile.yaml`. Any character outside `[A-Za-z0-9_-]`
    (path separators, `..`, leading dots, whitespace) could either
    escape the base dir or produce a nonsense filesystem path. Reject
    them here, before we touch the filesystem.
    """
    if not _PROFILE_NAME_PATTERN.fullmatch(name):
        raise ProfileError(
            f"invalid profile name {name!r} (from {source}): must match "
            f"[A-Za-z0-9_-]+ (no path separators, no leading dots, no spaces)."
        )
    return name


# --- Public API -----------------------------------------------------------


def load_active_profile(
    explicit_name: Optional[str] = None,
    *,
    base_dir: Optional[Path] = None,
) -> Profile:
    """Resolve, read, and validate the active profile.

    Args:
        explicit_name: value of the root-level `--profile` flag; `None`
            when the flag was omitted.
        base_dir: override for the base directory that holds
            `<name>/profile.yaml`. Mostly for tests. Production callers
            pass `None` and let `default_profiles_base_dir()` decide.

    Returns:
        An immutable `Profile` populated from `profile.yaml`.

    Raises:
        ProfileError: no active profile is selectable (no flag/env/symlink),
            profile.yaml is missing, unreadable, malformed, or missing any
            of `REQUIRED_FIELDS`. The error message names the exact
            absolute path or dotted field.
    """
    name = resolve_profile_name(explicit_name)
    base = base_dir or default_profiles_base_dir()
    profile_root = base / name
    profile_yaml = profile_root / "profile.yaml"

    if not profile_yaml.exists():
        raise ProfileError(
            f"profile {name!r}: profile.yaml not found at {profile_yaml}. "
            "Create it (see profiles/mineru/profile.yaml for the seed) or "
            "adjust --profile / MINERU_PROFILE to point at an existing profile."
        )

    try:
        raw_text = profile_yaml.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(
            f"profile {name!r}: could not read {profile_yaml}: "
            f"{type(exc).__name__}"
        ) from exc

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(
            f"profile {name!r}: profile.yaml at {profile_yaml} is not valid YAML "
            f"({type(exc).__name__}). Fix the file and retry."
        ) from exc

    if not isinstance(data, dict):
        raise ProfileError(
            f"profile {name!r}: profile.yaml at {profile_yaml} must be a "
            f"mapping at the top level; got {type(data).__name__}."
        )

    return _build_profile(
        data=data,
        name_from_flag_or_env=name,
        profile_root=profile_root,
        profile_yaml=profile_yaml,
    )


def get_profile(ctx: Any) -> Profile:
    """Lazy-hydrate the active profile from a Typer context.

    Foundation redesign (2026-08-28 rev): the root `mineru` callback no
    longer eagerly loads the active profile — it just stashes the
    `--profile <name>` flag value on `ctx.obj["profile"]`. Every verb
    that needs a profile calls this helper at its point of use. On the
    FIRST call, `get_profile` resolves the active-profile name (per
    `resolve_profile_name`: --profile flag > env vars > `current` symlink),
    loads + validates `profile.yaml`, and caches BOTH the resulting
    `Profile` and its derived `SecretsConfig` on `ctx.obj` under the keys
    `profile_obj` and `secrets_config`. Subsequent calls return the
    cached `Profile` without re-parsing YAML.

    Failure is fail-loud: any `ProfileError` (no active profile
    selectable, `--profile bogus` with no such file, malformed YAML,
    missing required field) is re-raised as `typer.BadParameter` so the
    CLI surfaces a clean usage frame instead of a raw traceback.

    Bootstrap subapps (`profile`, `humans`) that must work WITHOUT an
    active profile simply never call this helper — they use their own
    path-resolution paths (`current_symlink_path`, `switch_active_profile`,
    the humans registry loader).

    Why lazy: rendering `--help` on a fresh clone with no `current`
    symlink used to require a heuristic that scanned argv for the token
    `--help`. That heuristic couldn't tell a flag from an option value,
    so `mineru --profile bogus telegram photo /tmp/x --caption "--help"`
    skipped profile validation and fell through to the DEFAULT profile —
    a real cross-profile isolation leak. Under lazy hydration, Click
    renders `--help` BEFORE the verb callback runs, so a verb's
    `get_profile(ctx)` is never reached on a help path (by construction),
    and a real command with a bogus profile fails loud at the first
    verb-side call.
    """
    obj = ctx.obj
    if obj is None:
        obj = {}
        try:
            ctx.obj = obj
        except AttributeError:
            # Stub context objects without a settable .obj: rehydrate
            # into whatever the caller gave us via ctx.ensure_object,
            # if that exists.
            ensure = getattr(ctx, "ensure_object", None)
            if ensure is not None:
                obj = ensure(dict)
            else:
                # No path to persist the cache; loading still works but
                # each call re-parses. Rare in prod; safe in unit tests.
                pass
    cached = obj.get("profile_obj")
    if cached is not None:
        return cached
    # `ctx.obj["profile"]` is the value of the root `--profile` flag,
    # stashed by the root callback. `None` when the flag was not passed,
    # in which case `resolve_profile_name` falls through to env vars and
    # then the `current` symlink.
    explicit = obj.get("profile")
    try:
        profile = load_active_profile(explicit)
    except ProfileError as exc:
        # `typer.BadParameter` renders as a clean CLI usage frame with
        # exit code 2. `--profile` is the parameter that most often drives
        # this failure; naming it as `param_hint` sends the operator
        # straight to the right knob even when the miss came from an env
        # var or a stale symlink.
        raise typer.BadParameter(str(exc), param_hint="--profile") from exc
    obj["profile_obj"] = profile
    obj["secrets_config"] = secrets_config_from_profile(profile)
    # Cat-B critical (2026-09-04 step-5 audit, Finding 7): export the
    # profile-scoped runtime env into `os.environ` on first hydration so
    # EVERY subprocess spawned during the rest of this command (through a
    # wrapper OR a bare `subprocess.run`) inherits the RIGHT profile's
    # workspace, Keychain namespace, and inject-queue path — not the
    # owner's default that a wrapper captured at import time. The wrappers
    # ALSO thread explicit env= dicts below (belt-and-braces), so a caller
    # that skips get_profile() still gets isolated env when it passes ctx.
    _export_profile_env(profile)
    return profile


# Snapshot of the LAST values `_export_profile_env` wrote into os.environ.
# In-process consumers (verbs like `mineru telegram inject`) consult this to
# distinguish "operator-set env override" (env value != snapshot) from
# "framework-managed" (env value == snapshot) so an operator test override
# still wins even when a prior `get_profile()` call already stamped env.
_FRAMEWORK_MANAGED_ENV: dict = {}
# The operator's own value of each key, captured the first time the
# framework overwrites it (None when the key was unset). Lets in-process
# resolvers recover the real workspace after a profile export.
_OPERATOR_ENV_BEFORE_EXPORT: dict = {}


def is_env_framework_managed(key: str) -> bool:
    """True when `os.environ[key]` matches the last framework-exported value.

    Used by in-process resolvers (`verbs/telegram._resolved_inject_queue_dir`)
    to decide whether the env value is a real operator override or just the
    framework's own `_export_profile_env` echo. Operator override → env wins;
    framework echo → derive from active profile.
    """
    import os as _os
    if key not in _FRAMEWORK_MANAGED_ENV:
        return False
    return _os.environ.get(key) == _FRAMEWORK_MANAGED_ENV[key]


def _export_profile_env(profile: Profile) -> None:
    """Export the three per-profile runtime env keys into `os.environ`.

    Keys:
      * MINERU_HOME             — every wrapper/tool that composes a path
                                  off `$MINERU_HOME` (bin/, scripts/, cache/,
                                  memory/) now targets THIS profile's tree.
      * MINERU_KEYCHAIN_ACCOUNT — every shell script that reads a Keychain
                                  slot via `security find-generic-password
                                  -a "$MINERU_KEYCHAIN_ACCOUNT"` targets
                                  THIS profile's namespace.
      * MINERU_INJECT_QUEUE_DIR — the daemon-inject queue is per-profile;
                                  computed as `<workspace>/cache/inject-queue`.

    A deliberate mutation of the process env — the fix is at the seam
    between the CLI frontend and every downstream subprocess. `deliver-
    output.py`, `push_send.py`, `cc-job-lib.sh`, the launchd trigger
    scripts, and any tool the operator drops on `$PATH` all see the
    active profile without needing to know about the wrapper layer.

    The three keys are UNCONDITIONALLY overwritten so back-to-back
    `runner.invoke()` calls in the same test process (a common CliRunner
    pattern for two-profile invariants) each see their own profile's env.
    Each written value is also recorded in `_FRAMEWORK_MANAGED_ENV` so an
    in-process resolver can distinguish "operator override" from "framework
    echo" (see `is_env_framework_managed`).
    """
    import os as _os
    workspace = getattr(profile, "workspace_absolute", None)
    if workspace is not None:
        home_value = str(workspace)
        # `workspace / "cache" / "inject-queue"` on a `Path`; keep the
        # str-cast so a duck-typed stub that returns a plain string also
        # composes cleanly via os.path.join semantics under Path().
        queue_value = str(Path(str(workspace)) / "cache" / "inject-queue")
        if not is_env_framework_managed("MINERU_HOME"):
            _OPERATOR_ENV_BEFORE_EXPORT["MINERU_HOME"] = _os.environ.get("MINERU_HOME")
        _os.environ["MINERU_HOME"] = home_value
        _os.environ["MINERU_INJECT_QUEUE_DIR"] = queue_value
        _FRAMEWORK_MANAGED_ENV["MINERU_HOME"] = home_value
        _FRAMEWORK_MANAGED_ENV["MINERU_INJECT_QUEUE_DIR"] = queue_value
    keychain = getattr(profile, "keychain_account", None)
    if keychain:
        _os.environ["MINERU_KEYCHAIN_ACCOUNT"] = keychain
        _FRAMEWORK_MANAGED_ENV["MINERU_KEYCHAIN_ACCOUNT"] = keychain


def secrets_config_from_profile(profile: Profile) -> SecretsConfig:
    """Bridge a loaded `Profile` into F2's `SecretsConfig`.

    F2's `SecretsResolver` needs three things from the profile: the
    ordered backend chain, the env prefix used by `EnvBackend`, and the
    Keychain account namespace used by `KeychainBackend`. All three live
    on the `Profile` after `load_active_profile()`.
    """
    return SecretsConfig(
        backends=list(profile.secrets_backends),
        env_prefix=profile.secrets_env_prefix,
        keychain_account=profile.keychain_account,
    )


# --- Import-time persona-name helper --------------------------------------

# Fallback persona name if `load_active_profile()` cannot succeed at import
# time (fresh checkout with no profile.yaml, no `current` symlink, YAML
# error). Kept in sync with the seed `profiles/mineru/profile.yaml`.
DEFAULT_ASSISTANT_NAME = "Mineru"


def resolve_assistant_name_for_help(fallback: str = DEFAULT_ASSISTANT_NAME) -> str:
    """Best-effort read of `Profile.assistant_name` for import-time help text.

    Typer renders the root `--help` string before the callback runs, so the
    active profile has to be resolved at import time — earlier than a `ctx.obj`
    lookup can help. That means the env vars and the `current` symlink
    PARTICIPATE (both are visible at import time), but the `--profile <name>`
    flag DOES NOT participate for the root `--help` render (Typer resolves
    `--help` before the callback). It still participates for every actual verb
    invocation, because the verb layer calls `get_profile(ctx)` which honors
    `ctx.obj["profile"]` (the flag value stashed by the root callback).

    Any failure (no active profile, missing profile.yaml, bad YAML, unexpected
    exception) is swallowed and `fallback` is returned, so a fresh checkout
    can still discover the verb tree via `mineru --help`.
    """
    try:
        return load_active_profile(None).assistant_name
    except Exception:  # noqa: BLE001 — help rendering must never crash
        return fallback


# --- Internals ------------------------------------------------------------


def _build_profile(
    *,
    data: Dict[str, Any],
    name_from_flag_or_env: str,
    profile_root: Path,
    profile_yaml: Path,
) -> Profile:
    """Validate `data` against `REQUIRED_FIELDS` and construct a `Profile`.

    Extra top-level keys (charter, connectors, cron, delivery, curation,
    ...) are preserved verbatim on `Profile.extras` for forward-compat.

    `name` from the YAML is authoritative for the field on the returned
    `Profile`. We ALSO cross-check it against the resolved profile name
    and fail loud on mismatch.
    """
    if "secrets" in data and not isinstance(data["secrets"], dict):
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: `secrets:` must be a mapping "
            f"with keys `backends` (list) and `env_prefix` (str); "
            f"got {type(data['secrets']).__name__}."
        )

    for field_path in REQUIRED_FIELDS:
        if not _has_path(data, field_path):
            raise ProfileError(
                f"profile.yaml at {profile_yaml} is missing required field "
                f"{field_path!r}. See §5.3 of the capability spec for the "
                "foundation schema."
            )

    if not isinstance(data["name"], str):
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: field 'name' must be a string, "
            f"got {type(data['name']).__name__}."
        )
    name = data["name"]
    if name != name_from_flag_or_env:
        raise ProfileError(
            f"profile.yaml at {profile_yaml} declares name={name!r} but was "
            f"loaded as {name_from_flag_or_env!r} (from --profile / "
            f"{PROFILE_NAME_ENV_VAR} / "
            f"{ACTIVE_SYMLINK_NAME!r} symlink). Align the two so downstream "
            "consumers see one canonical name."
        )

    for string_field in _REQUIRED_STRING_FIELDS:
        if not isinstance(data[string_field], str):
            raise ProfileError(
                f"profile.yaml at {profile_yaml}: field {string_field!r} must "
                f"be a string, got {type(data[string_field]).__name__}."
            )

    # Cat-B critical (2026-09-04 step-5 audit, Finding 12): reject any
    # `launchd_label_prefix` that would be unsafe when substituted into
    # a rendered filename under `launchd/`. The hydrator's dest-inside-
    # target guard is the last line of defense; this reject catches
    # malformed values at the boundary so the operator sees a clean
    # error, not a hydration-time traceback. The onboarding-generated
    # default `com.<name>` matches this pattern trivially.
    launchd_prefix = data["launchd_label_prefix"]
    if not _LAUNCHD_LABEL_PREFIX_PATTERN.fullmatch(launchd_prefix):
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: `launchd_label_prefix` "
            f"{launchd_prefix!r} must match {_LAUNCHD_LABEL_PREFIX_PATTERN.pattern!r} "
            "(letter first, then letters/digits/`.`/`_`/`-` only). "
            "Path separators, `..`, whitespace, and other shell/filesystem "
            "metacharacters are rejected because this value is substituted "
            "verbatim into rendered launchd plist filenames."
        )

    secrets_block = data["secrets"]
    backends_raw = secrets_block["backends"]
    if not isinstance(backends_raw, list) or not all(
        isinstance(b, str) for b in backends_raw
    ):
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: `secrets.backends` must be a "
            f"list of strings (e.g. {DEFAULT_BACKENDS!r})."
        )
    if not backends_raw:
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: `secrets.backends` must be a "
            f"non-empty list (e.g. {DEFAULT_BACKENDS!r}); an empty chain "
            "would silently resolve every secret as absent."
        )
    unknown_backends = [
        b for b in backends_raw if b not in _KNOWN_SECRETS_BACKENDS
    ]
    if unknown_backends:
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: unknown `secrets.backends` "
            f"entries {unknown_backends!r}; expected any of "
            f"{sorted(_KNOWN_SECRETS_BACKENDS)}."
        )
    env_prefix = secrets_block["env_prefix"]
    if not isinstance(env_prefix, str) or not env_prefix:
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: `secrets.env_prefix` must be a "
            f"non-empty string (e.g. {DEFAULT_ENV_PREFIX!r})."
        )

    google_account = _resolve_google_account(data, profile_yaml=profile_yaml)
    imessage_enabled = _resolve_imessage_enabled(data, profile_yaml=profile_yaml)

    # Cat-B critical (2026-09-04 step-5 audit, Finding 6): reject any profile
    # whose `workspace_absolute` collapses onto the SHARED workspace root
    # (`default_workspace_root()`). The onboarding bug that landed the shared
    # root here made every per-profile consumer (msearch/memory, caches,
    # cloakbrowser-profile, inject-queue, every hydrated `{{MINERU_HOME}}` in
    # plists/templates) resolve to the OWNER's tree — a real cross-profile
    # data leak. After the onboarding fix, EVERY profile's `workspace_absolute`
    # is its OWN `profiles/<name>/` dir, distinct from the shared root; a
    # profile.yaml still pointing at the shared root is the pre-fix bug on
    # disk and must fail loud so the operator migrates it before it re-leaks.
    workspace_abs = _coerce_absolute_path(
        data["workspace_absolute"],
        field_name="workspace_absolute",
        profile_yaml=profile_yaml,
    )
    # Both sides resolved: an unresolved root spelled through a symlink
    # (`/tmp` vs `/private/tmp`) used to slip past this guard.
    shared_root = default_workspace_root().resolve()
    runtime_root = data.get(RUNTIME_ROOT_KEY)
    if runtime_root is not None and runtime_root != RUNTIME_ROOT_SHARED:
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: `{RUNTIME_ROOT_KEY}` must be "
            f"{RUNTIME_ROOT_SHARED!r} when set, got {runtime_root!r}."
        )
    if workspace_abs == shared_root and runtime_root != RUNTIME_ROOT_SHARED:
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: `workspace_absolute` "
            f"({workspace_abs}) collapses onto the shared workspace root "
            f"({shared_root}); every per-profile consumer would resolve to "
            "the owner's tree. Set `workspace_absolute` to this profile's "
            "OWN directory (typically `<workspace_root>/profiles/<name>`), "
            f"or, for the single operator whose install IS the shared root, "
            f"opt in with `{RUNTIME_ROOT_KEY}: {RUNTIME_ROOT_SHARED}`. "
            "See reports/2026-09-04-mineru-step5-security-audit.md Finding 6."
        )

    return Profile(
        name=name,
        display_name=data["display_name"],
        assistant_name=data["assistant_name"],
        timezone=data["timezone"],
        keychain_account=data["keychain_account"],
        launchd_label_prefix=data["launchd_label_prefix"],
        workspace_absolute=workspace_abs,
        memory_root=_coerce_absolute_path(
            data["memory_root"],
            field_name="memory_root",
            profile_yaml=profile_yaml,
            follow_symlinks=False,
        ),
        briefs_root=_coerce_absolute_path(
            data["briefs_root"],
            field_name="briefs_root",
            profile_yaml=profile_yaml,
            follow_symlinks=False,
        ),
        journal_apple_notes_folder=data["journal_apple_notes_folder"],
        secrets_backends=list(backends_raw),
        secrets_env_prefix=env_prefix,
        google_account=google_account,
        imessage_enabled=imessage_enabled,
        profile_root=profile_root,
        profile_yaml_path=profile_yaml,
        extras=_extras_from(data),
    )


def _resolve_google_account(
    data: Dict[str, Any], *, profile_yaml: Path
) -> Optional[str]:
    """Read `google_account` off profile.yaml, preferring the top-level key.

    Historically the onboarding walkthrough wrote the email at the
    top level, but the loader never lifted it out of `extras` — so the
    field was carried through as `Profile.extras['google_account']`
    only. This helper prefers the top-level key AND accepts the legacy
    `extras`-shaped location (a nested `extras: {google_account: ...}`
    block that some older hand-edited profile.yaml files may still
    carry) so we don't break existing installs.

    Returns `None` when the field is absent — legacy profiles that
    never wired a Google account skip the per-profile `--account`
    injection and fall back to gog's own default-account behavior.
    """
    for candidate in (
        data.get("google_account"),
        (data.get("extras") or {}).get("google_account")
        if isinstance(data.get("extras"), dict)
        else None,
    ):
        if candidate is None:
            continue
        if not isinstance(candidate, str):
            raise ProfileError(
                f"profile.yaml at {profile_yaml}: `google_account` must be a "
                f"string, got {type(candidate).__name__}."
            )
        trimmed = candidate.strip()
        if not trimmed:
            raise ProfileError(
                f"profile.yaml at {profile_yaml}: `google_account` is present "
                "but empty; either remove the key or set it to the Google "
                "Workspace email this profile owns."
            )
        if not _EMAIL_SHAPE_PATTERN.fullmatch(trimmed):
            raise ProfileError(
                f"profile.yaml at {profile_yaml}: `google_account` "
                f"{trimmed!r} is not a well-formed email address."
            )
        return trimmed
    return None


def _resolve_imessage_enabled(
    data: Dict[str, Any], *, profile_yaml: Path
) -> bool:
    """Read `imessage_enabled` off profile.yaml; default `True`.

    Default-True keeps every existing profile (the seed, the owner's
    real one, every legacy hand-authored profile.yaml) working exactly
    as before — the gate opts newly-created NON-owner profiles OUT of
    iMessage, not existing ones IN.

    Fail loud on a non-bool value so a typo (`imessage_enabled: "no"`)
    doesn't quietly become a truthy string.
    """
    if "imessage_enabled" not in data:
        return True
    value = data["imessage_enabled"]
    if not isinstance(value, bool):
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: `imessage_enabled` must be a "
            f"boolean (true/false), got {type(value).__name__} {value!r}."
        )
    return value


def _has_path(data: Dict[str, Any], dotted: str) -> bool:
    """Return True iff a dotted-path key exists in the nested mapping."""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _coerce_absolute_path(
    value: Any, *, field_name: str, profile_yaml: Path,
    follow_symlinks: bool = True,
) -> Path:
    """Turn a YAML string path into an expanded, absolute `Path`.

    Absolute-path fields must be non-empty strings that are absolute after
    `~`-expansion. Empty or relative inputs used to silently resolve
    against the process CWD — under a cron/launchd job whose working
    directory is arbitrary, that would silently point the memory tree
    or briefs output at the wrong root. Fail loud instead.
    """
    if not isinstance(value, str):
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: field {field_name!r} must be "
            f"a string path, got {type(value).__name__}."
        )
    if not value.strip():
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: field {field_name!r} is empty; "
            "must be an absolute path (e.g. '/Users/you/mineru-workspace')."
        )
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise ProfileError(
            f"profile.yaml at {profile_yaml}: field {field_name!r} must be "
            f"an absolute path (starts with '/' or '~'), got {value!r}."
        )
    if follow_symlinks:
        return expanded.resolve()
    # Normalize (`..`, `.`) but do NOT follow symlinks: `memory_root` and
    # `briefs_root` may be symlinks into the profile, and rendered files must
    # carry the configured path, not where it happens to point today.
    return Path(os.path.normpath(str(expanded)))


# Foundation-required top-level keys we already consume as first-class
# fields on `Profile`. Everything else lands on `extras` verbatim.
_FOUNDATION_TOP_LEVEL_KEYS = frozenset(
    {
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
        "secrets",
        # Promoted to first-class fields on `Profile` — the loader now
        # lifts them out of `extras` and populates `Profile.google_account`
        # / `Profile.imessage_enabled` directly (see `_resolve_google_account`
        # / `_resolve_imessage_enabled`), so they must be excluded from
        # `extras` to keep the two sources from drifting.
        "google_account",
        "imessage_enabled",
    }
)


def _extras_from(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract every top-level key not consumed as a foundation field.

    Preserves forward-compat: a profile.yaml can carry `charter:`,
    `connectors:`, `cron:`, or any later-increment key without breaking
    the loader.
    """
    return {
        key: value
        for key, value in data.items()
        if key not in _FOUNDATION_TOP_LEVEL_KEYS
    }
