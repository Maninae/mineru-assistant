"""Portable profile bundle format for `profile export` and `profile import`.

A bundle is a `.tar.gz` archive of the profile directory
(`<workspace_root>/profiles/<name>/`) laid out so the top-level entry
inside the archive is the profile name itself. Same shape the profile
loader expects on disk, so `import` is a plain extract + rename.

Security model (read before changing what gets included):
  - Secrets NEVER travel in a bundle. `mineru secrets` writes to the
    macOS Keychain, and the Keychain is a per-machine store — a secret
    exported here would be a plaintext leak. The bundler explicitly
    EXCLUDES any path segment matching a secrets pattern (`secrets.yaml`,
    `secrets/`, `.env*`, `id_*`, `*.pem`, `*.key`, `*token*`, `*cookie*`,
    `*session*.json`, `credentials*`, `*apikey*`).
  - The archive is written owner-only (0600): it carries personal data.
  - Cache and log noise (`cache/`, `logs/`, `__pycache__/`, `*.pyc`,
    `.git/`, `.DS_Store`) never help the operator on the receiving end
    and can carry stale device-specific state, so they're excluded too.
  - Include list is a whitelist of file suffixes typical for profile
    config (`.yaml`, `.yml`, `.md`, `.json`, `.toml`) plus a tolerated
    catch-all for other regular files under the profile root — but
    every candidate is filtered through the exclude list first, so a
    hand-planted secret named `secrets.yaml` cannot slip through even
    if a well-meaning operator drops it into a config subdirectory.

Extraction discipline for `import`:
  - The archive is extracted to a temp dir OUTSIDE the workspace so a
    malformed archive can never partial-write into `<profiles_base>/`.
  - Every member is checked for path traversal (`..` segments,
    absolute paths, symlink escapes) BEFORE extraction. A hostile
    tarball cannot land a file outside its extraction root.
  - Once the temp extract validates, the whole tree is atomically moved
    into `<profiles_base>/<name>/` via `shutil.move`.

This module is stdlib-only (no `zipfile`, no `zstandard`, no custom
codecs). `.tar.gz` is universally understood.
"""

from __future__ import annotations

import fnmatch
import io
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

import yaml


# --- Exclusion / inclusion policy ----------------------------------------

# Directory names that never travel in a bundle. Matched as a full path
# segment (case-sensitive on POSIX; the profile tree is under the
# operator's `$HOME`, and every mineru workspace is POSIX-shaped).
EXCLUDED_DIR_NAMES = frozenset(
    {
        "cache",
        "logs",
        ".git",
        "__pycache__",
        ".venv",
        "node_modules",
        # `secrets/` as a directory name is a common convention (matches
        # the write-side hygiene rule in secrets/CLAUDE.md-style
        # policies). Reject the whole subtree.
        "secrets",
    }
)

# File basenames that unambiguously carry secrets. Matched as the full
# basename (case-insensitive so `.ENV` and `.env` both hit).
EXCLUDED_FILE_BASENAMES = frozenset(
    {
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
        ".env",
        ".ds_store",
    }
)

# Glob suffixes for files we always reject. Matched against the LOWERED
# basename so `.PEM` and `.pem` both hit. Includes:
#   - private-key extensions (`.pem`, `.key`, `.p12`, `.pfx`)
#   - editor / OS scratch (`.swp`, `.swo`, `.pyc`, `.DS_Store`)
#   - shell history (`.bash_history`, `.zsh_history`) — unlikely to
#     appear in a profile root, but the belt-and-braces line is one
#     entry per pattern.
EXCLUDED_SUFFIXES = frozenset(
    {
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".env",
        ".pyc",
        ".swp",
        ".swo",
    }
)

# Prefixes matched against the basename (case-insensitive). SSH-style
# `id_*` keypairs and any file starting with `.env`.
EXCLUDED_BASENAME_PREFIXES = ("id_", ".env")

