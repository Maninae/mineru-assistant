"""Tests for the hydration plan builder + apply (Phase 2 chunk 1).

Covers:
  - `build_plan` on a synthetic engine tree yields the right
    RENDER / SYMLINK / MKDIR actions with correct source/dest paths.
  - `build_plan` MUTATES NOTHING on disk (pure planning).
  - `apply_plan(dry_run=True)` mutates nothing.
  - `apply_plan(dry_run=False)` into a fresh tmp target creates the
    rendered file (correct content) + symlinks pointing where expected.
  - The non-empty-target guard raises `HydrationError` without force.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest

from mineru_cli.install import (
    DEFAULT_ENGINE_CODE_DIRS,
    OVERLAY_ENGINE_CODE_DIRS,
    DEFAULT_PRIVATE_DATA_FILES,
    DEFAULT_USER_DATA_DIRS,
    RECURSIVE_TEMPLATE_DIRS,
    HydrationAction,
    HydrationActionKind,
    HydrationError,
    HydrationPlan,
    apply_plan,
    build_plan,
    build_render_context,
)


# --- Synthetic engine tree fixture ---------------------------------------


def _snapshot(root: Path) -> List[Path]:
    """Return the sorted set of paths under `root` for pre/post-mutation checks."""
    if not root.exists():
        return []
    return sorted(root.rglob("*"))


def _make_synthetic_engine(tmp_path: Path) -> Path:
    """Build a small engine tree: template + plain file + user-data dir.

    Matches the shape the task spec exercises:
      engine/IDENTITY.md.template  (RENDER)
      engine/plain.txt             (SYMLINK)
      engine/memory/               (user-data — SYMLINK to profile_root)
      engine/.git/                 (excluded)
      engine/.venv/                (excluded)
    """
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "IDENTITY.md.template").write_text(
        "# {{USER_NAME}} — {{PERSONA_NAME}}",
        encoding="utf-8",
    )
    (engine / "plain.txt").write_text("static engine content", encoding="utf-8")
    (engine / "memory").mkdir()
    (engine / ".git").mkdir()
    (engine / ".venv").mkdir()
    return engine


def _find(actions: List[HydrationAction], kind: HydrationActionKind,
          dest_name: str) -> HydrationAction:
    """Locate the action of `kind` whose dest basename matches."""
    for action in actions:
        if action.kind == kind and action.dest.name == dest_name:
            return action
    raise AssertionError(
        f"no {kind.value} action with dest basename {dest_name!r}; "
        f"actions were: {[(a.kind.value, a.dest.name) for a in actions]}"
    )


# --- build_plan: shape of the plan --------------------------------------


def test_build_plan_yields_mkdir_render_and_symlinks(tmp_path: Path) -> None:
    """The plan carries one MKDIR (target root), one RENDER, and symlinks."""
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    target = tmp_path / "target"

    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )

    # MKDIR (first action) points at target_root itself.
    assert plan.actions[0].kind == HydrationActionKind.MKDIR
    assert plan.actions[0].dest == target

    # RENDER: IDENTITY.md.template -> target/IDENTITY.md
    render_action = _find(plan.actions, HydrationActionKind.RENDER, "IDENTITY.md")
    assert render_action.source == engine / "IDENTITY.md.template"
    assert render_action.dest == target / "IDENTITY.md"

    # SYMLINK for plain.txt -> engine
    plain_action = _find(plan.actions, HydrationActionKind.SYMLINK, "plain.txt")
    assert plain_action.source == engine / "plain.txt"
    assert plain_action.dest == target / "plain.txt"

    # SYMLINK for memory -> profile_root
    memory_action = _find(plan.actions, HydrationActionKind.SYMLINK, "memory")
    assert memory_action.source == profile_root / "memory"
    assert memory_action.dest == target / "memory"


def test_build_plan_excludes_dotgit_and_dotvenv(tmp_path: Path) -> None:
    """`.git` and the exact `.venv` dir never make it into the plan.

    The exclusion is an EXACT match (not a prefix): a legit sibling that
    starts with `.venv` (`.venv-notes`, `.venvrc`) still lands in the
    plan. Tightened from the prior `.venv*` prefix glob so a user file
    with an unlucky name never gets silently dropped.
    """
    engine = _make_synthetic_engine(tmp_path)
    (engine / ".venv-notes").mkdir()  # legit sibling that must survive.
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )
    dest_names = {a.dest.name for a in plan.actions}
    assert ".git" not in dest_names
    assert ".venv" not in dest_names
    assert ".venv-notes" in dest_names


def test_build_plan_treats_user_data_as_engine_when_profile_root_none(
    tmp_path: Path,
) -> None:
    """Without `profile_root`, user-data dirs fall through to engine symlink."""
    engine = _make_synthetic_engine(tmp_path)
    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=None,
    )
    memory_action = _find(plan.actions, HydrationActionKind.SYMLINK, "memory")
    # Without profile_root, memory symlinks to engine/memory, not to a
    # private overlay.
    assert memory_action.source == engine / "memory"


def test_build_plan_briefs_glob_matches_prefix(tmp_path: Path) -> None:
    """`briefs_*` in DEFAULT_USER_DATA_DIRS matches any `briefs_...` name."""
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "briefs_morning").mkdir()
    (engine / "briefs_news").mkdir()
    profile_root = tmp_path / "private"
    profile_root.mkdir()

    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={},
        profile_root=profile_root,
    )
    morning = _find(plan.actions, HydrationActionKind.SYMLINK, "briefs_morning")
    news = _find(plan.actions, HydrationActionKind.SYMLINK, "briefs_news")
    assert morning.source == profile_root / "briefs_morning"
    assert news.source == profile_root / "briefs_news"


def test_build_plan_actions_are_sorted_by_dest_name(tmp_path: Path) -> None:
    """Deterministic order after the leading MKDIR (stable diffs)."""
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )
    non_mkdir_dest_names = [
        a.dest.name for a in plan.actions if a.kind != HydrationActionKind.MKDIR
    ]
    assert non_mkdir_dest_names == sorted(non_mkdir_dest_names)


def test_build_plan_missing_engine_root_raises(tmp_path: Path) -> None:
    with pytest.raises(HydrationError) as exc:
        build_plan(
            engine_root=tmp_path / "does-not-exist",
            target_root=tmp_path / "target",
            context={},
        )
    assert "does not exist" in str(exc.value)


# --- build_plan: purity (no filesystem writes) ---------------------------


def test_build_plan_mutates_nothing_on_disk(tmp_path: Path) -> None:
    """PURE planning: build_plan does not touch target_root or profile_root."""
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    target = tmp_path / "target"

    before_engine = _snapshot(engine)
    before_profile = _snapshot(profile_root)
    assert not target.exists()

    build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )

    assert _snapshot(engine) == before_engine
    assert _snapshot(profile_root) == before_profile
    assert not target.exists()


# --- apply_plan: dry-run --------------------------------------------------


def test_apply_plan_dry_run_default_mutates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`apply_plan(dry_run=True)` prints the plan and touches nothing."""
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )

    apply_plan(plan, dry_run=True)

    captured = capsys.readouterr()
    assert "HydrationPlan" in captured.out
    assert not target.exists()


