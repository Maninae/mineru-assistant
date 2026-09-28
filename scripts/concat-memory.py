#!/usr/bin/env python3
"""
concat-memory.py — Concatenate memory files into a single text dump.

PURPOSE
-------
Creates a consolidated memory dump that a new LLM session can read to bootstrap
context. Useful for giving a fresh session awareness of long-term memory,
preferences, people, and other persistent context without loading dozens of
individual files.

HOW IT WORKS
------------
1. Scans the workspace for memory files:
   - Top-level: MEMORY.md
   - Recursive: everything under memory/

2. Filters out excluded paths using regex patterns (see EXCLUDE_PATTERNS)

3. Concatenates all files with clear headers:
   - Each file gets a "## path/to/file.md" header
   - Files are separated by "---" dividers
   - Output starts with a "# Memory Dump" header

EXCLUSION PATTERNS
------------------
Modify EXCLUDE_PATTERNS to control what gets excluded. Uses Python regex
matching against paths relative to workspace root.

Current exclusions:
    - memory/people/friends/*  — Friend profiles (too granular)
    - memory/daily/*           — Daily session logs (use memory_search instead)

COMPACT MODE (--compact)
------------------------
For smaller dumps (~40% reduction), --compact also excludes patterns listed
in `COMPACT_EXCLUDE_PATTERNS` (empty by default in the shipped engine).
Operators customize this list to skip deeper context (e.g. therapy deep
dives, extended-family sub-profiles, travel history, social tracking, or
any other subtree that adds pages without carrying essential context).

Use compact mode when you need essential context without deep personal history.

USAGE
-----
    # Output to stdout (pipe or redirect as needed)
    python3 concat-memory.py
    
    # Write to a temporary file, prints the path to stdout
    # Useful for passing to another tool or script
    python3 concat-memory.py --temp
    
    # Write to a specific file
    python3 concat-memory.py -o /path/to/output.md
    python3 concat-memory.py --output memory-dump.md
    
    # Compact mode — excludes deeper context files (~40% smaller)
    python3 concat-memory.py --compact
    python3 concat-memory.py --compact --temp

EXAMPLES
--------
    # Dump to temp file and read it
    DUMP=$(python3 scripts/concat-memory.py --temp)
    cat "$DUMP"
    
    # Pipe to clipboard (macOS)
    python3 scripts/concat-memory.py | pbcopy
    
    # Save to workspace
    python3 scripts/concat-memory.py -o /tmp/memory-bootstrap.md
    
    # Count lines/size
    python3 scripts/concat-memory.py | wc -l
    python3 scripts/concat-memory.py | wc -c

OUTPUT FORMAT
-------------
    # Memory Dump
    
    Consolidated memory files for session bootstrap.
    
    ---
    
    ## MEMORY.md
    
    [contents of MEMORY.md]
    
    ---
    
    ## memory/people/family/parents.md
    
    [contents of that file]
    
    ---
    
    [... more files ...]

CONFIGURATION
-------------
Edit these constants in the script to customize behavior:

    EXCLUDE_PATTERNS  — List of regex patterns to exclude
    MEMORY_DIRS       — Directories to scan (default: ["memory"])
    TOP_LEVEL_FILES   — Top-level files to include (default: ["MEMORY.md"])
    WORKSPACE         — Workspace root ($MINERU_HOME, default ~/.mineru)
"""

import argparse
import os
import re
import tempfile
from pathlib import Path
from typing import List

# Patterns to exclude (relative to workspace root)
EXCLUDE_PATTERNS = [
    r"^memory/daily/.*",
    # Operational/meta files not useful for context bootstrap
    r"^memory/README\.md$",
    r"^memory/openclaw-dev-setup-guide\.md$",
]

# Additional patterns for --compact mode (deeper context, less commonly needed).
# Empty by default in the shipped engine; operators add their own regex
# patterns here to skip subtrees they don't want in a compact dump.
# Examples (uncomment + rename subtrees to match your own memory layout):
#   r"^memory/<user>/therapy-.*",           # therapy deep dives
#   r"^memory/<user>/childhood\.md$",       # childhood details
#   r"^memory/<user>/interests-.*",         # detailed interests
#   r"^memory/faith/bible-studies\.md$",    # bible study notes
#   r"^memory/travel/.*",                   # travel history
#   r"^memory/people/family/grandparents/.*",  # extended family
#   r"^memory/social/.*",                   # social tracking
COMPACT_EXCLUDE_PATTERNS: List[str] = []