# fnmatch globs against the LOWERED basename of files AND directories,
# mirroring a secrets-aware .gitignore. Broad on purpose: a note that merely
# mentions "token" in its filename stays home, and the export listing shows it.
EXCLUDED_BASENAME_GLOBS = (
    "*token*",
    "*cookie*",
    "*session*.json",
    "credentials*",
    "*.pem",
    "*.key",
    "*apikey*",
    ".env*",
)

# The bundle holds personal profile data, so it is created owner-only.
EXPORT_ARCHIVE_FILE_MODE = 0o600


class BundleError(RuntimeError):
    """Raised on any export / import failure with an operator-facing message.

    The `__str__` always names the file, directory, or bundle path that
    triggered the failure so a Typer handler can render it verbatim.
    """


@dataclass(frozen=True)
class ExportPlan:
    """Preview of what an export will include.

    Attributes:
        profile_name: name of the profile being exported.
        profile_root: source directory on disk.
        out_path: target archive path.
        included: relative paths (posix-style) that will land in the
            archive, in walk order.
        excluded: relative paths that were rejected, with the reason.
    """

    profile_name: str
    profile_root: Path
    out_path: Path
    included: List[str] = field(default_factory=list)
    excluded: List[dict] = field(default_factory=list)


def _reason_to_exclude(rel_path: Path, is_dir: bool) -> Optional[str]:
    """Return an exclusion reason for `rel_path`, or `None` to include.

    `rel_path` is relative to the profile root; walk the segments and
    check each against the directory blacklist. Also check the basename
    against the file / suffix / prefix blacklists.
    """
    for segment in rel_path.parts[:-1]:
        if segment in EXCLUDED_DIR_NAMES:
            return f"under excluded directory {segment!r}"
    basename = rel_path.name
    lowered = basename.lower()
    for pattern in EXCLUDED_BASENAME_GLOBS:
        if fnmatch.fnmatchcase(lowered, pattern):
            return f"excluded pattern {pattern!r}"
    if is_dir:
        if basename in EXCLUDED_DIR_NAMES:
            return f"excluded directory name {basename!r}"
        return None
    if lowered in EXCLUDED_FILE_BASENAMES:
        return f"excluded file name {basename!r}"
    for suffix in EXCLUDED_SUFFIXES:
        if lowered.endswith(suffix):
            return f"excluded suffix {suffix!r}"
    for prefix in EXCLUDED_BASENAME_PREFIXES:
        if lowered.startswith(prefix):
            return f"excluded basename prefix {prefix!r}"
    return None


def build_export_plan(
    profile_name: str, profile_root: Path, out_path: Path
) -> ExportPlan:
    """Walk the profile root, decide include/exclude for each entry.

    Never touches `out_path` — that write is `write_export_archive`'s
    job. Returned plan can be inspected in tests + rendered by the CLI
    as the "what's included" listing.
    """
    if not profile_root.is_dir():
        raise BundleError(
            f"profile export: profile root {profile_root} is not a "
            "directory."
        )
    plan = ExportPlan(
        profile_name=profile_name,
        profile_root=profile_root,
        out_path=out_path,
    )
    for dirpath, dirnames, filenames in os.walk(profile_root):
        rel_dir = Path(dirpath).relative_to(profile_root)
        # Prune excluded directories in-place so os.walk doesn't recurse
        # into them (and their contents never enter the plan at all).
        kept_dirs = []
        for d in list(dirnames):
            child_rel = rel_dir / d
            reason = _reason_to_exclude(child_rel, is_dir=True)
            if reason is None:
                kept_dirs.append(d)
            else:
                plan.excluded.append(
                    {"path": (child_rel).as_posix() + "/", "reason": reason}
                )
        # Sort for deterministic walk output (tests + operator listing).
        dirnames[:] = sorted(kept_dirs)
        for filename in sorted(filenames):
            rel_file = rel_dir / filename
            reason = _reason_to_exclude(rel_file, is_dir=False)
            if reason is None:
                plan.included.append(rel_file.as_posix())
            else:
                plan.excluded.append(
                    {"path": rel_file.as_posix(), "reason": reason}
                )
    return plan