# --- apply_plan: real writes ---------------------------------------------


def test_apply_plan_writes_rendered_file_and_symlinks(tmp_path: Path) -> None:
    """`apply_plan(dry_run=False)` renders the template and lays symlinks."""
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    (profile_root / "memory").mkdir()
    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )

    apply_plan(plan, dry_run=False)

    assert target.is_dir()

    rendered = target / "IDENTITY.md"
    assert rendered.exists()
    assert rendered.read_text(encoding="utf-8") == "# Sam — Mineru"

    plain_link = target / "plain.txt"
    assert plain_link.is_symlink()
    assert os.readlink(plain_link) == str(engine / "plain.txt")
    assert plain_link.read_text(encoding="utf-8") == "static engine content"

    memory_link = target / "memory"
    assert memory_link.is_symlink()
    assert os.readlink(memory_link) == str(profile_root / "memory")


def test_apply_plan_refuses_non_empty_target_without_force(
    tmp_path: Path,
) -> None:
    """Safety guard: existing non-empty target aborts unless force=True."""
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "sentinel.txt").write_text("keep me", encoding="utf-8")

    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )

    with pytest.raises(HydrationError) as exc:
        apply_plan(plan, dry_run=False)
    assert "non-empty" in str(exc.value)
    # Sentinel survived.
    assert (target / "sentinel.txt").read_text(encoding="utf-8") == "keep me"


def test_apply_plan_force_overrides_non_empty_target_guard(
    tmp_path: Path,
) -> None:
    """`force=True` bypasses the non-empty-target safety guard.

    The sentinel that would normally survive the guard is left in place
    UNLESS its name collides with an action's dest (in which case the
    action's own idempotent handling decides). The important property
    this test locks: apply succeeds instead of raising HydrationError.
    """
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "sentinel.txt").write_text("keep me", encoding="utf-8")

    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )
    # No raise: force=True lets us proceed past the non-empty-target guard.
    apply_plan(plan, dry_run=False, force=True)

    # The rendered file landed.
    assert (target / "IDENTITY.md").read_text(encoding="utf-8") == \
        "# Sam — Mineru"
    # The sentinel did not collide with any action's dest, so it survived.
    assert (target / "sentinel.txt").read_text(encoding="utf-8") == "keep me"


def test_apply_plan_empty_target_is_fine(tmp_path: Path) -> None:
    """An existing but EMPTY target is not a guard-trigger — apply proceeds."""
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    target = tmp_path / "target"
    target.mkdir()  # empty
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )
    apply_plan(plan, dry_run=False)
    assert (target / "IDENTITY.md").read_text(encoding="utf-8") == \
        "# Sam — Mineru"


# --- build_render_context (light coverage; renderer tests cover deeper) --


class _StubProfile:
    """Duck-typed Profile stand-in for context-assembly tests."""

    def __init__(self, **fields: object) -> None:
        defaults = {
            "assistant_name": "Mineru",
            "display_name": "Sam",
            "timezone": "America/Los_Angeles",
            "workspace_absolute": Path("/tmp/mineru"),
            "memory_root": Path("/tmp/mineru/memory"),
            "briefs_root": Path("/tmp/mineru"),
            "launchd_label_prefix": "com.mineru",
            "keychain_account": "mineru",
            "secrets_env_prefix": "MINERU_SECRET_",
            "extras": {},
        }
        defaults.update(fields)
        for key, value in defaults.items():
            setattr(self, key, value)


