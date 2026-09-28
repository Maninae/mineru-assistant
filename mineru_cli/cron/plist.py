"""Pure Python launchd-plist renderer for `mineru cron`.

`render_plist(job, profile) -> str` returns an Apple Property-List 1.0 XML
document as a plain `str`. This module is intentionally **filesystem-neutral**:

  * NEVER opens a file.
  * NEVER calls `launchctl`.
  * NEVER touches `~/Library/LaunchAgents`.
  * NEVER mutates the caller's `CronJob` or `Profile`.

Any code that writes the rendered plist to disk lives elsewhere (P4-05's
`mineru cron install` verb) and MUST default to `--dry-run` per the Phase 4
cutover contract in `reports/2026-08-01-mineru-phase4-cutover.md` §7.

---

Default program format: **trigger-script**
============================================

For LLM jobs this renderer emits::

    ProgramArguments = ['/bin/bash', '<workspace_absolute>/scripts/trigger-<name>-claude-code.sh']

which preserves live parity with the 18 plists under `~/Library/LaunchAgents/`.
The alternate `mineru cron run <name>` invocation described in §3 of the
cutover doc is deliberately NOT wired here. When it lands it will be gated
by a `program: 'trigger-script' | 'mineru-cli'` field on `CronJob` (loader
change in `mineru_cli.cron.config` + a matching YAML surface). Rendering
then flips inside `_llm_program_args`; every other section of the plist
stays unchanged. Choosing trigger-script now means the toy-test in P4-04
exercises the same argv shape launchd is already running today, so a
misconfigured trigger surfaces immediately instead of after a shell fork.

For SCRIPT jobs `ProgramArguments` comes verbatim from `job.program_args`
(the launchd argv is exactly what the loader accepted).

---

Cron string dialect
===================

Every entry in `job.schedule` is a five-field cron string of the form::

    MM HH DOM MON DOW

Fields accept two shapes only:

  * `*` (any) — no matching launchd key is emitted.
  * a plain non-negative integer — validated against its natural range
    and emitted as `<integer>N</integer>` under the matching launchd key.

Ranges (`1-5`), steps (`*/2`), and lists (`1,3,5`) are **rejected loudly**
with a `PlistRenderError` naming the offending field. launchd's
`StartCalendarInterval` takes a single integer per key, so any of these
compound forms would silently misfire under launchd; matching the
humanizer's discipline in `mineru_cli.verbs.custom.humanize_schedule`
keeps the whole cron surface honest.

Day-of-week mapping: cron accepts both `0` and `7` for Sunday; launchd
uses `0=Sun..6=Sat`. Both cron forms map to launchd `0`.

Multi-instance schedules (e.g. a 3x/week job on Mon/Wed/Fri and a 2x/week job on Tue/Fri) render
`StartCalendarInterval` as an `<array>` of `<dict>` entries. Single-instance
schedules render as a bare `<dict>`. The switch is entirely driven by
`len(job.schedule)`.

Day-of-month vs. day-of-week (the cleanup-retention case): when the
cron string carries DOM=2 and DOW=* (e.g. `7 4 2 * *` for "the 2nd of
every month at 04:07"), the renderer emits `<key>Day</key><integer>2</integer>`
and simply omits `Weekday`. Matches the live
`com.mineru.cleanup-retention.plist`.

Environment variables (`EnvironmentVariables`) and `WorkingDirectory` are
always emitted; the loader already applied `defaults.env` /
`defaults.default_working_directory` fallbacks (or, for `WorkingDirectory`,
we fall back to `profile.workspace_absolute` at render time when the
per-job field is still unset). `TimeOut` is emitted whenever
`job.timeout_seconds > 0`, which the loader guarantees for every job
(default 1800). The comment above `StartCalendarInterval` reuses the
existing humanizer so there is one canonical English rendering of a cron
string across the CLI.
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Tuple
from xml.sax.saxutils import escape as _xml_escape

from mineru_cli.cron.model import (
    CRON_JOB_KIND_LLM,
    CRON_JOB_KIND_SCRIPT,
    CronJob,
)
from mineru_cli.profile.schema import Profile
from mineru_cli.verbs.custom import humanize_schedule


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlistRenderError(RuntimeError):
    """Raised when a job cannot be rendered to a valid launchd plist.

    Reason strings always name the offending job (or the offending
    schedule string) so a caller propagating this up a verb layer can
    render it verbatim without further munging.
    """


# ---------------------------------------------------------------------------
# XML plumbing — small, boring helpers so the top-level render_plist reads
# like the plist itself instead of a string-concat forest.
# ---------------------------------------------------------------------------


_XML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0">\n'
)
_XML_FOOTER = "</plist>\n"

# 4-space indent for children of the root `<dict>`, matching the shape
# of every live `com.mineru.*.plist`. Nested elements (children of an
# `<array>` or a `StartCalendarInterval` `<dict>`) get 4 more spaces per
# level. Kept as a module constant so the fixtures are stable and a
# copy-paste comparison to a live file is legible.
_INDENT = "    "


def _render_key(key: str, indent: str) -> str:
    """Return a `<key>NAME</key>` line at `indent`."""
    return f"{indent}<key>{_xml_escape(key)}</key>\n"


def _render_string(value: str, indent: str) -> str:
    """Return a `<string>VALUE</string>` line at `indent`, XML-escaped."""
    return f"{indent}<string>{_xml_escape(value)}</string>\n"


def _render_integer(value: int, indent: str) -> str:
    """Return an `<integer>N</integer>` line at `indent`."""
    return f"{indent}<integer>{int(value)}</integer>\n"


def _render_array_of_strings(values: Tuple[str, ...], indent: str) -> str:
    """Render a `<array>` of `<string>` children at `indent`."""
    inner = indent + _INDENT
    parts: List[str] = [f"{indent}<array>\n"]
    for v in values:
        parts.append(_render_string(v, inner))
    parts.append(f"{indent}</array>\n")
    return "".join(parts)


# Canonical key order inside a StartCalendarInterval <dict>. Follows the
# live plists (`Weekday`, `Day`, `Hour`, `Minute`) and adds `Month` for
# completeness; only keys actually present in the parsed cron dict are
# emitted, so an unused `Month` is silently dropped.
_CALENDAR_KEY_ORDER: Tuple[str, ...] = ("Month", "Day", "Weekday", "Hour", "Minute")


def _render_calendar_dict(interval: Mapping[str, int], indent: str) -> str:
    """Render one `StartCalendarInterval` `<dict>` at `indent`.

    Only keys that appear in `interval` are emitted, so a schedule like
    `0 7 * * *` produces just `Hour` + `Minute` (matches morning-brief),
    and `7 4 2 * *` produces `Day` + `Hour` + `Minute` (matches
    cleanup-retention). Key order follows `_CALENDAR_KEY_ORDER`.
    """
    inner = indent + _INDENT
    parts: List[str] = [f"{indent}<dict>\n"]
    for key in _CALENDAR_KEY_ORDER:
        if key in interval:
            parts.append(_render_key(key, inner))
            parts.append(_render_integer(interval[key], inner))
    parts.append(f"{indent}</dict>\n")
    return "".join(parts)


def _render_string_dict(pairs: Mapping[str, str], indent: str) -> str:
    """Render a `<dict>` of `<key>` + `<string>` pairs (env, etc.).

    Key order follows the caller's mapping iteration order; for a Python
    3.7+ `dict` that's YAML-preservation insertion order, which the
    fixtures rely on.
    """
    inner = indent + _INDENT
    parts: List[str] = [f"{indent}<dict>\n"]
    for key, value in pairs.items():
        parts.append(_render_key(key, inner))
        parts.append(_render_string(value, inner))
    parts.append(f"{indent}</dict>\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Cron parsing
# ---------------------------------------------------------------------------


_CRON_ANY_TOKEN = "*"
# Plain non-negative integer, no sign, no separators. We reject everything
# more elaborate (`1-5`, `*/2`, `1,3,5`) with a targeted error so a caller
# never gets a silently-misfiring plist.
_CRON_PLAIN_INT_RE = re.compile(r"^\d+$")


def _reject_complex_cron_field(value: str, *, field_name: str, schedule: str) -> None:
    """Raise if `value` is neither `*` nor a plain integer."""
    if value == _CRON_ANY_TOKEN:
        return
    if _CRON_PLAIN_INT_RE.match(value):
        return
    raise PlistRenderError(
        f"schedule {schedule!r} field {field_name!r} value {value!r} is not "
        "a plain integer or '*'. Ranges (`1-5`), steps (`*/2`), and lists "
        "(`1,3,5`) are unsupported — `StartCalendarInterval` takes a single "
        "integer per launchd key."
    )


def _parse_cron_string_to_calendar_dict(schedule: str) -> Dict[str, int]:
    """Convert a 5-field cron string to a launchd StartCalendarInterval dict.

    Fields map (in order): `Minute`, `Hour`, `Day` (DOM), `Month` (MON),
    `Weekday` (DOW). Any field equal to `*` is omitted from the returned
    dict. Any other non-integer field raises `PlistRenderError`.

    Day-of-week `7` maps to launchd `0` (both are Sunday). Range checks
    are loud, not silent (a value like `Hour=24` is a misconfigured
    schedule, not a fire-in-the-void event).
    """
    parts = schedule.split()
    if len(parts) != 5:
        raise PlistRenderError(
            f"schedule {schedule!r} is not a 5-field cron string "
            "(expected `MM HH DOM MON DOW`)."
        )
    minute_field, hour_field, dom_field, month_field, dow_field = parts

    _reject_complex_cron_field(minute_field, field_name="minute", schedule=schedule)
    _reject_complex_cron_field(hour_field, field_name="hour", schedule=schedule)
    _reject_complex_cron_field(dom_field, field_name="day-of-month", schedule=schedule)
    _reject_complex_cron_field(month_field, field_name="month", schedule=schedule)
    _reject_complex_cron_field(dow_field, field_name="day-of-week", schedule=schedule)

    interval: Dict[str, int] = {}

    if minute_field != _CRON_ANY_TOKEN:
        minute = int(minute_field)
        if not 0 <= minute <= 59:
            raise PlistRenderError(
                f"schedule {schedule!r}: minute {minute} out of range 0..59."
            )
        interval["Minute"] = minute

    if hour_field != _CRON_ANY_TOKEN:
        hour = int(hour_field)
        if not 0 <= hour <= 23:
            raise PlistRenderError(
                f"schedule {schedule!r}: hour {hour} out of range 0..23."
            )
        interval["Hour"] = hour

    if dom_field != _CRON_ANY_TOKEN:
        dom = int(dom_field)
        if not 1 <= dom <= 31:
            raise PlistRenderError(
                f"schedule {schedule!r}: day-of-month {dom} out of range 1..31."
            )
        interval["Day"] = dom

    if month_field != _CRON_ANY_TOKEN:
        month = int(month_field)
        if not 1 <= month <= 12:
            raise PlistRenderError(
                f"schedule {schedule!r}: month {month} out of range 1..12."
            )
        interval["Month"] = month

    if dow_field != _CRON_ANY_TOKEN:
        dow = int(dow_field)
        # cron accepts both 0 and 7 for Sunday; launchd uses 0=Sun..6=Sat.
        if dow == 7:
            dow = 0
        if not 0 <= dow <= 6:
            raise PlistRenderError(
                f"schedule {schedule!r}: day-of-week {dow_field} out of "
                "range (cron 0..6 or 7; launchd 0=Sun..6=Sat)."
            )
        interval["Weekday"] = dow

    return interval


# ---------------------------------------------------------------------------
# Program-args + resolve helpers
# ---------------------------------------------------------------------------


# Trigger-script naming convention. The live plists all point at
# `$MINERU_HOME/scripts/trigger-<name>-claude-code.sh`; the
# renderer preserves the exact suffix and lets the workspace path come
# from the loaded profile, so a worktree-scoped profile renders paths
# under the worktree instead of clobbering the live workspace.
TRIGGER_SCRIPT_INTERPRETER = "/bin/bash"
TRIGGER_SCRIPT_TEMPLATE = "scripts/trigger-{name}-claude-code.sh"


def _llm_program_args(job: CronJob, profile: Profile) -> Tuple[str, ...]:
    """Return the ProgramArguments argv for an LLM job (trigger-script mode).

    See the module docstring for why trigger-script is the Phase 4 default.
    """
    workspace = str(profile.workspace_absolute).rstrip("/")
    script_rel = TRIGGER_SCRIPT_TEMPLATE.format(name=job.name)
    return (TRIGGER_SCRIPT_INTERPRETER, f"{workspace}/{script_rel}")


def _resolve_working_directory(job: CronJob, profile: Profile) -> str:
    """Resolve the plist WorkingDirectory for `job`.

    The loader stores the config value verbatim (per-job override, or
    `defaults.default_working_directory` if the loader chose to hydrate
    it, which today it does not). When both are absent the profile's
    workspace path is the load-bearing fallback — a launchd plist without
    `WorkingDirectory` inherits `/` and every relative path in the
    trigger script silently misfires.
    """
    if job.working_directory:
        return job.working_directory
    return str(profile.workspace_absolute)


def _resolve_env(job: CronJob, profile: Profile) -> Dict[str, str]:
    """Return the EnvironmentVariables dict for `job`.

    The loader already applied `defaults.default_env` as a fallback when
    the per-job block was omitted, so we merge that in first. On TOP of
    it we ALWAYS stamp three profile-scoped env keys — MINERU_HOME,
    MINERU_KEYCHAIN_ACCOUNT, MINERU_INJECT_QUEUE_DIR — so every
    trigger-script sees the RIGHT profile's workspace, Keychain, and
    inject-queue regardless of the operator's per-job env block.

    Step-5 audit, Findings 7 + 9 (Sep-4-2026): the earlier cron plists
    exported only PATH + HOME, so a non-owner profile's cron job would
    inherit the shell's default MINERU_HOME (owner's `~/.mineru`) and
    every subprocess it fired (`security` for Keychain, `deliver-output`
    for Telegram) targeted the OWNER's namespace. Stamping the three
    per-profile keys AFTER the user-authored env means an operator
    override for PATH/HOME still lands, but MINERU_HOME can never be
    accidentally hand-set to a value that leaks the profile boundary.
    """
    env = dict(job.env)
    workspace = str(profile.workspace_absolute).rstrip("/")
    env["MINERU_HOME"] = workspace
    env["MINERU_KEYCHAIN_ACCOUNT"] = profile.keychain_account
    env["MINERU_INJECT_QUEUE_DIR"] = f"{workspace}/cache/inject-queue"
    return env


def _humanize_multi(schedules: Tuple[str, ...]) -> str:
    """Return a comma-joined human phrase for one or more cron strings.

    Reuses `mineru_cli.verbs.custom.humanize_schedule` so there is exactly
    one canonical English rendering across the CLI. Passing a single
    schedule yields the same phrase the humanizer would print on its own;
    passing a multi-instance list joins them with `, `.
    """
    return ", ".join(humanize_schedule(s) for s in schedules)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_plist(job: CronJob, profile: Profile) -> str:
    """Render `job` for `profile` as a launchd XML plist string.

    Returns a `str` only — never opens a file, never calls `launchctl`,
    never touches `~/Library/LaunchAgents`. The plist body has this
    canonical shape, which mirrors the majority of the live
    `com.mineru.*.plist` files:

        Label
        ProgramArguments
        <humanized schedule comment>
        StartCalendarInterval
        TimeOut
        WorkingDirectory
        EnvironmentVariables

    Log-path keys (`StandardOutPath` / `StandardErrorPath`) that appear
    on a handful of live plists are intentionally omitted — the current
    `CronJob` schema has no field for them, so a future increment will
    add both the schema field and the corresponding emit block here in
    lockstep.

    Args:
        job:     the `CronJob` to render. `job.name` supplies the plist
                 label suffix, `job.kind` picks trigger-script vs.
                 verbatim `program_args`, and `job.schedule` (a
                 non-empty tuple after loader validation) drives
                 `StartCalendarInterval` single-vs-multi.
        profile: the loaded `Profile`. Supplies the label prefix and the
                 workspace fallback for `WorkingDirectory` and LLM
                 trigger-script paths.

    Raises:
        PlistRenderError: on any malformed schedule, an empty
            `program_args` on a `kind='script'` job, or an unknown
            `kind` value. The loader should have caught all of these,
            but the renderer double-checks so a hand-built `CronJob`
            (test fixture, etc.) still fails loud.
    """
    label = f"{profile.launchd_label_prefix}.{job.name}"

    if job.kind == CRON_JOB_KIND_LLM:
        program_args = _llm_program_args(job, profile)
    elif job.kind == CRON_JOB_KIND_SCRIPT:
        if not job.program_args:
            raise PlistRenderError(
                f"job {job.name!r} kind='script' has empty program_args; "
                "the loader should have rejected this."
            )
        program_args = tuple(job.program_args)
    else:  # pragma: no cover — loader rejects unknown kinds
        raise PlistRenderError(
            f"job {job.name!r}: unknown kind {job.kind!r}."
        )

    working_directory = _resolve_working_directory(job, profile)
    env = _resolve_env(job, profile)

    calendar_dicts: List[Dict[str, int]] = [
        _parse_cron_string_to_calendar_dict(s) for s in job.schedule
    ]
    is_multi_schedule = len(calendar_dicts) > 1
    humanized_line = _humanize_multi(job.schedule)

    out: List[str] = [_XML_HEADER, "<dict>\n"]

    # Label
    out.append(_render_key("Label", _INDENT))
    out.append(_render_string(label, _INDENT))
    out.append("\n")

    # ProgramArguments
    out.append(_render_key("ProgramArguments", _INDENT))
    out.append(_render_array_of_strings(program_args, _INDENT))
    out.append("\n")

    # Informational comment above StartCalendarInterval, reusing the
    # humanizer so multi-instance schedules render as e.g.
    # "weekly on Tuesday at 22:00, weekly on Friday at 22:00".
    out.append(f"{_INDENT}<!-- {_xml_escape(humanized_line)} -->\n")

    # StartCalendarInterval
    out.append(_render_key("StartCalendarInterval", _INDENT))
    if is_multi_schedule:
        inner = _INDENT + _INDENT
        out.append(f"{_INDENT}<array>\n")
        for interval in calendar_dicts:
            out.append(_render_calendar_dict(interval, inner))
        out.append(f"{_INDENT}</array>\n")
    else:
        out.append(_render_calendar_dict(calendar_dicts[0], _INDENT))
    out.append("\n")

    # TimeOut (loader guarantees a positive int; emit unconditionally so
    # even the 1800 default is explicit, matching the Phase 4 baseline).
    if job.timeout_seconds:
        out.append(_render_key("TimeOut", _INDENT))
        out.append(_render_integer(job.timeout_seconds, _INDENT))
        out.append("\n")

    # WorkingDirectory
    out.append(_render_key("WorkingDirectory", _INDENT))
    out.append(_render_string(working_directory, _INDENT))
    out.append("\n")

    # EnvironmentVariables
    out.append(_render_key("EnvironmentVariables", _INDENT))
    out.append(_render_string_dict(env, _INDENT))

    out.append("</dict>\n")
    out.append(_XML_FOOTER)
    return "".join(out)


__all__ = [
    "PlistRenderError",
    "render_plist",
]
