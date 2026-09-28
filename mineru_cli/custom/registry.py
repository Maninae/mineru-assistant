"""User-defined custom-verb registry — YAML-backed, atomic writes at 0600.

This module owns `<profile_root>/custom_verbs.yaml`, the source of truth for
`mineru custom {add,list,remove,show}` and for the dynamic dispatch that
turns each registered entry into a `mineru <name>` command at CLI-load time.

Schema per entry (spec §0 decision #9, materialized here):

  name:           string, matches ^[a-z][a-z0-9-]*$; must NOT collide with any
                  built-in verb name (from `mineru_cli.app.app.registered_groups`
                  or `.registered_commands`).
  description:    one-line string used as the `--help` short description.
  command:        list[str], the argv template shelled out via subprocess.run;
                  supports a `{args}` placeholder that gets replaced with the
                  Typer extra-args pass-through when the dispatcher fires.
  cwd:            optional absolute directory (default = profile.workspace_absolute).
  schedule:       optional cron-ish string (informational until Phase 4).
  deploy_notes:   optional multi-line free text shown by `custom show`.
  env:            optional dict[str, str] added to the subprocess env.

⚠️ SAFETY (READ BEFORE EDITING) ⚠️

  This module does NO network I/O. It never invokes the shelled-out command
  itself — that is the dispatcher's job in `mineru_cli.verbs.custom`. The
  registry only serializes / deserializes the YAML, validates entries, and
  hands out immutable `CustomVerbEntry` snapshots.

  Every write is atomic (O_EXCL on a sibling temp path + rename) and the
  file is chmod'd 0600 before any bytes hit disk. A registry leak (e.g.
  a world-readable `profile_root/`) would expose the shell command a user
  registered — which may itself be sensitive (e.g. `foo --token=$X`) — so
  the 0600 mode is the load-bearing security control here.

  `custom_verbs.yaml` missing entirely is a valid state (fresh install,
  no verbs registered yet). Every read returns an empty registry rather
  than raising. The fail-loud contract only applies to WRITES: `add_entry`
  refuses a malformed / colliding entry with `CustomVerbError` carrying
  the exact failure reason.

Design invariants:

  - The registry is per-profile: the file lives under
    `<profile.profile_root>/custom_verbs.yaml`. A different profile has
    its own file and its own set of registered verbs.
  - Atomic writes: `_atomic_write_600` opens a sibling `.tmp-<pid>-<ts>`
    at 0600 via `O_EXCL | O_CREAT | O_WRONLY` (so a racing writer fails
    early rather than corrupting each other's temp file), writes the
    payload, fsyncs, then `os.replace`s onto the final path. The final
    file is thus never observed half-written by a concurrent reader.
  - No custom-verb NAME may collide with a built-in verb name. Collision
    detection is at add-time, not load-time, because a runtime skip-and-
    warn (see `mineru_cli.verbs.custom.register_custom_dispatch`) is the
    correct behavior when a downstream engine adds a NEW built-in that
    shadows an already-registered custom verb: we do not want to break
    the CLI on that upgrade, so the load path warns instead of raising.
    But at REGISTRATION time we refuse loudly so the user knows.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml


# --- Constants -------------------------------------------------------------

# Filename that holds the per-profile registry. Kept as a module constant so
# tests can import it and assert on the exact file the writer touched.
CUSTOM_VERBS_FILENAME = "custom_verbs.yaml"

# Directory permission bits for the profile root when we create it. We do NOT
# tighten an existing profile_root that the loader already produced; that dir
# may be shared with other tooling. Only the registry FILE gets 0600.
PROFILE_DIR_MODE = 0o700

# File permission bits for the registry YAML. 0600 keeps it readable only by
# the profile's macOS user, matching the retention-cache posture in
# `mineru_cli.wrappers.telegram_image_cache`.
REGISTRY_FILE_MODE = 0o600

# Regex for valid custom-verb names. Matches the spec's §0 decision #9 shape:
# lowercase kebab-case, must start with a letter, may contain digits and
# hyphens after that. Deliberately narrow so a custom verb name cannot
# accidentally look like a flag (`-x`), a path fragment (`foo/bar`), or a
# shell metachar (`foo|bar`).
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")

# Env override lets tests point every discovery+write at a `tmp_path` without
# needing to build a full Profile. When set, the registry lives at
# `<override>/custom_verbs.yaml` regardless of the profile. Production
# callers omit it and let the profile's `profile_root` decide.
CUSTOM_VERBS_ROOT_ENV = "MINERU_CUSTOM_VERBS_ROOT"

# The set of built-in verb names we must never let a custom verb shadow.
# Populated lazily from `mineru_cli.app.app.registered_groups` +
# `.registered_commands` at check-time so a new built-in noun added by
# Phase 4+ is picked up automatically. Kept as a function (not a module
# constant) so the app-import order stays deterministic — importing
# `mineru_cli.app` from this module would create a cycle.


# --- Errors ----------------------------------------------------------------


class CustomVerbError(RuntimeError):
    """Raised on invalid entry, name collision, or IO failure.

    The `__str__` always names the exact field or path that failed so a
    downstream Typer handler can render it verbatim without wrapping.
    """


# --- Data model ------------------------------------------------------------


@dataclass(frozen=True)
class CustomVerbEntry:
    """Immutable snapshot of one registered custom verb.

    Frozen so a dispatcher can hold a reference without worrying that the
    registry has drifted mid-invocation. To pick up a changed
    `custom_verbs.yaml`, call `CustomVerbRegistry.load(...)` again.

    `command` is stored as a tuple (dataclass-frozen-friendly) but the
    to_yaml / from_yaml round-trip serializes it as a plain list because
    that is the YAML idiom users expect to hand-edit.
    """

    name: str
    description: str
    command: Tuple[str, ...]
    cwd: Optional[str] = None
    schedule: Optional[str] = None
    deploy_notes: Optional[str] = None
    env: Mapping[str, str] = field(default_factory=dict)
    # When True (default, backward-compatible), the dispatcher seeds the
    # subprocess env from `os.environ.copy()` + `entry.env`. When False,
    # it seeds from a MINIMAL base (PATH, HOME, USER, LANG) + `entry.env`
    # so a shelled-out third-party script does NOT inherit the parent
    # shell's tokens (MINERU_SECRET_*, OAuth material, etc.). Only
    # opt-out because a decade of `custom_verbs.yaml` files predating
    # this field must keep working unchanged.
    env_inherit: bool = True

    def to_yaml_dict(self) -> Dict[str, Any]:
        """Return the dict shape the on-disk YAML uses for this entry.

        Only fields the user actually set are emitted — a `null` schedule
        or empty env would just add noise to the hand-editable file.
        `env_inherit` is emitted only when False, so a fresh add without
        the flag produces the same YAML shape it always did.
        """
        payload: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "command": list(self.command),
        }
        if self.cwd:
            payload["cwd"] = self.cwd
        if self.schedule:
            payload["schedule"] = self.schedule
        if self.deploy_notes:
            payload["deploy_notes"] = self.deploy_notes
        if self.env:
            payload["env"] = dict(self.env)
        if not self.env_inherit:
            payload["env_inherit"] = False
        return payload

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CustomVerbEntry":
        """Build an entry from a YAML mapping; raise on shape errors.

        Called on both add-time (fresh entry from Typer prompts) and
        load-time (rehydrating an existing YAML file). The validation is
        identical on both paths so a hand-edited YAML that violates the
        schema fails just as loudly as a bad `custom add` call.
        """
        if not isinstance(data, Mapping):
            raise CustomVerbError(
                f"custom verb entry must be a mapping; got {type(data).__name__}."
            )
        name_raw = data.get("name")
        if not isinstance(name_raw, str) or not name_raw:
            raise CustomVerbError(
                "custom verb entry missing 'name' (must be a non-empty string)."
            )
        _validate_name_shape(name_raw)

        description_raw = data.get("description")
        if not isinstance(description_raw, str) or not description_raw:
            raise CustomVerbError(
                f"custom verb {name_raw!r}: 'description' must be a non-empty string."
            )

        command_raw = data.get("command")
        if not isinstance(command_raw, list) or not command_raw:
            raise CustomVerbError(
                f"custom verb {name_raw!r}: 'command' must be a non-empty list of strings."
            )
        if not all(isinstance(x, str) for x in command_raw):
            raise CustomVerbError(
                f"custom verb {name_raw!r}: every 'command' element must be a string."
            )

        cwd_raw = data.get("cwd")
        if cwd_raw is not None and not isinstance(cwd_raw, str):
            raise CustomVerbError(
                f"custom verb {name_raw!r}: 'cwd' must be a string or omitted."
            )
        # Absolute-path enforcement: a relative cwd would silently pick up
        # whatever directory Typer was invoked from, which is a footgun in
        # a launchd-run cron. Empty string is allowed as "unset".
        if cwd_raw and not os.path.isabs(cwd_raw):
            raise CustomVerbError(
                f"custom verb {name_raw!r}: 'cwd' must be an absolute path; got {cwd_raw!r}."
            )

        schedule_raw = data.get("schedule")
        if schedule_raw is not None and not isinstance(schedule_raw, str):
            raise CustomVerbError(
                f"custom verb {name_raw!r}: 'schedule' must be a string or omitted."
            )

        deploy_notes_raw = data.get("deploy_notes")
        if deploy_notes_raw is not None and not isinstance(deploy_notes_raw, str):
            raise CustomVerbError(
                f"custom verb {name_raw!r}: 'deploy_notes' must be a string or omitted."
            )

        env_raw = data.get("env")
        if env_raw is None:
            env_dict: Dict[str, str] = {}
        elif not isinstance(env_raw, Mapping):
            raise CustomVerbError(
                f"custom verb {name_raw!r}: 'env' must be a mapping of str -> str."
            )
        else:
            env_dict = {}
            for key, val in env_raw.items():
                if not isinstance(key, str) or not isinstance(val, str):
                    raise CustomVerbError(
                        f"custom verb {name_raw!r}: 'env' keys/values must all be strings."
                    )
                env_dict[key] = val

        env_inherit_raw = data.get("env_inherit", True)
        if not isinstance(env_inherit_raw, bool):
            raise CustomVerbError(
                f"custom verb {name_raw!r}: 'env_inherit' must be a boolean (true/false)."
            )

        return cls(
            name=name_raw,
            description=description_raw,
            command=tuple(command_raw),
            cwd=cwd_raw or None,
            schedule=schedule_raw or None,
            deploy_notes=deploy_notes_raw or None,
            env=env_dict,
            env_inherit=env_inherit_raw,
        )


# --- Path resolution ------------------------------------------------------


def registry_path_for_profile(profile: Any) -> Path:
    """Return the absolute registry path for `profile`, honoring the env override.

    `profile` is the `Profile` dataclass hydrated by `load_active_profile`.
    We accept it as `Any` here to avoid an import cycle with
    `mineru_cli.profile` (the registry is a leaf module; the profile
    layer knows about the registry via `custom_verbs.yaml` schema alone).

    Env-override precedence mirrors `MINERU_PROFILE_ROOT` in the loader:
    when `MINERU_CUSTOM_VERBS_ROOT` is set, we drop the file at
    `<override>/custom_verbs.yaml`. This is the ONLY way tests point the
    registry at a `tmp_path` without needing to build a full Profile.
    """
    override = os.environ.get(CUSTOM_VERBS_ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve() / CUSTOM_VERBS_FILENAME
    profile_root = getattr(profile, "profile_root", None)
    if profile_root is None:
        raise CustomVerbError(
            "cannot resolve custom_verbs.yaml: profile has no `profile_root`. "
            "Set MINERU_CUSTOM_VERBS_ROOT for the ephemeral test path."
        )
    return Path(profile_root) / CUSTOM_VERBS_FILENAME


# --- Built-in verb collision detection ------------------------------------


def builtin_verb_names() -> frozenset[str]:
    """Return every built-in verb name at the root of the mineru CLI.

    Reaches into `mineru_cli.app.app` lazily to avoid an import cycle: the
    registry is imported by `mineru_cli.verbs.custom`, which is imported
    by `mineru_cli.app`. Deferring the import to call-time lets `app.py`
    finish importing before we touch it. See the module docstring's
    collision-detection design note.

    Includes both noun groups (added via `add_typer`, e.g. `telegram`,
    `finance`) and top-level bare commands (added via `@app.command`,
    e.g. `brevity`), so `custom add` refuses any name that would clash.
    """
    from mineru_cli.app import app as root_app

    names: set[str] = set()
    for group in getattr(root_app, "registered_groups", []):
        if group.name:
            names.add(group.name)
    for command in getattr(root_app, "registered_commands", []):
        if command.name:
            names.add(command.name)
    return frozenset(names)


def _validate_name_shape(name: str) -> None:
    """Raise on a name that fails the regex; message names the value."""
    if not NAME_PATTERN.match(name):
        raise CustomVerbError(
            f"custom verb name {name!r} is invalid: names must match "
            f"{NAME_PATTERN.pattern!r} (lowercase, start with a letter, "
            "hyphens allowed after the first character)."
        )


def validate_no_collision(name: str, builtin_names: Iterable[str]) -> None:
    """Raise if `name` collides with any known built-in verb.

    Kept as a public helper so the `custom add` verb can validate BEFORE
    it prompts for the rest of the fields (a collision on `gmail` should
    fail before the user types a description).
    """
    builtin_set = set(builtin_names)
    if name in builtin_set:
        raise CustomVerbError(
            f"custom verb name {name!r} collides with a built-in verb. "
            "Pick a different name (e.g. add a prefix like `sam-` or "
            "`my-`)."
        )


# --- Atomic write ---------------------------------------------------------


def _atomic_write_600(target: Path, payload: str) -> None:
    """Write `payload` to `target` atomically at mode 0600.

    Sequence:
      1. Ensure the parent dir exists.
      2. Open a sibling `.tmp-<pid>-<ns>` path with `O_EXCL | O_CREAT | O_WRONLY`
         and mode 0600 — the exclusive create guarantees we do NOT race
         with a concurrent writer's temp file (each writer picks a unique
         suffix from `os.getpid()` + `time.time_ns()`).
      3. Write the bytes, fsync the FD (durability guarantee against a
         crash between write and rename), close.
      4. `os.replace` the temp path onto the final path — atomic swap on
         POSIX. A concurrent reader either sees the pre-swap contents or
         the post-swap contents, never a half-written file.

    Idempotent: if the target already exists, it is REPLACED (rename is
    destructive). Callers that need "create only" semantics must do their
    own existence check first — which is what `add_entry` does when it
    refuses to overwrite an existing name.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f"{target.name}.tmp-{os.getpid()}-{time.time_ns()}"

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(tmp_path), flags, REGISTRY_FILE_MODE)
    except FileExistsError as exc:
        # Extremely narrow race: another writer picked the exact same
        # pid+ns suffix. We surface loudly on the first collision
        # (single-writer safe) rather than retrying — a second writer
        # here is a real anomaly worth failing the command on.
        raise CustomVerbError(
            f"temp path collision writing {target}: {exc}. Retry the command."
        ) from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Clean up the temp file on any failure so the parent dir does
        # not accumulate half-written `.tmp-*` cruft.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise

    # Enforce mode defensively — some filesystems ignore the mode arg to
    # `os.open`. A no-op on a well-behaved local disk; correctness step
    # on the others.
    try:
        os.chmod(tmp_path, REGISTRY_FILE_MODE)
        os.replace(str(tmp_path), str(target))
    except BaseException:
        # A chmod/replace failure at this point still needs the temp file
        # cleaned up so the parent dir doesn't accumulate `.tmp-*` cruft
        # a future writer would have to reason about.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# --- Registry surface -----------------------------------------------------