def test_build_render_context_covers_persona_and_user_identity() -> None:
    ctx = build_render_context(_StubProfile(), connectors=None, env={})
    assert ctx["PERSONA_NAME"] == "Mineru"
    assert ctx["PERSONA_NAME_LOWER"] == "mineru"
    assert ctx["USER_NAME"] == "Sam"
    assert ctx["USER_TIMEZONE"] == "America/Los_Angeles"


def test_build_render_context_paths_are_stringified() -> None:
    """Path fields render as strings so templates can concatenate."""
    ctx = build_render_context(_StubProfile(), connectors=None, env={})
    assert ctx["MINERU_HOME"] == "/tmp/mineru"
    assert ctx["MEMORY_ROOT"] == "/tmp/mineru/memory"
    assert isinstance(ctx["MINERU_HOME"], str)


def test_build_render_context_home_from_env() -> None:
    ctx = build_render_context(
        _StubProfile(), connectors=None, env={"HOME": "/Users/example"}
    )
    assert ctx["USER_HOME"] == "/Users/example"


def test_build_render_context_missing_home_omits_key() -> None:
    ctx = build_render_context(_StubProfile(), connectors=None, env={})
    assert "USER_HOME" not in ctx


def test_build_render_context_connectors_copy_through(tmp_path: Path) -> None:
    ctx = build_render_context(
        _StubProfile(),
        connectors={
            "USER_PRIMARY_EMAIL": "sam@example.com",
            "CHURCH_CALENDAR_ID": None,  # None entries are skipped
        },
        env={},
    )
    assert ctx["USER_PRIMARY_EMAIL"] == "sam@example.com"
    assert "CHURCH_CALENDAR_ID" not in ctx


def test_build_render_context_extras_map_persona_and_user_fields() -> None:
    profile = _StubProfile(
        extras={
            "persona_emoji": "🦊",
            "user_pronouns": "he/him",
            "household": [{"name": "Robin", "relation": "partner"}],
        }
    )
    ctx = build_render_context(profile, connectors=None, env={})
    assert ctx["PERSONA_EMOJI"] == "🦊"
    assert ctx["USER_PRONOUNS"] == "he/him"
    assert ctx["USER_HOUSEHOLD"] == [{"name": "Robin", "relation": "partner"}]


def test_build_render_context_derives_he_him_his_pronoun_forms() -> None:
    """Standard `he/him/his` yields subject/object/possessive + CAP + verb-agreement forms."""
    profile = _StubProfile(extras={"user_pronouns": "he/him/his"})
    ctx = build_render_context(profile, connectors=None, env={})
    assert ctx["USER_PRONOUN_SUBJECT"] == "he"
    assert ctx["USER_PRONOUN_SUBJECT_CAP"] == "He"
    assert ctx["USER_PRONOUN_OBJECT"] == "him"
    assert ctx["USER_PRONOUN_OBJECT_CAP"] == "Him"
    assert ctx["USER_POSSESSIVE"] == "his"
    assert ctx["USER_POSSESSIVE_CAP"] == "His"
    # Verb agreement: singular pronoun → "is" / "'s" / verb-ends-in-"s".
    assert ctx["USER_IS"] == "is"
    assert ctx["USER_S"] == "'s"
    assert ctx["USER_VERB_S"] == "s"


def test_build_render_context_derives_she_her_her_pronoun_forms() -> None:
    profile = _StubProfile(extras={"user_pronouns": "she/her/her"})
    ctx = build_render_context(profile, connectors=None, env={})
    assert ctx["USER_PRONOUN_SUBJECT"] == "she"
    assert ctx["USER_PRONOUN_OBJECT"] == "her"
    assert ctx["USER_POSSESSIVE"] == "her"
    assert ctx["USER_POSSESSIVE_CAP"] == "Her"
    assert ctx["USER_IS"] == "is"
    assert ctx["USER_S"] == "'s"
    assert ctx["USER_VERB_S"] == "s"


def test_build_render_context_derives_they_them_their_pronoun_forms() -> None:
    """Both `they/them/their` and `they/them/theirs` yield determiner `their`,
    plus plural verb agreement so templates render `they are` / `they want`."""
    for raw in ("they/them/their", "they/them/theirs"):
        profile = _StubProfile(extras={"user_pronouns": raw})
        ctx = build_render_context(profile, connectors=None, env={})
        assert ctx["USER_PRONOUN_SUBJECT"] == "they"
        assert ctx["USER_PRONOUN_OBJECT"] == "them"
        # Possessive-pronoun `theirs` normalizes to the determiner `their`.
        assert ctx["USER_POSSESSIVE"] == "their", raw
        assert ctx["USER_POSSESSIVE_CAP"] == "Their", raw
        # Plural verb agreement.
        assert ctx["USER_IS"] == "are", raw
        assert ctx["USER_S"] == "'re", raw
        assert ctx["USER_VERB_S"] == "", raw


