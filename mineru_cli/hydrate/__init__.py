"""Back-compat shim for the old `mineru_cli.hydrate` import path.

The engine package moved to `mineru_cli.install` in Sep 2026 to
mirror the renamed user-facing verb (`mineru profile install`, née
`profile hydrate`). External callers (workspace scripts, cron jobs,
third-party tools) that still import `from mineru_cli.hydrate import
...` continue to work through this shim.

Everything the old package exported is re-exported from
`mineru_cli.install`; there is no divergence. New code SHOULD import
from `mineru_cli.install` directly. This shim will be removed once
the deprecation window on the verb-level alias closes (~90 days
after the Sep 2026 rename).

The `apply`, `plan`, `context`, and `renderer` submodules are also
re-exported so `from mineru_cli.hydrate.plan import X`,
`from mineru_cli.hydrate.renderer import Y`, etc. keep resolving.
"""

from mineru_cli.install import (  # noqa: F401 — re-export surface
    CHARTER_FLATTEN_DIR_NAME,
    DEFAULT_ENGINE_CODE_DIRS,
    DEFAULT_PRIVATE_DATA_FILES,
    DEFAULT_USER_DATA_DIRS,
    RECURSIVE_INSTALL_TEMPLATE_DIRS,
    RECURSIVE_TEMPLATE_DIRS,
    HydrationAction,
    HydrationActionKind,
    HydrationError,
    HydrationPlan,
    apply_plan,
    build_plan,
    build_render_context,
    find_unrendered_markers,
    render_template,
)

# Re-export the submodules under the legacy dotted paths so
# `from mineru_cli.hydrate.plan import HydrationPlan` (and the
# renderer/context/apply variants) keep resolving. Attribute access
# (`mineru_cli.hydrate.plan`) is covered by the bindings below;
# `import mineru_cli.hydrate.plan` and `from mineru_cli.hydrate.plan
# import X` need the aliases registered in `sys.modules` too, because
# Python's import machinery looks up the dotted path there.
import sys as _sys

from mineru_cli.install import apply as apply  # noqa: F401
from mineru_cli.install import context as context  # noqa: F401
from mineru_cli.install import plan as plan  # noqa: F401
from mineru_cli.install import renderer as renderer  # noqa: F401

for _sub in ("apply", "context", "plan", "renderer"):
    _sys.modules[f"mineru_cli.hydrate.{_sub}"] = _sys.modules[
        f"mineru_cli.install.{_sub}"
    ]
del _sys, _sub

__all__ = [
    "CHARTER_FLATTEN_DIR_NAME",
    "DEFAULT_ENGINE_CODE_DIRS",
    "DEFAULT_PRIVATE_DATA_FILES",
    "DEFAULT_USER_DATA_DIRS",
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
    "apply",
    "context",
    "plan",
    "renderer",
]
