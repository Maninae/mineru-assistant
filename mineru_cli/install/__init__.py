"""Install engine for the mineru CLI (Phase 2 genericization).

Renders `.template` files against the active profile's context and
lays down symlinks that connect the engine tree to the private
per-user overlay. See the 2026-08-28 genericize-templating and
split-manifest reports for the design.

Historical note: this package was named ``mineru_cli.hydrate`` before
Sep 2026. The user-facing verb (`mineru profile install`) and the
package name were realigned in step (b) of the naming-consolidation
pass. `mineru_cli.hydrate` remains as a back-compat shim that re-
exports every public symbol below; new callers should import from
`mineru_cli.install` directly.

Module split:

  * `renderer.py` — hand-rolled Mustache-SUBSET renderer.
  * `plan.py`    — pure planning: the engine walk + `build_plan`.
  * `plan_types.py`, `plan_constants.py`, `plan_guards.py`,
    `plan_post_walk.py` — plan shapes, placement constants, path
    guards, and the overlay / private-data passes.
  * `apply.py`   — filesystem mutation (`apply_plan`); its docstring
    holds the re-install rules. `apply_preflight.py` enforces them.
  * `context.py` — `{{VAR}}` context assembly from a `Profile`.

Public surface (re-exported here):
  - `render_template(text, context) -> str`
  - `find_unrendered_markers(text) -> List[str]` (drift detection)
  - `HydrationActionKind`, `HydrationAction`, `HydrationPlan`
  - `build_plan(...)` — pure plan; `apply_plan(...)` — mutates
  - `build_render_context(profile, connectors, env)`
  - `HydrationError` — raised on any apply-time failure
  - `ApplyReport` — created / updated / unchanged counts from `apply_plan`
"""

from mineru_cli.install.apply import ApplyReport, apply_plan
from mineru_cli.install.context import build_render_context
from mineru_cli.install.plan import (
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
    HydrationAction,
    HydrationActionKind,
    HydrationError,
    HydrationPlan,
    build_plan,
)
from mineru_cli.install.renderer import (
    find_unrendered_markers,
    render_template,
)

__all__ = [
    "ApplyReport",
    "CHARTER_FLATTEN_DIR_NAME",
    "CONDITIONAL_PRIVATE_DATA_FILES",
    "DEFAULT_ENGINE_CODE_DIRS",
    "DEFAULT_PRIVATE_DATA_FILES",
    "DEFAULT_USER_DATA_DIRS",
    "FRAMEWORK_RESERVED_NAMES",
    "OVERLAY_ENGINE_CODE_DIRS",
    "OVERLAY_ENGINE_TREE_DIRS",
    "PROFILE_TEMPLATE_OVERRIDES_DIR_NAME",
    "RECURSIVE_INSTALL_TEMPLATE_DIRS",
    "RECURSIVE_TEMPLATE_DIRS",
    "HydrationAction",
    "HydrationActionKind",
    "HydrationError",
    "HydrationPlan",
    "apply_plan",
    "build_plan",
    "build_render_context",
    "find_unrendered_markers",
    "render_template",
]