# Operator excludes live outside the engine: one regex per line in
# `$MINERU_HOME/config/compact-excludes.txt` (override the path with
# MINERU_COMPACT_EXCLUDES_FILE). Blank lines and `#` comments are skipped.
COMPACT_EXCLUDES_FILE = Path(
    os.environ.get(
        "MINERU_COMPACT_EXCLUDES_FILE",
        str(Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru"))) / "config" / "compact-excludes.txt"),
    )
)


def load_operator_compact_excludes(excludes_file: Path = COMPACT_EXCLUDES_FILE) -> List[str]:
    """Read the operator's compact-exclude regexes, or [] when the file is absent."""
    if not excludes_file.is_file():
        return []
    patterns = []
    for line in excludes_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


COMPACT_EXCLUDE_PATTERNS = COMPACT_EXCLUDE_PATTERNS + load_operator_compact_excludes()

# Workspace root: MINERU_HOME, not the script's location, since scripts/ may
# be a symlink into the engine checkout while memory lives in the workspace.
WORKSPACE = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))

# Directories to scan for memory files
MEMORY_DIRS = [
    "memory",
]

# Also include top-level memory files
TOP_LEVEL_FILES = [
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
    "MEMORY.md",
]


def should_exclude(rel_path: str, compact: bool = False) -> bool:
    """Check if a path matches any exclusion pattern."""
    patterns = EXCLUDE_PATTERNS + (COMPACT_EXCLUDE_PATTERNS if compact else [])
    for pattern in patterns:
        if re.match(pattern, rel_path):
            return True
    return False


def collect_memory_files(compact: bool = False) -> List[Path]:
    """Collect all memory files, respecting exclusions."""
    files = []
    
    # Top-level files
    for filename in TOP_LEVEL_FILES:
        filepath = WORKSPACE / filename
        if filepath.exists():
            files.append(filepath)
    
    # Memory directories
    for dirname in MEMORY_DIRS:
        dirpath = WORKSPACE / dirname
        if not dirpath.exists():
            continue
        
        for filepath in sorted(dirpath.rglob("*.md")):
            rel_path = str(filepath.relative_to(WORKSPACE))
            if not should_exclude(rel_path, compact):
                files.append(filepath)
    
    return files


def concatenate_files(files: List[Path]) -> str:
    """Concatenate file contents with headers."""
    sections = []
    
    for filepath in files:
        rel_path = filepath.relative_to(WORKSPACE)
        try:
            content = filepath.read_text(encoding="utf-8")
            # Use XML-style tags for clear file boundaries
            section = f'<memory_file path="{rel_path}">\n{content}\n</memory_file>'
            sections.append(section)
        except Exception as e:
            sections.append(f'<memory_file path="{rel_path}">\n[Error reading file: {e}]\n</memory_file>')
    
    header = """<memory_dump>
<meta>
This is a consolidated memory dump for session bootstrap.
Contains: long-term memory, people profiles, and persistent context.
Purpose: Understand who the operator is, key people in his life, and ongoing projects.
Each file is wrapped in <memory_file path="..."> tags.
</meta>

"""
    footer = "\n</memory_dump>"
    return header + "\n\n".join(sections) + footer


def main():
    parser = argparse.ArgumentParser(description="Concatenate memory files for session bootstrap")
    parser.add_argument("-o", "--output", type=str, help="Output file path")
    parser.add_argument("--temp", action="store_true", help="Write to temp file, print path")
    parser.add_argument("--compact", action="store_true", help="Exclude deeper context (therapy, travel, extended family, etc.)")
    args = parser.parse_args()

    files = collect_memory_files(compact=args.compact)
    
    if not files:
        print("No memory files found.")
        return
    
    output = concatenate_files(files)
    
    if args.temp:
        # Write to temp file, print path
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(output)
            print(f.name)
    elif args.output:
        # Write to specified file
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote {len(output)} bytes to {args.output}")
    else:
        # stdout
        print(output)


if __name__ == "__main__":
    main()
