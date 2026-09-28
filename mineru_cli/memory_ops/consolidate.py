"""Stage and distill one day's raw session fragments.

The end-to-end memory-consolidation pipeline: given a date, find
every session-fragment file under `<memory_root>/daily/` for that
day, concatenate them (with per-file headers) into a single
`<memory_root>/daily/<YYYY-MM-DD>.raw.md` bundle, then hand the
bundle to a distiller callable that turns it into the final
consolidated `<memory_root>/daily/<YYYY-MM-DD>.md`.

Distiller seam (still PLUGGABLE):
  - `distiller: Callable[[str], str]` — takes the concatenated raw
    bundle text, returns the distilled consolidated text.
  - `distiller=None` (the default) uses
    `distiller_claude.default_claude_cli_distiller`, which shells out
    to headless Claude Code (`claude -p`). See that module for the
    exact invocation and failure modes.
  - Tests and alternative backends inject their own callable via the
    same seam; the pipeline stays LLM-agnostic.

Behavior:
  - No fragments for the requested date -> no files written; result
    reports `STATUS_NO_FRAGMENTS` so the CLI verb can exit non-zero.
  - Fragments found -> `.raw.md` is always written; `.md` is written
    from the distiller output. If the distiller raises (e.g.
    `DistillerError` from the default Claude CLI backend), the
    exception propagates and NO `.md` is written, so a failure never
    corrupts the consolidated file.

The concatenation ordering matches the warm-resume reader: session
fragments sort by filename stem, which for the standard slugs
(`YYYY-MM-DD_HH-MM-SS.md`, `YYYY-MM-DD-<slug>.md`) is chronological
by session start.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional

from mineru_cli.memory_ops.distiller_claude import default_claude_cli_distiller


_FRAGMENT_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})[-_].+\.md$")

# Status vocabulary is small and closed.
# STATUS_NO_FRAGMENTS: no session fragments were found for the target
#   date. Nothing was written; the CLI verb exits non-zero.
# STATUS_DISTILLED:    fragments were found, `.raw.md` and `.md` were
#   both written.
# STATUS_PARTIAL_NO_LLM: retained as a backwards-compatible alias for
#   STATUS_NO_FRAGMENTS. The pre-default-distiller pipeline used it
#   for two cases (no fragments AND fragments-but-no-distiller); with
#   the default distiller wired, only the no-fragments case remains.
STATUS_NO_FRAGMENTS = "no-fragments"
STATUS_DISTILLED = "distilled"
STATUS_PARTIAL_NO_LLM = STATUS_NO_FRAGMENTS


@dataclass(frozen=True)
class ConsolidateResult:
    """Outcome of a `consolidate_daily_fragments` call.

    Attributes:
        target_date: the day the fragments belong to.
        fragments_found: number of session fragments discovered.
        raw_bundle_path: path to the written `.raw.md` (always set
            when any fragments were found).
        consolidated_path: path to the distilled `.md` (always set
            when any fragments were found and the distiller ran to
            completion).
        distillation_status: `"distilled"` when both files were
            written, `"no-fragments"` when the day had no fragments
            and nothing was written.
    """

    target_date: date
    fragments_found: int
    raw_bundle_path: Optional[Path]
    consolidated_path: Optional[Path]
    distillation_status: str


def find_dates_missing_consolidation(memory_root: Path) -> List[date]:
    """List dates under `<memory_root>/daily/` that have session fragments
    but no consolidated `<YYYY-MM-DD>.md`.

    Used by the CLI verb's `--missing` backfill mode: after the nightly
    recurring cron has been offline for a stretch (a machine off, an
    auth outage), this returns the days the operator needs to replay.

    Returns dates in ascending order so the CLI processes them oldest
    first (matches how a human would repair the tree by hand).
    """
    daily_dir = Path(memory_root) / "daily"
    if not daily_dir.is_dir():
        return []

    fragment_dates: set = set()
    consolidated_dates: set = set()
    for entry in daily_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        # `.raw.md` and `.denoise.log` are artifacts, not fragments and
        # not consolidated outputs — ignore both.
        if name.endswith(".raw.md") or name.endswith(".denoise.log"):
            continue
        # Consolidated file: `<YYYY-MM-DD>.md` with no other segment.
        stem = name[:-3] if name.endswith(".md") else ""
        if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
            try:
                consolidated_dates.add(date.fromisoformat(stem))
                continue
            except ValueError:
                pass
        # Fragment: `<YYYY-MM-DD>[-_]<slug>.md`.
        m = _FRAGMENT_PATTERN.match(name)
        if m is None:
            continue
        try:
            fragment_dates.add(date.fromisoformat(m.group("date")))
        except ValueError:
            continue

    missing = fragment_dates - consolidated_dates
    return sorted(missing)


def _iter_fragments_for_date(daily_dir: Path, target_date: date) -> List[Path]:
    """Return sorted session-fragment paths for `target_date`.

    Fragment rule: filename matches `YYYY-MM-DD[-_].+\\.md` where the
    date equals `target_date`. Skips artifacts (`*.raw.md`,
    `*.denoise.log`) and the consolidated `YYYY-MM-DD.md` itself.
    """
    if not daily_dir.is_dir():
        return []
    stem_wanted = target_date.isoformat()
    matches: List[Path] = []
    for entry in daily_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if name.endswith(".raw.md") or name.endswith(".denoise.log"):
            continue
        m = _FRAGMENT_PATTERN.match(name)
        if m is None:
            continue
        if m.group("date") != stem_wanted:
            continue
        matches.append(entry)
    matches.sort(key=lambda p: p.name)
    return matches


def _build_raw_bundle(target_date: date, fragments: List[Path]) -> str:
    """Concatenate `fragments` into one string with per-file headers.

    Header shape (matches an XML tag so an LLM downstream sees clean
    boundaries):

        <fragment date="YYYY-MM-DD" file="basename.md">
        <fragment body>
        </fragment>
    """
    parts: List[str] = []
    parts.append(
        f'<daily_bundle date="{target_date.isoformat()}" '
        f'fragments="{len(fragments)}">'
    )
    for f in fragments:
        try:
            body = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            body = ""
        parts.append("")
        parts.append(
            f'<fragment date="{target_date.isoformat()}" file="{f.name}">'
        )
        parts.append(body.rstrip("\n"))
        parts.append("</fragment>")
    parts.append("")
    parts.append("</daily_bundle>")
    return "\n".join(parts) + "\n"


def consolidate_daily_fragments(
    memory_root: Path,
    target_date: date,
    *,
    distiller: Optional[Callable[[str], str]] = None,
) -> ConsolidateResult:
    """Stage and distill one day's session fragments end-to-end.

    Args:
        memory_root: absolute path to the profile's memory tree; the
            fragment scanner reads `<memory_root>/daily/`.
        target_date: which day to consolidate.
        distiller: callable that turns the raw bundle string into the
            final consolidated string. Defaults to the headless-Claude
            CLI distiller (`default_claude_cli_distiller`); pass a
            custom callable to plug in an alternative LLM backend or a
            test double.

    Returns:
        A `ConsolidateResult` describing what was written. On the
        no-fragments path, no files are written and
        `distillation_status == STATUS_NO_FRAGMENTS`. On the happy
        path, both `.raw.md` and `.md` are written and
        `distillation_status == STATUS_DISTILLED`.

    Raises:
        Whatever the `distiller` callable raises. The default backend
        raises `DistillerError` when `claude` is missing on PATH,
        exits non-zero, or times out. The raw bundle is preserved on
        failure (the `.raw.md` sibling) but the consolidated `.md` is
        NOT written, so a distiller failure never leaves a corrupt
        consolidated file behind.
    """
    daily_dir = Path(memory_root) / "daily"
    fragments = _iter_fragments_for_date(daily_dir, target_date)

    if not fragments:
        return ConsolidateResult(
            target_date=target_date,
            fragments_found=0,
            raw_bundle_path=None,
            consolidated_path=None,
            distillation_status=STATUS_NO_FRAGMENTS,
        )

    daily_dir.mkdir(parents=True, exist_ok=True)
    raw_bundle = _build_raw_bundle(target_date, fragments)
    raw_path = daily_dir / f"{target_date.isoformat()}.raw.md"
    raw_path.write_text(raw_bundle, encoding="utf-8")

    active_distiller = distiller if distiller is not None else default_claude_cli_distiller
    consolidated_text = active_distiller(raw_bundle)
    consolidated_path = daily_dir / f"{target_date.isoformat()}.md"
    consolidated_path.write_text(consolidated_text, encoding="utf-8")
    return ConsolidateResult(
        target_date=target_date,
        fragments_found=len(fragments),
        raw_bundle_path=raw_path,
        consolidated_path=consolidated_path,
        distillation_status=STATUS_DISTILLED,
    )
