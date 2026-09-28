"""Hydration plan shapes: the error type, action kinds, actions, and the plan.

Leaf module (no imports from the rest of `mineru_cli.install`) so the
planner, the post-walk passes, the guards, and `apply.py` can all share
these types without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class HydrationError(RuntimeError):
    """Raised on any plan/apply failure the layer can name itself.

    Kept a `RuntimeError` (not a Typer error) so pure library callers
    can catch it cleanly; the verb layer wraps it into `typer.Exit`.
    """


# ---------------------------------------------------------------------------
# Action dataclasses + enum
# ---------------------------------------------------------------------------


class HydrationActionKind(str, Enum):
    """Kind of one hydration action.

    Stringly-typed per the project Python style guide — see
    `AccessTier` for the reference pattern. A plan can serialize to
    JSON without an enum encoder shim.

    Members:
        RENDER  — read a template file, render it, write the rendered
                  bytes at the destination.
        SYMLINK — create a symlink at the destination pointing at the
                  source (engine file, engine dir, or profile-root dir).
        MKDIR   — ensure the destination directory exists (idempotent).
                  `source` is `None`.
    """

    RENDER = "render"
    SYMLINK = "symlink"
    MKDIR = "mkdir"


@dataclass(frozen=True)
class HydrationAction:
    """One atomic hydration step.

    Attributes:
        kind: which of RENDER / SYMLINK / MKDIR this action is.
        source: source path (template to render, engine file to link,
            or private-overlay dir to link). `None` for MKDIR.
        dest: the destination path this action creates or writes.
        note: short human-readable description used in the plan render.
    """

    kind: HydrationActionKind
    source: Optional[Path]
    dest: Path
    note: str = ""


@dataclass
class HydrationPlan:
    """An ordered list of `HydrationAction`s + the render context.

    The context rides on the plan (not on each RENDER action) so a
    caller can inspect the plan without triggering a render pass, and
    so `apply_plan` has one authoritative context to render every
    template against.
    """

    actions: List[HydrationAction] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        """Return a human-readable summary of the plan, grouped by kind.

        Empty-kind sections are omitted so the output stays scannable
        on small plans.
        """
        lines: List[str] = [f"HydrationPlan ({len(self.actions)} actions)"]
        for kind in (
            HydrationActionKind.MKDIR,
            HydrationActionKind.RENDER,
            HydrationActionKind.SYMLINK,
        ):
            group = [a for a in self.actions if a.kind == kind]
            if not group:
                continue
            lines.append("")
            lines.append(f"{kind.value.upper()} ({len(group)}):")
            for action in group:
                if action.kind == HydrationActionKind.MKDIR:
                    lines.append(f"  {action.dest}")
                elif action.kind == HydrationActionKind.SYMLINK:
                    # Match `ls -l`: `dest -> source`. The dest is the
                    # newly-created symlink, the source is what it points
                    # at, so readers looking for "where does X land?" find
                    # the runtime path on the LEFT.
                    lines.append(f"  {action.dest}  ->  {action.source}")
                else:
                    lines.append(f"  {action.source}  ->  {action.dest}")
        return "\n".join(lines)