def write_export_archive(plan: ExportPlan) -> None:
    """Emit the `.tar.gz` archive described by `plan`.

    Every member is written under a top-level `<profile_name>/`
    directory so an unpacked archive lays out as
    `<extract_root>/<profile_name>/profile.yaml`, matching the on-disk
    layout the loader expects.

    Never opens or reads an excluded path (belt-and-braces: the plan
    already excluded them).
    """
    out_path = plan.out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file O_EXCL at 0600, so the bundle is never
    # readable by others, not even for the instant before a chmod. The
    # finished temp file replaces the target (overwrite, parity with `tar czf`).
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".partial", dir=str(out_path.parent)
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(temp_fd, EXPORT_ARCHIVE_FILE_MODE)
        with os.fdopen(temp_fd, "wb") as temp_file:
            with tarfile.open(fileobj=temp_file, mode="w:gz") as tar:
                for rel_posix in plan.included:
                    src = plan.profile_root / rel_posix
                    arcname = f"{plan.profile_name}/{rel_posix}"
                    # `add(recursive=False)`: recursion comes from the plan's
                    # flat list, so an implicitly added subdir cannot slip in
                    # every child (including a re-excluded one).
                    tar.add(src, arcname=arcname, recursive=False)
        os.replace(temp_path, out_path)
    except BaseException:
        if temp_path.exists():
            temp_path.unlink()
        raise


# --- Import path --------------------------------------------------------


@dataclass(frozen=True)
class BundleContents:
    """Snapshot of a validated bundle after safe extraction.

    Attributes:
        profile_name: the profile name read from the archive's
            top-level directory (also cross-checked against the YAML).
        temp_root: temp dir where the bundle was extracted (caller
            owns cleanup — `import_bundle` does that for you).
        profile_dir: absolute path to `<temp_root>/<profile_name>/`.
    """

    profile_name: str
    temp_root: Path
    profile_dir: Path


def _member_is_safe(member: tarfile.TarInfo, extract_root: Path) -> bool:
    """Return True iff `member` extracts INSIDE `extract_root`.

    Reject:
      - Absolute paths in `member.name`.
      - `..` components anywhere in the resolved path.
      - Symlinks / hardlinks whose target escapes `extract_root`.
    """
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        return False
    resolved = (extract_root / name).resolve()
    try:
        resolved.relative_to(extract_root.resolve())
    except ValueError:
        return False
    if member.issym() or member.islnk():
        link_target = member.linkname or ""
        if link_target.startswith("/") or ".." in Path(link_target).parts:
            return False
        # Resolve the link target relative to the member's directory.
        anchor = (extract_root / name).parent
        link_resolved = (anchor / link_target).resolve()
        try:
            link_resolved.relative_to(extract_root.resolve())
        except ValueError:
            return False
    return True


def _safe_extract(archive: tarfile.TarFile, extract_root: Path) -> List[str]:
    """Extract `archive` into `extract_root`, refusing any unsafe member.

    Returns the list of extracted names (posix). Raises `BundleError`
    on the first unsafe member so the operator sees a targeted message
    instead of a partially-populated extract root.
    """
    extracted: List[str] = []
    for member in archive.getmembers():
        if not _member_is_safe(member, extract_root):
            raise BundleError(
                f"profile import: bundle member {member.name!r} escapes "
                "the extraction root (path traversal or unsafe symlink). "
                "Refusing to extract."
            )
    # All members checked; safe to extract.
    archive.extractall(extract_root)
    extracted = [m.name for m in archive.getmembers()]
    return extracted


