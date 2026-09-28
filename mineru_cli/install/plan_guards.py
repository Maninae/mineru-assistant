"""Planning-time path guards: sandbox containment and self-symlink refusal.

Every dest the planner emits passes through `assert_dest_inside_target`;
every SYMLINK additionally passes through `assert_not_self_symlink`.
Both run before `apply_plan` writes anything, so a bad plan fails loud
with zero side effects.

Containment has two modes, chosen by what apply will do at the dest:
  - `follow_leaf=True` (RENDER, and the target root): apply writes bytes
    AT the dest, so a symlink planted at the leaf would redirect the
    write. The check resolves the whole path, leaf included.
  - `follow_leaf=False` (SYMLINK, MKDIR): apply replaces or refuses a
    leaf symlink and never writes through it, so only the link's own
    location matters. This is what lets a re-install walk over the
    previous install's symlinks, which point at the engine clone
    (outside the target) by design.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from mineru_cli.install.plan_types import HydrationError


def link_location(path: Path, *, logical_parent: Optional[Path] = None) -> Path:
    """Return where `path` itself sits on disk, without following its leaf.

    The parent chain is resolved (a symlinked parent dir still counts);
    the leaf is appended verbatim and the result normalized so a `..`
    leaf cannot sneak past. `logical_parent` replaces the resolved parent
    when the caller knows the parent will be materialized as a real dir
    before this path is written (the overlay dirs).
    """
    parent = logical_parent if logical_parent is not None else path.parent.resolve()
    return Path(os.path.normpath(str(parent / path.name)))


def assert_dest_inside_target(
    dest: Path,
    target_root_resolved: Path,
    *,
    source: Optional[Path],
    follow_leaf: bool = True,
    logical_parent: Optional[Path] = None,
) -> None:
    """Fail loud if `dest` escapes `target_root_resolved` after normalization.

    Closes path traversal from user-controlled strings joined onto the
    target (a hand-edited `launchd_label_prefix`, a poisoned engine tree
    with `..` in a template filename, a symlinked parent dir pointing
    outside). `Path.resolve()` normalizes `..` even on nonexistent paths,
    so a traversal payload collapses to its real destination first.

    Args:
        dest: the planned destination.
        target_root_resolved: the resolved sandbox root.
        source: the action source, named in the error message.
        follow_leaf: resolve the leaf too (see module docstring).
        logical_parent: see `link_location`; only used when
            `follow_leaf=False`.
    """
    if follow_leaf:
        resolved = dest.resolve()
    else:
        resolved = link_location(dest, logical_parent=logical_parent)
    if resolved == target_root_resolved or resolved.is_relative_to(target_root_resolved):
        return
    source_note = f" from source {source}" if source is not None else ""
    raise HydrationError(
        f"hydration dest {dest} (resolves to {resolved}){source_note} "
        f"escapes target root {target_root_resolved}; refusing to "
        "materialize a path outside the sandbox. Check "
        "`launchd_label_prefix` in profile.yaml and any `..` in template "
        "filenames under the engine tree."
    )


def assert_not_self_symlink(
    dest: Path, source: Path, *, logical_parent: Optional[Path] = None
) -> None:
    """Fail loud when a planned symlink would point at its own location.

    Two real layouts produce this: an engine tree installed as a real dir
    at `<target>/engine` (the clone root becomes `<target>`, so
    `<target>/bin -> <target>/bin`), and `--target` equal to the profile
    root (`profiles/x/memory -> profiles/x/memory`). Apply would either
    refuse on a real dir or create a looping link; neither is recoverable
    without the operator noticing, so the plan refuses up front.
    """
    location = link_location(dest, logical_parent=logical_parent)
    if source.resolve() != location:
        return
    raise HydrationError(
        f"planned symlink {dest} would point at itself (source {source} "
        f"resolves to the same path {location}). The install target "
        "overlaps the engine clone or the profile root; pass a --target "
        "that is neither, and keep the engine at <workspace>/engine as a "
        "symlink to the clone's engine/ dir."
    )
