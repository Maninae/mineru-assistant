"""Hydration planning constants: which engine/profile paths go where.

Leaf module shared by `plan.py` (the engine walk) and
`plan_post_walk.py` (the engine-code, overlay, and private-data passes).
"""

from __future__ import annotations

import fnmatch
from typing import Dict, List, Optional


# Top-level names we NEVER touch during a plan walk. Runtime state,
# transient caches, venvs, output artifacts, IDE metadata — none belong
# in a hydration plan (they would either explode the plan or clobber
# live state on apply).
_EXCLUDED_TOP_LEVEL_NAMES = frozenset({
    ".git",
    "cache",
    "logs",
    "__pycache__",
    ".pytest_cache",
    "output",
    ".venv",
})

# Default user-data dir names: exact basenames or `fnmatch` globs
# (`briefs_*`, `*journal_exports`). Two roles:
#   (1) Matching entries encountered during the engine-tree walk become
#       SYMLINKs into the private profile overlay so writes land in per-
#       user territory.
#   (2) After the walk, the plan also emits UNCONDITIONAL symlink actions
#       for these names — because the engine tree typically does NOT ship
#       them as directories (they are pure user-data), yet a live-equivalent
#       workspace needs each dir present. See split-manifest §3, §1i.
DEFAULT_USER_DATA_DIRS: List[str] = [
    "memory",
    "briefs_*",
    "reports",
    "creations",
    "inbox",
    "outbox",
    "archive",
    # Leading-`*` glob so an older operator's `owen_journal_exports/` links
    # too. With no match in the profile, the canonical `journal_exports`
    # is linked anyway (see `canonical_name_for_leading_glob`).
    "*journal_exports",
]

# Engine-code directories that live at the ENGINE CLONE ROOT (the parent
# of `engine/` in the shipping repo layout, e.g. `mineru-assistant/lib/`).
# Symlinked WHOLESALE into the installed workspace (`<target>/lib ->
# <clone>/lib`), per split-manifest §3 (the runtime overlay layout).
DEFAULT_ENGINE_CODE_DIRS: List[str] = [
    "browser",
    "lib",
    "app",
    "tests",
]

# Engine-code directories at the CLONE ROOT that are OVERLAID per entry
# instead of symlinked whole, so an operator's private tools sit next to
# the engine's. `<target>/<dir>` is a real directory holding one symlink
# per top-level entry of `<clone>/<dir>/` plus one per entry of
# `<profile_root>/<dir>/`. A name on both sides is a planning error: a
# private file must never shadow an engine file silently.
OVERLAY_ENGINE_CODE_DIRS = ("bin", "scripts")

# Directories INSIDE the engine tree (`engine/<dir>/`) that get the same
# per-entry overlay as `OVERLAY_ENGINE_CODE_DIRS`. `config/` ships example
# files (`launchd-jobs.example.json`); the operator's private
# `<profile_root>/config/launchd-jobs.json` links in beside them.
OVERLAY_ENGINE_TREE_DIRS = ("config",)

# Entry names never linked by an overlay pass (bytecode caches, Finder
# litter). Both sides routinely carry them, so linking them would raise
# spurious collisions.
OVERLAY_IGNORED_NAMES = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".DS_Store",
})

# Files that live at the PRIVATE PROFILE ROOT and want a top-level symlink
# in the hydrated workspace (as opposed to the directory-shaped user-data
# under `DEFAULT_USER_DATA_DIRS`). Landline reads its config from
# `<workspace>/landline.json`, so an install without the symlink breaks
# the daemon. See split-manifest §1i.
DEFAULT_PRIVATE_DATA_FILES: List[str] = [
    "landline.json",
]

