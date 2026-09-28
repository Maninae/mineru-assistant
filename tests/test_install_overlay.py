"""Planning tests for the install overlays and private-data precedence.

Covers:
  - `bin/` + `scripts/` (clone root) and `config/` (engine tree) plan as
    real dirs of per-entry links, engine side + profile side.
  - A name on both sides is a planning error naming the path.
  - Overlay dests keep the traversal guard; self-symlinks are refused.
  - `MEMORY.md` / `USER.md` / `prompts/TODO.md` / `*.local.md` link to
    the profile when present there and skip the template render.
  - `<profile_root>/overrides/<relpath>.template` renders instead of the
    engine template; orphan overrides raise.
  - `*journal_exports` matches `owen_journal_exports` and falls back to
    the canonical `journal_exports` name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

import pytest

from mineru_cli.install import (
    CONDITIONAL_PRIVATE_DATA_FILES,
    DEFAULT_ENGINE_CODE_DIRS,
    DEFAULT_USER_DATA_DIRS,
    OVERLAY_ENGINE_CODE_DIRS,
    HydrationAction,
    HydrationActionKind,
    HydrationError,
    apply_plan,
    build_plan,
)


def make_install_layout(tmp_path: Path) -> Tuple[Path, Path, Path, Path]:
    """Build `clone/{engine,bin,scripts,lib,...}`, a profile, and a target path.

    Returns `(engine_root, clone_root, profile_root, target_root)`.
    """
    clone = tmp_path / "clone"
    engine = clone / "engine"
    (engine / "charter").mkdir(parents=True)
    (engine / "prompts").mkdir()
    (engine / "config").mkdir()
    (engine / "charter" / "MEMORY.md.template").write_text("stub memory for {{USER_NAME}}", encoding="utf-8")
    (engine / "charter" / "USER.md.template").write_text("stub user", encoding="utf-8")
    (engine / "charter" / "IDENTITY.md.template").write_text("engine identity {{USER_NAME}}", encoding="utf-8")
    (engine / "prompts" / "TODO.md.template").write_text("stub todo", encoding="utf-8")
    (engine / "config" / "launchd-jobs.example.json").write_text("{}", encoding="utf-8")
    for name in list(DEFAULT_ENGINE_CODE_DIRS) + list(OVERLAY_ENGINE_CODE_DIRS):
        (clone / name).mkdir()
    (clone / "bin" / "msearch").write_text("#!/bin/sh\n", encoding="utf-8")
    (clone / "scripts" / "deliver-output.py").write_text("", encoding="utf-8")
    (clone / "scripts" / "firewall").mkdir()
    (clone / "scripts" / "firewall" / "screener.py").write_text("", encoding="utf-8")
    (clone / "scripts" / "__pycache__").mkdir()
    profile = tmp_path / "workspace" / "profiles" / "sam"
    profile.mkdir(parents=True)
    target = tmp_path / "workspace"
    return engine, clone, profile, target


def plan_for(engine: Path, profile: Path, target: Path) -> List[HydrationAction]:
    """Build a plan with a minimal context and return its actions."""
    return build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam"},
        profile_root=profile,
    ).actions


def actions_at(actions: List[HydrationAction], dest: Path) -> List[HydrationAction]:
    """Every action whose dest is exactly `dest`."""
    return [a for a in actions if a.dest == dest]


# --- per-entry overlay -------------------------------------------------------


def test_overlay_dirs_are_real_dirs_with_engine_and_profile_entries(tmp_path: Path) -> None:
    """`bin/`, `scripts/`, `config/` MKDIR + one link per top-level entry."""
    engine, clone, profile, target = make_install_layout(tmp_path)
    (profile / "scripts").mkdir()
    (profile / "scripts" / "house_scan.py").write_text("", encoding="utf-8")
    (profile / "bin").mkdir()
    os.symlink("/opt/elsewhere/monarch", profile / "bin" / "monarch")
    (profile / "config").mkdir()
    (profile / "config" / "launchd-jobs.json").write_text("{}", encoding="utf-8")

    actions = plan_for(engine, profile, target)
    for name in ("bin", "scripts", "config"):
        kinds = [a.kind for a in actions_at(actions, target / name)]
        assert kinds == [HydrationActionKind.MKDIR], (name, kinds)

    expected_links = {
        target / "bin" / "msearch": clone / "bin" / "msearch",
        target / "bin" / "monarch": profile / "bin" / "monarch",
        target / "scripts" / "deliver-output.py": clone / "scripts" / "deliver-output.py",
        target / "scripts" / "firewall": clone / "scripts" / "firewall",
        target / "scripts" / "house_scan.py": profile / "scripts" / "house_scan.py",
        target / "config" / "launchd-jobs.example.json": engine / "config" / "launchd-jobs.example.json",
        target / "config" / "launchd-jobs.json": profile / "config" / "launchd-jobs.json",
    }
    for dest, source in expected_links.items():
        (action,) = actions_at(actions, dest)
        assert action.kind == HydrationActionKind.SYMLINK
        assert action.source == source
    # Nested entries link as one unit; caches never link.
    assert not actions_at(actions, target / "scripts" / "firewall" / "screener.py")
    assert not actions_at(actions, target / "scripts" / "__pycache__")
    # Wholesale code dirs are unchanged.
    for name in DEFAULT_ENGINE_CODE_DIRS:
        (action,) = actions_at(actions, target / name)
        assert action.kind == HydrationActionKind.SYMLINK and action.source == clone / name


def test_overlay_collision_between_engine_and_profile_raises(tmp_path: Path) -> None:
    """A private file named like an engine file is a hard planning error."""
    engine, _clone, profile, target = make_install_layout(tmp_path)
    (profile / "scripts").mkdir()
    (profile / "scripts" / "deliver-output.py").write_text("private fork", encoding="utf-8")

    with pytest.raises(HydrationError) as exc:
        plan_for(engine, profile, target)
    message = str(exc.value)
    assert "collision" in message
    assert str(target / "scripts" / "deliver-output.py") in message


def test_overlay_dests_keep_the_traversal_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlay dir name that escapes the target (`../escape`) is refused
    at planning time, before anything is written."""
    engine, clone, profile, target = make_install_layout(tmp_path)
    # `<clone>/../escape` is a sibling of the clone, outside the target.
    (tmp_path / "escape").mkdir()
    (tmp_path / "escape" / "payload").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "mineru_cli.install.plan.OVERLAY_ENGINE_CODE_DIRS", ("../escape",)
    )

    with pytest.raises(HydrationError) as exc:
        plan_for(engine, profile, target)
    assert "escapes target root" in str(exc.value)


