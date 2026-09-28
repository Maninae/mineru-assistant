"""Hydration apply: executes a `HydrationPlan` against the filesystem.

Separated from `plan.py` so the planning half stays pure. The default is
dry-run (print only). A real apply first runs `preflight_plan`
(`apply_preflight.py`), which decides every refusal before the first
write, then executes each action idempotently. Re-running an install
over its own previous output is safe and converges.

Rules (the single home for them):

Target guard (skipped with `force=True`):
  * The target may already contain framework and runtime state
    (`FRAMEWORK_RESERVED_NAMES`: `profiles`, `active`, `current`,
    `engine`, `people.yaml`, `humans.yaml`, `cache`, `logs`, `archive`,
    `output`, `.venv*`, `.claude`) and anything this plan itself writes
    (a previous install). Any other top-level entry refuses the install.

Per-dest shape, by planned action (checked for every action up front):

| Planned | Nothing there | Symlink there | Real file there | Real dir there |
|---------|---------------|---------------|-----------------|----------------|
| MKDIR   | create | error; force: replace with a real dir | error | no-op |
| SYMLINK | create | no-op if same source, else replace | error; force: replace | error, even with force |
| RENDER  | write | error, even with force | rewrite only if content differs | error, even with force |

  * A real directory is never removed and a symlink at a render path is
    never written through (`O_NOFOLLOW`); the operator moves it aside.
  * The CLI exposes no force; `force` is a library-only escape hatch.
  * Every template is rendered before the first write, so a template
    error (unknown variable) leaves the target untouched.
  * `apply_plan` returns an `ApplyReport` with created / updated /
    unchanged counts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import typer

from mineru_cli.install.apply_preflight import preflight_plan
from mineru_cli.install.plan_types import (
    HydrationAction,
    HydrationActionKind,
    HydrationError,
    HydrationPlan,
)
from mineru_cli.install.renderer import render_template


@dataclass
class ApplyReport:
    """Outcome counts of one `apply_plan` run (all zero for a dry-run)."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0

    def summary(self) -> str:
        """One-line human summary, e.g. `3 created, 1 updated, 40 unchanged`."""
        return (
            f"{self.created} created, {self.updated} updated, "
            f"{self.unchanged} unchanged"
        )


def apply_plan(
    plan: HydrationPlan,
    *,
    dry_run: bool = True,
    force: bool = False,
) -> ApplyReport:
    """Execute a `HydrationPlan`. Default is dry-run (prints, no writes).

    Args:
        plan: the plan to execute.
        dry_run: when True (default), print `plan.render()` and return.
        force: library-only; relaxes the target guard and the forceable
            per-dest conflicts (see the module docstring table).
    Returns:
        The `ApplyReport` counts.
    Raises:
        HydrationError: a pre-flight refusal (nothing written) or an
            apply-time failure (unreadable template, unwritable dest).
    """
    report = ApplyReport()
    if dry_run:
        # typer.echo so CliRunner captures it in `result.output`.
        typer.echo(plan.render())
        return report
    preflight_plan(plan, force=force)
    rendered_by_dest = _render_all(plan)
    for action in plan.actions:
        _apply_one(action, rendered_by_dest, report)
    return report


def _render_all(plan: HydrationPlan) -> Dict[Path, str]:
    """Render every RENDER action up front, so a template error writes nothing.

    Raises `HydrationError` (unreadable template) or `ValueError` (bad
    template, unknown variable) before the first filesystem mutation.
    """
    rendered_by_dest: Dict[Path, str] = {}
    for action in plan.actions:
        if action.kind != HydrationActionKind.RENDER:
            continue
        if action.source is None:
            raise HydrationError(f"RENDER action has no source: dest={action.dest}")
        try:
            template_text = action.source.read_text(encoding="utf-8")
        except OSError as exc:
            raise HydrationError(
                f"could not read template {action.source}: {type(exc).__name__}"
            ) from exc
        rendered_by_dest[action.dest] = render_template(template_text, plan.context)
    return rendered_by_dest


def _apply_one(
    action: HydrationAction, rendered_by_dest: Dict[Path, str], report: ApplyReport
) -> None:
    """Execute one action against the filesystem and count the outcome."""
    if action.kind == HydrationActionKind.MKDIR:
        _apply_mkdir(action.dest, report)
        return
    action.dest.parent.mkdir(parents=True, exist_ok=True)
    if action.kind == HydrationActionKind.RENDER:
        _write_rendered(action.dest, rendered_by_dest[action.dest], report)
        return
    if action.kind == HydrationActionKind.SYMLINK:
        _apply_symlink(action, report)
        return
    raise HydrationError(  # pragma: no cover (enum-exhaustive)
        f"unknown action kind: {action.kind!r}"
    )


def _apply_mkdir(dest: Path, report: ApplyReport) -> None:
    """Ensure a real dir at `dest`; a pointer symlink (force only) is replaced."""
    if dest.is_symlink():
        dest.unlink()  # a pointer, never data; preflight allowed this under force
        dest.mkdir(parents=True)
        report.updated += 1
        return
    if dest.is_dir():
        report.unchanged += 1
        return
    dest.mkdir(parents=True, exist_ok=True)
    report.created += 1


def _write_rendered(dest: Path, rendered: str, report: ApplyReport) -> None:
    """Write `rendered` to `dest`, skipping identical content.

    The write is `O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW`: a symlink
    planted at `dest` after pre-flight makes the open fail (`ELOOP`)
    instead of redirecting rendered bytes outside the sandbox.
    """
    existed = dest.is_file() and not dest.is_symlink()
    if existed and _read_text_or_none(dest) == rendered:
        report.unchanged += 1
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(str(dest), flags, 0o644)
    except OSError as exc:
        raise HydrationError(
            f"could not open RENDER dest {dest} for writing "
            f"({type(exc).__name__}: {exc}); a pre-existing symlink at "
            "this path would have redirected the write outside the sandbox."
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    if existed:
        report.updated += 1
    else:
        report.created += 1


def _read_text_or_none(path: Path) -> Any:
    """Return the file's UTF-8 text, or None when unreadable or not UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _apply_symlink(action: HydrationAction, report: ApplyReport) -> None:
    """Create, keep, or replace the symlink `action.dest -> action.source`.

    Same-source links are left alone; other links and (under force) real
    files are replaced. A real directory is refused here too, as a second
    line behind pre-flight.
    """
    if action.source is None:
        raise HydrationError(f"SYMLINK action has no source: dest={action.dest}")
    dest = action.dest
    if dest.is_symlink():
        if os.readlink(dest) == str(action.source):
            report.unchanged += 1
            return
        dest.unlink()
        os.symlink(action.source, dest)
        report.updated += 1
        return
    if dest.is_dir():
        raise HydrationError(
            f"symlink dest {dest} is an existing directory; refuse to "
            "remove directories implicitly."
        )
    if dest.exists():
        dest.unlink()
        os.symlink(action.source, dest)
        report.updated += 1
        return
    os.symlink(action.source, dest)
    report.created += 1
