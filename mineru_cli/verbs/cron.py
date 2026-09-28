"""`mineru cron` sub-app (P4-03) — READ-ONLY verbs for the launchd surface.

⚠️⚠️ HARD SAFETY POSTURE — READ TWICE ⚠️⚠️

  Nothing in this module writes to `~/Library/LaunchAgents`, executes
  `launchctl`, or edits a `recurring/*.md` file under the live workspace
  at `$MINERU_HOME/`. The write-side verbs (`install`, `run`)
  are gated to a later phase (P4-04 / P4-05) and MUST land with
  `--dry-run` defaults per the Phase-4 cutover contract in
  `reports/2026-08-01-mineru-phase4-cutover.md` §7.

  This file provides the six read-only verbs:

    * `list [--json] [--verbose]`   inventory + live-plist reconciliation
    * `status <name>`               single-job dashboard
    * `logs <name> [-n N] [-f]`     tail the newest log for one job
    * `edit <name>`                 open $EDITOR on the instruction file
    * `plist <name> [--out PATH]`   render + emit the plist without installing
    * `diff <name>`                 unified diff of rendered vs installed

  Read paths:
    * cron.yaml is loaded via `mineru_cli.cron.load_cron_config`.
    * Live plists are parsed with `plistlib.load` from an env-overridable
      directory (`MINERU_LAUNCHD_DIR`, default `~/Library/LaunchAgents`).
      Tests point this at `tmp_path` so no real launchd files are
      touched.

  Write paths (guarded by hard refusals):
    * `plist --out PATH` refuses if PATH resolves inside the resolved
      `MINERU_LAUNCHD_DIR` (LANDMINE: writing here is an implicit install).
    * `edit <name>` refuses to open anything under the live
      `$MINERU_HOME/recurring/` (the "recurring rewrite" is a
      later gated step; this verb must not touch those files today).

Import-time side effects:

  Importing this module must NOT read `~/Library/LaunchAgents`, must NOT
  parse cron.yaml, must NOT resolve the profile. Every path resolution
  happens lazily inside the verb bodies so `mineru --help` on a fresh
  checkout is exercised without triggering any I/O beyond Typer's own
  registration walk.
"""

from __future__ import annotations

import datetime as _datetime
import difflib
import io
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import typer

from mineru_cli.cron import (
    CRON_JOB_KIND_LLM,
    CRON_JOB_KIND_SCRIPT,
    CronConfig,
    CronConfigError,
    CronJob,
    PlistRenderError,
    all_job_names,
    get_job,
    load_cron_config,
    render_plist,
)
from mineru_cli.cron.freshness import (
    check_expected_output_fresh,
    resolve_marker_placeholders,
)
from mineru_cli.cron.model import PreStep
from mineru_cli.profile import get_profile
from mineru_cli.profile.schema import Profile
from mineru_cli.verbs.custom import humanize_schedule


# ---------------------------------------------------------------------------
# Env knobs — resolved lazily so tests can `monkeypatch.setenv(...)` before
# each invocation without a module reload dance. Mirrors the browser verb's
# BROWSER_PORT_ENV / BROWSER_SNAPSHOT_PATH_ENV pattern.
# ---------------------------------------------------------------------------

# Directory the read-only verbs consult for currently-installed live
# plists. Default is macOS's per-user LaunchAgents dir. Tests override
# this to a `tmp_path` so the suite never reads (let alone writes)
# real launchd files. The env var is READ-ONLY on the verb side —
# `mineru cron install` will honor the same knob when P4-05 lands.
LAUNCHD_DIR_ENV = "MINERU_LAUNCHD_DIR"
DEFAULT_LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"

# The `recurring/` sub-tree under the LIVE workspace is off-limits for
# `mineru cron edit`. The verb points at a WORKSPACE-scoped `recurring/`
# via `profile.workspace_absolute`; any resolved path landing inside the
# live `$MINERU_HOME/recurring/` is refused with a message pointing at
# the cutover doc's §4.
#
# HARD-SAFETY: the guarded live paths MUST derive from the `MINERU_HOME`
# seam (default `$MINERU_HOME`). Hardcoding one user's path would (a) leak
# that username and (b) protect the WRONG directory for every other user
# — the guard would silently stop guarding. Tests monkeypatch these
# constants directly (see `LIVE_WORKSPACE_PATH` below), so import-time
# capture of the default is safe.
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
LIVE_RECURRING_DIR = _MINERU_HOME / "recurring"

# Default editor when `$EDITOR` is unset. `vi` is available on every
# macOS install; anything fancier (`code -w`, `nano`) would silently
# fail on a fresh box.
DEFAULT_EDITOR = "vi"

# Follow-mode polling interval. `logs -f` is a pure Python tail loop
# (never shells out to `tail -f`) — the interval is short enough to
# feel live and long enough not to burn CPU when nothing is written.
FOLLOW_POLL_INTERVAL_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Top-level `mineru cron` sub-app.
# ---------------------------------------------------------------------------


