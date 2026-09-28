"""Render the annotated memory tree.

Behavioral reference: the live `generate-memory-tree.sh`. The engine
version drops the shell dependency (no `tree` binary needed) and stays
generic (root path is passed in).

For each `.md` file under `memory_root`, look up its YAML-frontmatter
`description:` and append it inline after the filename. Two directories
are collapsed to a summary line (their file count only): `daily/` and
`monthly/` — those hold transient rolling logs, not curated notes, and
listing every file every session drowns the tree.

Output layout (fenced markdown block, ready to paste into a prompt):

    ## Current Structure

    ```
    <tree lines with inline # descriptions>
    |-- daily/  # N session logs
    |-- monthly/  # M monthly summaries
    ```
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional


# YAML frontmatter: `---\n...key: value...\n---\n` at top of a .md file.
# We only care about the `description:` line inside the leading block.
_FRONTMATTER_OPEN_RE = re.compile(r"^---\s*$")
_DESCRIPTION_LINE_RE = re.compile(
    r"^description:\s*(?P<value>.*?)\s*$"
)

# Directories excluded from the file-by-file listing (still summarized).
_SUMMARIZED_DIRS = ("daily", "monthly")
_IGNORED_NAMES = {"__pycache__", ".DS_Store"}
_IGNORED_SUFFIXES = (".pyc",)


def _strip_quotes(value: str) -> str:
    """Trim matching leading/trailing quotes (single or double)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _read_description(md_path: Path, max_scan_lines: int = 40) -> str:
    """Return the `description:` value from a `.md` file's frontmatter.

    Returns `""` when there is no frontmatter block, no description
    key inside it, or the file is unreadable. Only scans the first
    `max_scan_lines` lines so a large note does not slow the walker.
    """
    try:
        with md_path.open("r", encoding="utf-8", errors="replace") as fp:
            lines: List[str] = []
            for _ in range(max_scan_lines):
                line = fp.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
    except OSError:
        return ""

    if not lines or not _FRONTMATTER_OPEN_RE.match(lines[0]):
        return ""
    in_block = False
    for line in lines:
        if _FRONTMATTER_OPEN_RE.match(line):
            if in_block:
                # Closing fence reached without finding a description.
                return ""
            in_block = True
            continue
        if not in_block:
            continue
        match = _DESCRIPTION_LINE_RE.match(line)
        if match:
            return _strip_quotes(match.group("value"))
    return ""


def _is_ignored(entry: Path) -> bool:
    """Skip caches, macOS junk, and compiled artifacts."""
    if entry.name in _IGNORED_NAMES:
        return True
    return any(entry.name.endswith(suf) for suf in _IGNORED_SUFFIXES)


def _walk_lines(
    root: Path,
    current: Path,
    prefix: str,
    out: List[str],
    summarized: Iterable[str],
) -> None:
    """Emit tree lines for `current` at depth `prefix`.

    Uses the classic `|-- name` / `\\-- name` style from `tree` so the
    output reads the same as the reference shell script's fallback.
    Directories in `summarized` are pruned here (only at the top level)
    and reported once at the tail of the caller's output.
    """
    try:
        raw = sorted(
            current.iterdir(),
            key=lambda p: (0 if p.is_dir() else 1, p.name.lower()),
        )
    except OSError:
        return
    summarized_set = set(summarized)
    entries: List[Path] = []
    for e in raw:
        if _is_ignored(e):
            continue
        if e.parent == root and e.is_dir() and e.name in summarized_set:
            continue  # collapsed to a summary line by build_memory_tree
        entries.append(e)

    for i, entry in enumerate(entries):
        last = i == len(entries) - 1
        connector = "\\-- " if last else "|-- "
        line = f"{prefix}{connector}{entry.name}"
        if entry.is_file() and entry.suffix == ".md":
            desc = _read_description(entry)
            if desc:
                line = f"{line}  # {desc}"
        out.append(line)
        if entry.is_dir():
            extension = "    " if last else "|   "
            _walk_lines(root, entry, prefix + extension, out, summarized)


def _count_md_files(directory: Path) -> int:
    """Count `.md` files anywhere under `directory` (recursive)."""
    if not directory.is_dir():
        return 0
    total = 0
    for path in directory.rglob("*.md"):
        if path.is_file():
            total += 1
    return total


def build_memory_tree(
    memory_root: Path,
    *,
    summarized_dirs: Optional[Iterable[str]] = None,
) -> str:
    """Return the annotated memory tree as a string.

    Args:
        memory_root: absolute path to the profile's memory tree.
        summarized_dirs: override the collapsed-to-summary set. Default
            is `("daily", "monthly")` matching the reference; passing
            an empty tuple lists everything.

    Returns:
        A markdown block with header + fenced code containing the tree.
        Always ends with a trailing newline.

    Contract:
        - If `memory_root` does not exist, returns a stub tree with a
          one-line error inside the fence rather than raising. The verb
          layer decides whether to surface that as an exit code.
    """
    summarized = tuple(summarized_dirs) if summarized_dirs is not None else _SUMMARIZED_DIRS
    lines: List[str] = ["## Current Structure", "", "```"]

    if not memory_root.is_dir():
        lines.append(f"(no memory tree at {memory_root})")
        lines.append("```")
        return "\n".join(lines) + "\n"

    _walk_lines(memory_root, memory_root, "", lines, summarized)

    for summary in summarized:
        subdir = memory_root / summary
        count = _count_md_files(subdir)
        label_kind = "session logs" if summary == "daily" else (
            "monthly summaries" if summary == "monthly" else "files"
        )
        lines.append(f"|-- {summary}/  # {count} {label_kind}")

    lines.append("```")
    return "\n".join(lines) + "\n"
