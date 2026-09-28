"""Parse YAML frontmatter tags from markdown files."""

import re
from pathlib import Path

_FRONTMATTER_RE = re.compile(r'^---\n(.*?)\n---', re.DOTALL)
_INLINE_TAGS_RE = re.compile(r'tags:\s*\[(.*?)\]')
_LIST_TAGS_RE = re.compile(r'tags:\s*\n((?:\s+-\s+.+\n?)+)')
_TAG_ITEM_RE = re.compile(r'^\s+-\s+(.+)$', re.MULTILINE)


def has_frontmatter(filepath: Path) -> bool:
    """Check if a markdown file has YAML frontmatter.

    Args:
        filepath: Path to the markdown file.

    Returns:
        True if the file starts with a valid frontmatter block.
    """
    try:
        content = filepath.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    return _FRONTMATTER_RE.match(content) is not None


def extract_tags(filepath: Path) -> list[str]:
    """Extract tags from YAML frontmatter of a markdown file.

    Handles both list format (- tag) and inline array format ([a, b, c]).

    Args:
        filepath: Path to the markdown file.

    Returns:
        List of tag strings. Empty list if no tags found.
    """
    try:
        content = filepath.read_text()
    except (OSError, UnicodeDecodeError):
        return []

    fm_match = _FRONTMATTER_RE.match(content)
    if not fm_match:
        return []

    frontmatter = fm_match.group(1)

    # Try inline array format: tags: [tag1, tag2]
    inline_match = _INLINE_TAGS_RE.search(frontmatter)
    if inline_match:
        items = inline_match.group(1)
        return [t.strip().strip('"\'') for t in items.split(',') if t.strip()]

    # Try list format with dashes
    tags_section = _LIST_TAGS_RE.search(frontmatter)
    if tags_section:
        tag_lines = tags_section.group(1)
        tags = _TAG_ITEM_RE.findall(tag_lines)
        return [t.strip().strip('"\'') for t in tags if t.strip()]

    return []
