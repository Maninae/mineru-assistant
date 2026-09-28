"""Apply pre-flight: every refusal decided BEFORE the first write.

`apply_plan` calls `preflight_plan` once; if it raises, nothing on disk
has changed. The rules themselves are documented in `apply.py`'s module
docstring (the single home for them); this module implements them.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import List, Optional, Set

from mineru_cli.install.plan_constants import FRAMEWORK_RESERVED_NAMES
from mineru_cli.install.plan_types import (
    HydrationActionKind,
    HydrationError,
    HydrationPlan,
)

# How many offending paths an error message spells out before eliding.
_OFFENDER_PREVIEW_LIMIT = 8


def infer_target_root(plan: HydrationPlan) -> Optional[Path]:
    """Return the plan's target root (dest of the first MKDIR action)."""
    for action in plan.actions:
        if action.kind == HydrationActionKind.MKDIR:
            return action.dest
    return None


def preflight_plan(plan: HydrationPlan, *, force: bool) -> None:
    """Raise `HydrationError` listing every conflict, or return cleanly."""
    target_root = infer_target_root(plan)
    if target_root is None:
        return
    if not force:
        _refuse_stray_target_content(plan, target_root)
    _refuse_dest_conflicts(plan, target_root, force=force)


def _is_reserved(name: str) -> bool:
    """True when `name` is framework or runtime state (never install output)."""
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in FRAMEWORK_RESERVED_NAMES)


def _is_operator_link_inside(entry: Path, target_root: Path) -> bool:
    """A root symlink whose target resolves inside the target is the operator's own
    link (a compat name, a private file linked from the profile): never a stray."""
    if not entry.is_symlink():
        return False
    try:
        resolved = entry.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(target_root.resolve())
    except ValueError:
        return False
    return True


def _planned_top_level_names(plan: HydrationPlan, target_root: Path) -> Set[str]:
    """First path component (under the target) of every planned dest."""
    names: Set[str] = set()
    for action in plan.actions:
        try:
            relative = action.dest.relative_to(target_root)
        except ValueError:
            continue
        if relative.parts:
            names.add(relative.parts[0])
    return names


def _refuse_stray_target_content(plan: HydrationPlan, target_root: Path) -> None:
    """Refuse a target holding entries that are neither reserved nor planned.

    Catches an operator's unrelated tree sitting at `--target`, while
    letting `profile init` output and a previous install's output through.
    """
    if not target_root.is_dir() or target_root.is_symlink():
        return
    planned = _planned_top_level_names(plan, target_root)
    strays = sorted(
        entry.name
        for entry in target_root.iterdir()
        if entry.name not in planned
        and not _is_reserved(entry.name)
        and not _is_operator_link_inside(entry, target_root)
    )
    if strays:
        raise HydrationError(
            f"target_root {target_root} already exists and is non-empty with "
            f"{len(strays)} entries this install does not own "
            f"({_preview(strays)}); refusing to install over them. Move them "
            "aside, or install into an empty directory."
        )


def _refuse_dest_conflicts(plan: HydrationPlan, target_root: Path, *, force: bool) -> None:
    """Refuse planned dests whose current on-disk shape apply cannot take."""
    hard: List[str] = []
    forceable: List[str] = []
    # A MKDIR dest that is a symlink gets replaced by an empty real dir
    # before its children are written, so what shows through the link now
    # (often the engine's own files) is not a conflict for those children.
    replaced_pointers = [
        a.dest for a in plan.actions
        if a.kind == HydrationActionKind.MKDIR and a.dest != target_root and a.dest.is_symlink()
    ]
    for action in plan.actions:
        dest = action.dest
        if dest == target_root:
            continue
        if any(pointer in dest.parents for pointer in replaced_pointers):
            continue
        is_link = dest.is_symlink()
        if not is_link and not dest.exists():
            continue
        is_dir = dest.is_dir() and not is_link
        if action.kind == HydrationActionKind.MKDIR:
            if is_link:
                forceable.append(f"{dest} (symlink where a real dir is planned)")
            elif not is_dir:
                hard.append(f"{dest} (file where a dir is planned)")
        elif action.kind == HydrationActionKind.SYMLINK:
            if is_dir:
                hard.append(f"{dest} (real dir where a symlink is planned)")
            elif not is_link:
                forceable.append(f"{dest} (real file where a symlink is planned)")
        elif action.kind == HydrationActionKind.RENDER:
            if is_link:
                hard.append(f"{dest} (symlink where a rendered file is planned)")
            elif is_dir:
                hard.append(f"{dest} (real dir where a rendered file is planned)")
    offenders = hard + ([] if force else forceable)
    if not offenders:
        return
    hint = (
        "Real directories and symlinks at render paths are never replaced "
        "(data-loss guard); move them aside and re-run."
    )
    if forceable and not force:
        hint += (
            " Real files and pointer symlinks are replaced only with the "
            "library-only force=True (the CLI has no force flag)."
        )
    raise HydrationError(
        f"{len(offenders)} planned install path(s) already exist in a shape "
        f"the install will not overwrite: {_preview(offenders)}. {hint}"
    )


def _preview(items: List[str]) -> str:
    """Comma-join up to `_OFFENDER_PREVIEW_LIMIT` items, eliding the rest."""
    shown = ", ".join(items[:_OFFENDER_PREVIEW_LIMIT])
    extra = len(items) - _OFFENDER_PREVIEW_LIMIT
    return shown + (f", ... (+{extra} more)" if extra > 0 else "")
