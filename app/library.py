"""Reports and creations browsing + safe file resolution.

Serves two source trees:
  - reports/    (mostly dated .md; a few PDFs and HTML reports)
  - creations/  (mixed content — nightly reflections, static HTML pages,
                 image exports; no subdirs today, but the walker tolerates them)

Path resolution is the security boundary. resolve_library_path() takes an
untrusted relpath, joins it under the source root, resolves symlinks, and
verifies the resolved path is still inside the root. Anything else raises
ValueError; callers turn that into a 404.

For entries whose name matches the `YYYY-MM-DD-slug[.ext]` convention that the
reports/ tree overwhelmingly uses — files like `2026-03-06-invoice.pdf` AND
extensionless subdirectories like `2026-03-06-quarterly-review` — we
humanize both `display_name` (title-cased slug — "Quarterly Review") and
add a `date_label` field ("Mar 6, 2026") so the mobile library list stops
rendering three-line ISO-slug names. Non-matching names retain the previous
`display_name` (basename with `.md` stripped) and omit `date_label` entirely
— the frontend then falls back to the raw `name`.

Python 3.9-compatible (system /usr/bin/python3). Stdlib only.
"""

import datetime
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from config import ALLOWED_EXTENSIONS, EXTENSION_KIND, LIBRARY_SOURCES


logger = logging.getLogger(__name__)


# Matches `YYYY-MM-DD-<tail>` for both files (`tail` = `slug.ext`) and
# extensionless directories (`tail` = raw slug). Peel `tail` apart in the
# humanizer below — an extension is any single trailing `.<ext>` piece. The
# date group is validated by `datetime.date` below, so a name like
# `9999-13-40-foo.md` won't clear that check even if the regex matches.
DATED_LIBRARY_ENTRY_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})-(?P<tail>.+)$"
)


# Slug segments that must render as an all-caps acronym rather than the
# generic `word.capitalize()` output (which would emit `Ai`, `Cc`, `Tv`,
# `3d`). Two sources feed this set:
#   - The reviewer's minimum list from design-pass-review.md F4.
#   - A scan of every dated name currently living under `reports/` and
#     `creations/` for short segments that read as acronyms (`cli`, `cve`,
#     `k12`, `v1`, `1m`).
# Membership check is lowercase, so a slug word matches regardless of its
# incoming case. If a future filename introduces a new acronym, add it here.
ACRONYM_SEGMENTS: Set[str] = {
    # Reviewer's explicit F4 list
    "ai", "cc", "ml", "llm", "api", "url", "ui", "ux",
    "seo", "pdf", "csv", "gpu", "cpu", "ios", "tv", "3d",
    # Observed in reports/ and creations/ dated names
    "cli", "cve", "k12", "v1", "1m",
}


def humanize_slug_word(word: str) -> str:
    """Render one hyphen-slug segment for a display_name.

    Segments in `ACRONYM_SEGMENTS` uppercase whole ("ai" → "AI"); anything
    else goes through the generic `str.capitalize` (first letter up, rest
    down). Load-bearing for the mobile Reports list: without this, common
    acronym slugs would read as `Ai Cc Pipeline` / `Cdrama Pinyin Tv Setup`.
    """
    if word.lower() in ACRONYM_SEGMENTS:
        return word.upper()
    return word.capitalize()


@dataclass
class LibraryEntry:
    """One row in a library directory listing.

    `name` is the on-disk filename and stays load-bearing: it is what the
    frontend joins onto the relpath to build /api/library/* and /raw/library/*
    URLs. `display_name` is the human-facing label — strips the trailing `.md`
    so the list reads "papers" instead of "papers.md" (other extensions stay
    untouched so `report.pdf` still reads as `report.pdf`). For entries whose
    name matches the dated convention (`YYYY-MM-DD-slug[.ext]`, files OR
    extensionless directories), display_name is replaced with the humanized
    slug and `date_label` (dropped in via `list_directory` after this
    dataclass is built) carries the pretty date.
    """
    name: str
    display_name: str
    relpath: str
    kind: str  # "file" | "dir"
    mtime: float
    size: int
    extension: str


def humanize_dated_filename(name: str) -> Optional[Tuple[str, str]]:
    """`(display_name, date_label)` for `YYYY-MM-DD-slug[.ext]`, else None.

    Accepts both files and extensionless directories. If the tail past the
    date carries a `.ext` suffix, the extension is peeled off before
    title-casing; a directory whose tail has no dot uses the whole tail as
    the slug. This mirrors how the reports/ tree stores both dated markdown
    files and dated subdirectories from the same generator.

    - display_name: hyphen-slug rendered title-case ("quarterly-review"
      → "Quarterly Review"). Empty / missing segments are dropped so
      "2026-03-06--extra.md" still cleans up.
    - date_label:  abbreviated month + day (no leading zero) + full year
      ("Mar 6, 2026"). Computed via strftime + `dt.day` so the formatting
      is portable across platforms — `%-d` isn't in the C89 spec, so we
      never lean on it.

    Returns None whenever the regex misses, the digits parse but the date
    is not real (e.g. Feb 30), or the slug reduces to nothing after cleanup
    (e.g. `2026-03-06-.md`).
    """
    match = DATED_LIBRARY_ENTRY_RE.match(name)
    if match is None:
        return None
    try:
        parsed_date = datetime.date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None
    tail = match.group("tail")
    if "." in tail:
        # File-shaped tail: peel off a trailing `.<ext>` (single-dot ext only).
        # rpartition splits at the last dot, matching the previous file-only
        # regex's `(.+)\.([^.]+)$` behavior. Refuse when either side goes empty
        # so `.md` / `foo.` don't sneak through as garbage humanizations.
        slug, _dot, extension = tail.rpartition(".")
        if not slug or not extension:
            return None
    else:
        # Directory-shaped tail (or a file lacking any extension, which the
        # list_directory extension allowlist already filters out): use the
        # whole tail as the slug.
        slug = tail
    slug_words = [segment for segment in slug.split("-") if segment]
    display_name = " ".join(humanize_slug_word(word) for word in slug_words)
    if not display_name:
        return None
    date_label = f"{parsed_date.strftime('%b')} {parsed_date.day}, {parsed_date.year}"
    return display_name, date_label