def test_overlay_replaces_leftover_wholesale_symlink_only_with_force(tmp_path: Path) -> None:
    """Upgrading an install whose `scripts` was a wholesale symlink: the
    pointer is a forceable conflict, then becomes a real overlay dir."""
    engine, clone, profile, target = make_install_layout(tmp_path)
    target.mkdir(exist_ok=True)
    os.symlink(clone / "scripts", target / "scripts")
    plan = build_plan(engine_root=engine, target_root=target, context={"USER_NAME": "Sam"}, profile_root=profile)

    with pytest.raises(HydrationError) as exc:
        apply_plan(plan, dry_run=False)
    assert str(target / "scripts") in str(exc.value)

    apply_plan(plan, dry_run=False, force=True)
    assert not (target / "scripts").is_symlink()
    assert os.readlink(target / "scripts" / "firewall") == str(clone / "scripts" / "firewall")
    assert (clone / "scripts" / "deliver-output.py").exists()


def test_self_symlink_when_engine_is_a_real_dir_inside_target(tmp_path: Path) -> None:
    """`<target>/engine` as a real engine tree makes the clone root the
    target itself, so `<target>/lib -> <target>/lib`: refused up front."""
    target = tmp_path / "workspace"
    engine = target / "engine"
    (engine / "charter").mkdir(parents=True)
    (target / "lib").mkdir()

    with pytest.raises(HydrationError) as exc:
        build_plan(engine_root=engine, target_root=target, context={})
    assert "point at itself" in str(exc.value)
    assert str(target / "lib") in str(exc.value)


def test_self_symlink_when_target_is_the_profile_root(tmp_path: Path) -> None:
    """Installing into the profile dir would link `memory` to itself."""
    engine, _clone, profile, _target = make_install_layout(tmp_path)
    (profile / "memory").mkdir()

    with pytest.raises(HydrationError) as exc:
        build_plan(engine_root=engine, target_root=profile, context={"USER_NAME": "Sam"}, profile_root=profile)
    assert "point at itself" in str(exc.value)


# --- private data files beat templates ---------------------------------------


def test_conditional_private_files_default_list() -> None:
    """The nine "data, not template" paths are all registered."""
    assert CONDITIONAL_PRIVATE_DATA_FILES == [
        "MEMORY.md",
        "USER.md",
        "prompts/TODO.md",
        "CLAUDE.local.md",
        "AGENTS.local.md",
        "IDENTITY.local.md",
        "TOOLS.local.md",
        "SECURITY.local.md",
        "prompts/TECHNICAL.local.md",
    ]