def test_build_render_context_pronoun_short_form_derives_possessive() -> None:
    """Short 2-slot pronouns (no possessive slot) derive the determiner.

    Without this, `{{USER_POSSESSIVE}} behalf` would render as the
    ungrammatical "he behalf" for the natural short form `he/him`.
    """
    for raw, subject, obj, poss in (
        ("he/him", "he", "him", "his"),
        ("she/her", "she", "her", "her"),
        ("they/them", "they", "them", "their"),
    ):
        profile = _StubProfile(extras={"user_pronouns": raw})
        ctx = build_render_context(profile, connectors=None, env={})
        assert ctx["USER_PRONOUN_SUBJECT"] == subject, raw
        assert ctx["USER_PRONOUN_OBJECT"] == obj, raw
        # No third slot → possessive derived from subject, never the bare subject.
        assert ctx["USER_POSSESSIVE"] == poss, raw


def test_build_render_context_omits_pronoun_forms_when_absent() -> None:
    """No `user_pronouns` in extras → no derived pronoun keys either."""
    ctx = build_render_context(_StubProfile(extras={}), connectors=None, env={})
    for key in (
        "USER_PRONOUN_SUBJECT",
        "USER_PRONOUN_OBJECT",
        "USER_POSSESSIVE",
        "USER_PRONOUN_SUBJECT_CAP",
        "USER_PRONOUN_OBJECT_CAP",
        "USER_POSSESSIVE_CAP",
        "USER_IS",
        "USER_S",
        "USER_VERB_S",
    ):
        assert key not in ctx


# --- HydrationPlan.render (formatting sanity) ---------------------------


def test_hydration_plan_render_groups_by_kind(tmp_path: Path) -> None:
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )
    rendered = plan.render()
    assert "MKDIR" in rendered
    assert "RENDER" in rendered
    assert "SYMLINK" in rendered
    # Kinds appear in the fixed order MKDIR -> RENDER -> SYMLINK
    assert rendered.index("MKDIR") < rendered.index("RENDER") < rendered.index("SYMLINK")


def test_hydration_plan_render_symlinks_are_dest_arrow_source(
    tmp_path: Path,
) -> None:
    """SYMLINK lines follow `ls -l` convention: `dest -> source`.

    The dest is the newly-created symlink; the source is what it points
    at. Readers looking for "where does X land?" find the runtime path
    on the LEFT of the arrow. Flipped from the earlier `source -> dest`
    rendering.
    """
    engine = _make_synthetic_engine(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
        profile_root=profile_root,
    )
    rendered = plan.render()
    # `plain.txt` symlinks target/plain.txt -> engine/plain.txt.
    plain_line = f"  {target / 'plain.txt'}  ->  {engine / 'plain.txt'}"
    assert plain_line in rendered, rendered
    # `memory` symlinks target/memory -> profile_root/memory.
    memory_line = f"  {target / 'memory'}  ->  {profile_root / 'memory'}"
    assert memory_line in rendered, rendered
    # Old shape (source -> dest) must not appear.
    old_shape = f"  {engine / 'plain.txt'}  ->  {target / 'plain.txt'}"
    assert old_shape not in rendered, rendered


def test_hydration_plan_action_kind_is_str_enum() -> None:
    """`HydrationActionKind` follows the project `(str, Enum)` pattern."""
    assert issubclass(HydrationActionKind, str)
    assert HydrationActionKind.RENDER.value == "render"
    assert HydrationActionKind.SYMLINK.value == "symlink"
    assert HydrationActionKind.MKDIR.value == "mkdir"


def test_default_user_data_dirs_contains_expected_entries() -> None:
    """Spec §3 lists the canonical user-data top-level names."""
    for name in ("memory", "briefs_*", "reports", "creations", "inbox", "outbox"):
        assert name in DEFAULT_USER_DATA_DIRS


# --- Recursive-template dirs (charter / prompts / recurring) --------------


def _make_recursive_template_engine(tmp_path: Path) -> Path:
    """Engine tree with a two-level recursive-template dir + plain siblings."""
    engine = tmp_path / "engine"
    engine.mkdir()
    # A top-level recursive-template dir with a template, a plain file, and
    # a nested subdir that ALSO holds a template (the two-level case).
    recurring = engine / "recurring"
    recurring.mkdir()
    (recurring / "morning-brief.md.template").write_text(
        "Brief for {{USER_NAME}}", encoding="utf-8"
    )
    (recurring / "helper.txt").write_text("static", encoding="utf-8")
    nested = recurring / "weekly-checkin"
    nested.mkdir()
    (nested / "setlist.md.template").write_text(
        "Set for {{USER_NAME}}", encoding="utf-8"
    )
    # A second recursive dir (single level) to prove the constant covers
    # more than one name.
    charter = engine / "charter"
    charter.mkdir()
    (charter / "CLAUDE.md.template").write_text("{{PERSONA_NAME}}", encoding="utf-8")
    # A regular top-level file to prove non-recursive entries still work.
    (engine / "plain.txt").write_text("top", encoding="utf-8")
    return engine