cron_app = typer.Typer(
    name="cron",
    help=(
        "Scheduled jobs (launchd) surface. Read verbs: list, status, "
        "logs, edit, plist, diff. Write verbs: run (P4-04), install "
        "and uninstall (P4-05). In the Phase-4 SAFE build, install / "
        "uninstall LIVE paths are gated behind --live-flip AND "
        "MINERU_CRON_ALLOW_LIVE=1; use --dry-run to preview."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# Resolver helpers (all lazy — no I/O at import time).
# ---------------------------------------------------------------------------


def _resolved_launchd_dir() -> Path:
    """Return the launchd dir the read verbs consult.

    An unset / empty `MINERU_LAUNCHD_DIR` collapses to the platform
    default (`~/Library/LaunchAgents`). A relative override resolves
    against the CWD (matches how tests seed tmp paths).
    """
    raw = os.environ.get(LAUNCHD_DIR_ENV)
    if not raw:
        return DEFAULT_LAUNCHD_DIR
    return Path(raw)


def _profile_for_ctx(ctx: typer.Context) -> Profile:
    """Return the active profile, hydrating on first call.

    Lazy hydration (2026-08-28 rev): `get_profile(ctx)` loads + validates
    the active profile on first call and caches it on `ctx.obj`; later
    calls return the cache. Any loader error (no active profile,
    `--profile bogus`, malformed yaml) surfaces as a clean CLI usage
    frame with exit 2 via `typer.BadParameter`.
    """
    return get_profile(ctx)


def _load_config(ctx: typer.Context) -> Tuple[Profile, CronConfig]:
    """Load the profile + cron.yaml; fail loud with a clean message on error."""
    profile = _profile_for_ctx(ctx)
    try:
        config = load_cron_config(profile)
    except CronConfigError as exc:
        typer.echo(f"mineru cron: {exc}", err=True)
        raise typer.Exit(code=2)
    return profile, config


def _installed_plist_path(profile: Profile, name: str) -> Path:
    """Return the absolute path where the plist for `name` would be installed."""
    label_prefix = profile.launchd_label_prefix
    return _resolved_launchd_dir() / f"{label_prefix}.{name}.plist"


def _read_installed_plist(path: Path) -> Optional[Dict[str, Any]]:
    """Return the parsed plist at `path`, or None if absent/unreadable.

    Failures collapse to None: a corrupted plist on disk shouldn't wedge
    the verb; the caller renders "unreadable" instead. Uses
    `plistlib.load` on the open binary file, matching Apple's own
    tooling contract.
    """
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            return plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def _get_job_or_exit(ctx: typer.Context, name: str) -> Tuple[Profile, CronConfig, CronJob]:
    """Load config + resolve the named job or exit 2 with a clean message."""
    profile, config = _load_config(ctx)
    job = get_job(config, name)
    if job is None:
        available = ", ".join(sorted(all_job_names(config)))
        typer.echo(
            f"mineru cron: no job named {name!r} in cron.yaml. "
            f"Available jobs: {available}",
            err=True,
        )
        raise typer.Exit(code=2)
    return profile, config, job


# ---------------------------------------------------------------------------
# `list` — inventory + live reconciliation.
# ---------------------------------------------------------------------------


def _humanize_schedules(schedules: Tuple[str, ...]) -> str:
    """Return a joined human phrase for one or more cron strings."""
    return ", ".join(humanize_schedule(s) for s in schedules)


def _job_is_live(profile: Profile, name: str) -> bool:
    """Return True iff a plist with the matching Label exists in the launchd dir.

    We don't just check the filename — we parse the file and verify
    `Label` matches the profile's expected `<prefix>.<name>`, so a
    randomly-named file under `~/Library/LaunchAgents` can't
    accidentally register as "live" for our job.
    """
    path = _installed_plist_path(profile, name)
    data = _read_installed_plist(path)
    if data is None:
        return False
    expected_label = f"{profile.launchd_label_prefix}.{name}"
    return data.get("Label") == expected_label


def _job_has_enabled_mismatch(profile: Profile, job: CronJob) -> bool:
    """Return True iff cron.yaml disagrees with the live install state.

    Mismatch = `enabled: true` in YAML but no plist installed, OR
    `enabled: false` in YAML but a plist IS installed. This is the
    signal an operator wants surfaced at a glance: "someone forgot to
    run install after a config change".
    """
    live = _job_is_live(profile, job.name)
    return job.enabled != live


@cron_app.command("list", help="List scheduled jobs (cron.yaml + live-plist reconciliation).")
def list_jobs(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of a table.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Include instruction path / model / program_args in the row.",
    ),
) -> None:
    """Show every job in cron.yaml plus its live-plist reconciliation.

    Columns (default table):
      name, kind, schedule (humanized), enabled, live?, mismatch?

    `--verbose` folds in the model / instruction for LLM jobs and the
    `program_args[0]` for script jobs.

    `--json` emits a list of dicts — each row carries `mismatch` as a
    boolean so scripted consumers can flag "someone forgot to run
    install after a config change" without re-implementing the rule.
    """
    profile, config = _load_config(ctx)
    rows: List[Dict[str, Any]] = []
    for job in config.jobs:
        live = _job_is_live(profile, job.name)
        row: Dict[str, Any] = {
            "name": job.name,
            "kind": job.kind,
            "schedule": list(job.schedule),
            "schedule_humanized": _humanize_schedules(job.schedule),
            "enabled": job.enabled,
            "live": live,
            "enabled_mismatch": job.enabled != live,
        }
        if verbose or json_out:
            if job.kind == CRON_JOB_KIND_LLM:
                row["model"] = job.model
                row["instruction"] = job.instruction
            else:
                row["program_args"] = list(job.program_args)
        rows.append(row)

    if json_out:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return

    # Table shape: name | kind | schedule (humanized) | enabled | live | ⚠
    name_w = max((len(r["name"]) for r in rows), default=4)
    kind_w = max((len(r["kind"]) for r in rows), default=4)
    sched_w = max((len(r["schedule_humanized"]) for r in rows), default=8)

    header = (
        f"  {'name'.ljust(name_w)}  "
        f"{'kind'.ljust(kind_w)}  "
        f"{'schedule'.ljust(sched_w)}  "
        f"{'enabled':<7}  "
        f"{'live':<4}  "
        f"mismatch"
    )
    typer.echo(header)
    typer.echo("  " + "-" * (len(header) - 2))
    for r in rows:
        marker = "  yes" if r["enabled_mismatch"] else "  no"
        typer.echo(
            f"  {r['name'].ljust(name_w)}  "
            f"{r['kind'].ljust(kind_w)}  "
            f"{r['schedule_humanized'].ljust(sched_w)}  "
            f"{('yes' if r['enabled'] else 'no').ljust(7)}  "
            f"{('yes' if r['live'] else 'no').ljust(4)}  "
            f"{marker.strip()}"
        )
        if verbose and r["kind"] == CRON_JOB_KIND_LLM:
            typer.echo(
                f"    model={r.get('model')}  instruction={r.get('instruction')}"
            )
        elif verbose and r["kind"] == CRON_JOB_KIND_SCRIPT:
            argv = r.get("program_args") or []
            joined = " ".join(argv) if argv else "(none)"
            typer.echo(f"    program={joined}")


# ---------------------------------------------------------------------------
# `status <name>` — single-job dashboard.
# ---------------------------------------------------------------------------


def _newest_log_file(workspace: Path, job_name: str) -> Optional[Path]:
    """Return the newest `.log` file under `<workspace>/logs/<job>/`.

    Returns None if the directory is missing or empty. `.err` files are
    ignored — the runbook grep pattern is `.log`, so surface parity
    matches.
    """
    log_dir = workspace / "logs" / job_name
    if not log_dir.is_dir():
        return None
    candidates = [p for p in log_dir.iterdir() if p.is_file() and p.suffix == ".log"]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _humanize_epoch(epoch: float) -> str:
    """Return an ISO-8601 local timestamp for `epoch` (seconds)."""
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def _diff_summary(rendered: str, installed_text: Optional[str]) -> str:
    """Return a one-line summary of the rendered vs installed diff."""
    if installed_text is None:
        return "no installed plist"
    if rendered == installed_text:
        return "clean (rendered == installed)"
    diff = list(
        difflib.unified_diff(
            installed_text.splitlines(),
            rendered.splitlines(),
            fromfile="installed",
            tofile="rendered",
            lineterm="",
        )
    )
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return f"differs (+{added} / -{removed} lines)"


@cron_app.command("status", help="Show status for one scheduled job.")
def status(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Job name (matches cron.yaml `name:`)."),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON.",
    ),
) -> None:
    """Show a compact dashboard for one job.

    Fields:
      * humanized schedule
      * newest log ts (mtime of the newest `.log` under
        `<workspace>/logs/<name>/`)
      * expected-output freshness (glob match within 2h, mirrors
        `cc-job-lib.sh::_check_expected_output`)
      * plist installed live? (bool)
      * diff summary vs cron.yaml (`clean`, `differs (+N/-M lines)`,
        or `no installed plist`)
    """
    profile, config, job = _get_job_or_exit(ctx, name)

    latest_log = _newest_log_file(profile.workspace_absolute, job.name)
    latest_log_ts = (
        _humanize_epoch(latest_log.stat().st_mtime) if latest_log is not None else None
    )
    latest_log_path = str(latest_log) if latest_log is not None else None

    freshness = check_expected_output_fresh(
        profile.workspace_absolute,
        job.expected_output_glob,
    )

    installed_path = _installed_plist_path(profile, job.name)
    installed_exists = installed_path.exists()

    try:
        rendered = render_plist(job, profile)
    except PlistRenderError as exc:
        typer.echo(f"mineru cron status: {exc}", err=True)
        raise typer.Exit(code=2)

    installed_text: Optional[str] = None
    if installed_exists:
        try:
            installed_text = installed_path.read_text(encoding="utf-8")
        except OSError:
            installed_text = None

    diff_summary_line = _diff_summary(rendered, installed_text)

    payload: Dict[str, Any] = {
        "name": job.name,
        "kind": job.kind,
        "schedule": list(job.schedule),
        "schedule_humanized": _humanize_schedules(job.schedule),
        "enabled": job.enabled,
        "live": _job_is_live(profile, job.name),
        "installed_plist_path": str(installed_path),
        "installed_plist_exists": installed_exists,
        "latest_log_path": latest_log_path,
        "latest_log_mtime": latest_log_ts,
        "expected_output_check": {
            "requested": freshness.requested,
            "fresh": freshness.fresh,
            "matched_path": (
                str(freshness.matched_path) if freshness.matched_path is not None else None
            ),
            "matches": [str(p) for p in freshness.matches],
            "resolved_glob": freshness.resolved_glob,
        },
        "diff_summary": diff_summary_line,
    }

    if json_out:
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    typer.echo(f"name:               {job.name}")
    typer.echo(f"kind:               {job.kind}")
    typer.echo(f"schedule:           {_humanize_schedules(job.schedule)}  ({', '.join(job.schedule)})")
    typer.echo(f"enabled (config):   {'yes' if job.enabled else 'no'}")
    typer.echo(f"installed live?:    {'yes' if payload['live'] else 'no'}")
    typer.echo(f"installed path:     {installed_path}")
    typer.echo(f"last log:           {latest_log_ts or '(no logs yet)'}")
    if latest_log_path:
        typer.echo(f"last log path:      {latest_log_path}")
    if freshness.requested:
        state = "fresh" if freshness.fresh else "STALE"
        typer.echo(f"expected output:    {state}  ({freshness.resolved_glob})")
        if freshness.matched_path is not None:
            typer.echo(f"  matched:          {freshness.matched_path}")
        elif freshness.matches:
            typer.echo(f"  latest match:     {freshness.matches[-1]} (older than 2h)")
    else:
        typer.echo("expected output:    (no check requested — glob not set)")
    typer.echo(f"diff vs installed:  {diff_summary_line}")


# ---------------------------------------------------------------------------
# `logs <name> [-n N] [-f]` — pure-Python tail.
# ---------------------------------------------------------------------------


def _tail_file_bytes(path: Path, num_lines: int) -> str:
    """Return the last `num_lines` lines from `path` as text.

    Uses a bounded seek+read from the tail to avoid loading the full
    file for a 100-line request against a large log. Falls back to a
    full read for files smaller than the seek window.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size == 0:
        return ""
    # Grab a generous slice from the tail: 4 KB per line is more than
    # any of our jobs produce, but for a corrupt / weird file this
    # still bounds the read.
    read_bytes = min(size, max(4096, num_lines * 4096))
    with path.open("rb") as fh:
        fh.seek(max(0, size - read_bytes))
        chunk = fh.read()
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-num_lines:])


def _follow_file(path: Path, *, poll_interval: float = FOLLOW_POLL_INTERVAL_SECONDS) -> None:
    """Poll `path` for appended bytes and print them until interrupted.

    A pure-Python tail: `open(binary) -> seek(end) -> loop { read; print }`.
    NEVER shells out to `tail -f`. Intercepts KeyboardInterrupt so the
    exit is quiet.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                chunk = fh.read()
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                else:
                    time.sleep(poll_interval)
    except KeyboardInterrupt:
        return
    except OSError as exc:
        typer.echo(f"mineru cron logs: follow aborted: {exc}", err=True)
        raise typer.Exit(code=2)


@cron_app.command("logs", help="Tail the newest log file for one job.")
def logs(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Job name (matches cron.yaml `name:`)."),
    lines: int = typer.Option(
        50,
        "-n",
        "--lines",
        help="Number of lines to print (default 50).",
    ),
    follow: bool = typer.Option(
        False,
        "-f",
        "--follow",
        help="Follow the newest log file (Ctrl-C to exit).",
    ),
) -> None:
    """Tail `<workspace>/logs/<name>/*.log` (newest file, N lines, optional -f).

    A read-only surface — never touches launchd, never edits files.
    Follow mode is a pure-Python poll loop; it does NOT shell out to
    `tail -f`.
    """
    profile, config, job = _get_job_or_exit(ctx, name)
    latest = _newest_log_file(profile.workspace_absolute, job.name)
    if latest is None:
        typer.echo(
            f"mineru cron logs: no logs at {profile.workspace_absolute / 'logs' / job.name}. "
            "Either the job hasn't fired yet or logs live under a different "
            "workspace.",
            err=True,
        )
        raise typer.Exit(code=1)
    if lines <= 0:
        typer.echo("mineru cron logs: --lines must be positive.", err=True)
        raise typer.Exit(code=2)

    typer.echo(f"# {latest}")
    text = _tail_file_bytes(latest, lines)
    if text:
        typer.echo(text)

    if follow:
        _follow_file(latest)


# ---------------------------------------------------------------------------
# `edit <name>` — open $EDITOR on the instruction file (workspace-scoped).
# ---------------------------------------------------------------------------


def _resolve_instruction_path(profile: Profile, job: CronJob) -> Optional[Path]:
    """Return the absolute path to the job's instruction file, or None.

    Script jobs have no instruction file. LLM jobs have `job.instruction`
    which is workspace-relative — join it onto `profile.workspace_absolute`.
    """
    if job.kind != CRON_JOB_KIND_LLM or not job.instruction:
        return None
    return (profile.workspace_absolute / job.instruction).resolve()


