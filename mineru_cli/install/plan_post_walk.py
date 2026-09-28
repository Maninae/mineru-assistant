"""Post-walk planning passes: engine code, overlays, and private data.

`build_plan` walks the engine tree first; these passes then grow the plan
into a live-equivalent workspace. Each pass is a stateless function that
receives the paths it needs plus the set of dests already planned, and
returns new actions (the caller appends them and updates the set).

  * `plan_wholesale_code_dirs`: `<target>/lib -> <clone>/lib` and friends.
  * `plan_overlay_dir`: `<target>/bin/` as a real dir of per-entry links
    from the engine side and the profile side (`bin`, `scripts`, `config`).
  * `plan_user_data_dirs`: `memory`, `briefs_*`, ... -> the profile.
  * `plan_private_data_files`: `landline.json` -> the profile, always.
  * `apply_conditional_private_data_files`: `MEMORY.md` and friends link
    to the profile when present there, replacing the template render.

Every dest goes through `assert_dest_inside_target`; every symlink also
through `assert_not_self_symlink`.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Set

from mineru_cli.install.plan_constants import (
    OVERLAY_IGNORED_NAMES,
    canonical_name_for_leading_glob,
    matches_user_data_pattern,
)
from mineru_cli.install.plan_guards import (
    assert_dest_inside_target,
    assert_not_self_symlink,
    link_location,
)
from mineru_cli.install.plan_types import (
    HydrationAction,
    HydrationActionKind,
    HydrationError,
)


def make_symlink_action(
    *,
    source: Path,
    dest: Path,
    note: str,
    target_root_resolved: Path,
    logical_parent: Optional[Path] = None,
) -> HydrationAction:
    """Build one SYMLINK action after the containment and self-link checks."""
    assert_dest_inside_target(
        dest,
        target_root_resolved,
        source=source,
        follow_leaf=False,
        logical_parent=logical_parent,
    )
    assert_not_self_symlink(dest, source, logical_parent=logical_parent)
    return HydrationAction(
        kind=HydrationActionKind.SYMLINK, source=source, dest=dest, note=note
    )


def plan_wholesale_code_dirs(
    *,
    clone_root: Path,
    code_dirs: List[str],
    target_root: Path,
    target_root_resolved: Path,
    already_dests: Set[Path],
) -> List[HydrationAction]:
    """One SYMLINK per engine-code dir that ships, `<target>/<d> -> <clone>/<d>`.

    Missing sources are skipped so a partial checkout still installs.
    """
    actions: List[HydrationAction] = []
    for code_dir in code_dirs:
        source = clone_root / code_dir
        dest = target_root / code_dir
        if dest in already_dests or not source.exists():
            continue
        actions.append(
            make_symlink_action(
                source=source,
                dest=dest,
                note="engine-code dir -> engine clone",
                target_root_resolved=target_root_resolved,
            )
        )
    return actions


def plan_overlay_dir(
    *,
    dir_name: str,
    engine_dir: Path,
    profile_dir: Optional[Path],
    target_root: Path,
    target_root_resolved: Path,
    already_dests: Set[Path],
) -> List[HydrationAction]:
    """Plan `<target>/<dir_name>/` as a real dir of per-entry symlinks.

    Emits a MKDIR for the dir, then one SYMLINK per top-level entry of
    `engine_dir` (files and subdirs alike, so `scripts/firewall` links as
    one entry), then one per entry of `profile_dir`. Returns `[]` when
    neither side exists.

    - A name on both sides raises `HydrationError`: a private file must
      never shadow an engine file silently.
    - `OVERLAY_IGNORED_NAMES` (`__pycache__`, `.DS_Store`) are skipped.
    - Children are containment-checked against the dir's own location,
      not through it, because apply turns a leftover `<target>/bin`
      symlink from an older wholesale install into a real dir first.
    """
    dest_dir = target_root / dir_name
    if dest_dir in already_dests:
        return []
    sides = [("engine", engine_dir), ("profile", profile_dir)]
    present = [(label, path) for label, path in sides if path is not None and path.is_dir()]
    if not present:
        return []

    assert_dest_inside_target(
        dest_dir, target_root_resolved, source=None, follow_leaf=False
    )
    actions: List[HydrationAction] = [
        HydrationAction(
            kind=HydrationActionKind.MKDIR,
            source=None,
            dest=dest_dir,
            note=f"overlay dir ({dir_name}/): engine + profile entries",
        )
    ]
    dir_location = link_location(dest_dir)
    owner_by_name = {}
    for label, side_dir in present:
        for entry in sorted(side_dir.iterdir(), key=lambda p: p.name):
            if entry.name in OVERLAY_IGNORED_NAMES:
                continue
            if entry.name in owner_by_name:
                raise HydrationError(
                    f"overlay collision at {dest_dir / entry.name}: "
                    f"{entry} (profile) and "
                    f"{engine_dir / entry.name} ({owner_by_name[entry.name]}) "
                    "both provide it. A private file may not shadow an "
                    "engine file; rename the private one."
                )
            owner_by_name[entry.name] = label
            actions.append(
                make_symlink_action(
                    source=entry,
                    dest=dest_dir / entry.name,
                    note=f"overlay entry ({label}) -> {label} copy",
                    target_root_resolved=target_root_resolved,
                    logical_parent=dir_location,
                )
            )
    return actions


def plan_user_data_dirs(
    *,
    patterns: List[str],
    profile_root: Path,
    target_root: Path,
    target_root_resolved: Path,
    already_dests: Set[Path],
) -> List[HydrationAction]:
    """Symlink every user-data dir into the profile overlay.

    - Exact names link unconditionally (the profile dir may not exist yet;
      a dangling link starts working once the first write creates it).
    - Globs link every matching entry under `profile_root`.
    - A leading-`*` glob with no match links its canonical name instead
      (`*journal_exports` -> `journal_exports`), like an exact name.
    """
    try:
        profile_entries = sorted(
            (p.name for p in profile_root.iterdir()) if profile_root.is_dir() else []
        )
    except OSError:
        profile_entries = []

    actions: List[HydrationAction] = []
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            names = [n for n in profile_entries if matches_user_data_pattern(n, [pattern])]
            canonical = canonical_name_for_leading_glob(pattern)
            if not names and canonical is not None:
                names = [canonical]
            note = "private user-data (glob) -> profile overlay"
        else:
            names = [pattern]
            note = "private user-data -> profile overlay"
        for name in names:
            dest = target_root / name
            if dest in already_dests:
                continue
            actions.append(
                make_symlink_action(
                    source=profile_root / name,
                    dest=dest,
                    note=note,
                    target_root_resolved=target_root_resolved,
                )
            )
            already_dests.add(dest)
    return actions


def plan_private_data_files(
    *,
    filenames: List[str],
    profile_root: Path,
    target_root: Path,
    target_root_resolved: Path,
    already_dests: Set[Path],
) -> List[HydrationAction]:
    """Symlink each always-private file (`landline.json`) into the profile.

    Emitted even when the source is absent, so creating the file later
    works without a re-install.
    """
    actions: List[HydrationAction] = []
    for filename in filenames:
        dest = target_root / filename
        if dest in already_dests:
            continue
        actions.append(
            make_symlink_action(
                source=profile_root / filename,
                dest=dest,
                note="private data file -> profile overlay",
                target_root_resolved=target_root_resolved,
            )
        )
        already_dests.add(dest)
    return actions


def apply_conditional_private_data_files(
    actions: List[HydrationAction],
    *,
    relpaths: List[str],
    profile_root: Path,
    target_root: Path,
    target_root_resolved: Path,
) -> List[HydrationAction]:
    """Swap template renders for profile symlinks where private data exists.

    For each relpath whose `<profile_root>/<relpath>` exists, drop every
    non-MKDIR action already planned at `<target>/<relpath>` (the template
    render) and append a SYMLINK to the private file. Absent private files
    leave the plan untouched, so a fresh operator still gets the stub.
    """
    result = list(actions)
    for relpath in relpaths:
        source = profile_root / relpath
        if not (source.exists() or source.is_symlink()):
            continue
        dest = target_root / relpath
        result = [
            a for a in result
            if a.kind == HydrationActionKind.MKDIR or a.dest != dest
        ]
        result.append(
            make_symlink_action(
                source=source,
                dest=dest,
                note="private data file (overrides template) -> profile overlay",
                target_root_resolved=target_root_resolved,
            )
        )
    return result
