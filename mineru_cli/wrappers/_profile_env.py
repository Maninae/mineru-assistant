"""Per-profile subprocess-env plumbing shared by every `run_*` wrapper.

The step-5 audit's Cat-B critical (2026-09-04, Finding 7) called out that
`mineru --profile <name>` was captured on `ctx.obj` but never exported into
`os.environ`, so every wrapper's module-level `_MINERU_HOME = Path(os.environ
.get("MINERU_HOME", ...))` line captured the DEFAULT at import time. A second
profile's `mineru --profile alice memory search foo` would then shell out to
`$HOME/.mineru/bin/msearch` with `MINERU_KEYCHAIN_ACCOUNT=mineru` — reading
the OWNER's memory tree and Keychain.

The fix layers two lines of defense:

  1. `get_profile(ctx)` exports MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT /
     MINERU_INJECT_QUEUE_DIR into `os.environ` on first hydration
     (see `mineru_cli.profile.loader._export_profile_env`). Any subprocess
     spawned after that point inherits the right env even if the wrapper
     never touches `env=`. This covers indirect calls (a shell script
     spawning `security` under the hood, `deliver-output.py` importing
     another module that reads `MINERU_KEYCHAIN_ACCOUNT`).

  2. Every `run_*` wrapper that shells out ALSO builds an explicit `env=`
     dict when a `ctx` is available on the call, using `profile_env_for_ctx`
     below. That defends against a hostile / racy env at wrapper time and
     matches the pattern the slack + deliver-output wrappers already use
     for their per-subprocess keys (SLACK_KEYCHAIN_ACCOUNT, etc.).

The helper is deliberately narrow: it does NOT own the wrapper-specific
overlays (SLACK_KEYCHAIN_ACCOUNT for slack, --account=... for gog). Those
stay on the wrapper that knows which sub-tool it's calling. This module
just factors out the three profile-scoped keys every wrapper wants to
inherit.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


PROFILE_ENV_KEYS = (
    "MINERU_HOME",
    "MINERU_KEYCHAIN_ACCOUNT",
    "MINERU_INJECT_QUEUE_DIR",
)


def profile_env_overlay(profile: Any) -> Dict[str, str]:
    """Return the profile-scoped env keys derived from a `Profile`.

    Args:
        profile: an object with `workspace_absolute` and `keychain_account`
            attributes (duck-typed so wrapper tests can pass a stand-in
            that only carries a subset of fields).

    Returns:
        A dict with up to three keys — `MINERU_HOME`,
        `MINERU_KEYCHAIN_ACCOUNT`, `MINERU_INJECT_QUEUE_DIR` — sourced
        from the profile. Any field the duck-typed stub omits (a
        `_StubProfile` in a wrapper unit test that only carries
        `keychain_account`) is simply skipped so downstream callers
        stay flexible. Values are always strings.
    """
    out: Dict[str, str] = {}
    workspace = getattr(profile, "workspace_absolute", None)
    if workspace is not None:
        workspace_str = str(workspace)
        out["MINERU_HOME"] = workspace_str
        out["MINERU_INJECT_QUEUE_DIR"] = (
            f"{workspace_str.rstrip('/')}/cache/inject-queue"
        )
    keychain = getattr(profile, "keychain_account", None)
    if keychain:
        out["MINERU_KEYCHAIN_ACCOUNT"] = keychain
    return out


def profile_env_for_ctx(ctx: Optional[Any]) -> Optional[Dict[str, str]]:
    """Build the `env=` dict every subprocess-invoking wrapper hands down.

    Merges `os.environ` (so PATH, HOME, TZ, etc. survive) with the
    profile-scoped overlay (MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT /
    MINERU_INJECT_QUEUE_DIR). Returns `None` when no active profile is on
    the ctx — callers then omit `env=` and inherit the ambient env,
    matching the pre-fix behavior for wrapper unit tests that stub ctx.

    The overlay ALWAYS wins over the ambient env for these three keys, so
    a hostile / racy env at wrapper time cannot silently redirect the
    subprocess back at the owner's tree.
    """
    if ctx is None:
        return None
    obj = getattr(ctx, "obj", None) or {}
    profile = obj.get("profile_obj")
    if profile is None:
        return None
    overlay = profile_env_overlay(profile)
    return {**os.environ, **overlay}


def merge_profile_env(
    ctx: Optional[Any], extra: Optional[Dict[str, str]] = None
) -> Optional[Dict[str, str]]:
    """Like `profile_env_for_ctx` but stacks a wrapper-specific overlay on top.

    Used by wrappers that add THEIR OWN key (SLACK_KEYCHAIN_ACCOUNT for
    slack, MINERU_KEYCHAIN_ACCOUNT-alias slots for deliver-output). The
    wrapper-specific `extra` mapping wins over both the ambient env and
    the profile overlay for its keys, so a wrapper's dedicated key
    overrides a matching key on the profile overlay if any collides.

    Returns `None` under the same condition as `profile_env_for_ctx`
    (no active profile on ctx) AND `extra` is empty / None — otherwise
    returns the merged dict.
    """
    if ctx is None and not extra:
        return None
    base = profile_env_for_ctx(ctx)
    if base is None and extra:
        base = dict(os.environ)
    if base is None:
        return None
    if extra:
        base.update(extra)
    return base