def _is_under(path: Path, root: Path) -> bool:
    """Return True iff `path` is at or under `root` (both resolved)."""
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    try:
        resolved_path.relative_to(resolved_root)
        return True
    except ValueError:
        return False


@cron_app.command("edit", help="Open $EDITOR on the job's instruction file.")
def edit(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Job name (matches cron.yaml `name:`)."),
) -> None:
    """Open the instruction file for `name` in `$EDITOR` (default `vi`).

    HARD SAFETY: refuses to open anything under
    `$MINERU_HOME/recurring/` — the "recurring rewrite" is a
    later gated step (see §4 of the Phase-4 cutover doc). The verb
    resolves the file against `profile.workspace_absolute`, so a
    worktree-scoped profile edits its own `recurring/*.md` and the live
    workspace stays untouched.

    Never touches plists / launchctl. Never edits a script job (they
    have no instruction file — surfaces a friendly message instead).
    """
    profile, config, job = _get_job_or_exit(ctx, name)
    if job.kind != CRON_JOB_KIND_LLM:
        typer.echo(
            f"mineru cron edit: job {name!r} is kind='{job.kind}' and has no "
            "instruction file. Edit `program_args` directly in cron.yaml.",
            err=True,
        )
        raise typer.Exit(code=2)

    resolved = _resolve_instruction_path(profile, job)
    if resolved is None:  # defensive; loader guarantees instruction on LLM jobs
        typer.echo(
            f"mineru cron edit: job {name!r} has no `instruction:` field in cron.yaml.",
            err=True,
        )
        raise typer.Exit(code=2)

    if _is_under(resolved, LIVE_RECURRING_DIR):
        typer.echo(
            "mineru cron edit: Phase 4 recurring rewrite gated. "
            f"Refusing to edit the live path at {resolved}. "
            "The workspace-scoped recurring/ tree lands in a later phase "
            "(see reports/2026-08-01-mineru-phase4-cutover.md §4).",
            err=True,
        )
        raise typer.Exit(code=2)

    if not resolved.exists():
        typer.echo(
            f"mineru cron edit: instruction file does not exist: {resolved}",
            err=True,
        )
        raise typer.Exit(code=2)

    editor = os.environ.get("EDITOR", DEFAULT_EDITOR).strip() or DEFAULT_EDITOR
    # Resolve the editor:
    #   * An absolute path is honored directly if it exists + is executable
    #     (`shutil.which` is PATH-first and sometimes rejects a bare
    #     absolute path on trimmed sandbox PATHs).
    #   * Otherwise we defer to `shutil.which` so a bare `vi` or `code`
    #     is looked up on PATH the same way a shell would.
    editor_path: Optional[str] = None
    editor_candidate = Path(editor)
    if editor_candidate.is_absolute() and os.access(editor_candidate, os.X_OK):
        editor_path = str(editor_candidate)
    else:
        editor_path = shutil.which(editor)
    if editor_path is None:
        typer.echo(
            f"mineru cron edit: editor {editor!r} not found on PATH. "
            "Set $EDITOR to an installed editor and retry.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        completed = subprocess.run([editor_path, str(resolved)], check=False)
    except OSError as exc:
        typer.echo(
            f"mineru cron edit: failed to launch {editor!r}: {exc}",
            err=True,
        )
        raise typer.Exit(code=2)
    raise typer.Exit(code=completed.returncode)


# ---------------------------------------------------------------------------
# `plist <name> [--out PATH]` — render + emit without installing.
# ---------------------------------------------------------------------------


@cron_app.command("plist", help="Render the plist for one job (stdout, or --out PATH).")
def plist_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Job name (matches cron.yaml `name:`)."),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        help=(
            "Optional output path. Refused if it resolves inside the "
            "launchd dir — plist writes there ARE an implicit install."
        ),
    ),
) -> None:
    """Render `job` to a launchd XML plist and print / write it.

    HARD SAFETY: if `--out` resolves inside the launchd dir (default
    `~/Library/LaunchAgents`, overridable via `MINERU_LAUNCHD_DIR`),
    refuse with a loud error naming the LANDMINE. Writing there IS an
    implicit install — the read-only verb must not smuggle that in.

    Default is stdout; the operator can inspect the rendered plist,
    diff it against the installed one (`mineru cron diff`), or pipe it
    into a review tool. Never contacts `launchctl`.
    """
    profile, config, job = _get_job_or_exit(ctx, name)
    try:
        rendered = render_plist(job, profile)
    except PlistRenderError as exc:
        typer.echo(f"mineru cron plist: {exc}", err=True)
        raise typer.Exit(code=2)

    if out is None:
        typer.echo(rendered, nl=False)
        return

    # LANDMINE guard: writing into the launchd dir IS an install. The
    # read-only verb refuses even if the operator points --out at a
    # subdirectory or a path with a symlink hop.
    launchd_dir = _resolved_launchd_dir()
    try:
        resolved_out = out.expanduser().resolve()
        resolved_launchd = launchd_dir.expanduser().resolve()
    except OSError as exc:
        typer.echo(
            f"mineru cron plist: could not resolve --out path: {exc}",
            err=True,
        )
        raise typer.Exit(code=2)

    # Check if the parent chain contains the launchd dir (guards both
    # `--out ~/Library/LaunchAgents/foo.plist` and
    # `--out ~/Library/LaunchAgents/sub/foo.plist` cases).
    if resolved_out == resolved_launchd or resolved_launchd in resolved_out.parents:
        typer.echo(
            "mineru cron plist: LANDMINE — refusing to write into the "
            f"launchd dir at {resolved_launchd}. Writing here IS an implicit "
            "install; use `mineru cron install` (P4-05, --dry-run defaulted) "
            "to materialize the plist.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        resolved_out.parent.mkdir(parents=True, exist_ok=True)
        resolved_out.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        typer.echo(
            f"mineru cron plist: could not write --out {resolved_out}: {exc}",
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(f"wrote {resolved_out}", err=True)


# ---------------------------------------------------------------------------
# `diff <name>` — unified diff of rendered plist vs installed plist.
# ---------------------------------------------------------------------------


@cron_app.command("diff", help="Unified diff: rendered plist vs currently-installed plist.")
def diff_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Job name (matches cron.yaml `name:`)."),
) -> None:
    """Show what `mineru cron install` would change for `name`.

    Reads the installed plist verbatim (text mode) and unified-diffs it
    against `render_plist(job, profile)`. Read-only; never touches
    launchctl. When no installed plist is present, prints a friendly
    header and the full rendered body so an operator can preview what
    a first install would drop in place.
    """
    profile, config, job = _get_job_or_exit(ctx, name)
    try:
        rendered = render_plist(job, profile)
    except PlistRenderError as exc:
        typer.echo(f"mineru cron diff: {exc}", err=True)
        raise typer.Exit(code=2)

    installed_path = _installed_plist_path(profile, job.name)
    if not installed_path.exists():
        typer.echo(f"# {installed_path}: not installed")
        typer.echo("# rendered (would install):")
        typer.echo(rendered, nl=False)
        return

    try:
        installed_text = installed_path.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(
            f"mineru cron diff: could not read {installed_path}: {exc}",
            err=True,
        )
        raise typer.Exit(code=2)

    if installed_text == rendered:
        typer.echo(f"# {installed_path}: clean (rendered == installed)")
        return

    diff_lines = difflib.unified_diff(
        installed_text.splitlines(keepends=False),
        rendered.splitlines(keepends=False),
        fromfile=f"installed:{installed_path}",
        tofile=f"rendered:{job.name}",
        lineterm="",
    )
    for line in diff_lines:
        typer.echo(line)


# ---------------------------------------------------------------------------
# `run <name> [--force] [--dry-run] [--date YYYY-MM-DD]` — in-band job runner.
# ---------------------------------------------------------------------------
#
# Reproduces `cc-job-lib.sh` semantics for one job in a stubbable form:
#
#   * `WORKSPACE`   env-overridable (default `${MINERU_HOME:-$HOME/.mineru}`).
#   * `CC_BIN`      env-overridable (default `$HOME/.local/bin/claude-fda`).
#   * `DELIVER_BIN` env-overridable (default `python3 $WORKSPACE/scripts/
#                   deliver-output.py`); a multi-word command is shlex-split.
#
# The stubbable envs match `cc-job-lib.sh` line-for-line so the toy-test
# can point them at `/tmp` scripts and neither `$MINERU_HOME/memory/` nor
# `~/Library/LaunchAgents/` is touched.
#
# The verb never invokes `launchctl`, never installs / bootstraps /
# bootouts a plist, and never mutates the recurring/*.md files. It is
# EXECUTIONAL surface only, gated by a HARD-SAFETY refusal (see
# `_refuse_live_workspace_without_optin`) that forbids running against
# the live workspace with the default CC_BIN unless
# `MINERU_CRON_ALLOW_LIVE=1` is explicitly set. `--dry-run` bypasses
# every real subprocess call by design.


# Env-var names for the runner. Colocated so tests can seed them and
# operators reading the file can immediately see the seam.
CC_BIN_ENV = "CC_BIN"
DELIVER_BIN_ENV = "DELIVER_BIN"
WORKSPACE_ENV = "WORKSPACE"
ALLOW_LIVE_ENV = "MINERU_CRON_ALLOW_LIVE"

# Path resolutions the HARD-SAFETY guard uses. Kept as constants so
# tests can monkeypatch them: pointing `LIVE_WORKSPACE_PATH` at
# `tmp_path` lets a positive-refusal test fire without touching the real
# `$MINERU_HOME/` workspace. Derived from the `MINERU_HOME` seam so the
# guard protects the invoking user's real workspace, not one hardcoded
# path (see `LIVE_RECURRING_DIR` above).
LIVE_WORKSPACE_PATH = _MINERU_HOME
DEFAULT_CC_BIN = str(Path.home() / ".local" / "bin" / "claude-fda")

# The rc value the runner surfaces when the CC exit is clean but the
# expected-output check fails. Matches the `rc=64` fallthrough in
# `cc-job-lib.sh::run_cc_job` so launchd sees the same failure code.
RC_MISSING_EXPECTED_OUTPUT = 64


def _resolve_workspace_env() -> Path:
    """Return the WORKSPACE the runner will honor.

    Matches `cc-job-lib.sh`'s `${WORKSPACE:=${MINERU_HOME:-$HOME/.mineru}}`. Note this
    is NOT `profile.workspace_absolute` — the runner reproduces the
    bash env-var seam so a test can point WORKSPACE at `tmp_path` while
    the profile remains hydrated from the fixture.
    """
    raw = os.environ.get(WORKSPACE_ENV)
    if raw:
        return Path(raw)
    return Path.home() / ".mineru"


def _resolve_cc_bin_env() -> Tuple[str, bool]:
    """Return (cc_bin, is_env_overridden).

    The bool is used by the HARD-SAFETY guard: an override means "the
    operator (or a test) explicitly wired CC_BIN to something they own,
    so a live-workspace run is intentional".
    """
    raw = os.environ.get(CC_BIN_ENV)
    if raw:
        return raw, True
    return DEFAULT_CC_BIN, False


def _resolve_deliver_argv_env(workspace: Path) -> Tuple[List[str], bool]:
    """Return (deliver_argv, is_env_overridden).

    Matches `cc-job-lib.sh`'s `DELIVER_BIN` default: multi-word so it
    can carry an interpreter + script path. We `shlex.split` when the
    env is set (letting operators pass either a bare command or a
    quoted argv) and default to `python3 <workspace>/scripts/deliver-output.py`.
    """
    raw = os.environ.get(DELIVER_BIN_ENV)
    if raw:
        # shlex handles quoted args in a shell-safe way (e.g.
        # `DELIVER_BIN='/tmp/stub.sh --pretend'`). An empty split
        # yields [], which the caller treats as "no deliver available"
        # and skips the alert with a warning.
        return shlex.split(raw), True
    return (
        ["python3", str(workspace / "scripts" / "deliver-output.py")],
        False,
    )


def _resolve_today_yesterday(date_arg: Optional[str]) -> Tuple[str, str]:
    """Return (today, yesterday) YYYY-MM-DD strings.

    When `--date` is passed, `today` is the operator-supplied date and
    `yesterday` is date - 1; this gives the toy-test a stable clock and
    matches `cc-job-lib.sh`'s optional `$1` override on the
    consolidation trigger. When unset, both come from
    `datetime.date.today()` (system TZ), matching the bash
    `date '+%Y-%m-%d'` / `date -v-1d '+%Y-%m-%d'` calls.
    """
    if date_arg is not None:
        try:
            base = _datetime.date.fromisoformat(date_arg)
        except ValueError as exc:
            raise typer.BadParameter(
                f"--date must be an ISO YYYY-MM-DD string; got {date_arg!r} "
                f"({exc})"
            )
    else:
        base = _datetime.date.today()
    yesterday = base - _datetime.timedelta(days=1)
    return base.isoformat(), yesterday.isoformat()


def _idempotency_guard_should_skip(
    workspace: Path, marker: Optional[str], today: str, yesterday: str
) -> Optional[Path]:
    """Return the first matching marker file whose mtime date == today, else None.

    Mirrors `cc-job-lib.sh::idempotent_guard` byte-for-byte:
      * empty marker -> never skip.
      * glob-expand the resolved marker (workspace-relative -> absolute).
      * for each hit, take its mtime, format as YYYY-MM-DD, compare to `today`.
      * on first equal match, return the path (caller prints the skip notice).

    `today` is passed in explicitly so `--date` overrides work; the
    freshness helper's placeholder resolver is reused so `{today}` /
    `{yesterday}` syntax is honored the same way in status + run.
    """
    if not marker:
        return None
    resolved = resolve_marker_placeholders(marker, today=today, yesterday=yesterday)
    if resolved.startswith("/"):
        abs_glob = resolved
    else:
        abs_glob = str(workspace / resolved)
    # Glob expansion via the stdlib (matches the bash `for f in $glob`
    # under `set +f`). We stat each hit and compare its mtime date to
    # today's local-date string; this is the exact rule cc-job-lib uses
    # (line 75 `ftime=$(date -r "$f" '+%Y-%m-%d')`), NOT a 24h window.
    import glob as _glob_mod

    for candidate_str in _glob_mod.glob(abs_glob):
        candidate = Path(candidate_str)
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        file_date = _datetime.date.fromtimestamp(mtime).isoformat()
        if file_date == today:
            return candidate
    return None


def _build_standard_prompt(instruction_relative: str) -> str:
    """Return the standard `Read <instr> and execute the job.` prompt.

    Matches `cc-job-lib.sh::run_cc_job` line 252 verbatim. Any change
    here needs a coordinated update on the bash side; that is why the
    live trigger scripts still SOURCE cc-job-lib.sh today and the runner
    reproduces the string exactly.
    """
    return f"Read {instruction_relative} and execute the job."


def _build_custom_prompt(
    workspace: Path,
    job: CronJob,
    today: str,
    yesterday: str,
) -> str:
    """Return the CC prompt for a `custom_prompt: true` LLM job.

    The prompt is:

        <contents of workspace/<job.instruction>>
        <optional custom_prompt_suffix, with placeholders resolved>

    The daily-consolidation trigger uses this shape:
      cat recurring/consolidate-daily-memories.md
      followed by an "IMPORTANT: The target date is <YESTERDAY>..." pin.
    weekly-deep-consolidation uses the same shape with no suffix.

    We resolve `{today}` / `{yesterday}` in the SUFFIX only — the
    instruction file itself is passed through verbatim so a human-edited
    prompt template survives round-trip.
    """
    if not job.instruction:
        raise RuntimeError(
            f"job {job.name!r}: custom_prompt is set but instruction is empty; "
            "this should be impossible after config validation."
        )
    instr_path = workspace / job.instruction
    body = instr_path.read_text(encoding="utf-8")
    suffix_raw = job.custom_prompt_suffix
    if not suffix_raw:
        return body
    suffix = resolve_marker_placeholders(suffix_raw, today=today, yesterday=yesterday)
    return body + suffix


def _resolve_pre_step_argv(step: PreStep, today: str, yesterday: str) -> List[str]:
    """Return the pre-step's argv with `{today}`/`{yesterday}` substituted.

    Every argv element is treated as a template — the placeholders may
    appear in a positional arg (e.g. `--since {yesterday}`) or in a
    filename. `resolve_marker_placeholders` is a no-op on strings that
    have no placeholder, so it's safe to apply blindly.
    """
    return [
        resolve_marker_placeholders(arg, today=today, yesterday=yesterday)
        for arg in step.cmd
    ]


def _write_dryrun_line(fh, line: str) -> None:
    """Write a line to the dryrun log AND echo it via typer.echo."""
    typer.echo(line)
    fh.write(line + "\n")
    fh.flush()


def _alert_command(deliver_argv: List[str], message: str) -> List[str]:
    """Return the `<deliver_argv...> --raw <msg>` invocation.

    Byte-for-byte parity with `cc-job-lib.sh::_send_failure_alert`:
    the alert path is `$DELIVER_BIN --raw "$msg"`, and `DELIVER_BIN`
    may be a multi-word command. We simply extend the resolved argv
    with `--raw` + the message.
    """
    return list(deliver_argv) + ["--raw", message]


def _cc_argv(cc_bin: str, model: str, prompt: str) -> List[str]:
    """Return the exact CC argv `cc-job-lib.sh::run_cc_job` uses.

    Line 251 of cc-job-lib.sh:
      "$CC_BIN" --permission-mode bypassPermissions --model "$model" \\
                --verbose --print "Read $instr and execute the job."

    Any drift here silently changes CC's dispatch (`--verbose --print`
    controls the streaming JSON output the live jobs rely on).
    """
    return [
        cc_bin,
        "--permission-mode",
        "bypassPermissions",
        "--model",
        model,
        "--verbose",
        "--print",
        prompt,
    ]


def _refuse_live_workspace_without_optin(
    workspace: Path,
    cc_bin: str,
    cc_bin_overridden: bool,
    dry_run: bool,
) -> None:
    """HARD-SAFETY guard: refuse to run against the live workspace.

    Refusal condition: NOT dry-run AND workspace resolves to
    `LIVE_WORKSPACE_PATH` AND CC_BIN is the default AND
    `MINERU_CRON_ALLOW_LIVE` is not `"1"`.

    A dry-run bypasses this — dry-run never invokes CC or the deliver
    stub, so there is nothing dangerous to gate.
    """
    if dry_run:
        return
    try:
        workspace_resolved = workspace.resolve()
        live_resolved = LIVE_WORKSPACE_PATH.resolve()
    except OSError:
        # A tmp workspace that vanished mid-invocation — the runner
        # will fail loud on the first real op; don't raise here.
        return
    if workspace_resolved != live_resolved:
        return
    if cc_bin_overridden:
        # The operator (or a test harness) wired CC_BIN to something
        # they own; treat as intentional.
        return
    if os.environ.get(ALLOW_LIVE_ENV) == "1":
        return
    typer.echo(
        "mineru cron run: HARD SAFETY — refusing to execute against the live "
        f"workspace at {LIVE_WORKSPACE_PATH} with the default CC_BIN "
        f"({DEFAULT_CC_BIN}). Either pass --dry-run, override CC_BIN to a "
        f"stub, or set {ALLOW_LIVE_ENV}=1 to opt in.",
        err=True,
    )
    raise typer.Exit(code=2)


def _open_logs(
    workspace: Path,
    job_name: str,
    *,
    dry_run: bool,
) -> Tuple[Path, Optional[Path], Any, Any]:
    """Open the log files and return (log_path, err_path, log_fh, err_fh).

    For a real run, both `<ts>.log` and `<ts>.err` are opened under
    `<workspace>/logs/<name>/`. For a dry-run, the log is written to a
    fresh `tempfile.mkdtemp()` directory instead — a dry-run must NEVER
    create state under the caller's workspace (previewing against the
    live workspace would otherwise silently seed `logs/<name>/` with
    dryrun-*.log files). The tempdir path is printed to stdout so the
    operator can locate the transcript.
    """
    ts = _datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    if dry_run:
        # Isolate the dry-run transcript entirely off the workspace tree
        # so a preview against the live workspace touches nothing under
        # `<workspace>/logs/`. Reusing tempfile.mkdtemp() keeps the file-
        # handle contract identical for the caller.
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"mineru-cron-dryrun-{job_name}-"))
        log_path = tmp_dir / f"dryrun-{ts}.log"
        log_fh = log_path.open("w", encoding="utf-8")
        typer.echo(f"[dry-run] transcript at {log_path}")
        return log_path, None, log_fh, None
    log_dir = workspace / "logs" / job_name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{ts}.log"
    err_path = log_dir / f"{ts}.err"
    log_fh = log_path.open("w", encoding="utf-8")
    err_fh = err_path.open("w", encoding="utf-8")
    return log_path, err_path, log_fh, err_fh


