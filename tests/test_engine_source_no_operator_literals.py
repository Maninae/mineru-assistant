"""Public-engine hygiene: code, tests, and the README carry no operator-private literals.

A forker reads the Python surface as closely as the templates: docstrings, verb
help text, fixture strings, CLI examples. This gate walks the code dirs plus
`README.md` and asserts none of the banned literals appear:

- `GENERIC_CODE_BANNED_LITERALS` (narrower than the template set: a wrapper module
  may legitimately name the product it wraps, so `Monarch`/`Tesla` are allowed here).
- The operator's private literals, loaded at test time from
  `$MINERU_HOME/config/banned-literals.txt` and `$MINERU_BANNED_LITERALS`
  (see `leak_gate_literals.py`). The repo never ships them.
"""

from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from leak_gate_literals import (
    FICTIONAL_EXAMPLE_BANNED_LITERALS,
    GENERIC_CODE_BANNED_LITERALS,
    OPERATOR_BANNED_LITERALS,
    describe_banned_literal_for_failure,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

CODE_TESTS_BANNED_LITERALS: Tuple[str, ...] = tuple(
    dict.fromkeys(GENERIC_CODE_BANNED_LITERALS + OPERATOR_BANNED_LITERALS.literals)
)

# Dirs walked by the gate: anything the framework ships that a forker reads.
SOURCE_SCAN_DIRS = ("mineru_cli", "app", "bin", "scripts", "tests", "browser", "lib")
# Single top-level files walked by the gate.
SOURCE_SCAN_FILES = ("README.md",)

SOURCE_SKIP_DIR_NAMES = frozenset({
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    ".git",
})

# Human-readable source extensions; binaries, images, .pyc are skipped.
SOURCE_SCAN_EXTS = frozenset({
    ".py", ".pyi",
    ".sh", ".bash",
    ".md", ".txt",
    ".yaml", ".yml",
    ".json", ".jsonl",
    ".js", ".mjs", ".ts",
    ".html", ".css",
    ".template", ".tmpl",
})

# Extensionless CLI scripts worth scanning.
SOURCE_SCAN_EXTENSIONLESS_NAMES = frozenset({
    "msearch",
    "amazon-orders",
    "browser",
    "gog-firewall",
    "imsg-firewall",
    "imsg-named",
    "safe-unzip",
    "slack-read",
    "slack-refresh-users",
    "monarch",
})

# Relative path (from repo root) -> literals legitimately present in that file.
# `leak_gate_literals.py` defines the generic tokens, so it names them.
SOURCE_ALLOWED_LITERALS_BY_FILE: Dict[str, frozenset] = {
    "tests/leak_gate_literals.py": frozenset(GENERIC_CODE_BANNED_LITERALS),
    # The loader tests exercise the example's fictional lines plus one more.
    "tests/test_leak_gate_literal_loader.py": frozenset(
        FICTIONAL_EXAMPLE_BANNED_LITERALS + ("Emberly Gazette",)
    ),
}


def is_scannable_source_file(path: Path) -> bool:
    """True for a regular, non-symlink source file outside the skipped dirs."""
    if any(part in SOURCE_SKIP_DIR_NAMES for part in path.parts):
        return False
    # Symlinks (bin/msearch -> scripts/msearch/msearch) are covered by their target.
    if not path.is_file() or path.is_symlink():
        return False
    extension = path.suffix.lower()
    if extension:
        return extension in SOURCE_SCAN_EXTS
    return path.name in SOURCE_SCAN_EXTENSIONLESS_NAMES


def source_scan_paths() -> List[Path]:
    """Every source file the gate walks, sorted for determinism."""
    paths: List[Path] = []
    for sub in SOURCE_SCAN_DIRS:
        base = REPO_ROOT / sub
        if base.exists():
            paths.extend(p for p in base.rglob("*") if is_scannable_source_file(p))
    for file_name in SOURCE_SCAN_FILES:
        top_level_path = REPO_ROOT / file_name
        if is_scannable_source_file(top_level_path):
            paths.append(top_level_path)
    return sorted(paths)


SOURCE_SCAN_PATHS = source_scan_paths()


def test_source_gate_walks_expected_source_tree() -> None:
    """The gate finds enough files to be meaningful; a collapse to zero fails loudly."""
    assert len(SOURCE_SCAN_PATHS) >= 100, (
        f"source gate found only {len(SOURCE_SCAN_PATHS)} files under "
        f"{SOURCE_SCAN_DIRS!r}; audit the walker exclusions before adjusting."
    )


@pytest.mark.parametrize("scan_root", SOURCE_SCAN_DIRS + SOURCE_SCAN_FILES)
def test_source_gate_covers_each_scan_root(scan_root: str) -> None:
    """Every configured dir or file contributes at least one scanned path."""
    root_path = REPO_ROOT / scan_root
    if not root_path.exists():
        pytest.skip(f"{scan_root} does not ship in this checkout.")
    covered = [p for p in SOURCE_SCAN_PATHS if p == root_path or root_path in p.parents]
    assert covered, f"{scan_root} exists but nothing under it is scan-eligible."


@pytest.mark.parametrize(
    "source_path",
    SOURCE_SCAN_PATHS,
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_source_file_contains_no_banned_operator_literal(source_path: Path) -> None:
    """Engine code, tests, and README contain none of `CODE_TESTS_BANNED_LITERALS`."""
    relative_path = source_path.relative_to(REPO_ROOT).as_posix()
    try:
        text = source_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        pytest.skip(f"unreadable as utf-8: {relative_path}")
    allowed_here = SOURCE_ALLOWED_LITERALS_BY_FILE.get(relative_path, frozenset())
    for banned in CODE_TESTS_BANNED_LITERALS:
        if banned in allowed_here:
            continue
        assert banned not in text, (
            f"{relative_path} contains banned literal "
            f"{describe_banned_literal_for_failure(banned)}; the public engine must "
            "not ship operator affiliations or personal examples. Genericize it "
            "(alice/bob/example-vendor) or move it behind a template variable."
        )