def test_build_plan_walks_recursive_template_dirs(tmp_path: Path) -> None:
    """`prompts/`, `recurring/` render nested templates in place; `charter/`
    FLATTENS its contents to the workspace root.

    Each `.template` under a `RECURSIVE_TEMPLATE_DIRS` dir becomes its own
    RENDER action mirrored under `target/<dir>/...`; nested subdirs are
    walked (not symlinked whole); plain files symlink back to the engine.
    Charter is a special case (split-manifest §3): its templates render
    at TOP LEVEL under `target/` because Claude Code's `@` imports read
    the charter from `~/.mineru/`, not `~/.mineru/charter/`.
    """
    engine = _make_recursive_template_engine(tmp_path)
    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Zephyr"},
    )

    assert "recurring" in RECURSIVE_TEMPLATE_DIRS
    # Charter is deliberately NOT in RECURSIVE_TEMPLATE_DIRS — it flattens.
    assert "charter" not in RECURSIVE_TEMPLATE_DIRS

    # Nested template renders to the mirrored path, suffix stripped.
    setlist = _find(plan.actions, HydrationActionKind.RENDER, "setlist.md")
    assert setlist.source == (
        engine / "recurring" / "weekly-checkin" / "setlist.md.template"
    )
    assert setlist.dest == (
        target / "recurring" / "weekly-checkin" / "setlist.md"
    )

    # Top-level-of-dir template renders under target/recurring/.
    brief = _find(plan.actions, HydrationActionKind.RENDER, "morning-brief.md")
    assert brief.dest == target / "recurring" / "morning-brief.md"

    # Charter templates FLATTEN to the workspace root — NOT under charter/.
    claude = _find(plan.actions, HydrationActionKind.RENDER, "CLAUDE.md")
    assert claude.dest == target / "CLAUDE.md"

    # A plain file inside a recursive dir SYMLINKs to the engine copy.
    helper = _find(plan.actions, HydrationActionKind.SYMLINK, "helper.txt")
    assert helper.source == engine / "recurring" / "helper.txt"
    assert helper.dest == target / "recurring" / "helper.txt"

    # MKDIRs exist for the dir root and the nested subdir.
    mkdir_dests = {
        a.dest for a in plan.actions if a.kind == HydrationActionKind.MKDIR
    }
    assert target / "recurring" in mkdir_dests
    assert target / "recurring" / "weekly-checkin" in mkdir_dests
    # No `target/charter/` MKDIR — charter flattens to `target_root`.
    assert target / "charter" not in mkdir_dests

    # The top-level plain file still symlinks as a normal engine entry.
    plain = _find(plan.actions, HydrationActionKind.SYMLINK, "plain.txt")
    assert plain.source == engine / "plain.txt"


def test_apply_plan_materializes_recursive_template_tree(tmp_path: Path) -> None:
    """Applying the plan renders nested templates and lays the mirrored tree.

    Also confirms the charter FLATTEN behavior: `CLAUDE.md` lands at the
    workspace root, not under `<target>/charter/`.
    """
    engine = _make_recursive_template_engine(tmp_path)
    target = tmp_path / "target"
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam", "PERSONA_NAME": "Zephyr"},
    )
    apply_plan(plan, dry_run=False)

    assert (target / "recurring" / "morning-brief.md").read_text(
        encoding="utf-8"
    ) == "Brief for Sam"
    assert (
        target / "recurring" / "weekly-checkin" / "setlist.md"
    ).read_text(encoding="utf-8") == "Set for Sam"
    # Charter FLATTENS: rendered file lands at `<target>/CLAUDE.md`, not
    # `<target>/charter/CLAUDE.md`.
    assert (target / "CLAUDE.md").read_text(encoding="utf-8") == "Zephyr"
    assert not (target / "charter" / "CLAUDE.md").exists()
    # The plain nested file is a symlink that reads through to engine content.
    helper = target / "recurring" / "helper.txt"
    assert helper.is_symlink()
    assert helper.read_text(encoding="utf-8") == "static"


# --- Traversal guards (Fix 1, Finding 12) --------------------------------


def test_build_plan_rejects_traversal_via_label_prefix_context(
    tmp_path: Path,
) -> None:
    """Cat-B critical (2026-09-04 step-5 audit, Finding 12).

    A hydration RENDER context whose `LAUNCHD_PREFIX` value carries a
    traversal payload (e.g. `../../../evil`) MUST NOT let the resulting
    filename escape `target_root`. The loader-side reject on
    `launchd_label_prefix` catches the profile-driven variant, but a
    caller that constructs a context directly can still stuff a bad
    value; this in-plan assertion is the belt.

    Vector: `LABEL_PREFIX.webapp.plist.template` under `engine/launchd/`
    substitutes `LABEL_PREFIX` -> `LAUNCHD_PREFIX` context value. With a
    traversal payload the rendered dest would compute to
    `target_root/launchd/../../../evil.webapp.plist`, which resolves
    OUTSIDE `target_root`.
    """
    engine = tmp_path / "engine"
    engine.mkdir()
    launchd = engine / "launchd"
    launchd.mkdir()
    (launchd / "LABEL_PREFIX.webapp.plist.template").write_text(
        "<key>Label</key><string>{{LAUNCHD_PREFIX}}.webapp</string>",
        encoding="utf-8",
    )
    target = tmp_path / "target"

    with pytest.raises(HydrationError) as exc:
        build_plan(
            engine_root=engine,
            target_root=target,
            context={"LAUNCHD_PREFIX": "../../../evil"},
        )
    msg = str(exc.value)
    assert "escapes target root" in msg or "escape" in msg.lower()
    # No filesystem writes must have landed (planning is pure; the guard
    # aborts BEFORE any apply, and even during planning nothing on disk
    # changed).
    assert not target.exists()