def _dispatch_alert(
    deliver_argv: List[str],
    deliver_overridden: bool,
    message: str,
    log_fh,
    err_fh,
) -> None:
    """Fire a Telegram alert via DELIVER_BIN, guarded like the bash `|| true`.

    Matches `cc-job-lib.sh::_send_failure_alert`: alert failures never
    mask the underlying CC / expected-output rc; we swallow any
    exception from the deliver subprocess with a warning to the log.

    The deliver stdout/stderr are captured into the run's log/err files
    so the operator can audit what got sent (matches the live behavior
    where the trigger's redirect covers the alert too).
    """
    if not deliver_argv:
        log_fh.write("[alert] DELIVER_BIN empty; skipping alert dispatch\n")
        log_fh.flush()
        return
    cmd = _alert_command(deliver_argv, message)
    log_fh.write(f"[alert] {shlex.join(cmd)}\n")
    log_fh.flush()
    try:
        subprocess.run(
            cmd,
            stdout=log_fh,
            stderr=err_fh if err_fh is not None else log_fh,
            check=False,
        )
    except OSError as exc:
        log_fh.write(f"[alert] dispatch failed: {exc}\n")
        log_fh.flush()


def _run_pre_steps(
    workspace: Path,
    job: CronJob,
    today: str,
    yesterday: str,
    deliver_argv: List[str],
    deliver_overridden: bool,
    log_fh,
    err_fh,
    dry_run: bool,
) -> Optional[int]:
    """Run every pre-step; return None on success, an rc on failure.

    * `allow_fail: true` -> log a warning, fire the same failure-alert
      message the bash trigger uses for its continue-on-fail steps
      (`journal export step failed (continuing)`-style), and continue.
    * `allow_fail: false` -> stop, fire the alert, return
      `RC_MISSING_EXPECTED_OUTPUT` (64) so launchd sees the failure.

    Dry-run: every step is PRINTED (with placeholders resolved) but
    NOT executed.
    """
    for i, step in enumerate(job.pre_steps):
        argv = _resolve_pre_step_argv(step, today, yesterday)
        header = (
            f"[pre-step {i+1}/{len(job.pre_steps)}] "
            f"allow_fail={step.allow_fail} cmd={shlex.join(argv)}"
        )
        if step.comment:
            header += f"  # {step.comment}"
        log_fh.write(header + "\n")
        log_fh.flush()
        if dry_run:
            typer.echo(header)
            continue
        try:
            completed = subprocess.run(
                argv,
                cwd=str(workspace),
                stdout=log_fh,
                stderr=err_fh if err_fh is not None else log_fh,
                check=False,
            )
        except OSError as exc:
            # Missing binary / permission error — surface as a pre-step
            # failure with rc=127-ish. We treat it identically to a
            # subprocess exit-nonzero: the allow_fail policy applies.
            log_fh.write(f"[pre-step {i+1}] launch failed: {exc}\n")
            log_fh.flush()
            rc = 127
        else:
            rc = completed.returncode
            log_fh.write(f"[pre-step {i+1}] exit {rc}\n")
            log_fh.flush()
        if rc != 0:
            if step.allow_fail:
                _dispatch_alert(
                    deliver_argv,
                    deliver_overridden,
                    f"⚠️ Cron job {job.name} pre-step {i+1} failed "
                    f"(allow_fail, continuing, exit {rc}). Check logs/{job.name}/.",
                    log_fh,
                    err_fh,
                )
                continue
            _dispatch_alert(
                deliver_argv,
                deliver_overridden,
                f"⚠️ Cron job {job.name} failed (pre-step {i+1} exit "
                f"{rc}). Check logs/{job.name}/.",
                log_fh,
                err_fh,
            )
            return RC_MISSING_EXPECTED_OUTPUT
    return None


