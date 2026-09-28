"""Build and cache the tag index from markdown files."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .tag_extractor import extract_tags


@dataclass
class Index:
    """In-memory tag index mapping tags to files and vice versa."""

    tag_to_files: dict[str, list[str]] = field(default_factory=dict)
    file_to_tags: dict[str, list[str]] = field(default_factory=dict)
    all_tags: list[str] = field(default_factory=list)
    built_at: float = 0.0


def _key_for(f: Path, root: Path | None) -> str:
    """Key a file by its path relative to the workspace root when possible.

    Falls back to relative-to-scanned-dir, then to basename, so distinct files
    with the same basename (e.g. multiple profile.md or README.md) never
    silently merge into one key.
    """
    if root is not None:
        try:
            return str(f.resolve().relative_to(root.resolve()))
        except ValueError:
            pass
    return f.name


def build_index(dirs: list[Path], root: Path | None = None) -> Index:
    """Scan markdown files under the given directories (recursively) and build a tag index.

    Args:
        dirs: List of directories to scan for .md files (recursively).
        root: Workspace root; files are keyed by their path relative to it so
            distinct files with the same basename don't collide.

    Returns:
        Populated Index with tag mappings.
    """
    tag_to_files: dict[str, list[str]] = {}
    file_to_tags: dict[str, list[str]] = {}

    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.md")):
            tags = extract_tags(f)
            if not tags:
                continue

            rel = _key_for(f, root)
            file_to_tags[rel] = tags

            for tag in tags:
                tag_lower = tag.lower().strip()
                if not tag_lower:
                    continue
                if tag_lower not in tag_to_files:
                    tag_to_files[tag_lower] = []
                if rel not in tag_to_files[tag_lower]:
                    tag_to_files[tag_lower].append(rel)

    for tag in tag_to_files:
        tag_to_files[tag] = sorted(tag_to_files[tag])

    all_tags = sorted(tag_to_files.keys())

    return Index(
        tag_to_files=dict(sorted(tag_to_files.items())),
        file_to_tags=dict(sorted(file_to_tags.items())),
        all_tags=all_tags,
        built_at=time.time(),
    )


def _serialize_index(index: Index) -> dict:
    return {
        "tag_to_files": index.tag_to_files,
        "file_to_tags": index.file_to_tags,
        "all_tags": index.all_tags,
        "built_at": index.built_at,
    }


def _deserialize_index(data: dict) -> Index:
    return Index(
        tag_to_files=data.get("tag_to_files", {}),
        file_to_tags=data.get("file_to_tags", {}),
        all_tags=data.get("all_tags", []),
        built_at=data.get("built_at", 0.0),
    )


def load_or_build(cache_path: Path, dirs: list[Path], max_age: int = 300, root: Path | None = None) -> Index:
    """Load index from cache if fresh, otherwise rebuild and save.

    Args:
        cache_path: Path to the JSON cache file.
        dirs: Directories to scan if rebuild is needed.
        max_age: Maximum cache age in seconds before rebuild.
        root: Workspace root passed through to build_index for path keying.

    Returns:
        The tag index, either from cache or freshly built.
    """
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            index = _deserialize_index(data)
            if time.time() - index.built_at < max_age:
                return index
        except (json.JSONDecodeError, KeyError):
            pass

    index = build_index(dirs, root=root)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(_serialize_index(index), indent=2))
    return index