def resolve_library_path(source: str, relpath: str) -> Path:
    """Resolve a (source, relpath) to a concrete path inside the source root.

    Raises ValueError for:
      - unknown source
      - absolute relpath
      - `..` traversal
      - symlink or resolved path escaping the source root
    Does NOT enforce the extension allowlist; that check lives at the file-
    serving boundary so directory listings still work.
    """
    if source not in LIBRARY_SOURCES:
        raise ValueError(f"unknown source: {source}")

    source_root = LIBRARY_SOURCES[source].resolve()

    relpath = (relpath or "").strip().lstrip("/")
    if not relpath:
        return source_root

    candidate_raw = Path(relpath)
    if candidate_raw.is_absolute():
        raise ValueError("absolute paths rejected")
    if any(part == ".." for part in candidate_raw.parts):
        raise ValueError("parent-traversal rejected")

    try:
        resolved = (source_root / candidate_raw).resolve()
    except OSError as resolve_error:
        # ENAMETOOLONG etc: the segment made the path structurally illegal.
        # Turn it into the same ValueError callers already handle as 404.
        raise ValueError("path could not be resolved") from resolve_error
    try:
        resolved.relative_to(source_root)
    except ValueError as escape_error:
        raise ValueError(f"path escapes source root: {relpath}") from escape_error
    return resolved


def list_directory(source: str, relpath: str = "") -> Dict:
    """Directory listing for /api/library/<source>[/<relpath>].

    Returns {source, relpath, entries: [LibraryEntry...]} with entries sorted
    by mtime desc so the newest content is at the top.
    """
    target = resolve_library_path(source, relpath)
    try:
        target_exists = target.exists()
    except OSError as probe_error:
        raise FileNotFoundError("cannot stat target") from probe_error
    if not target_exists:
        raise FileNotFoundError(f"no such directory: {source}/{relpath}")
    try:
        target_is_dir = target.is_dir()
    except OSError as probe_error:
        raise FileNotFoundError("cannot stat target") from probe_error
    if not target_is_dir:
        raise NotADirectoryError(f"{source}/{relpath} is a file, not a dir")

    source_root = LIBRARY_SOURCES[source].resolve()
    entries: List[Dict] = []
    try:
        children = list(target.iterdir())
    except OSError as walk_error:
        logger.warning("iterdir failed for %s (errno %s)", source, walk_error.errno)
        return {"source": source, "relpath": relpath, "entries": []}
    for child in children:
        try:
            stat = child.stat()
        except OSError as stat_error:
            logger.warning("skip child (errno %s)", stat_error.errno)
            continue
        kind = "dir" if child.is_dir() else "file"
        extension = child.suffix.lower()
        # Filter out dotfiles and disallowed extensions so private / non-viewable
        # files never show up in the listing.
        if child.name.startswith("."):
            continue
        if kind == "file" and extension not in ALLOWED_EXTENSIONS:
            continue
        display_name = child.name[:-len(".md")] if child.name.endswith(".md") else child.name
        # Files AND extensionless dated directories share the humanization —
        # `2026-03-06-quarterly-review/` reads as "Quarterly Review"
        # just like the `.md` sibling would. The humanizer itself decides
        # whether the tail has an extension; anything not matching the dated
        # convention keeps its stripped-.md display_name and gets no date_label.
        date_label: Optional[str] = None
        humanized = humanize_dated_filename(child.name)
        if humanized is not None:
            display_name, date_label = humanized
        entry = LibraryEntry(
            name=child.name,
            display_name=display_name,
            relpath=str(child.relative_to(source_root)),
            kind=kind,
            mtime=stat.st_mtime,
            size=stat.st_size if kind == "file" else 0,
            extension=extension,
        )
        entry_dict = entry.__dict__.copy()
        if date_label is not None:
            entry_dict["date_label"] = date_label
        entries.append(entry_dict)

    # Directories first (they're often navigation anchors), then newest files.
    entries.sort(key=lambda e: (e["kind"] != "dir", -e["mtime"]))
    return {
        "source": source,
        "relpath": relpath,
        "entries": entries,
    }


def classify_extension(path: Path) -> str:
    """One of: 'text' | 'markdown' | 'image' | 'pdf' | 'html' | 'other'.

    Reads the extension-to-kind mapping from `config.EXTENSION_KIND` so a new
    served extension automatically classifies correctly — no second table to
    keep in sync. Anything not in the mapping (including files the handler
    already refused via the allowlist) falls through to 'other'.
    """
    return EXTENSION_KIND.get(path.suffix.lower(), "other")