@cron_app.command(
    "run",
    help=(
        "Execute one scheduled job in-band (idempotency guard + CC "
        "invocation + expected-output check + failure alert). "
        "HARD-SAFETY: refuses to run against the live workspace with the "
        "default CC_BIN unless MINERU_CRON_ALLOW_LIVE=1."
    ),
)
def run(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Job name (matches cron.yaml `name:`)."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the idempotency guard even if today's marker exists.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print the intended CC argv + prompt + pre-steps without "
            "invoking CC_BIN or DELIVER_BIN. Writes to "
            "logs/<name>/dryrun-<ts>.log so real logs stay clean."
        ),
    ),
    date: Optional[str] = typer.Option(
        None,
        "--date",
        help=(
            "Pin today's date (YYYY-MM-DD) used by the idempotency guard "
            "and placeholder resolution. Matches cc-job-lib.sh's system-TZ "
            "`date +%Y-%m-%d`."
        ),
    ),
) -> None:
    """Execute `name` in-band, mirroring `cc-job-lib.sh` semantics.

    Read env seams:
      * WORKSPACE   default `${MINERU_HOME:-$HOME/.mineru}`.
      * CC_BIN      default `$HOME/.local/bin/claude-fda`.
      * DELIVER_BIN default `python3 $WORKSPACE/scripts/deliver-output.py`.

    Flow (LLM jobs):
      1. HARD-SAFETY refusal (see `_refuse_live_workspace_without_optin`).
      2. Idempotency guard against `job.idempotency_marker` (skipped with
         `--force`).
      3. Open per-run logs at `<workspace>/logs/<name>/<ts>.log(.err)`
         (or `dryrun-<ts>.log` under --dry-run).
      4. Run every `job.pre_step` in order, honoring `allow_fail`.
      5. Build the prompt: standard `Read <instr>` for `custom_prompt=false`,
         or `<instruction body> + <resolved suffix>` for `custom_prompt=true`.
      6. Invoke CC with the exact argv `cc-job-lib.sh` uses.
      7. If the job declares `expected_output_glob`, check freshness;
         a stale/missing hit fires a failure alert and sets rc=64
         (launchd sees the miss).
      8. On any failure, fire a Telegram alert (byte-for-byte parity
         with `_send_failure_alert`). The alert path is `|| true` — it
         NEVER masks the underlying rc.

    Script jobs bypass steps 4-7: their `program_args` is the whole
    unit, invoked as-is with no idempotency check. This matches the
    live plists — script jobs don't source `cc-job-lib.sh`.
    """
    profile, config, job = _get_job_or_exit(ctx, name)

    workspace = _resolve_workspace_env()
    cc_bin, cc_bin_overridden = _resolve_cc_bin_env()
    deliver_argv, deliver_overridden = _resolve_deliver_argv_env(workspace)
    today, yesterday = _resolve_today_yesterday(date)

    _refuse_live_workspace_without_optin(
        workspace,
        cc_bin,
        cc_bin_overridden,
        dry_run,
    )

    # --- Script jobs: raw argv invocation, no CC, no idempotency ----
    if job.kind == CRON_JOB_KIND_SCRIPT:
        log_path, err_path, log_fh, err_fh = _open_logs(workspace, job.name, dry_run=dry_run)
        try:
            argv = [
                resolve_marker_placeholders(a, today=today, yesterday=yesterday)
                for a in job.program_args
            ]
            header = f"[{job.name}] SCRIPT argv={shlex.join(argv)}"
            log_fh.write(header + "\n")
            log_fh.flush()
            if dry_run:
                typer.echo(header)
                typer.echo(f"[{job.name}] --dry-run: skipping subprocess invocation")
                raise typer.Exit(code=0)
            try:
                completed = subprocess.run(
                    argv,
                    cwd=str(workspace),
                    stdout=log_fh,
                    stderr=err_fh if err_fh is not None else log_fh,
                    check=False,
                )
                rc = completed.returncode
            except OSError as exc:
                log_fh.write(f"[{job.name}] launch failed: {exc}\n")
                rc = 127
            log_fh.write(f"[{job.name}] exit {rc}\n")
            if rc != 0:
                _dispatch_alert(
                    deliver_argv,
                    deliver_overridden,
                    f"⚠️ Cron job {job.name} failed (script exit {rc}). "
                    f"Check logs/{job.name}/.",
                    log_fh,
                    err_fh,
                )
            raise typer.Exit(code=rc)
        finally:
            log_fh.close()
            if err_fh is not None:
                err_fh.close()

    # --- LLM jobs ---------------------------------------------------
    # Idempotency guard (before we open logs, matching the live
    # trigger's `idempotent_guard` position — the skip should be visible
    # in launchd's own log and not create a spurious per-run log file).
    if not force:
        skip_hit = _idempotency_guard_should_skip(
            workspace, job.idempotency_marker, today, yesterday
        )
        if skip_hit is not None:
            # Byte-for-byte match to cc-job-lib.sh line 84.
            typer.echo(
                f"[idempotent_guard] Already ran today: {skip_hit} — skipping."
            )
            raise typer.Exit(code=0)

    log_path, err_path, log_fh, err_fh = _open_logs(
        workspace, job.name, dry_run=dry_run
    )
    try:
        ts_now = _datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        header = f"[{ts_now}] Starting {job.name}"
        log_fh.write(header + "\n")
        log_fh.flush()
        if dry_run:
            typer.echo(header)

        # --- Pre-steps ---
        pre_rc = _run_pre_steps(
            workspace,
            job,
            today,
            yesterday,
            deliver_argv,
            deliver_overridden,
            log_fh,
            err_fh,
            dry_run,
        )
        if pre_rc is not None:
            raise typer.Exit(code=pre_rc)

        # --- Build prompt ---
        if job.custom_prompt:
            prompt = _build_custom_prompt(workspace, job, today, yesterday)
        else:
            prompt = _build_standard_prompt(job.instruction or "")
        argv = _cc_argv(cc_bin, job.model or "", prompt)

        cc_line = f"[{ts_now}] CC model={job.model} instruction={job.instruction}"
        log_fh.write(cc_line + "\n")
        log_fh.write(f"[cc-argv] {shlex.join(argv)}\n")
        log_fh.write(f"[cc-prompt-len] {len(prompt)} chars\n")
        log_fh.flush()

        if dry_run:
            # Emit BOTH the argv and the prompt to stdout so a reviewer
            # can copy either the exact CC command or the exact prompt
            # into a debugging session. cc-argv line is one shell-safe
            # string; the prompt is delimited so a multi-line custom
            # prompt is easy to spot.
            typer.echo(f"[cc-argv] {shlex.join(argv)}")
            typer.echo("[cc-prompt] <<<PROMPT")
            typer.echo(prompt)
            typer.echo("PROMPT>>>")
            typer.echo(f"[{job.name}] --dry-run: skipping CC + DELIVER invocation")
            raise typer.Exit(code=0)

        # --- Real CC invocation ---
        try:
            completed = subprocess.run(
                argv,
                cwd=str(workspace),
                stdout=log_fh,
                stderr=err_fh if err_fh is not None else log_fh,
                check=False,
            )
            rc = completed.returncode
        except OSError as exc:
            log_fh.write(f"[cc] launch failed: {exc}\n")
            rc = 127
        cc_done_line = f"[{_datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}] CC exited with code {rc}"
        log_fh.write(cc_done_line + "\n")
        log_fh.flush()

        # --- Alerts + expected-output check ---
        if rc != 0:
            _dispatch_alert(
                deliver_argv,
                deliver_overridden,
                f"⚠️ Cron job {job.name} failed (CC exited nonzero, exit {rc}). "
                f"Check logs/{job.name}/.",
                log_fh,
                err_fh,
            )
        elif job.expected_output_glob:
            check = check_expected_output_fresh(
                workspace,
                job.expected_output_glob,
                today=today,
                yesterday=yesterday,
            )
            if not check.fresh:
                _dispatch_alert(
                    deliver_argv,
                    deliver_overridden,
                    f"⚠️ Cron job {job.name} failed (no recent output matched "
                    f"'{job.expected_output_glob}', exit {rc}). Check logs/{job.name}/.",
                    log_fh,
                    err_fh,
                )
                # Surface the missing-output failure to launchd. The
                # alert NEVER masks the CC rc: we only bump when CC
                # itself was clean.
                rc = RC_MISSING_EXPECTED_OUTPUT

        done_line = f"[{_datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}] Done (final rc={rc})"
        log_fh.write(done_line + "\n")
        log_fh.flush()
        raise typer.Exit(code=rc)
    finally:
        log_fh.close()
        if err_fh is not None:
            err_fh.close()


