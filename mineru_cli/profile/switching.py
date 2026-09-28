"""Atomic active-profile switching (`mineru profile use <name>`).

Rewrites the `active` symlink at the workspace root to point at a target
profile directory. The write is atomic: a temporary symlink is created
next to `active` and `os.replace`'d over it in a single syscall, so a
concurrent reader sees either the old value or the new value, never a
partial state.

The symlink was renamed from `current` to `active` on 2026-09-16 (audit
§2A F4). The writer here now targets `active`; the reader in
`loader._resolve_name_from_active_symlink` still falls back to reading
a pre-rename `current` symlink so existing workspaces resolve without
being forced to re-run `profile use`.

Safety discipline (matches `mineru_cli.verbs.cron` install hardening):

  - Refuse to overwrite a REGULAR file at the `active` path. Only a
    pre-existing symlink or a missing path is acceptable — a regular
    file there was placed by hand, and clobbering it silently would
    surprise the operator.
  - Refuse to point at a non-existent profile (missing `profile.yaml`)
    or a name that fails the safe-character class.
  - Symlink target is stored RELATIVE to the workspace root when the
    profile lives at `<workspace_root>/profiles/<name>` (the target
    layout), so the workspace tree remains portable across machines
    without an absolute-path rewrite.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from mineru_cli.profile.loader import (
    ACTIVE_SYMLINK_NAME,
    CURRENT_SYMLINK_NAME,
    ProfileError,
    _validated_profile_name,
    active_symlink_path,
    default_profiles_base_dir,
    default_workspace_root,
    legacy_current_symlink_path,
)


def _relative_target(profile_dir: Path, workspace_root: Path) -> str:
    """Return the string to store in the `active` symlink.

    We prefer a workspace-root-relative target when possible so the
    symlink survives moving the workspace tree; fall back to the
    absolute path when the profile lives outside the workspace (an
    exotic MINERU_PROFILE_ROOT override, e.g.).

    IMPORTANT: we deliberately do NOT `.resolve()` `profile_dir`. When
    `<profiles_base>/<name>` is itself a symlink to an external
    location (a test fixture or a hand-managed layout), following the
    symlink and then storing the resolved external path here silently
    changes the profile NAME the reader will later extract via
    `Path(target).name`. The un-resolved path preserves the intended
    basename (`alice`), so a downstream `load_active_profile()` looks
    at `<base>/alice/profile.yaml` — the same file the operator
    activated. See tests/test_profile.py :: `test_switch_active_profile_symlinked_profile_dir`.
    """
    try:
        rel = profile_dir.relative_to(workspace_root)
        return str(rel)
    except ValueError:
        # profile_dir lives outside workspace_root (legacy
        # MINERU_PROFILE_ROOT override); store its absolute form, but
        # still without `.resolve()` so the last path component is the
        # profile name, not whatever a symlink points at.
        return str(profile_dir)


def switch_active_profile(
    name: str,
    *,
    workspace_root: Optional[Path] = None,
    profiles_base_dir: Optional[Path] = None,
) -> Path:
    """Atomically re-point the `active` symlink at profile `name`.

    Args:
        name: profile name to activate. Must match `[A-Za-z0-9_-]+` and
            resolve to an existing `<profiles_base>/<name>/profile.yaml`.
        workspace_root: override for the workspace root (mostly tests).
        profiles_base_dir: override for the profiles base dir (mostly tests).

    Returns:
        The absolute path of the `active` symlink after the swap.

    Raises:
        ProfileError: on an invalid name, a target profile with no
            `profile.yaml`, or an `active` path that is a regular file
            (not a symlink and not missing).
    """
    _validated_profile_name(name, source="`profile use <name>` argument")
    ws = workspace_root or default_workspace_root()
    base = profiles_base_dir or default_profiles_base_dir()
    profile_dir = base / name
    profile_yaml = profile_dir / "profile.yaml"
    if not profile_yaml.exists():
        raise ProfileError(
            f"profile {name!r}: refusing to activate — profile.yaml not "
            f"found at {profile_yaml}. Create the profile first or check "
            f"the spelling."
        )

    active = active_symlink_path(ws)
    ws.mkdir(parents=True, exist_ok=True)

    # Refuse to clobber a real file at the `active` path. `os.path.islink`
    # is True even for a dangling symlink; that's fine (we replace it).
    # `active.exists()` follows symlinks, so a healthy symlink is `exists()`;
    # combine the checks explicitly.
    if active.exists() and not os.path.islink(active):
        # Regular file or directory sits at the pointer path. Refuse
        # loudly rather than deleting whatever the operator put there.
        raise ProfileError(
            f"refusing to overwrite non-symlink at {active}. Remove it "
            f"manually and retry `mineru profile use {name}`."
        )
    # A dangling symlink (islink True, exists False) is fine to replace.

    target = _relative_target(profile_dir, ws)

    # Atomic swap: create a tmp symlink next to `active`, then
    # `os.replace` it. `os.replace` is atomic on POSIX; a concurrent
    # reader sees either the old value or the new value, never a
    # partial state. We use a PID-suffixed tmp name so two concurrent
    # `profile use` invocations don't collide on the tmp path.
    tmp = ws / f".{ACTIVE_SYMLINK_NAME}.tmp.{os.getpid()}"
    if tmp.exists() or os.path.islink(tmp):
        # Left over from a crashed previous run. Safe to remove — it's
        # our own PID-suffixed name.
        tmp.unlink()
    os.symlink(target, tmp)
    try:
        os.replace(tmp, active)
    except OSError:
        # Clean up the tmp on any failure so a retry starts fresh.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    # Migration-window bookkeeping (2026-09-16 rename): if a pre-rename
    # `current` symlink still lives in this workspace, refresh it to
    # point at the same new target so a downstream reader that walked
    # in via the legacy fallback path can't return a stale answer if
    # `active` were later removed. If no legacy symlink is present we
    # do nothing — no need to plant one on a fresh workspace. A regular
    # file at the legacy path is left alone (we already refuse to
    # clobber non-symlinks at the `active` path above; the legacy path
    # gets the same courtesy).
    legacy = legacy_current_symlink_path(ws)
    if os.path.islink(legacy):
        legacy_tmp = ws / f".{CURRENT_SYMLINK_NAME}.tmp.{os.getpid()}"
        if legacy_tmp.exists() or os.path.islink(legacy_tmp):
            legacy_tmp.unlink()
        os.symlink(target, legacy_tmp)
        try:
            os.replace(legacy_tmp, legacy)
        except OSError:
            try:
                legacy_tmp.unlink()
            except OSError:
                pass
            # Don't fail the whole switch on a legacy-mirror failure —
            # `active` is written, and that's the canonical read source.

    return active


__all__ = ["switch_active_profile"]
