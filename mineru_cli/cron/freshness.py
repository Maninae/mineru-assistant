"""Expected-output freshness check — shared by `cron status` and `cron run`.

Mirrors `_check_expected_output` in `$MINERU_HOME/scripts/cc-job-lib.sh`
(the bash rule powering the live cron-failure alerts today): "at least
one file matching the glob has mtime within the last 2 hours". Kept in a
dedicated module so `cron run` (P4-04) reuses the exact same logic
without duplicating it — divergence between the status verb and the
runner is the exact class of bug this module is meant to prevent.

Filesystem-neutral:

  * NEVER opens a file — only `stat`s the glob matches.
  * NEVER spawns a subprocess.
  * NEVER touches `~/Library/LaunchAgents` (that surface lives in the
    verb layer, not here).

Placeholders `{today}` and `{yesterday}` in the glob string are
resolved with system-TZ date arithmetic, matching the live
`date +%Y-%m-%d` / `date -v-1d '+%Y-%m-%d'` calls in the trigger
scripts. This keeps the same idempotency-marker syntax used by the
YAML config working here.
"""

from __future__ import annotations

import datetime
import glob as _glob
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


# 2 hours = 7200 seconds. Matches the `-mmin -120` (find) rule in
# `cc-job-lib.sh::_check_expected_output` so a job whose expected output
# was written more than 2 hours ago fails freshness the same way in both
# surfaces. A tunable would be a footgun (drift risk); keep it locked.
FRESHNESS_WINDOW_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class FreshnessCheck:
    """One freshness verdict for a job's `expected_output_glob`.

    Fields:
      requested:      True iff a non-empty glob was passed. When False, the
                      check trivially succeeds — the loader treats a
                      `None` / missing `expected_output_glob` as
                      "no check requested" and the runner must not fire
                      a false alert.
      fresh:          True iff at least one file matching the resolved
                      glob has an mtime within `FRESHNESS_WINDOW_SECONDS`.
      matched_path:   the fresh file that satisfied the check (first
                      hit in the glob's natural order), or None if
                      nothing was fresh.
      matches:        every file matching the glob (fresh or stale) —
                      useful to `cron status` so an operator sees the
                      stale output rather than "nothing matched".
      resolved_glob:  the glob string after `{today}`/`{yesterday}` were
                      substituted — useful for error messages.
    """

    requested: bool
    fresh: bool
    matched_path: Optional[Path]
    matches: List[Path]
    resolved_glob: str


def resolve_marker_placeholders(
    pattern: str,
    *,
    today: Optional[str] = None,
    yesterday: Optional[str] = None,
) -> str:
    """Substitute `{today}` / `{yesterday}` with local-date strings.

    Args:
        pattern: the raw glob/marker (may contain `{today}` and/or
                 `{yesterday}` — the config loader has already validated
                 that no other placeholders are present).
        today, yesterday: opt-in overrides for tests; both default to
                 today's / yesterday's local date in `YYYY-MM-DD` form.

    Returns:
        The pattern with placeholders replaced. Passing a pattern with
        no placeholders returns it verbatim.

    A `str.format`-based substitution is intentionally NOT used —
    literal `{` chars in a filesystem glob (rare but possible) would
    break the whole call. Simple `str.replace` per known key keeps the
    surface honest.
    """
    if not pattern:
        return pattern
    if today is None:
        today = datetime.date.today().isoformat()
    if yesterday is None:
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    return pattern.replace("{today}", today).replace("{yesterday}", yesterday)


def check_expected_output_fresh(
    workspace: Path,
    pattern: Optional[str],
    *,
    now: Optional[float] = None,
    today: Optional[str] = None,
    yesterday: Optional[str] = None,
) -> FreshnessCheck:
    """Return a freshness verdict for `pattern` inside `workspace`.

    Args:
        workspace: absolute path the glob is resolved against when
                   `pattern` is workspace-relative (matches the
                   `WORKSPACE=$MINERU_HOME` convention in the bash
                   trigger scripts).
        pattern:   the `expected_output_glob` from `CronJob`. May be
                   None or empty ("no check requested"); may be
                   workspace-relative (`briefs_morning/morning-*.md`)
                   or absolute (`/tmp/...` — rare, but the bash rule
                   accepts it). May contain `{today}` / `{yesterday}`.
        now:       epoch-seconds override for tests (default: `time.time()`).
        today, yesterday: opt-in overrides for tests, passed through to
                   `resolve_marker_placeholders`.

    Returns:
        A `FreshnessCheck`. When `pattern` is None/empty the verdict is
        `requested=False, fresh=True` — the check is a no-op, matching
        the bash `[ -z "$glob" ] && return 0` short-circuit.

    Never raises. A glob that matches nothing returns
    `fresh=False, matched_path=None, matches=[]`. A workspace that does
    not exist behaves identically (no matches).
    """
    if pattern is None or not str(pattern).strip():
        return FreshnessCheck(
            requested=False,
            fresh=True,
            matched_path=None,
            matches=[],
            resolved_glob="",
        )

    resolved = resolve_marker_placeholders(
        pattern, today=today, yesterday=yesterday
    )
    if resolved.startswith("/"):
        abs_glob = resolved
    else:
        # Preserve `Path` semantics: `Path(workspace) / resolved` folds
        # a workspace-relative pattern (e.g. `briefs_morning/morning-*.md`)
        # onto the workspace root the loader captured.
        abs_glob = str(Path(workspace) / resolved)

    now_epoch = now if now is not None else time.time()
    matches: List[Path] = [Path(p) for p in sorted(_glob.glob(abs_glob))]

    fresh_hit: Optional[Path] = None
    for candidate in matches:
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if (now_epoch - mtime) <= FRESHNESS_WINDOW_SECONDS:
            fresh_hit = candidate
            break

    return FreshnessCheck(
        requested=True,
        fresh=fresh_hit is not None,
        matched_path=fresh_hit,
        matches=matches,
        resolved_glob=abs_glob,
    )


__all__ = [
    "FRESHNESS_WINDOW_SECONDS",
    "FreshnessCheck",
    "check_expected_output_fresh",
    "resolve_marker_placeholders",
]