@dataclass(frozen=True)
class CustomVerbRegistry:
    """In-memory snapshot of the on-disk custom-verbs registry.

    Immutable: `add_entry` / `remove_entry` return a NEW registry rather
    than mutating this one. The disk write is orchestrated by the caller
    (typically the `custom add` verb) via `save_to`, so a caller that
    wants to compose multiple changes into one atomic write can do so.

    In practice the verbs go one-change-per-write (the workflow is
    interactive), but the shape keeps the option open.
    """

    entries: Tuple[CustomVerbEntry, ...] = ()
    path: Optional[Path] = None

    # ---- construction ---------------------------------------------------

    @classmethod
    def load(cls, profile: Any) -> "CustomVerbRegistry":
        """Load the registry for `profile`; missing file -> empty registry.

        Discovery is FAIL-OPEN: a missing custom_verbs.yaml is a valid
        state (fresh install), so we return an empty registry rather
        than raising. Malformed YAML or a schema-invalid entry, however,
        fails LOUD — that's a real bug the operator needs to fix.
        """
        path = registry_path_for_profile(profile)
        return cls.load_from(path)

    @classmethod
    def load_from(cls, path: Path) -> "CustomVerbRegistry":
        """Load from an explicit path (used by tests + by `load`)."""
        if not path.exists():
            return cls(entries=(), path=path)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CustomVerbError(
                f"could not read custom verbs registry at {path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            data = yaml.safe_load(raw_text) or {}
        except yaml.YAMLError as exc:
            raise CustomVerbError(
                f"custom verbs registry at {path} is not valid YAML: {exc}"
            ) from exc

        if not isinstance(data, Mapping):
            raise CustomVerbError(
                f"custom verbs registry at {path} must be a mapping at the "
                f"top level; got {type(data).__name__}."
            )

        verbs_raw = data.get("verbs")
        if verbs_raw is None:
            return cls(entries=(), path=path)
        if not isinstance(verbs_raw, list):
            raise CustomVerbError(
                f"custom verbs registry at {path}: `verbs:` must be a list; "
                f"got {type(verbs_raw).__name__}."
            )

        entries: List[CustomVerbEntry] = []
        seen: set[str] = set()
        for idx, raw in enumerate(verbs_raw):
            entry = CustomVerbEntry.from_mapping(raw)
            if entry.name in seen:
                raise CustomVerbError(
                    f"custom verbs registry at {path}: duplicate verb name "
                    f"{entry.name!r} at index {idx}. Remove or rename one."
                )
            seen.add(entry.name)
            entries.append(entry)
        return cls(entries=tuple(entries), path=path)

    # ---- read surface ---------------------------------------------------

    def list_entries(self) -> Tuple[CustomVerbEntry, ...]:
        """Return every registered entry (order matches on-disk order)."""
        return self.entries

    def get_entry(self, name: str) -> Optional[CustomVerbEntry]:
        """Return the entry named `name`, or None on miss.

        Name lookup is case-sensitive: the regex forces lowercase, so a
        case-insensitive lookup would silently accept an invalid input.
        """
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    # ---- write surface --------------------------------------------------

    def add_entry(
        self,
        entry: CustomVerbEntry,
        *,
        builtin_names: Optional[Iterable[str]] = None,
    ) -> "CustomVerbRegistry":
        """Return a new registry with `entry` appended.

        Validates the entry's name shape + collision against the built-in
        verb set BEFORE appending. Refuses to overwrite an existing entry
        with the same name — the caller must `remove_entry` first if
        they want to replace it. This is deliberate: a silent overwrite
        would eat any operator-authored changes to `deploy_notes` etc.
        """
        _validate_name_shape(entry.name)
        if self.get_entry(entry.name) is not None:
            raise CustomVerbError(
                f"custom verb {entry.name!r} already registered. Run "
                f"`mineru custom remove {entry.name}` first if you want to replace it."
            )
        builtin = frozenset(builtin_names) if builtin_names is not None else builtin_verb_names()
        validate_no_collision(entry.name, builtin)
        return CustomVerbRegistry(entries=(*self.entries, entry), path=self.path)

    def remove_entry(self, name: str) -> "CustomVerbRegistry":
        """Return a new registry without the entry named `name`.

        Raises if the name is not present — a silent no-op would leave
        the operator thinking the entry was gone when it was not.
        """
        if self.get_entry(name) is None:
            raise CustomVerbError(
                f"custom verb {name!r} is not registered; nothing to remove."
            )
        remaining = tuple(e for e in self.entries if e.name != name)
        return CustomVerbRegistry(entries=remaining, path=self.path)

    # ---- persistence ----------------------------------------------------

    def save_to(self, path: Optional[Path] = None) -> Path:
        """Serialize this registry to disk atomically at 0600.

        `path` defaults to the registry's own `.path` (set at load-time).
        An empty registry writes a file with `verbs: []` so a subsequent
        read gets an empty registry back without hitting the "missing
        file -> empty" fallback (nice for scripted assertions).
        """
        target = path or self.path
        if target is None:
            raise CustomVerbError(
                "save_to: no path — construct the registry via `load(profile)` "
                "or pass an explicit path."
            )
        payload_dict = {"verbs": [e.to_yaml_dict() for e in self.entries]}
        text = yaml.safe_dump(
            payload_dict,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
        # Guarantee trailing newline so `diff` between two identical
        # registries never reports a whitespace-only delta.
        if not text.endswith("\n"):
            text += "\n"
        _atomic_write_600(target, text)
        return target
