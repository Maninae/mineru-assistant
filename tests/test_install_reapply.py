"""Re-install tests: `apply_plan` over an existing tree (rules in apply.py).

Covers idempotent re-apply, symlink retargeting, render-if-changed,
real-file / real-dir conflicts with and without force, and the target
guard that tolerates framework state and prior-install output.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Tuple

import pytest

from mineru_cli.install import (
    FRAMEWORK_RESERVED_NAMES,
    HydrationError,
    HydrationPlan,
    apply_plan,
    build_plan,
)


def make_layout(tmp_path: Path) -> Tuple[Path, Path, Path]:
    """Build `clone/{engine,bin,lib}`, `workspace/profiles/sam`; return paths.

    Returns `(engine_root, profile_root, target_root)`.
    """
    clone = tmp_path / "clone"
    engine = clone / "engine"
    (engine / "charter").mkdir(parents=True)
    (engine / "charter" / "IDENTITY.md.template").write_text("# {{USER_NAME}}", encoding="utf-8")
    (engine / "plain.txt").write_text("engine", encoding="utf-8")
    (clone / "bin").mkdir()
    (clone / "bin" / "msearch").write_text("", encoding="utf-8")
    (clone / "lib").mkdir()
    target = tmp_path / "workspace"
    profile = target / "profiles" / "sam"
    (profile / "memory").mkdir(parents=True)
    return engine, profile, target


def plan_with(engine: Path, profile: Path, target: Path, user_name: str = "Sam") -> HydrationPlan:
    """Plan an install with a one-key context."""
    return build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": user_name},
        profile_root=profile,
    )


def tree_state(root: Path) -> Dict[str, str]:
    """Map every path under `root` (profiles excluded) to its link target or content."""
    state: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if relative.startswith("profiles"):
            continue
        if path.is_symlink():
            state[relative] = "-> " + os.readlink(path)
        elif path.is_file():
            state[relative] = path.read_text(encoding="utf-8")
        else:
            state[relative] = "<dir>"
    return state


def test_second_apply_is_a_no_op(tmp_path: Path) -> None:
    """Same plan twice: nothing created or updated the second time."""
    engine, profile, target = make_layout(tmp_path)
    first = apply_plan(plan_with(engine, profile, target), dry_run=False)
    before = tree_state(target)

    plan = plan_with(engine, profile, target)
    second = apply_plan(plan, dry_run=False)

    assert first.created > 0
    assert (second.created, second.updated) == (0, 0)
    assert second.unchanged == len(plan.actions)
    assert tree_state(target) == before


def test_reapply_rerenders_only_changed_content(tmp_path: Path) -> None:
    """A context change rewrites the render; identical renders are skipped."""
    engine, profile, target = make_layout(tmp_path)
    apply_plan(plan_with(engine, profile, target), dry_run=False)

    report = apply_plan(plan_with(engine, profile, target, user_name="Alex"), dry_run=False)
    assert report.updated == 1
    assert report.created == 0
    assert (target / "IDENTITY.md").read_text(encoding="utf-8") == "# Alex"


def test_reapply_retargets_a_symlink_pointing_elsewhere(tmp_path: Path) -> None:
    """A link to a different source is replaced (the old target untouched)."""
    engine, profile, target = make_layout(tmp_path)
    apply_plan(plan_with(engine, profile, target), dry_run=False)
    stale = tmp_path / "stale-memory"
    stale.mkdir()
    (target / "memory").unlink()
    os.symlink(stale, target / "memory")

    report = apply_plan(plan_with(engine, profile, target), dry_run=False)
    assert report.updated == 1
    assert os.readlink(target / "memory") == str(profile / "memory")
    assert stale.is_dir()


def test_real_files_at_symlink_dests_are_all_listed_before_any_write(tmp_path: Path) -> None:
    """Every real file in a symlink's way is named up front; nothing is written."""
    engine, profile, target = make_layout(tmp_path)
    target.mkdir(exist_ok=True)
    (target / "plain.txt").write_text("hand edit", encoding="utf-8")
    (target / "landline.json").write_text("{}", encoding="utf-8")
    before = tree_state(target)

    with pytest.raises(HydrationError) as exc:
        apply_plan(plan_with(engine, profile, target), dry_run=False)
    message = str(exc.value)
    assert str(target / "plain.txt") in message
    assert str(target / "landline.json") in message
    assert tree_state(target) == before


def test_force_replaces_real_files_at_symlink_dests(tmp_path: Path) -> None:
    engine, profile, target = make_layout(tmp_path)
    target.mkdir(exist_ok=True)
    (target / "plain.txt").write_text("hand edit", encoding="utf-8")

    apply_plan(plan_with(engine, profile, target), dry_run=False, force=True)
    assert os.readlink(target / "plain.txt") == str(engine / "plain.txt")


@pytest.mark.parametrize("force", [False, True])
def test_real_dir_at_symlink_dest_is_refused_even_with_force(tmp_path: Path, force: bool) -> None:
    """Data-loss guard: a real `memory/` dir is never replaced by a link."""
    engine, profile, target = make_layout(tmp_path)
    (target / "memory").mkdir(parents=True)
    (target / "memory" / "note.md").write_text("precious", encoding="utf-8")

    with pytest.raises(HydrationError) as exc:
        apply_plan(plan_with(engine, profile, target), dry_run=False, force=force)
    assert str(target / "memory") in str(exc.value)
    assert (target / "memory" / "note.md").read_text(encoding="utf-8") == "precious"
    assert not (target / "IDENTITY.md").exists()


def test_symlink_at_render_dest_is_refused_even_with_force(tmp_path: Path) -> None:
    engine, profile, target = make_layout(tmp_path)
    target.mkdir(exist_ok=True)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("keep", encoding="utf-8")
    # Plan against a clean target, then plant the link: at plan time a
    # render dest symlinked outside the sandbox is already an escape error.
    plan = plan_with(engine, profile, target)
    os.symlink(elsewhere, target / "IDENTITY.md")

    with pytest.raises(HydrationError) as exc:
        apply_plan(plan, dry_run=False, force=True)
    assert str(target / "IDENTITY.md") in str(exc.value)
    assert elsewhere.read_text(encoding="utf-8") == "keep"


def test_reserved_names_and_prior_output_do_not_trip_the_target_guard(tmp_path: Path) -> None:
    """`profile init` output, runtime state, and a previous install are fine."""
    engine, profile, target = make_layout(tmp_path)
    for name in ("cache", "logs", "output", ".venv", ".claude", ".git", "app-state"):
        (target / name).mkdir()
    (target / "people.yaml").write_text("", encoding="utf-8")
    os.symlink("profiles/sam", target / "active")
    os.symlink(engine, target / "engine")
    apply_plan(plan_with(engine, profile, target), dry_run=False)

    report = apply_plan(plan_with(engine, profile, target), dry_run=False)
    assert report.created == 0
    assert "profiles" in FRAMEWORK_RESERVED_NAMES


def test_stray_entry_in_target_is_refused_without_force(tmp_path: Path) -> None:
    engine, profile, target = make_layout(tmp_path)
    (target / "somebody-elses-notes.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(HydrationError) as exc:
        apply_plan(plan_with(engine, profile, target), dry_run=False)
    assert "non-empty" in str(exc.value)
    assert "somebody-elses-notes.txt" in str(exc.value)
    assert not (target / "IDENTITY.md").exists()