def test_build_plan_rejects_traversal_in_engine_template_filename(
    tmp_path: Path,
) -> None:
    """A poisoned engine tree with `..` in a template filename must fail
    loud at plan-build time, before `apply_plan` gets a chance to write.

    We exercise the recursive-template walker (charter/prompts/recurring/
    launchd/app-deploy) because that walker composes dest paths as
    `target_dir / child.name` — and `child.name` on Path with a
    traversal-looking basename SHOULD be caught by the resolve() +
    is_relative_to() guard.
    """
    engine = tmp_path / "engine"
    engine.mkdir()
    prompts = engine / "prompts"
    prompts.mkdir()
    # A regular (non-template) file whose basename escapes the sandbox
    # via the recursive-template SYMLINK path.
    # `Path("target/prompts") / "../../evil.md"` resolves outside target.
    (prompts / "../../evil.md")  # illustrative; can't be an actual file
    # Actually create a legit file at `engine/prompts/legit.md.template`
    # and then simulate a poisoned entry by writing to a path whose
    # BASENAME is a traversal. macOS lets a file's basename be anything
    # that fits into one path component, so a literal `..` alone works
    # as the last component (walked as such by iterdir), but a name
    # containing a `/` is impossible. Compose the vector differently:
    # a legit basename that CONTAINS `..` and no `/` still resolves
    # inside the target after joining and normalization, so it does NOT
    # trigger the guard on its own. The load-bearing vector is the
    # filename-token substitution one; this test additionally locks that
    # a `..` basename on its own IS accepted (it does not escape after
    # normalization: `target/prompts/..` -> `target/`, still inside),
    # which is why we combine `..` with a NESTED subdir to actually
    # escape.
    nested = prompts / "sub"
    nested.mkdir()
    # A plain-file basename with a `..` sibling above it — impossible via
    # a single filename. Use a template-filename token that DOES compose
    # a traversal via LAUNCHD_PREFIX in the launchd/ tree instead.
    launchd = engine / "launchd"
    launchd.mkdir()
    (launchd / "LABEL_PREFIX.plist.template").write_text(
        "<Label>{{LAUNCHD_PREFIX}}</Label>", encoding="utf-8"
    )
    target = tmp_path / "target"

    # A LAUNCHD_PREFIX composed of nested `../` escapes via
    # `target/launchd/../../../etc/passwd.plist`.
    with pytest.raises(HydrationError) as exc:
        build_plan(
            engine_root=engine,
            target_root=target,
            context={"LAUNCHD_PREFIX": "../../../etc/passwd"},
        )
    assert "escape" in str(exc.value).lower()


def test_build_plan_rejects_preexisting_symlink_dest_pointing_outside_sandbox(
    tmp_path: Path,
) -> None:
    """A pre-existing symlink at a planned dest pointing OUTSIDE target
    is caught at plan-build time (before any write) because `dest.resolve()`
    follows the symlink to its external destination, which trips the
    is_relative_to() guard. Belt: this is the earliest possible catch.
    """
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "IDENTITY.md.template").write_text(
        "# {{USER_NAME}}", encoding="utf-8"
    )
    target = tmp_path / "target"
    target.mkdir()
    external = tmp_path / "external-victim.txt"
    external.write_text("do not clobber me", encoding="utf-8")
    os.symlink(external, target / "IDENTITY.md")

    with pytest.raises(HydrationError) as exc:
        build_plan(
            engine_root=engine,
            target_root=target,
            context={"USER_NAME": "Sam"},
        )
    assert "escape" in str(exc.value).lower()
    # External victim untouched — plan-build never wrote anything.
    assert external.read_text(encoding="utf-8") == "do not clobber me"


def test_apply_plan_render_refuses_symlink_planted_after_plan(
    tmp_path: Path,
) -> None:
    """Braces (`O_NOFOLLOW`) test: symlink planted BETWEEN plan and apply.

    Split the plan and apply steps so the plan-time dest.resolve() sees
    a clean dest (nothing at `target/IDENTITY.md`), passes the escape
    check, and hands a plan to `apply_plan`. THEN, before apply runs, a
    symlink is planted at that dest pointing outside the sandbox. If
    `_apply_render` did NOT use `O_NOFOLLOW`, `write_text` would follow
    the symlink and clobber the external file. With `O_NOFOLLOW` +
    force=True (bypassing the belt), the render open must fail with
    ELOOP and raise `HydrationError`.
    """
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "IDENTITY.md.template").write_text(
        "# {{USER_NAME}}", encoding="utf-8"
    )
    target = tmp_path / "target"
    target.mkdir()
    external = tmp_path / "external-victim.txt"
    external.write_text("do not clobber me", encoding="utf-8")

    # Plan first — dest is clean, so no escape check fires.
    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam"},
    )
    # NOW plant the symlink at the render dest.
    render_dest = target / "IDENTITY.md"
    os.symlink(external, render_dest)

    # `force=True` bypasses the per-action non-empty guard so we exercise
    # the `_apply_render` O_NOFOLLOW path specifically.
    with pytest.raises(HydrationError) as exc:
        apply_plan(plan, dry_run=False, force=True)
    msg = str(exc.value)
    assert "IDENTITY.md" in msg or str(render_dest) in msg
    # External victim survived — O_NOFOLLOW refused the symlink follow.
    assert external.read_text(encoding="utf-8") == "do not clobber me"


