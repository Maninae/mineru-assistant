"""Hydration planning: the pure plan builder.

`build_plan` walks the engine tree and returns a `HydrationPlan`; zero
filesystem writes happen while planning. Every side effect is deferred to
`apply.py`. Context assembly lives in `context.py`.

Module map:
  * `plan_types.py`     : `HydrationError`, action kinds, actions, the plan.
  * `plan_constants.py` : which names go where (user data, overlays, ...).
  * `plan_guards.py`    : sandbox containment + self-symlink refusal.
  * `plan_post_walk.py` : engine-code, overlay, and private-data passes.
  * this module         : the engine walk, template overrides, `build_plan`.

Every public name from the split modules is re-exported here so
`from mineru_cli.install.plan import X` keeps working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from mineru_cli.install.plan_constants import (  # noqa: F401 (re-exports)
    CHARTER_FLATTEN_DIR_NAME,
    CONDITIONAL_PRIVATE_DATA_FILES,
    DEFAULT_ENGINE_CODE_DIRS,
    DEFAULT_PRIVATE_DATA_FILES,
    DEFAULT_USER_DATA_DIRS,
    FRAMEWORK_RESERVED_NAMES,
    OVERLAY_ENGINE_CODE_DIRS,
    OVERLAY_ENGINE_TREE_DIRS,
    PROFILE_TEMPLATE_OVERRIDES_DIR_NAME,
    RECURSIVE_INSTALL_TEMPLATE_DIRS,
    RECURSIVE_TEMPLATE_DIRS,
    TEMPLATE_SUFFIX,
    _ALL_RECURSIVE_TEMPLATE_DIRS,
    _EXCLUDED_TOP_LEVEL_NAMES,
    _FILENAME_TOKEN_MAP,
    matches_user_data_pattern,
)
from mineru_cli.install.plan_guards import (
    assert_dest_inside_target,
    assert_not_self_symlink,
)
from mineru_cli.install.plan_post_walk import (
    apply_conditional_private_data_files,
    plan_overlay_dir,
    plan_private_data_files,
    plan_user_data_dirs,
    plan_wholesale_code_dirs,
)
from mineru_cli.install.plan_types import (  # noqa: F401 (re-exports)
    HydrationAction,
    HydrationActionKind,
    HydrationError,
    HydrationPlan,
)


class TemplateOverrides:
    """Per-profile template overrides under `<profile_root>/overrides/`.

    Maps an engine template (`engine/recurring/x.md.template`) to the
    profile's replacement (`overrides/recurring/x.md.template`) by their
    shared engine-relative path, and records which overrides were used so
    `build_plan` can reject orphans (an override with no engine template).
    """

    def __init__(self, engine_root: Path, overrides_root: Optional[Path]) -> None:
        self.engine_root = engine_root
        self.overrides_root = (
            overrides_root if overrides_root is not None and overrides_root.is_dir() else None
        )
        self.used: Set[Path] = set()

    def source_for(self, engine_template: Path) -> Path:
        """Return the override for `engine_template` if one exists, else it."""
        if self.overrides_root is None:
            return engine_template
        candidate = self.overrides_root / engine_template.relative_to(self.engine_root)
        if candidate.is_file():
            self.used.add(candidate)
            return candidate
        return engine_template

    def assert_no_orphans(self) -> None:
        """Fail loud on any override file that matched no engine template."""
        if self.overrides_root is None:
            return
        orphans = sorted(
            p for p in self.overrides_root.rglob(f"*{TEMPLATE_SUFFIX}")
            if p.is_file() and p not in self.used
        )
        if orphans:
            raise HydrationError(
                "template override(s) with no engine counterpart: "
                + ", ".join(str(p) for p in orphans)
                + ". Each `overrides/<relpath>.template` must mirror an "
                "existing `engine/<relpath>.template`; fix the path or "
                "delete the stale override."
            )


def build_plan(
    engine_root: Path,
    target_root: Path,
    context: Dict[str, Any],
    *,
    profile_root: Optional[Path] = None,
    user_data_dirs: Optional[List[str]] = None,
    engine_clone_root: Optional[Path] = None,
    engine_code_dirs: Optional[List[str]] = None,
    private_data_files: Optional[List[str]] = None,
    conditional_private_data_files: Optional[List[str]] = None,
) -> HydrationPlan:
    """Walk `engine_root` and produce a `HydrationPlan`. PURE, no writes.

    Engine walk (per split-manifest §3):
      * Excluded top-level names (`.git`, `cache`, `logs`, `.venv`, ...)
        are skipped.
      * `charter/` FLATTENS: its templates render at `<target>/*.md`
        because Claude Code's `@` import loader reads from the root.
      * `prompts/`, `recurring/`, `launchd/`, `app-deploy/` are walked
        recursively; each nested `.template` renders in place.
      * `config/` (`OVERLAY_ENGINE_TREE_DIRS`) is left to the overlay pass.
      * A dir matching a user-data pattern links into `profile_root`.
      * A top-level `.template` renders; anything else links to the engine.
      * Template overrides: `<profile_root>/overrides/<relpath>.template`
        renders instead of `engine/<relpath>.template`; an override with
        no engine counterpart raises.

    Post-walk passes (`plan_post_walk.py`):
      * `browser`, `lib`, `app`, `tests` link wholesale to the clone root.
      * `bin/`, `scripts/` (clone root) and `config/` (engine tree) become
        real dirs of per-entry links, engine side plus profile side; a
        name on both sides raises.
      * User-data dirs link into the profile (globs enumerate it).
      * `landline.json` links into the profile unconditionally.
      * `MEMORY.md`, `USER.md`, `prompts/TODO.md`, and the `*.local.md`
        files link into the profile WHEN PRESENT there, replacing any
        template render at the same path.

    Every dest is checked against the sandbox (`plan_guards.py`) and no
    symlink may point at its own location.

    Args:
        engine_root: the public engine tree to walk.
        target_root: where apply writes.
        context: the render context (carried on the plan).
        profile_root: private overlay root; without it the user-data,
            private-file, overlay-profile-side, and override logic is off.
        user_data_dirs: override for `DEFAULT_USER_DATA_DIRS`.
        engine_clone_root: dir holding `bin/`, `scripts/`, `lib/`, ...;
            defaults to `engine_root.parent`.
        engine_code_dirs: override for `DEFAULT_ENGINE_CODE_DIRS` (the
            wholesale-linked dirs).
        private_data_files: override for `DEFAULT_PRIVATE_DATA_FILES`.
        conditional_private_data_files: override for
            `CONDITIONAL_PRIVATE_DATA_FILES`.

    Returns:
        A `HydrationPlan`: MKDIRs first in walk order, then every other
        action sorted by dest basename.
    """
    patterns = list(DEFAULT_USER_DATA_DIRS if user_data_dirs is None else user_data_dirs)

    if not engine_root.exists():
        raise HydrationError(f"engine_root does not exist: {engine_root}")
    if not engine_root.is_dir():
        raise HydrationError(f"engine_root is not a directory: {engine_root}")

    # Resolve once so every dest is checked against one canonical boundary.
    target_root_resolved = target_root.resolve()
    assert_dest_inside_target(target_root, target_root_resolved, source=None)
    actions: List[HydrationAction] = [
        HydrationAction(
            kind=HydrationActionKind.MKDIR,
            source=None,
            dest=target_root,
            note="ensure target root exists",
        )
    ]
    overrides = TemplateOverrides(
        engine_root,
        profile_root / PROFILE_TEMPLATE_OVERRIDES_DIR_NAME if profile_root is not None else None,
    )

    for entry in sorted(engine_root.iterdir(), key=lambda p: p.name):
        if entry.name in _EXCLUDED_TOP_LEVEL_NAMES:
            continue
        if entry.is_dir() and entry.name == CHARTER_FLATTEN_DIR_NAME:
            # target_root already has its MKDIR; do not emit a second one.
            actions.extend(
                _classify_recursive_template_dir(
                    dir_entry=entry,
                    target_dir=target_root,
                    context=context,
                    target_root_resolved=target_root_resolved,
                    overrides=overrides,
                    emit_dir_mkdir=False,
                )
            )
            continue
        if entry.is_dir() and entry.name in _ALL_RECURSIVE_TEMPLATE_DIRS:
            actions.extend(
                _classify_recursive_template_dir(
                    dir_entry=entry,
                    target_dir=target_root / entry.name,
                    context=context,
                    target_root_resolved=target_root_resolved,
                    overrides=overrides,
                )
            )
            continue
        if entry.is_dir() and entry.name in OVERLAY_ENGINE_TREE_DIRS:
            continue  # handled by the overlay pass below
        action = _classify_entry(
            entry=entry,
            target_root=target_root,
            profile_root=profile_root,
            patterns=patterns,
            overrides=overrides,
        )
        if action is None:
            continue
        _check_action_dest(action, target_root_resolved)
        actions.append(action)
    overrides.assert_no_orphans()

    # --- post-walk passes -------------------------------------------------
    already_dests = {a.dest for a in actions}

    def extend(new_actions: List[HydrationAction]) -> None:
        actions.extend(new_actions)
        already_dests.update(a.dest for a in new_actions)

    clone_root = engine_clone_root if engine_clone_root is not None else engine_root.parent
    extend(
        plan_wholesale_code_dirs(
            clone_root=clone_root,
            code_dirs=list(DEFAULT_ENGINE_CODE_DIRS if engine_code_dirs is None else engine_code_dirs),
            target_root=target_root,
            target_root_resolved=target_root_resolved,
            already_dests=already_dests,
        )
    )
    overlay_sources = [(name, clone_root / name) for name in OVERLAY_ENGINE_CODE_DIRS]
    overlay_sources += [(name, engine_root / name) for name in OVERLAY_ENGINE_TREE_DIRS]
    for dir_name, engine_dir in overlay_sources:
        extend(
            plan_overlay_dir(
                dir_name=dir_name,
                engine_dir=engine_dir,
                profile_dir=profile_root / dir_name if profile_root is not None else None,
                target_root=target_root,
                target_root_resolved=target_root_resolved,
                already_dests=already_dests,
            )
        )

    if profile_root is not None:
        extend(
            plan_user_data_dirs(
                patterns=patterns,
                profile_root=profile_root,
                target_root=target_root,
                target_root_resolved=target_root_resolved,
                already_dests=already_dests,
            )
        )
        extend(
            plan_private_data_files(
                filenames=list(
                    DEFAULT_PRIVATE_DATA_FILES if private_data_files is None else private_data_files
                ),
                profile_root=profile_root,
                target_root=target_root,
                target_root_resolved=target_root_resolved,
                already_dests=already_dests,
            )
        )
        actions = apply_conditional_private_data_files(
            actions,
            relpaths=list(
                CONDITIONAL_PRIVATE_DATA_FILES
                if conditional_private_data_files is None
                else conditional_private_data_files
            ),
            profile_root=profile_root,
            target_root=target_root,
            target_root_resolved=target_root_resolved,
        )

    # Stable order: MKDIRs keep walk order (top-down materialization); every
    # other action sorts by basename so `plan.render()` diffs stay readable.
    mkdir_actions = [a for a in actions if a.kind == HydrationActionKind.MKDIR]
    other_actions = sorted(
        (a for a in actions if a.kind != HydrationActionKind.MKDIR),
        key=lambda a: a.dest.name,
    )
    return HydrationPlan(actions=mkdir_actions + other_actions, context=dict(context))


def _check_action_dest(action: HydrationAction, target_root_resolved: Path) -> None:
    """Run the guards that fit `action.kind` (see `plan_guards.py`)."""
    follow_leaf = action.kind == HydrationActionKind.RENDER
    assert_dest_inside_target(
        action.dest,
        target_root_resolved,
        source=action.source,
        follow_leaf=follow_leaf,
    )
    if action.kind == HydrationActionKind.SYMLINK and action.source is not None:
        assert_not_self_symlink(action.dest, action.source)


def _classify_recursive_template_dir(
    *,
    dir_entry: Path,
    target_dir: Path,
    context: Dict[str, Any],
    target_root_resolved: Path,
    overrides: TemplateOverrides,
    emit_dir_mkdir: bool = True,
) -> List[HydrationAction]:
    """Walk a recursive-template dir, one action per nested entry.

    Returns, depth-first and name-sorted:
      * a MKDIR for `target_dir` (unless `emit_dir_mkdir=False`, used by
        the charter flatten whose target is the already-MKDIR'd root) and
        for every nested subdir;
      * a RENDER per `*.template`, dest = the suffix-stripped name with
        filename tokens substituted (`LABEL_PREFIX.webapp.plist.template`
        -> `com.mineru.webapp.plist`), source = the profile override when
        one exists;
      * a SYMLINK per plain file, dest -> the engine file.

    Excluded names are skipped at every depth. Every dest is guarded
    against traversal (a poisoned `launchd_label_prefix` or a `..` in an
    engine filename).
    """
    actions: List[HydrationAction] = []
    if emit_dir_mkdir:
        mkdir_action = HydrationAction(
            kind=HydrationActionKind.MKDIR,
            source=None,
            dest=target_dir,
            note="recursive-template dir",
        )
        assert_dest_inside_target(
            target_dir, target_root_resolved, source=dir_entry, follow_leaf=False
        )
        actions.append(mkdir_action)
    for child in sorted(dir_entry.iterdir(), key=lambda p: p.name):
        if child.name in _EXCLUDED_TOP_LEVEL_NAMES:
            continue
        if child.is_dir():
            actions.extend(
                _classify_recursive_template_dir(
                    dir_entry=child,
                    target_dir=target_dir / child.name,
                    context=context,
                    target_root_resolved=target_root_resolved,
                    overrides=overrides,
                )
            )
            continue
        if child.name.endswith(TEMPLATE_SUFFIX):
            rendered_name = child.name[: -len(TEMPLATE_SUFFIX)]
            if not rendered_name:
                raise HydrationError(
                    f"template file has empty rendered name: {child}"
                )
            rendered_name = _substitute_filename_tokens(
                rendered_name, context, source=child
            )
            source = overrides.source_for(child)
            action = HydrationAction(
                kind=HydrationActionKind.RENDER,
                source=source,
                dest=target_dir / rendered_name,
                note=(
                    "render profile override -> concrete file (recursive)"
                    if source != child
                    else "render template -> concrete file (recursive)"
                ),
            )
        else:
            action = HydrationAction(
                kind=HydrationActionKind.SYMLINK,
                source=child,
                dest=target_dir / child.name,
                note="engine file (recursive-template dir)",
            )
        _check_action_dest(action, target_root_resolved)
        actions.append(action)
    return actions


def _substitute_filename_tokens(
    name: str, context: Dict[str, Any], *, source: Path
) -> str:
    """Replace every `_FILENAME_TOKEN_MAP` token in `name` with its context value.

    The launchd/ dir ships filenames like `LABEL_PREFIX.webapp.plist`
    (with `LABEL_PREFIX` as a literal, NOT `{{LABEL_PREFIX}}` — `{`/`}` in
    a filename would be a mess). This walks `_FILENAME_TOKEN_MAP` and
    substitutes each `filename_token` with `str(context[context_key])`.

    A missing context key while `filename_token` is present in `name`
    fails LOUD with a `HydrationError` naming both the offending file
    and the missing key. Silent fallback would produce a nonsense filename
    (`LABEL_PREFIX.webapp.plist`) surviving into the apply pass.

    Non-matching filenames are returned unchanged (no-op), so recurring/,
    charter/, prompts/ pass through untouched.
    """
    out = name
    for filename_token, context_key in _FILENAME_TOKEN_MAP.items():
        if filename_token not in out:
            continue
        if context_key not in context:
            raise HydrationError(
                f"template filename {source.name!r} contains token "
                f"{filename_token!r} but render context is missing "
                f"the substitution key {context_key!r}; cannot compute "
                f"a real filename for {source}."
            )
        out = out.replace(filename_token, str(context[context_key]))
    return out




def _classify_entry(
    *,
    entry: Path,
    target_root: Path,
    profile_root: Optional[Path],
    patterns: List[str],
    overrides: TemplateOverrides,
) -> Optional[HydrationAction]:
    """Turn one top-level engine entry into a `HydrationAction` (or None).

    Returns `None` for excluded entries (engine-internal noise, skipped
    silently rather than raising).
    """
    name = entry.name
    if name in _EXCLUDED_TOP_LEVEL_NAMES:
        return None

    # User-data dirs first, so a dir named `memory` is never treated as
    # an engine dir.
    if (
        entry.is_dir()
        and profile_root is not None
        and matches_user_data_pattern(name, patterns)
    ):
        return HydrationAction(
            kind=HydrationActionKind.SYMLINK,
            source=profile_root / name,
            dest=target_root / name,
            note="user-data dir -> private overlay",
        )

    if entry.is_file() and name.endswith(TEMPLATE_SUFFIX):
        rendered_name = name[: -len(TEMPLATE_SUFFIX)]
        if not rendered_name:
            raise HydrationError(
                f"template file has empty rendered name: {entry}"
            )
        source = overrides.source_for(entry)
        return HydrationAction(
            kind=HydrationActionKind.RENDER,
            source=source,
            dest=target_root / rendered_name,
            note=(
                "render profile override -> concrete file"
                if source != entry
                else "render template -> concrete file"
            ),
        )

    return HydrationAction(
        kind=HydrationActionKind.SYMLINK,
        source=entry,
        dest=target_root / name,
        note="engine file/dir",
    )