def open_bundle(bundle_path: Path, extract_root: Path) -> BundleContents:
    """Extract `bundle_path` into `extract_root` and validate its shape.

    Shape requirements:
      - Archive is a valid gzipped tar.
      - After extraction, exactly ONE directory sits at the root
        (`<extract_root>/<profile_name>/`).
      - That directory contains a readable `profile.yaml` whose `name:`
        field matches the top-level directory name.
      - `name` matches the safe-character class enforced by the
        profile loader (`[A-Za-z0-9_-]+`).
    """
    from mineru_cli.profile.loader import _validated_profile_name

    if not bundle_path.exists():
        raise BundleError(
            f"profile import: bundle {bundle_path} does not exist."
        )
    try:
        with tarfile.open(bundle_path, "r:gz") as tar:
            _safe_extract(tar, extract_root)
    except tarfile.TarError as exc:
        raise BundleError(
            f"profile import: {bundle_path} is not a valid .tar.gz "
            f"archive ({type(exc).__name__})."
        ) from exc

    top_entries = [
        p for p in extract_root.iterdir() if not p.name.startswith(".")
    ]
    if len(top_entries) != 1 or not top_entries[0].is_dir():
        raise BundleError(
            f"profile import: {bundle_path} must contain exactly one "
            "top-level profile directory; found "
            f"{[p.name for p in top_entries]!r}."
        )
    profile_dir = top_entries[0]
    top_name = profile_dir.name
    # Enforce the safe-character class BEFORE we ever build a path with
    # this name (`<profiles_base>/<name>/`) — a `..` here would be a
    # path traversal.
    _validated_profile_name(top_name, source=f"top-level dir of {bundle_path}")

    profile_yaml = profile_dir / "profile.yaml"
    if not profile_yaml.exists():
        raise BundleError(
            f"profile import: {bundle_path} is missing "
            f"{profile_yaml.relative_to(extract_root)} — every valid "
            "profile bundle carries a profile.yaml at the top level."
        )
    try:
        data = yaml.safe_load(profile_yaml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BundleError(
            f"profile import: profile.yaml inside {bundle_path} is not "
            f"valid YAML ({type(exc).__name__})."
        ) from exc
    if not isinstance(data, dict):
        raise BundleError(
            f"profile import: profile.yaml inside {bundle_path} must be "
            f"a mapping at the top level; got {type(data).__name__}."
        )
    yaml_name = data.get("name")
    if not isinstance(yaml_name, str) or not yaml_name:
        raise BundleError(
            f"profile import: profile.yaml inside {bundle_path} is "
            "missing a string `name:` field."
        )
    if yaml_name != top_name:
        raise BundleError(
            f"profile import: bundle's top-level directory is "
            f"{top_name!r} but its profile.yaml declares name={yaml_name!r}. "
            "The two must match so the loader picks it up under one "
            "canonical name."
        )
    return BundleContents(
        profile_name=top_name,
        temp_root=extract_root,
        profile_dir=profile_dir,
    )


def install_bundle_contents(
    contents: BundleContents,
    profiles_base_dir: Path,
) -> Path:
    """Move a validated `BundleContents` into `<profiles_base>/<name>/`.

    Fails loud if the target dir already exists (name-uniqueness rule).
    Uses `shutil.move` so the extracted tree lands atomically at the
    destination on the common (same-filesystem) case.
    """
    dest = profiles_base_dir / contents.profile_name
    if dest.exists():
        raise BundleError(
            f"profile import: target {dest} already exists — refusing to "
            "overwrite. Remove the existing profile first (or pick a "
            "different profile name in the source bundle) and retry."
        )
    profiles_base_dir.mkdir(parents=True, exist_ok=True)
    # `shutil.move` on the same filesystem is a rename (atomic); across
    # filesystems it copies + removes. Either way the destination is
    # complete before the tempdir cleanup runs.
    shutil.move(str(contents.profile_dir), str(dest))
    return dest


__all__ = [
    "BundleContents",
    "BundleError",
    "EXCLUDED_BASENAME_GLOBS",
    "EXCLUDED_BASENAME_PREFIXES",
    "EXCLUDED_DIR_NAMES",
    "EXCLUDED_FILE_BASENAMES",
    "EXCLUDED_SUFFIXES",
    "ExportPlan",
    "build_export_plan",
    "install_bundle_contents",
    "open_bundle",
    "write_export_archive",
]