# ---------------------------------------------------------------------------
# `install <name> [--dry-run] [--backup-existing] [--all] [--live-flip]`
# `uninstall <name> [--dry-run] [--all] [--live-flip]`
# ---------------------------------------------------------------------------
#
# The write-side of the cron surface, gated HARD in this Phase-4 SAFE build.
#
# In this build, the LIVE path (writing to `~/Library/LaunchAgents` and calling
# `launchctl bootstrap`) is disabled by default. To take that path an operator
# must BOTH pass `--live-flip` AND set `MINERU_CRON_ALLOW_LIVE=1`. Otherwise
# a plain `mineru cron install <name>` (with no `--dry-run`) prints a loud
# gate message and exits non-zero. This is the "reversible, isolated parts"
# discipline the Phase-4 cutover doc calls for: install works, but running
# it against the real launchd is still the operator's explicit call in a later step.
#
# `--dry-run` bypasses every filesystem write and every `launchctl` call by
# design. It PRINTS what would happen: the rendered plist, the write target,
# and the exact `launchctl bootstrap` / `bootout` commands. This is the
# default operator surface until the live gate flips.
#
# Blocklists (LANDMINE §7):
#   * `telegram-daemon` / `daemon-watchdog` — the Landline daemon plists.
#     Never touched by `mineru cron` under any flag combination.
#   * Any per-install paused recipe (a trigger script with NO live plist) that
#     an operator wants defensively refused can be added to
#     `BLOCKLIST_JOB_NAMES` below.
#
# Backup discipline: `--backup-existing` copies the current plist at the
# target path to `<workspace>/archive/launchd-backup-<YYYY-MM-DD>/<name>.plist`
# BEFORE writing the new one. Uses `shutil.copy2` (preserves mtime), then
# rewrites the target. Never `rm`.
#
# Uninstall discipline: `bootout` first, then `trash` the plist (via the
# `/usr/bin/trash` CLI). Never `rm`. Dry-run prints both the bootout command
# and the plist path it WOULD trash.


# Hard-coded blocklists per LANDMINE. These names must never be touched by
# any `mineru cron install` / `uninstall` invocation — under any flag
# combination, including `--live-flip` + `MINERU_CRON_ALLOW_LIVE=1`. The
# daemon plists belong to Landline (a separate repo + config). An operator
# with a paused per-install recipe (trigger script but no live plist) can
# extend this tuple with that recipe's name to defensively refuse it too.
BLOCKLIST_JOB_NAMES: Tuple[str, ...] = (
    "telegram-daemon",
    "daemon-watchdog",
)


def _resolve_workspace_absolute(profile: Profile) -> Path:
    """Return the profile's workspace absolute path.

    Kept as its own helper so the backup dir resolution + the install
    guardrails both see the same value in the same shape (Path).
    """
    return profile.workspace_absolute


def _backup_dir_for_today(profile: Profile) -> Path:
    """Return the `<workspace>/archive/launchd-backup-<today>/` path.

    The workspace-relative backup dir lives inside the profile-owned
    workspace so a worktree-scoped profile's backups land inside the
    worktree, not in the live workspace. Uses `datetime.date.today()`
    (system TZ), matching the runner's `--date`-less path.
    """
    today = _datetime.date.today().isoformat()
    return _resolve_workspace_absolute(profile) / "archive" / f"launchd-backup-{today}"


def _uid() -> int:
    """Return the current UID; used to build `gui/<uid>` domain targets."""
    return os.getuid()


def _bootstrap_command(plist_path: Path) -> List[str]:
    """Return the `launchctl bootstrap gui/<uid> <plist>` argv.

    Per LANDMINE §7: macOS 13+ uses `bootstrap` / `bootout` (not `load`
    / `unload`). The domain target is `gui/<uid>` for per-user agents.
    """
    return ["launchctl", "bootstrap", f"gui/{_uid()}", str(plist_path)]


def _bootout_command(label: str) -> List[str]:
    """Return the `launchctl bootout gui/<uid>/<label>` argv."""
    return ["launchctl", "bootout", f"gui/{_uid()}/{label}"]


def _live_launchd_dir() -> Path:
    """Return the resolved live LaunchAgents dir (`~/Library/LaunchAgents`)."""
    return DEFAULT_LAUNCHD_DIR.expanduser().resolve()


def _is_live_launchd_target(target_dir: Path) -> bool:
    """Return True iff `target_dir` resolves to `~/Library/LaunchAgents`.

    Compared on the fully-resolved (symlink-followed) paths so an
    `MINERU_LAUNCHD_DIR` that points at a symlink into the real
    LaunchAgents dir still trips the gate.
    """
    try:
        return target_dir.expanduser().resolve() == _live_launchd_dir()
    except OSError:
        return False


def _refuse_blocklisted_job(name: str) -> None:
    """Exit non-zero if `name` is on the hard-coded blocklist."""
    if name in BLOCKLIST_JOB_NAMES:
        typer.echo(
            f"mineru cron: LANDMINE — refusing to touch {name!r}. "
            "This job is on the hard-coded blocklist "
            f"({', '.join(BLOCKLIST_JOB_NAMES)}); the daemon plists belong "
            "to Landline (never managed by `mineru cron`), and per-install "
            "paused recipes can be added to this blocklist too. "
            "See reports/2026-08-01-mineru-phase4-cutover.md §7.",
            err=True,
        )
        raise typer.Exit(code=2)


def _refuse_live_install_without_optin(
    target_dir: Path,
    dry_run: bool,
    live_flip: bool,
) -> None:
    """Enforce the Phase-4 SAFE build's live-flip gate.

    The rules, in order:

      * `--dry-run` always bypasses this guard (nothing is written).
      * Not `--live-flip` + not `--dry-run`: refuse with the LOUD Phase-4
        gate message. the operator re-enables the live path in a later gated step.
      * `--live-flip` + `MINERU_CRON_ALLOW_LIVE != "1"`: refuse — the env
        var is the belt-and-braces guard so `--live-flip` alone in a stray
        invocation cannot hit real launchd.
      * Target dir resolves to `~/Library/LaunchAgents` requires the same
        gate: `--live-flip` + `MINERU_CRON_ALLOW_LIVE=1`. Any other
        `MINERU_LAUNCHD_DIR` (tmp dir, worktree mirror) proceeds.
    """
    if dry_run:
        return
    if not live_flip:
        typer.echo(
            "mineru cron install: LIVE INSTALL DISABLED (Phase 4 SAFE build); "
            "pass --live-flip AND set MINERU_CRON_ALLOW_LIVE=1 to enable. "
            "Use --dry-run to preview the rendered plist and the launchctl "
            "commands that would run.",
            err=True,
        )
        raise typer.Exit(code=2)
    if os.environ.get(ALLOW_LIVE_ENV) != "1":
        typer.echo(
            f"mineru cron install: --live-flip requires {ALLOW_LIVE_ENV}=1. "
            "The env var is the belt-and-braces guard so a stray --live-flip "
            "cannot hit real launchd.",
            err=True,
        )
        raise typer.Exit(code=2)
    if _is_live_launchd_target(target_dir):
        # Even with --live-flip + ALLOW_LIVE=1, the live LaunchAgents
        # dir is a special case: the Phase-4 SAFE build refuses to
        # touch it. the operator's cutover runbook (§6) flips this in a later
        # gated step. Until then, the env-var gate protects the real
        # launchd from an operator typo.
        typer.echo(
            "mineru cron install: LANDMINE — refusing to write into the "
            f"live LaunchAgents dir at {target_dir}. Phase 4 SAFE build "
            "keeps the real launchd off-limits; point MINERU_LAUNCHD_DIR "
            "at a mirror to install there.",
            err=True,
        )
        raise typer.Exit(code=2)


