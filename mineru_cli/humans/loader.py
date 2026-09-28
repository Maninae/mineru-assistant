"""Loader for the machine-level human registry.

CANONICAL FILE NAME (2026-09-16 audit §2A F3): `people.yaml` at the
workspace root. The legacy `humans.yaml` name is READ as a fallback
during the standard 90-day compat window, mirroring the `active` /
`current` symlink layering from commit `c318df6`:

  - Readers prefer `people.yaml`; if absent, they fall back to
    `humans.yaml`. Pre-2026-09-16 workspaces keep resolving without
    forcing an operator-side rename.
  - Writers ALWAYS write the canonical `people.yaml`. When a pre-
    existing legacy `humans.yaml` is present, the writer ALSO refreshes
    it so a reader that walked in via the fallback path cannot return
    a stale answer. A fresh workspace is NOT retrofitted with a
    `humans.yaml` — the mirror only happens when one already exists.

The YAML BODY key remains `humans:` for back-compat with pre-rename
consumers (scripts and dashboards). The filename changed; the payload
shape did not.

Fail-loud contract (regardless of which physical filename was read):

  - Missing registry file             -> `HumansError` naming the exact path
                                         the loader tried (people.yaml by
                                         default, then legacy humans.yaml).
  - Non-mapping top level              -> `HumansError` naming the type seen.
  - Missing `humans:` block            -> `HumansError` naming the field.
  - Handle outside `[A-Za-z0-9_-]+`    -> `HumansError`.
  - Missing `telegram_id`/`display_name` on any entry -> `HumansError`.
  - Non-integer `telegram_id`          -> `HumansError`.
  - Duplicate handle                   -> `HumansError`.

An EMPTY registry (no `humans:` block AT ALL) is REJECTED — a machine
that runs Landline has at least one human on it, and a silently empty
registry would leave every access.yaml validation pass with "unknown
owner" errors that point at the wrong file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from mineru_cli.humans.schema import Human, HumansRegistry
from mineru_cli.profile.loader import default_workspace_root


# Canonical filename (2026-09-16 rename). Written by every writer;
# preferred by every reader.
PEOPLE_YAML_FILENAME = "people.yaml"

# Legacy filename retained for the 90-day compat window. Read as a
# fallback when `people.yaml` is absent; written ONLY as a mirror
# refresh when it already exists (never planted on a fresh workspace).
HUMANS_YAML_FILENAME = "humans.yaml"

# Same safe-character class the profile-name resolver uses. Handles are
# joined into filesystem-ish paths (via access.yaml -> "owner:" field)
# and rendered in log lines, so restrict them the same way.
_HANDLE_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class HumansError(RuntimeError):
    """Raised on any human-registry loader failure.

    The `__str__` always names the exact file path or field so a Typer
    handler can render it verbatim without wrapping.

    Type name kept as `HumansError` (not `PeopleError`) so pre-rename
    `except HumansError:` handlers keep catching the loader failures.
    """


def default_people_yaml_path(workspace_root: Optional[Path] = None) -> Path:
    """Return the canonical absolute path of `people.yaml` at the workspace root.

    Canonical write target. Every writer (`_atomic_write_humans_yaml`
    in `mineru_cli.profile.onboarding`) emits this path; every reader
    tries it first and falls back to the legacy `humans.yaml` name if
    absent (see `resolve_registry_yaml_path`).
    """
    root = workspace_root or default_workspace_root()
    return root / PEOPLE_YAML_FILENAME


def legacy_humans_yaml_path(workspace_root: Optional[Path] = None) -> Path:
    """Return the absolute path of the LEGACY `humans.yaml` (pre-rename).

    Retained for the 90-day compat window. Readers fall back to this
    path when `people.yaml` is absent; writers refresh it when it
    already exists but never plant one on a fresh workspace.
    """
    root = workspace_root or default_workspace_root()
    return root / HUMANS_YAML_FILENAME


def default_humans_yaml_path(workspace_root: Optional[Path] = None) -> Path:
    """LEGACY alias for the registry-file path helper.

    Kept so pre-2026-09-16 imports (`from mineru_cli.humans import
    default_humans_yaml_path`) continue to resolve. Returns the
    canonical `people.yaml` path — which is where the file actually
    lives post-rename — so a downstream caller that grabs the path
    by the legacy name still lands on the currently-used file. New
    code should call `default_people_yaml_path` directly.
    """
    return default_people_yaml_path(workspace_root)


def resolve_registry_yaml_path(
    workspace_root: Optional[Path] = None,
) -> Path:
    """Pick the registry-file path to READ from, honoring the fallback.

    Preference order:
      1. `<workspace_root>/people.yaml` (canonical) — if it exists.
      2. `<workspace_root>/humans.yaml` (legacy fallback) — if it
         exists AND people.yaml does not.
      3. `<workspace_root>/people.yaml` (canonical) — the "nothing on
         disk" case; the caller reads this and gets a fail-loud
         `HumansError` naming the canonical path.

    Never raises — a missing file is the caller's problem, and the
    canonical path is the right thing to name in that error message
    so the operator knows where to create it.
    """
    canonical = default_people_yaml_path(workspace_root)
    if canonical.exists():
        return canonical
    legacy = legacy_humans_yaml_path(workspace_root)
    if legacy.exists():
        return legacy
    return canonical


def load_humans_registry(
    *, path: Optional[Path] = None
) -> HumansRegistry:
    """Load and validate the machine-level human registry.

    Args:
        path: override for the registry-file path (mostly tests).
            Production callers pass `None` and let
            `resolve_registry_yaml_path()` decide (preferring
            `people.yaml`, falling back to a legacy `humans.yaml`).

    Returns:
        An immutable `HumansRegistry` with one entry per YAML block.

    Raises:
        HumansError: on missing file, malformed YAML, schema violation,
            or duplicate handle.
    """
    yaml_path = path or resolve_registry_yaml_path()
    if not yaml_path.exists():
        raise HumansError(
            f"human registry not found at {yaml_path}. Create it with "
            "the machine-level human registry — see `mineru people "
            "--help` for the expected schema."
        )
    try:
        raw_text = yaml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HumansError(
            f"could not read human registry at {yaml_path}: {type(exc).__name__}"
        ) from exc
    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise HumansError(
            f"human registry at {yaml_path} is not valid YAML "
            f"({type(exc).__name__}). Fix the file and retry."
        ) from exc
    if not isinstance(data, dict):
        raise HumansError(
            f"human registry at {yaml_path} must be a mapping at the top "
            f"level; got {type(data).__name__}."
        )
    if "humans" not in data:
        raise HumansError(
            f"human registry at {yaml_path}: missing required top-level "
            "`humans:` block. Expected shape:\n"
            "  humans:\n"
            "    <handle>:\n"
            "      telegram_id: <int>\n"
            "      display_name: <string>"
        )
    humans_block = data["humans"]
    if not isinstance(humans_block, dict):
        raise HumansError(
            f"human registry at {yaml_path}: `humans:` must be a mapping "
            f"of handle -> entry; got {type(humans_block).__name__}."
        )
    if not humans_block:
        raise HumansError(
            f"human registry at {yaml_path}: `humans:` is empty. A machine "
            "that runs Landline must have at least one human registered."
        )
    entries: "dict[str, Human]" = {}
    for handle, entry in humans_block.items():
        if not isinstance(handle, str):
            raise HumansError(
                f"human registry at {yaml_path}: handle {handle!r} must "
                f"be a string, got {type(handle).__name__}."
            )
        if not _HANDLE_PATTERN.fullmatch(handle):
            raise HumansError(
                f"human registry at {yaml_path}: handle {handle!r} must "
                "match [A-Za-z0-9_-]+ (no path separators, no spaces)."
            )
        if not isinstance(entry, dict):
            raise HumansError(
                f"human registry at {yaml_path}: entry for handle "
                f"{handle!r} must be a mapping; got {type(entry).__name__}."
            )
        human = _build_human(handle, entry, yaml_path)
        # Duplicate handles cannot actually appear in a well-formed YAML
        # (later keys shadow earlier ones silently in PyYAML), but assert
        # for clarity in case a future loader tolerates duplicates.
        entries[handle] = human
    return HumansRegistry(entries_by_handle=entries)


def _build_human(handle: str, entry: dict, yaml_path: Path) -> Human:
    """Validate one YAML entry and return an immutable `Human`."""
    for required in ("telegram_id", "display_name"):
        if required not in entry:
            raise HumansError(
                f"human registry at {yaml_path}: entry {handle!r} is "
                f"missing required field {required!r}."
            )
    telegram_id_raw: Any = entry["telegram_id"]
    if not isinstance(telegram_id_raw, int) or isinstance(telegram_id_raw, bool):
        # `bool` is a subclass of `int` in Python; a stray `true:`/`false:`
        # would otherwise sneak through. Reject explicitly.
        raise HumansError(
            f"human registry at {yaml_path}: entry {handle!r} field "
            f"`telegram_id` must be an integer, got "
            f"{type(telegram_id_raw).__name__}."
        )
    display_name = entry["display_name"]
    if not isinstance(display_name, str) or not display_name.strip():
        raise HumansError(
            f"human registry at {yaml_path}: entry {handle!r} field "
            f"`display_name` must be a non-empty string."
        )
    return Human(
        handle=handle,
        telegram_id=telegram_id_raw,
        display_name=display_name,
    )