def test_private_data_files_present_replace_template_renders(tmp_path: Path) -> None:
    """Present in the profile: one SYMLINK at the dest, no RENDER."""
    engine, _clone, profile, target = make_install_layout(tmp_path)
    (profile / "prompts").mkdir()
    for relpath in CONDITIONAL_PRIVATE_DATA_FILES:
        (profile / relpath).write_text(f"private {relpath}", encoding="utf-8")

    actions = plan_for(engine, profile, target)
    for relpath in CONDITIONAL_PRIVATE_DATA_FILES:
        (action,) = actions_at(actions, target / relpath)
        assert action.kind == HydrationActionKind.SYMLINK, relpath
        assert action.source == profile / relpath

    apply_plan(build_plan(engine_root=engine, target_root=target, context={"USER_NAME": "Sam"}, profile_root=profile), dry_run=False)
    assert (target / "MEMORY.md").read_text(encoding="utf-8") == "private MEMORY.md"
    assert (target / "prompts" / "TODO.md").read_text(encoding="utf-8") == "private prompts/TODO.md"


def test_private_data_files_absent_render_stubs_and_skip_local_md(tmp_path: Path) -> None:
    """Fresh operator: templates render as today; `*.local.md` plan nothing."""
    engine, _clone, profile, target = make_install_layout(tmp_path)

    actions = plan_for(engine, profile, target)
    for relpath in ("MEMORY.md", "USER.md", "prompts/TODO.md"):
        (action,) = actions_at(actions, target / relpath)
        assert action.kind == HydrationActionKind.RENDER, relpath
    for relpath in ("TOOLS.local.md", "SECURITY.local.md", "prompts/TECHNICAL.local.md"):
        assert not actions_at(actions, target / relpath), relpath


# --- per-profile template overrides ------------------------------------------


def test_profile_override_renders_instead_of_engine_template(tmp_path: Path) -> None:
    """`overrides/charter/IDENTITY.md.template` replaces the engine source."""
    engine, _clone, profile, target = make_install_layout(tmp_path)
    override = profile / "overrides" / "charter" / "IDENTITY.md.template"
    override.parent.mkdir(parents=True)
    override.write_text("personal identity for {{USER_NAME}}", encoding="utf-8")

    plan = build_plan(engine_root=engine, target_root=target, context={"USER_NAME": "Sam"}, profile_root=profile)
    (action,) = actions_at(plan.actions, target / "IDENTITY.md")
    assert action.kind == HydrationActionKind.RENDER
    assert action.source == override
    (untouched,) = actions_at(plan.actions, target / "USER.md")
    assert untouched.source == engine / "charter" / "USER.md.template"

    apply_plan(plan, dry_run=False)
    assert (target / "IDENTITY.md").read_text(encoding="utf-8") == "personal identity for Sam"


def test_orphan_profile_override_raises(tmp_path: Path) -> None:
    """An override mirroring no engine template fails loud, naming it."""
    engine, _clone, profile, target = make_install_layout(tmp_path)
    orphan = profile / "overrides" / "recurring" / "gone.md.template"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("x", encoding="utf-8")

    with pytest.raises(HydrationError) as exc:
        plan_for(engine, profile, target)
    assert str(orphan) in str(exc.value)


# --- journal exports glob ----------------------------------------------------


def test_journal_exports_entry_is_a_leading_glob() -> None:
    assert "*journal_exports" in DEFAULT_USER_DATA_DIRS
    assert "journal_exports" not in DEFAULT_USER_DATA_DIRS


def test_journal_glob_matches_legacy_prefixed_dir(tmp_path: Path) -> None:
    """An older operator's `owen_journal_exports/` links under its own name."""
    engine, _clone, profile, target = make_install_layout(tmp_path)
    (profile / "owen_journal_exports").mkdir()

    actions = plan_for(engine, profile, target)
    (action,) = actions_at(actions, target / "owen_journal_exports")
    assert action.source == profile / "owen_journal_exports"
    assert not actions_at(actions, target / "journal_exports")


def test_journal_glob_falls_back_to_canonical_name(tmp_path: Path) -> None:
    """No match in the profile: `journal_exports` still links (fresh install)."""
    engine, _clone, profile, target = make_install_layout(tmp_path)

    actions = plan_for(engine, profile, target)
    (action,) = actions_at(actions, target / "journal_exports")
    assert action.source == profile / "journal_exports"


def test_operator_symlink_inside_target_is_not_a_stray(tmp_path: Path) -> None:
    """A root symlink pointing inside the target (a compat name, a private file
    linked from the profile) is the operator's own and never trips the guard."""
    from mineru_cli.install.apply_preflight import _is_operator_link_inside

    target = tmp_path / "ws"
    (target / "profiles" / "p").mkdir(parents=True)
    (target / "profiles" / "p" / "resume.pdf").write_text("x", encoding="utf-8")
    (target / "resume.pdf").symlink_to(target / "profiles" / "p" / "resume.pdf")
    (target / "outside").symlink_to(tmp_path)
    assert _is_operator_link_inside(target / "resume.pdf", target)
    assert not _is_operator_link_inside(target / "outside", target)