def _safety_check_target_dir(target_dir: Path, resolved_target_path: Path) -> None:
    """Refuse to write outside the resolved `target_dir`.

    Belt-and-braces guard against a `job.name` (or a path-hop) that
    would escape the launchd dir. The final resolved path MUST have
    `target_dir` as an ancestor (or equal it).
    """
    try:
        resolved_target = target_dir.expanduser().resolve()
    except OSError as exc:
        typer.echo(
            f"mineru cron install: could not resolve target dir "
            f"{target_dir}: {exc}",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        resolved_target_path.relative_to(resolved_target)
    except ValueError:
        typer.echo(
            "mineru cron install: LANDMINE — resolved plist path "
            f"{resolved_target_path} would escape target dir "
            f"{resolved_target}. Refusing.",
            err=True,
        )
        raise typer.Exit(code=2)


def _resolve_trash_bin() -> Optional[str]:
    """Return the absolute path to the `trash` CLI, or None if absent.

    macOS ships `trash` at `/usr/bin/trash` on modern systems; some
    older installs used a Homebrew build at `/usr/local/bin/trash` or
    `/opt/homebrew/bin/trash`. We defer to `shutil.which` first so a
    PATH-overridden `trash` (e.g. a test harness stub) is honored, then
    fall back to the known-good absolute paths.
    """
    which_hit = shutil.which("trash")
    if which_hit:
        return which_hit
    for candidate in ("/usr/bin/trash", "/usr/local/bin/trash", "/opt/homebrew/bin/trash"):
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _perform_backup(
    installed_path: Path,
    job_name: str,
    profile: Profile,
    dry_run: bool,
) -> Optional[Path]:
    """Copy the currently-installed plist to the workspace backup dir.

    Returns the resolved destination path (or None if there was nothing
    to back up). In dry-run, PRINTS what it would do but does not touch
    the filesystem.

    Uses `shutil.copy2` to preserve mtime — a launchd operator debugging
    a rollback wants to see the original mtime, not "now". The backup dir
    lives under the profile's workspace so a worktree profile never
    writes into the live workspace.
    """
    if not installed_path.exists():
        return None
    backup_dir = _backup_dir_for_today(profile)
    backup_path = backup_dir / f"{job_name}.plist"
    if dry_run:
        typer.echo(
            f"[dry-run] would back up {installed_path} -> {backup_path}"
        )
        return backup_path
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Defense-in-depth: refuse to follow a symlink at the backup
    # destination. A malicious or accidental pre-existing symlink at
    # `<workspace>/archive/launchd-backup-<today>/<name>.plist` would
    # otherwise cause shutil.copy2 to clobber an arbitrary file the
    # symlink targets.
    if backup_path.is_symlink():
        typer.echo(
            "mineru cron install: LANDMINE — refusing to write backup "
            f"through a symlink at {backup_path}. Remove the symlink "
            "manually and retry.",
            err=True,
        )
        raise typer.Exit(code=2)
    shutil.copy2(installed_path, backup_path)
    typer.echo(f"backed up {installed_path} -> {backup_path}", err=True)
    return backup_path


def _install_one(
    profile: Profile,
    config: CronConfig,
    job: CronJob,
    target_dir: Path,
    dry_run: bool,
    backup_existing: bool,
    live_flip: bool,
) -> None:
    """Install one job: render plist, optionally back up, optionally bootstrap.

    Flow:
      1. Blocklist check (LANDMINE).
      2. Live-install gate (Phase-4 SAFE build refusal unless
         `--dry-run` OR (`--live-flip` + `MINERU_CRON_ALLOW_LIVE=1`)).
      3. Render the plist via P4-02's `render_plist`.
      4. Resolve the target path (`<target_dir>/<label>.plist`).
      5. `_safety_check_target_dir` — refuse if the resolved path
         escapes the resolved target dir.
      6. If `--backup-existing` and a plist is already there, back it up.
      7. In dry-run: PRINT the rendered plist + the launchctl commands
         that would run. In live: write the plist, then run
         `launchctl bootstrap gui/<uid> <plist>`.
    """
    _refuse_blocklisted_job(job.name)
    _refuse_live_install_without_optin(target_dir, dry_run, live_flip)

    try:
        rendered = render_plist(job, profile)
    except PlistRenderError as exc:
        typer.echo(f"mineru cron install: {exc}", err=True)
        raise typer.Exit(code=2)

    label = f"{profile.launchd_label_prefix}.{job.name}"
    resolved_target_dir = target_dir.expanduser().resolve() if target_dir.expanduser().exists() else target_dir.expanduser()
    # We may be resolving a dir that doesn't exist yet (fresh tmp path);
    # in that case fall back to the un-resolved expanduser'd path — the
    # equality check in `_safety_check_target_dir` only cares that the
    # target_path is inside `target_dir`, not that both exist.
    target_path = resolved_target_dir / f"{label}.plist"
    # For the escape check, we resolve target_path's parent (which is
    # `target_dir`) even when the file itself doesn't yet exist.
    try:
        check_target = target_path.parent.resolve() / target_path.name
    except OSError:
        check_target = target_path
    _safety_check_target_dir(target_dir, check_target)

    bootstrap_argv = _bootstrap_command(target_path)

    if dry_run:
        typer.echo(f"# would install {job.name!r} at {target_path}")
        typer.echo(rendered, nl=False)
        typer.echo("# launchctl commands that would run:")
        # Idempotent semantics: an existing installed plist for this
        # label must be booted OUT before bootstrap can succeed. We
        # print both so an operator can copy-paste the reinstall
        # verbatim.
        if target_path.exists():
            typer.echo(f"  {shlex.join(_bootout_command(label))}")
        typer.echo(f"  {shlex.join(bootstrap_argv)}")
        if backup_existing:
            _perform_backup(target_path, job.name, profile, dry_run=True)
        return

    # LIVE install path — only reachable after the gate above passed.
    #
    # Defense-in-depth #1: the `run` verb refuses to touch the LIVE
    # workspace at `LIVE_WORKSPACE_PATH` without an explicit ALLOW_LIVE=1
    # opt-in. `install` must do the same, because a `--live-flip` with a
    # mirror `MINERU_LAUNCHD_DIR` + `--backup-existing` while the active
    # profile still points at the live workspace would otherwise write
    # backups into `$MINERU_HOME/archive/launchd-backup-<today>/`.
    # Mirror the guard `_refuse_live_workspace_without_optin` uses so the
    # semantics are consistent.
    try:
        profile_workspace_resolved = profile.workspace_absolute.resolve()
        live_workspace_resolved = LIVE_WORKSPACE_PATH.resolve()
    except OSError:
        profile_workspace_resolved = None
        live_workspace_resolved = None
    if (
        profile_workspace_resolved is not None
        and profile_workspace_resolved == live_workspace_resolved
        and os.environ.get(ALLOW_LIVE_ENV) != "1"
    ):
        typer.echo(
            "mineru cron install: HARD SAFETY — refusing to install with "
            f"the active profile's workspace pointing at the LIVE workspace "
            f"({LIVE_WORKSPACE_PATH}). --backup-existing would land in "
            f"{LIVE_WORKSPACE_PATH}/archive/. Point the profile at a "
            f"worktree/mirror workspace, or set {ALLOW_LIVE_ENV}=1 to opt in.",
            err=True,
        )
        raise typer.Exit(code=2)

    if backup_existing:
        _perform_backup(target_path, job.name, profile, dry_run=False)

    resolved_target_dir.mkdir(parents=True, exist_ok=True)
    # Defense-in-depth #2: `Path.write_text` follows symlinks at the
    # leaf, so a pre-existing symlink at
    # `<launchd_dir>/<label>.plist` would clobber the arbitrary file
    # the symlink points at. `_safety_check_target_dir` above validates
    # the parent, not the leaf; catch the leaf explicitly here (and the
    # symlink itself is fine to trash from launchd, but we refuse to
    # write THROUGH it).
    if target_path.is_symlink():
        typer.echo(
            "mineru cron install: LANDMINE — refusing to write through a "
            f"symlink at {target_path}. Remove the symlink manually and "
            "retry.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        # os.O_NOFOLLOW hardens the write against a symlink race between
        # the is_symlink() check above and the actual open (TOCTOU): if
        # `target_path` is a symlink at open time, the OS raises ELOOP
        # and we bail without writing through it.
        fd = os.open(
            target_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o644,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as target_fh:
            target_fh.write(rendered)
    except OSError as exc:
        typer.echo(
            f"mineru cron install: could not write {target_path}: {exc}",
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(f"wrote {target_path}", err=True)

    # bootout any prior installation of this label (idempotent), then
    # bootstrap. bootout is best-effort — if the plist was never loaded
    # its rc is non-zero and that's fine.
    try:
        subprocess.run(_bootout_command(label), check=False)
    except OSError as exc:
        typer.echo(
            f"mineru cron install: bootout of {label} failed (continuing): {exc}",
            err=True,
        )
    try:
        completed = subprocess.run(bootstrap_argv, check=False)
    except OSError as exc:
        typer.echo(
            f"mineru cron install: launchctl bootstrap failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=2)
    if completed.returncode != 0:
        typer.echo(
            f"mineru cron install: launchctl bootstrap {label} exited "
            f"{completed.returncode}",
            err=True,
        )
        raise typer.Exit(code=completed.returncode)


def _uninstall_one(
    profile: Profile,
    job_name: str,
    target_dir: Path,
    dry_run: bool,
    live_flip: bool,
) -> None:
    """Uninstall one job: bootout, then trash the plist.

    Same gate discipline as install: `--dry-run` always OK; otherwise
    `--live-flip` + `MINERU_CRON_ALLOW_LIVE=1` required, and the live
    LaunchAgents dir is off-limits regardless in this Phase-4 SAFE
    build.
    """
    _refuse_blocklisted_job(job_name)
    _refuse_live_install_without_optin(target_dir, dry_run, live_flip)

    label = f"{profile.launchd_label_prefix}.{job_name}"
    resolved_target_dir = target_dir.expanduser().resolve() if target_dir.expanduser().exists() else target_dir.expanduser()
    target_path = resolved_target_dir / f"{label}.plist"

    bootout_argv = _bootout_command(label)

    if dry_run:
        typer.echo(f"# would uninstall {job_name!r} at {target_path}")
        typer.echo("# launchctl commands that would run:")
        typer.echo(f"  {shlex.join(bootout_argv)}")
        if target_path.exists():
            typer.echo(f"# would trash: {target_path}")
        else:
            typer.echo(f"# nothing to trash: {target_path} does not exist")
        return

    # LIVE uninstall path — only reachable after the gate above passed.
    try:
        subprocess.run(bootout_argv, check=False)
    except OSError as exc:
        typer.echo(
            f"mineru cron uninstall: bootout of {label} failed (continuing): {exc}",
            err=True,
        )

    if not target_path.exists():
        typer.echo(
            f"mineru cron uninstall: {target_path} not present (nothing to trash).",
            err=True,
        )
        return

    trash_bin = _resolve_trash_bin()
    if trash_bin is None:
        typer.echo(
            "mineru cron uninstall: `trash` CLI not found on PATH "
            "(/usr/bin/trash, /usr/local/bin/trash, /opt/homebrew/bin/trash). "
            f"Install it (`brew install trash`) or move {target_path} by hand. "
            "REFUSING to use `rm` (CLAUDE.md rule).",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        completed = subprocess.run([trash_bin, "-v", str(target_path)], check=False)
    except OSError as exc:
        typer.echo(
            f"mineru cron uninstall: `trash` failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=2)
    if completed.returncode != 0:
        typer.echo(
            f"mineru cron uninstall: `trash {target_path}` exited "
            f"{completed.returncode}",
            err=True,
        )
        raise typer.Exit(code=completed.returncode)
    typer.echo(f"trashed {target_path}", err=True)


def _iter_all_enabled_jobs(config: CronConfig) -> List[CronJob]:
    """Return every enabled job, blocklist entries skipped silently.

    Blocklisted names never appear in cron.yaml today, but the filter
    keeps `--all` safe if one ever sneaks in. Disabled jobs are also
    skipped — an operator uses `enabled: false` to say "declared but
    don't install this".
    """
    return [
        job
        for job in config.jobs
        if job.enabled and job.name not in BLOCKLIST_JOB_NAMES
    ]


@cron_app.command(
    "install",
    help=(
        "Install one scheduled job (or every enabled job with --all) to "
        "the launchd dir resolved from MINERU_LAUNCHD_DIR. "
        "Phase 4 SAFE build: LIVE INSTALL DISABLED unless --live-flip "
        "AND MINERU_CRON_ALLOW_LIVE=1 are BOTH set. Use --dry-run "
        "to preview the rendered plist and the launchctl commands."
    ),
)
def install(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(
        None,
        help="Job name (matches cron.yaml `name:`). Omit with --all.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print the rendered plist + the launchctl commands that WOULD "
            "run, but perform no filesystem write and no launchctl call."
        ),
    ),
    backup_existing: bool = typer.Option(
        False,
        "--backup-existing",
        help=(
            "Before writing, copy any existing plist at the target path to "
            "<workspace>/archive/launchd-backup-<today>/<name>.plist. "
            "Uses `shutil.copy2`; never `rm`."
        ),
    ),
    all_jobs: bool = typer.Option(
        False,
        "--all",
        help=(
            "Install every enabled job in cron.yaml. In this Phase-4 SAFE "
            "build --all is ALSO dry-run-only until MINERU_CRON_ALLOW_LIVE=1."
        ),
    ),
    live_flip: bool = typer.Option(
        False,
        "--live-flip",
        help=(
            "Opt in to the live install path. REQUIRED (with "
            "MINERU_CRON_ALLOW_LIVE=1) to write into any launchd dir; "
            "still refused if the target dir is ~/Library/LaunchAgents."
        ),
    ),
) -> None:
    """Materialize a job's plist into the resolved launchd dir.

    Target dir is `MINERU_LAUNCHD_DIR` (default `~/Library/LaunchAgents`).
    Refuses to write into `~/Library/LaunchAgents` in this Phase-4 SAFE
    build; point MINERU_LAUNCHD_DIR at a tmp/mirror dir to install
    there. `--dry-run` prints the plist + the launchctl commands and
    writes nothing.

    Blocklisted (LANDMINE §7): `telegram-daemon`, `daemon-watchdog`
    (plus any per-install paused recipe an operator adds to
    `BLOCKLIST_JOB_NAMES`). Refused under any flag combination.
    """
    profile, config = _load_config(ctx)
    target_dir = _resolved_launchd_dir()

    # --all is dry-run-only in the Phase-4 SAFE build. This is the
    # LOUDEST safety knob: batch-installing every job into the live
    # launchd dir would be catastrophic if the gate ever slipped.
    if all_jobs:
        if not dry_run and not live_flip:
            typer.echo(
                "mineru cron install --all: LIVE BATCH INSTALL DISABLED "
                "(Phase 4 SAFE build); pass --dry-run to preview every job, "
                "or --live-flip AND MINERU_CRON_ALLOW_LIVE=1 to enable "
                "the live path.",
                err=True,
            )
            raise typer.Exit(code=2)
        if name is not None:
            typer.echo(
                "mineru cron install: pass either a job name OR --all, not both.",
                err=True,
            )
            raise typer.Exit(code=2)
        jobs = _iter_all_enabled_jobs(config)
        if not jobs:
            typer.echo("mineru cron install: no enabled jobs in cron.yaml.", err=True)
            raise typer.Exit(code=2)
        for job in jobs:
            typer.echo(f"# --- {job.name} ---")
            _install_one(
                profile,
                config,
                job,
                target_dir,
                dry_run=dry_run,
                backup_existing=backup_existing,
                live_flip=live_flip,
            )
        return

    if name is None:
        typer.echo(
            "mineru cron install: pass a job name (or --all).",
            err=True,
        )
        raise typer.Exit(code=2)
    _, _, job = _get_job_or_exit(ctx, name)
    _install_one(
        profile,
        config,
        job,
        target_dir,
        dry_run=dry_run,
        backup_existing=backup_existing,
        live_flip=live_flip,
    )


@cron_app.command(
    "uninstall",
    help=(
        "Uninstall one scheduled job (or every enabled job with --all) "
        "from the launchd dir. Bootout first, then `trash` the plist "
        "(never `rm`). Phase 4 SAFE build: LIVE UNINSTALL DISABLED unless "
        "--live-flip AND MINERU_CRON_ALLOW_LIVE=1 are BOTH set."
    ),
)
def uninstall(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(
        None,
        help="Job name (matches cron.yaml `name:`). Omit with --all.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print the bootout command + the plist path that WOULD be "
            "trashed. Performs no launchctl call and no filesystem write."
        ),
    ),
    all_jobs: bool = typer.Option(
        False,
        "--all",
        help=(
            "Uninstall every enabled job in cron.yaml. In this Phase-4 SAFE "
            "build --all is ALSO dry-run-only until MINERU_CRON_ALLOW_LIVE=1."
        ),
    ),
    live_flip: bool = typer.Option(
        False,
        "--live-flip",
        help=(
            "Opt in to the live uninstall path. REQUIRED (with "
            "MINERU_CRON_ALLOW_LIVE=1) to bootout / trash a real plist; "
            "still refused if the target dir is ~/Library/LaunchAgents."
        ),
    ),
) -> None:
    """Remove a job's plist from the resolved launchd dir.

    `bootout gui/<uid>/<label>` first (idempotent — non-zero exit is
    fine if the label wasn't loaded), then `trash <plist>` (never `rm`).
    Same gate discipline as `install`.
    """
    profile, config = _load_config(ctx)
    target_dir = _resolved_launchd_dir()

    if all_jobs:
        if not dry_run and not live_flip:
            typer.echo(
                "mineru cron uninstall --all: LIVE BATCH UNINSTALL DISABLED "
                "(Phase 4 SAFE build); pass --dry-run to preview every job, "
                "or --live-flip AND MINERU_CRON_ALLOW_LIVE=1 to enable "
                "the live path.",
                err=True,
            )
            raise typer.Exit(code=2)
        if name is not None:
            typer.echo(
                "mineru cron uninstall: pass either a job name OR --all, not both.",
                err=True,
            )
            raise typer.Exit(code=2)
        jobs = _iter_all_enabled_jobs(config)
        if not jobs:
            typer.echo("mineru cron uninstall: no enabled jobs in cron.yaml.", err=True)
            raise typer.Exit(code=2)
        for job in jobs:
            typer.echo(f"# --- {job.name} ---")
            _uninstall_one(
                profile,
                job.name,
                target_dir,
                dry_run=dry_run,
                live_flip=live_flip,
            )
        return

    if name is None:
        typer.echo(
            "mineru cron uninstall: pass a job name (or --all).",
            err=True,
        )
        raise typer.Exit(code=2)
    # Resolve via `get_job` so blocklisted names surface via the same
    # error path as `install`. If the job isn't in cron.yaml we still
    # want to refuse a blocklisted name (defense in depth), so the
    # blocklist is checked in `_uninstall_one` too.
    _, _, _ = _get_job_or_exit(ctx, name)
    _uninstall_one(
        profile,
        name,
        target_dir,
        dry_run=dry_run,
        live_flip=live_flip,
    )


__all__ = [
    "cron_app",
    "LAUNCHD_DIR_ENV",
    "DEFAULT_LAUNCHD_DIR",
    "LIVE_RECURRING_DIR",
    "CC_BIN_ENV",
    "DELIVER_BIN_ENV",
    "WORKSPACE_ENV",
    "ALLOW_LIVE_ENV",
    "LIVE_WORKSPACE_PATH",
    "DEFAULT_CC_BIN",
    "RC_MISSING_EXPECTED_OUTPUT",
    "BLOCKLIST_JOB_NAMES",
]