# ---------------------------------------------------------------------------
# Rehearsal must-fix #2 (2026-09-16): post-walk passes for live equivalence.
#
# `build_plan` walks the engine tree and then emits two additional groups of
# actions so the hydrated workspace is shaped like a live install:
#   (a) engine-code symlinks (`bin`/`browser`/`lib`/`app`/`scripts`/`tests`)
#       into the engine clone root (defaults to `engine_root.parent`);
#   (b) private-data symlinks — the user-data DIRS (memory, briefs_*, ...)
#       and the private DATA FILES (`landline.json`, ...) into the profile
#       overlay.
# Both groups route their dests through `_assert_dest_inside_target`, so a
# `..` payload on either pass raises `HydrationError`. These tests promote
# the reviewer's rehearsal probe into permanent coverage.
# ---------------------------------------------------------------------------


def _make_engine_clone_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Materialize the shipping shape: `<clone>/{engine,bin,browser,...}`.

    Returns `(engine_root, engine_clone_root)`. The clone root holds the
    engine-code dirs as siblings of `engine/`, mirroring the real repo layout.
    """
    engine_clone = tmp_path / "clone"
    engine_clone.mkdir()
    engine = engine_clone / "engine"
    engine.mkdir()
    (engine / "plain.txt").write_text("engine", encoding="utf-8")
    for name in list(DEFAULT_ENGINE_CODE_DIRS) + list(OVERLAY_ENGINE_CODE_DIRS):
        (engine_clone / name).mkdir()
        (engine_clone / name / f"{name}-marker.txt").write_text(
            f"marker for {name}", encoding="utf-8"
        )
    return engine, engine_clone


def test_build_plan_emits_engine_code_symlinks_from_clone_root(
    tmp_path: Path,
) -> None:
    """The engine-code pass emits SYMLINK actions for every entry in
    `DEFAULT_ENGINE_CODE_DIRS` that exists on disk, pointing at
    `<engine_clone_root>/<name>` and landing at `<target>/<name>`.

    Uses the default `engine_clone_root` (== `engine_root.parent`), which
    is the shape a real checkout has: `mineru-assistant/{engine,bin,...}`.
    """
    engine, engine_clone = _make_engine_clone_layout(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    target = tmp_path / "target"

    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={"USER_NAME": "Sam"},
        profile_root=profile_root,
    )

    for name in DEFAULT_ENGINE_CODE_DIRS:
        action = _find(plan.actions, HydrationActionKind.SYMLINK, name)
        assert action.source == engine_clone / name, (
            f"engine-code symlink for {name!r} must point at the clone root, "
            f"not {action.source}"
        )
        assert action.dest == target / name


def test_build_plan_engine_code_pass_skips_missing_sources(tmp_path: Path) -> None:
    """A partial checkout (missing `browser/`) still hydrates cleanly — the
    engine-code pass silently omits actions for sources that do not exist
    on disk. Forker layouts vary; fail-loud here is unwelcome."""
    engine, engine_clone = _make_engine_clone_layout(tmp_path)
    # Remove one of the engine-code dirs to simulate a partial checkout.
    from shutil import rmtree
    rmtree(engine_clone / "browser")

    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={},
    )
    dest_names = {a.dest.name for a in plan.actions}
    assert "browser" not in dest_names
    # The others still land.
    assert "bin" in dest_names
    assert "lib" in dest_names


def test_build_plan_engine_code_pass_honors_explicit_clone_root(
    tmp_path: Path,
) -> None:
    """Passing `engine_clone_root=` explicitly wins over the default
    (`engine_root.parent`), so a caller with a non-sibling checkout layout
    can point the pass wherever the code dirs really live."""
    engine, _unused_clone = _make_engine_clone_layout(tmp_path)
    # A totally separate dir with its own bin/ tree.
    override_clone = tmp_path / "other-clone"
    override_clone.mkdir()
    (override_clone / "bin").mkdir()
    (override_clone / "bin" / "override-marker").write_text("x", encoding="utf-8")

    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={},
        engine_clone_root=override_clone,
    )
    marker = _find(plan.actions, HydrationActionKind.SYMLINK, "override-marker")
    assert marker.source == override_clone / "bin" / "override-marker"
    assert marker.dest == tmp_path / "target" / "bin" / "override-marker"


def test_build_plan_private_data_pass_symlinks_user_data_dirs_from_profile(
    tmp_path: Path,
) -> None:
    """User-data dirs in `DEFAULT_USER_DATA_DIRS` (exact-match entries and
    glob prefixes) get unconditional symlinks into the profile overlay
    when the engine walk doesn't already surface them. Glob entries
    (`briefs_*`) enumerate matching subdirs under `profile_root`.
    """
    engine, _clone = _make_engine_clone_layout(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    # Pre-populate the profile with a memory dir and two briefs_* dirs.
    (profile_root / "memory").mkdir()
    (profile_root / "briefs_morning").mkdir()
    (profile_root / "briefs_news").mkdir()

    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={},
        profile_root=profile_root,
    )

    # Exact-match entries.
    for name in ("memory", "reports", "creations", "inbox", "outbox"):
        action = _find(plan.actions, HydrationActionKind.SYMLINK, name)
        assert action.source == profile_root / name

    # Glob entries enumerate matching subdirs under `profile_root`.
    for briefs_name in ("briefs_morning", "briefs_news"):
        action = _find(plan.actions, HydrationActionKind.SYMLINK, briefs_name)
        assert action.source == profile_root / briefs_name


def test_build_plan_private_data_pass_symlinks_landline_json_from_profile(
    tmp_path: Path,
) -> None:
    """`landline.json` (`DEFAULT_PRIVATE_DATA_FILES` entry) gets a top-level
    symlink at `<target>/landline.json` pointing at the profile overlay.
    The source is allowed to not exist yet — the symlink is laid down
    unconditionally so a fresh install can drop the file in later."""
    engine, _clone = _make_engine_clone_layout(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    # Deliberately do NOT create `landline.json` — the plan pass must still
    # emit a symlink action so a first-run install works.
    assert "landline.json" in DEFAULT_PRIVATE_DATA_FILES

    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={},
        profile_root=profile_root,
    )
    action = _find(plan.actions, HydrationActionKind.SYMLINK, "landline.json")
    assert action.source == profile_root / "landline.json"
    assert action.dest == tmp_path / "target" / "landline.json"


def test_build_plan_private_data_pass_noop_without_profile_root(
    tmp_path: Path,
) -> None:
    """Without `profile_root`, neither the user-data pass nor the private-
    data-file pass emits — the whole overlay chunk is skipped so the plan
    stays valid for engine-only smoke tests."""
    engine, _clone = _make_engine_clone_layout(tmp_path)

    plan = build_plan(
        engine_root=engine,
        target_root=tmp_path / "target",
        context={},
        profile_root=None,
    )
    dest_names = {a.dest.name for a in plan.actions}
    # No landline.json symlink got emitted.
    assert "landline.json" not in dest_names


def test_build_plan_overlay_dir_leftover_symlink_needs_force_and_spares_target(
    tmp_path: Path,
) -> None:
    """A leftover `<target>/bin` symlink (older wholesale install, or a
    planted pointer outside the sandbox) is a pointer, never data.

    Planning succeeds (the overlay children are checked against the dir's
    own location), apply refuses without force, and with force the pointer
    is replaced by a real dir while the external tree stays untouched.
    """
    engine, engine_clone = _make_engine_clone_layout(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    external = tmp_path / "external-victim"
    external.mkdir()
    (external / "keep.txt").write_text("keep", encoding="utf-8")
    os.symlink(external, target / "bin")

    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={},
        engine_clone_root=engine_clone,
    )
    with pytest.raises(HydrationError) as exc:
        apply_plan(plan, dry_run=False)
    assert str(target / "bin") in str(exc.value)

    apply_plan(plan, dry_run=False, force=True)
    assert (target / "bin").is_dir() and not (target / "bin").is_symlink()
    assert os.readlink(target / "bin" / "bin-marker.txt") == str(
        engine_clone / "bin" / "bin-marker.txt"
    )
    assert sorted(p.name for p in external.iterdir()) == ["keep.txt"]


def test_build_plan_rejects_dest_escape_via_symlinked_parent_dir(
    tmp_path: Path,
) -> None:
    """Containment still follows PARENT dirs: a recursive-template dir
    that is a symlink outside the sandbox makes every child dest escape."""
    engine = tmp_path / "engine"
    (engine / "prompts").mkdir(parents=True)
    (engine / "prompts" / "GMAIL.md.template").write_text("x", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    external = tmp_path / "external-victim"
    external.mkdir()
    os.symlink(external, target / "prompts")

    with pytest.raises(HydrationError) as exc:
        build_plan(engine_root=engine, target_root=target, context={})
    assert "escape" in str(exc.value).lower()


@pytest.mark.parametrize("leaf_name", ["memory", "landline.json"])
def test_leftover_leaf_symlink_outside_sandbox_is_replaced_not_followed(
    tmp_path: Path, leaf_name: str
) -> None:
    """A symlink at a planned SYMLINK dest is only a pointer: planning
    accepts it, apply swaps it for the planned link, and whatever it used
    to point at (outside the sandbox) is never touched."""
    engine, _clone = _make_engine_clone_layout(tmp_path)
    profile_root = tmp_path / "private"
    profile_root.mkdir()
    (profile_root / "memory").mkdir()

    target = tmp_path / "target"
    target.mkdir()
    external = tmp_path / "external-victim"
    external.write_text("do not clobber me", encoding="utf-8")
    os.symlink(external, target / leaf_name)

    plan = build_plan(
        engine_root=engine,
        target_root=target,
        context={},
        profile_root=profile_root,
    )
    apply_plan(plan, dry_run=False)
    assert os.readlink(target / leaf_name) == str(profile_root / leaf_name)
    assert external.read_text(encoding="utf-8") == "do not clobber me"
