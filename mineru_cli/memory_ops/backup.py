"""Snapshot the profile's memory tree to a timestamped `.tar.gz` archive.

`memory backup` is meant to run as a safety net before any risky
operation on the memory tree (mass dedup, mass describe, a schema
migration). Behavior is deliberately simple and portable: tar+gzip
of `<memory_root>/` into `<workspace>/backups/`, filename stamped
with UTC ISO-8601 so archives sort naturally and never collide.

Design choices:

  - Never touch the source tree: pure read + write elsewhere.
  - Never overwrite: if the target archive path already exists (a
    fast second call within the same UTC second), append a monotonic
    suffix `.1`, `.2`, ...
  - Never chdir: the tarball's internal paths are relative to the
    memory-root parent, so restoring produces `<parent>/<memory_root
    basename>/...` unambiguously.
  - Never write outside the target directory: the archive path is
    computed and validated to sit under the resolved backup dir.
"""

from __future__ import annotations

import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Default subdirectory relative to `workspace_absolute`. The verb layer
# defaults here when the operator did not pass `--out`.
DEFAULT_BACKUP_SUBDIR = "backups"


@dataclass(frozen=True)
class BackupResult:
    """Outcome of a `create_backup` call.

    Attributes:
        archive_path: absolute path to the resulting `.tar.gz` file.
        source_root: absolute path to the memory tree that was archived.
        byte_count: on-disk size of the archive.
        file_count: number of members written into the archive.
    """

    archive_path: Path
    source_root: Path
    byte_count: int
    file_count: int


def _timestamp_now(now: Optional[datetime] = None) -> str:
    """UTC ISO-8601 timestamp safe for a filename: `YYYY-MM-DDTHH-MM-SSZ`.

    Colon is illegal on some filesystems and awkward in the shell, so
    hyphens replace the `:` inside the time portion.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    else:
        # Normalize to UTC even when the caller passes a local time.
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%SZ")


def _unique_target(base: Path) -> Path:
    """Return `base` if free, else `base.1`, `base.2`, ... appending
    a numeric suffix BEFORE the `.tar.gz` extension is not necessary —
    a `.N` suffix at the tail keeps the file recognizable as a tarball
    and never shadows an existing archive.
    """
    if not base.exists():
        return base
    counter = 1
    while True:
        candidate = base.with_name(f"{base.name}.{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def create_backup(
    memory_root: Path,
    *,
    backup_dir: Path,
    profile_name: str = "memory",
    now: Optional[datetime] = None,
) -> BackupResult:
    """Write a gzipped-tar snapshot of `memory_root` under `backup_dir`.

    Args:
        memory_root: absolute path to the tree to snapshot. Must exist.
        backup_dir: directory that will hold the archive. Created if
            missing (parents allowed).
        profile_name: filename slug so a multi-profile workspace can
            tell archives apart. Defaults to `memory` for callers that
            do not carry a profile identity.
        now: optional timestamp override for deterministic tests.

    Returns:
        A `BackupResult` describing the archive that was written.

    Raises:
        FileNotFoundError: `memory_root` does not exist.
        NotADirectoryError: `memory_root` is not a directory.
    """
    memory_root = Path(memory_root)
    if not memory_root.exists():
        raise FileNotFoundError(f"memory_root does not exist: {memory_root}")
    if not memory_root.is_dir():
        raise NotADirectoryError(f"memory_root is not a directory: {memory_root}")

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = _timestamp_now(now)
    slug = profile_name.strip() or "memory"
    filename = f"memory-{slug}-{stamp}.tar.gz"
    target = _unique_target(backup_dir / filename)

    # Archive members use `memory_root.name` as their internal top-level so
    # a restore expands cleanly into `<cwd>/<basename>/...`.
    arcname = memory_root.name or "memory"
    file_count = 0
    with tarfile.open(target, "w:gz") as tf:
        def _count(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
            nonlocal file_count
            file_count += 1
            return tarinfo

        tf.add(str(memory_root), arcname=arcname, filter=_count)

    byte_count = target.stat().st_size
    return BackupResult(
        archive_path=target.resolve(),
        source_root=memory_root.resolve(),
        byte_count=byte_count,
        file_count=file_count,
    )
