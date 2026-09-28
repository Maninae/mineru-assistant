"""Build the session-start warm-resume context bundle.

Behavioral reference: the live `warm_resume_new_session_with_context.sh`
in an operator workspace. The engine version is generic — it takes the
profile's `memory_root` (and `timezone` for the header stamp) and never
touches a hardcoded path.

Output shape (XML-tagged so an agent can scan it deterministically):

    <session_warmup>
    <current_time>Monday, September 16, 2026 - 10:23 AM PT</current_time>

    <instructions>
    You are resuming from a fresh session. ...
    </instructions>

    <recent_days count="N">

    <day date="YYYY-MM-DD" day="Weekday">
    <consolidated body>
    </day>

    ...
    </recent_days>

    <today count="M">
    <session file="YYYY-MM-DD-slug.md">
    <fragment body>
    </session>
    ...
    </today>

    </session_warmup>

Selection rules (mirror the shell reference):
  - "Consolidated daily" file: filename stem matches `YYYY-MM-DD` exactly.
  - "Session fragment": filename stem starts with `YYYY-MM-DD-` or
    `YYYY-MM-DD_` (dash or underscore separator, then anything).
  - Skip any file whose name ends with `.raw.md` or `.denoise.log`.
  - Recent days: the three most recent consolidated files whose date
    is within the last 3 days AND is not today.
  - Today: every session fragment whose date stem matches today. The
    consolidated file for today (if it exists) is intentionally shown
    under `<recent_days>` semantics elsewhere; here it is skipped.

The reference uses the system TZ env var; we honor `Profile.timezone`
via `zoneinfo.ZoneInfo` for the timestamp header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo


# `YYYY-MM-DD` prefix (year-month-day). Anchored at start of the stem.
_DATE_STEM_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?P<sep>[-_]|$)")

# How far back the recent-days window reaches, matching the reference.
_RECENT_DAYS_WINDOW = 3
_RECENT_DAYS_MAX = 3


@dataclass(frozen=True)
class _FragmentFile:
    """A daily-log file discovered on disk with its parsed date stem."""

    path: Path
    stem: str
    file_date: date
    is_consolidated: bool


def _parse_stem(stem: str) -> Optional[_FragmentFile]:
    """Return a `_FragmentFile` descriptor for a matching stem, else None.

    A consolidated file has the exact stem `YYYY-MM-DD`; a session
    fragment starts with `YYYY-MM-DD` followed by `-` or `_` and then
    anything. Anything else is ignored.
    """
    match = _DATE_STEM_RE.match(stem)
    if match is None:
        return None
    try:
        file_date = date.fromisoformat(match.group("date"))
    except ValueError:
        return None
    is_consolidated = match.group("sep") == "" and stem == match.group("date")
    return _FragmentFile(
        path=Path(""),  # filled in by caller
        stem=stem,
        file_date=file_date,
        is_consolidated=is_consolidated,
    )


def _iter_daily_files(daily_dir: Path) -> Iterable[_FragmentFile]:
    """Yield every valid daily file under `daily_dir`, skipping artifacts.

    Skipped: `*.raw.md`, `*.denoise.log`, and any file whose stem does
    not begin with a `YYYY-MM-DD` date prefix.
    """
    if not daily_dir.is_dir():
        return
    for entry in sorted(daily_dir.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name.endswith(".raw.md") or name.endswith(".denoise.log"):
            continue
        if not name.endswith(".md"):
            continue
        stem = name[: -len(".md")]
        parsed = _parse_stem(stem)
        if parsed is None:
            continue
        yield _FragmentFile(
            path=entry,
            stem=parsed.stem,
            file_date=parsed.file_date,
            is_consolidated=parsed.is_consolidated,
        )


def _select_recent_consolidated(
    files: Iterable[_FragmentFile], today: date
) -> List[_FragmentFile]:
    """Return the up-to-3 most recent consolidated files inside the window.

    Match rule: `is_consolidated` AND `today - 3 days <= file_date < today`.
    Sorted OLDEST FIRST so the emitted bundle reads forward chronologically.
    """
    cutoff = today - timedelta(days=_RECENT_DAYS_WINDOW)
    candidates = [
        f
        for f in files
        if f.is_consolidated and cutoff <= f.file_date < today
    ]
    candidates.sort(key=lambda f: f.file_date, reverse=True)
    top = candidates[:_RECENT_DAYS_MAX]
    top.sort(key=lambda f: f.file_date)  # oldest first for the output
    return top


def _select_today_fragments(
    files: Iterable[_FragmentFile], today: date
) -> List[_FragmentFile]:
    """Return today's session fragments (skips the consolidated file if any)."""
    return sorted(
        [f for f in files if f.file_date == today and not f.is_consolidated],
        key=lambda f: f.stem,
    )