# Private DATA files that may collide with an engine template: the rule
# is "data, not template". When `<profile_root>/<relpath>` exists, the
# plan symlinks `<target>/<relpath>` to it and SKIPS rendering the engine
# template that would land on the same path (`charter/MEMORY.md.template`,
# `charter/USER.md.template`, `prompts/TODO.md.template`). When the private
# file is absent (a fresh operator) the template renders as a starter
# stub. The `*.local.md` entries have no engine template; they are the
# private halves the charter `@`-imports, linked only when present.
CONDITIONAL_PRIVATE_DATA_FILES: List[str] = [
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

# Directory under `<profile_root>/` holding per-profile template overrides.
# `<profile_root>/overrides/<relpath>.template` renders INSTEAD of
# `engine/<relpath>.template` (same context, same dest). This is the
# sanctioned way to keep a personal variant of a generic recipe or charter
# file. An override with no engine counterpart is a planning error, so a
# typo or an upstream rename cannot silently drop the personal variant.
PROFILE_TEMPLATE_OVERRIDES_DIR_NAME = "overrides"

# Top-level names in the install target that belong to the framework or
# to runtime state rather than to the install. The non-empty-target guard
# in `apply.py` ignores them (plus anything the plan itself writes), so
# `mineru setup` can install into the root that `profile init` just
# populated. `fnmatch` patterns.
FRAMEWORK_RESERVED_NAMES = (
    "profiles",
    "active",
    "current",
    "engine",
    "people.yaml",
    "humans.yaml",
    "cache",
    "logs",
    "output",
    ".git",
    ".gitignore",
    ".gitattributes",
    "app-state",
    ".venv*",
    ".claude",
    ".DS_Store",
)

# File suffix that marks a template for rendering. Sibling name is the
# rendered output (`foo.md.template` -> `foo.md`).
TEMPLATE_SUFFIX = ".template"

# Top-level engine dir whose contents are FLATTENED to the workspace root
# at hydrate time. The charter files (SOUL.md, AGENTS.md, CLAUDE.md, ...)
# ship as templates under `engine/charter/` but MUST land at the workspace
# root — Claude Code's `@CLAUDE.md` import loader reads from `~/.mineru/`,
# not `~/.mineru/charter/`. Per split-manifest §3 the runtime overlay has
# every charter file at TOP LEVEL. Handled separately from
# `RECURSIVE_TEMPLATE_DIRS` (which mirrors under `<target>/<name>/`).
CHARTER_FLATTEN_DIR_NAME = "charter"

# Top-level engine directories whose CONTENTS are a mix of `.template`
# files (each rendered to its sibling name) and plain files (each
# symlinked back to the engine copy), laid out one or more levels deep.
# Unlike a plain engine dir (symlinked whole) or a user-data dir
# (symlinked into the private overlay), each of these is WALKED
# RECURSIVELY: every nested `.template` becomes its own RENDER action and
# the rendered subtree is materialized under `target_root/<name>/...`.
# This is what lets `prompts/` and `recurring/` render their per-file
# templates in place instead of being opaque symlinked dirs.
# See split-manifest §5.5 (Option A).
#
# `charter/` is deliberately NOT in this set — its contents FLATTEN to
# the workspace root instead of mirroring under `<target>/charter/`. See
# `CHARTER_FLATTEN_DIR_NAME` above and the build-plan special case.
RECURSIVE_TEMPLATE_DIRS = frozenset({"prompts", "recurring"})

# Top-level engine directories that behave the SAME as
# `RECURSIVE_TEMPLATE_DIRS` (walk recursively, render `.template` files,
# symlink plain siblings) BUT additionally support FILENAME-level token
# substitution — the launchd and app-deploy per-operator install artifacts.
# A file named `LABEL_PREFIX.webapp.plist.template` renders to
# `<launchd_prefix>.webapp.plist` (with `LABEL_PREFIX` substituted by the
# operator's `LAUNCHD_PREFIX` context value, e.g. `com.mineru`), so a
# downstream operator's install gets its own launchd label namespace
# baked into the plist filenames as
# well as the content. Filename-token substitution is applied to EVERY
# recursive-template dir (a no-op on filenames without the token), so
# this set is purely a documentation/discovery marker for the install
# dirs — the walk itself unions this set with `RECURSIVE_TEMPLATE_DIRS`.
RECURSIVE_INSTALL_TEMPLATE_DIRS = frozenset({"launchd", "app-deploy"})

# Every top-level dir that gets the recursive-template treatment. Union of
# the two sets above; kept as a module constant so `_classify_entry` and
# the walk share one authoritative view.
_ALL_RECURSIVE_TEMPLATE_DIRS = RECURSIVE_TEMPLATE_DIRS | RECURSIVE_INSTALL_TEMPLATE_DIRS

# Filename-level token substitutions applied while walking a recursive-
# template dir. Each mapping is `filename_token -> context_key`: the literal
# `filename_token` in a template filename is replaced by `str(context[key])`
# at plan-build time. `LABEL_PREFIX -> LAUNCHD_PREFIX` is the load-bearing
# entry: launchd plist template filenames carry `LABEL_PREFIX` literally
# (e.g. `LABEL_PREFIX.webapp.plist.template`) because a `{{LAUNCHD_PREFIX}}`
# tag in a filename would confuse the render layer (`{`/`}` on disk).
_FILENAME_TOKEN_MAP: Dict[str, str] = {
    "LABEL_PREFIX": "LAUNCHD_PREFIX",
}


def matches_user_data_pattern(name: str, patterns: List[str]) -> bool:
    """Return True iff `name` matches any user-data pattern.

    Patterns are exact names (`memory`) or `fnmatch` globs (`briefs_*`,
    `*journal_exports`); matching is case-sensitive on every platform.
    """
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def canonical_name_for_leading_glob(pattern: str) -> Optional[str]:
    """Return the fallback name for a leading-`*` glob, else None.

    `*journal_exports` names a canonical dir (`journal_exports`) while
    also accepting legacy prefixed spellings. When the profile holds no
    match, the planner links the canonical name so a fresh install still
    routes writes into the profile. Trailing globs (`briefs_*`) have no
    canonical name and return None.
    """
    if not pattern.startswith("*"):
        return None
    rest = pattern[1:]
    if not rest or any(ch in rest for ch in "*?["):
        return None
    return rest
