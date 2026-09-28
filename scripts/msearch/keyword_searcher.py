"""Deterministic keyword search across tags and file content."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .index_builder import Index


def search_tags(keyword: str, index: Index) -> list[dict]:
    """Find files whose tags match the keyword (exact or substring).

    Args:
        keyword: Search term (case-insensitive).
        index: The tag index to search.

    Returns:
        List of dicts with 'file' and 'matched_tags' keys.
    """
    keyword_lower = keyword.lower()
    file_matches: dict[str, list[str]] = {}

    for tag, files in index.tag_to_files.items():
        if keyword_lower in tag:
            for f in files:
                if f not in file_matches:
                    file_matches[f] = []
                file_matches[f].append(tag)

    return [{"file": f, "matched_tags": sorted(tags)} for f, tags in sorted(file_matches.items())]


def search_content(keyword: str, dirs: list[Path], root: Path | None = None) -> list[dict]:
    """Grep for keyword in markdown files across given directories (recursively).

    Args:
        keyword: Search term (case-insensitive).
        dirs: Directories to search.
        root: Workspace root; results are keyed by workspace-relative path so
            distinct files with the same basename don't collide.

    Returns:
        List of dicts with 'file' and 'matched_lines' keys.
    """
    existing_dirs = [str(d.resolve()) for d in dirs if d.is_dir()]
    if not existing_dirs:
        return []

    try:
        result = subprocess.run(
            ["grep", "-inr", "--include=*.md", keyword] + existing_dirs,
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if not result.stdout.strip():
        return []

    root_resolved = root.resolve() if root is not None else None

    file_lines: dict[str, list[dict]] = {}
    for line in result.stdout.strip().split('\n'):
        # Format: filepath:linenum:text
        parts = line.split(':', 2)
        if len(parts) < 3:
            continue
        filepath, line_num_str, text = parts
        try:
            line_num = int(line_num_str)
        except ValueError:
            continue

        # Key by workspace-relative path so distinct files with the same
        # basename (e.g. multiple profile.md or README.md) don't merge.
        p = Path(filepath)
        key: str
        if root_resolved is not None:
            try:
                key = str(p.resolve().relative_to(root_resolved))
            except ValueError:
                key = p.name
        else:
            key = p.name

        if key not in file_lines:
            file_lines[key] = []
        file_lines[key].append({"line": line_num, "text": text.strip()})

    return [{"file": f, "matched_lines": lines} for f, lines in sorted(file_lines.items())]


def search(keyword: str, index: Index, dirs: list[Path], root: Path | None = None) -> dict:
    """Combined tag and content search.

    Args:
        keyword: Search term (case-insensitive).
        index: The tag index.
        dirs: Directories to search for content.
        root: Workspace root; passed through so content matches are keyed by
            workspace-relative path (disambiguates same-basename files).

    Returns:
        Dict with 'tag_matches' and 'content_matches' keys.
    """
    return {
        "tag_matches": search_tags(keyword, index),
        "content_matches": search_content(keyword, dirs, root=root),
    }