def _format_header_time(now: datetime) -> str:
    """`Monday, September 16, 2026 - 10:23 AM PDT` style header string.

    Uses `%-I` on POSIX for a leading-zero-stripped hour; the reference
    shell script uses `date +"%-I:%M %p %Z"`. `strftime` on macOS/Linux
    honors `%-I`; on Windows it would need `%#I`, but the engine is a
    macOS/Linux tool so `%-I` is fine.
    """
    day_of_week = now.strftime("%A")
    date_full = now.strftime("%B ") + str(now.day) + now.strftime(", %Y")
    time_str = now.strftime("%-I:%M %p %Z").strip()
    if not time_str.endswith(now.strftime("%Z")):
        # `%Z` may be empty on some naive datetimes; keep the header sane.
        time_str = now.strftime("%-I:%M %p").strip()
    return f"{day_of_week}, {date_full} - {time_str}"


def _read_file_body(path: Path) -> str:
    """Read a memory file; return `""` on decode error (never crash)."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def build_warm_resume(
    memory_root: Path,
    *,
    timezone: str = "UTC",
    now: Optional[datetime] = None,
) -> str:
    """Return the XML-tagged warm-resume bundle as a single string.

    Args:
        memory_root: absolute path to the profile's memory tree. The
            daily-fragment scanner reads `<memory_root>/daily/`.
        timezone: IANA zone name for the header stamp. Defaults to UTC
            so a bare call in a test never crashes on a missing zone.
        now: optional datetime override for deterministic tests. When
            omitted, `datetime.now(ZoneInfo(timezone))` is used.

    Returns:
        The bundle text; ends with a trailing newline so shell
        redirection produces a well-formed file.

    Contract:
        - Never raises on a missing `daily/` directory: the output
          simply reports zero recent days and a zero-count today block.
        - Never raises on unreadable individual files: they contribute
          an empty body but the surrounding XML shape stays valid.
    """
    if now is None:
        try:
            zone = ZoneInfo(timezone)
        except Exception:
            zone = ZoneInfo("UTC")
        now = datetime.now(zone)

    today = now.date()
    daily_dir = memory_root / "daily"
    files = list(_iter_daily_files(daily_dir))
    recent = _select_recent_consolidated(files, today)
    today_fragments = _select_today_fragments(files, today)

    header = _format_header_time(now)
    out: List[str] = []
    out.append("<session_warmup>")
    out.append(f"<current_time>{header}</current_time>")
    out.append("")
    out.append("<instructions>")
    out.append(
        "You are resuming from a fresh session. Below is your recent history - "
        "the last 3 days of consolidated daily memories and any session "
        "fragments from today. Read all of it."
    )
    out.append(
        "This is your continuity bridge; use it to orient yourself before responding."
    )
    out.append("</instructions>")
    out.append("")
    out.append(f'<recent_days count="{len(recent)}">')
    for f in recent:
        day_name = f.file_date.strftime("%A")
        out.append("")
        out.append(f'<day date="{f.file_date.isoformat()}" day="{day_name}">')
        out.append(_read_file_body(f.path).rstrip("\n"))
        out.append("</day>")
    out.append("</recent_days>")
    out.append("")
    if today_fragments:
        out.append(f'<today count="{len(today_fragments)}">')
        for f in today_fragments:
            out.append("")
            out.append(f'<session file="{f.path.name}">')
            out.append(_read_file_body(f.path).rstrip("\n"))
            out.append("</session>")
        out.append("</today>")
    else:
        out.append('<today count="0">No sessions yet today.</today>')
    out.append("")
    out.append("</session_warmup>")
    return "\n".join(out) + "\n"
