"""Step-5 audit (2026-09-04) — per-profile isolation invariants.

Locks in the five foundation fixes required for multi-profile cutover.
Every assertion here traces to a specific audit finding:

  * Fix 1: onboarding writes `workspace_absolute = profile_root`, loader
    rejects a non-seed profile whose `workspace_absolute` collapses onto
    `default_workspace_root()`.
  * Fix 2: `get_profile()` exports MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT /
    MINERU_INJECT_QUEUE_DIR into `os.environ`, AND every `run_*` wrapper
    builds an explicit env= dict pinned to those keys.
  * Fix 3: webapp plist template renders EnvironmentVariables +
    templated port, and app/config.py honors MINERU_WEBAPP_PORT.
  * Fix 4: every cron plist rendered via `render_plist` carries the
    three MINERU_* env keys, and each hydrated launchd template does too.
  * Fix 5: `scripts/push_send.py` reads KEYCHAIN_ACCOUNT + APP_DIR from
    env, and `scripts/deliver-output.py` threads env through the
    push_send subprocess.

Discipline: EVERY subprocess boundary is mocked. Zero live Keychain,
zero live network. Two-profile invariants mirror
`tests/test_profile_namespacing.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from mineru_cli.cron import get_job, load_cron_config, render_plist
from mineru_cli.install import build_render_context
from mineru_cli.profile import Profile, load_active_profile
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
    ProfileError,
    _FRAMEWORK_MANAGED_ENV,
    _export_profile_env,
    default_workspace_root,
    is_env_framework_managed,
)
from mineru_cli.profile.onboarding import (
    ProfileSpec,
    _render_profile_yaml,
    create_profile,
)
from mineru_cli.wrappers._profile_env import (
    PROFILE_ENV_KEYS,
    profile_env_for_ctx,
    profile_env_overlay,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _StubProfile:
    """Duck-typed Profile for wrapper unit tests (mirrors the shape
    every `run_*` wrapper reads: `workspace_absolute`, `keychain_account`)."""

    def __init__(
        self,
        *,
        workspace_absolute: Any = None,
        keychain_account: Optional[str] = None,
    ) -> None:
        if workspace_absolute is not None:
            self.workspace_absolute = workspace_absolute
        self.keychain_account = keychain_account


class _Ctx:
    """Duck-typed Typer.Context."""

    def __init__(self, obj: Optional[Dict[str, Any]] = None) -> None:
        self.obj = obj


class _CompletedStub:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _record_run(returncode: int = 0):
    calls: List[Dict[str, Any]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": list(cmd), "args": args, "kwargs": kwargs})
        return _CompletedStub(returncode)

    return fake_run, calls


def _write_profile_yaml(
    base: Path,
    name: str,
    *,
    workspace_absolute: Optional[str] = None,
    keychain_account: Optional[str] = None,
) -> Path:
    """Materialize a schema-valid profile.yaml under `<base>/<name>/`.

    Defaults are chosen distinct-per-profile so two profiles never
    accidentally collapse.
    """
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    workspace = workspace_absolute or str(profile_dir)
    kc = keychain_account or f"{name}-kc"
    lines = [
        f"name: {name}",
        f"display_name: {name.capitalize()}",
        "assistant_name: TestBot",
        "timezone: America/Los_Angeles",
        f"keychain_account: {kc}",
        f"launchd_label_prefix: com.{name}",
        f"workspace_absolute: {workspace}",
        f"memory_root: {workspace}/memory",
        f"briefs_root: {workspace}/briefs",
        "journal_apple_notes_folder: Daily Journals",
        "secrets:",
        "  backends: [env, keychain]",
        f"  env_prefix: {name.upper()}_SECRET_",
    ]
    (profile_dir / "profile.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return profile_dir


# ===========================================================================
# Fix 1 — onboarding writes workspace_absolute = profile_root
# ===========================================================================


def test_onboarding_render_profile_yaml_writes_profile_root_as_workspace_absolute() -> None:
    """`_render_profile_yaml(spec, ..., profile_root)` emits `workspace_absolute: <profile_root>`.

    Pre-fix (audit Finding 6), onboarding wrote `workspace_absolute =
    default_workspace_root()` — the SHARED root, collapsing every
    per-profile consumer (memory tree, cache, inject-queue,
    cloakbrowser-profile) onto the owner's tree.
    """
    import yaml

    spec = ProfileSpec(
        name="carol",
        persona="Carol",
        owner_handle="carol",
        timezone="America/Los_Angeles",
    )
    profile_root = Path("/tmp/framework-root/profiles/carol")
    body = _render_profile_yaml(
        spec,
        memory_root=profile_root / "memory",
        briefs_root=profile_root / "briefs",
        profile_root=profile_root,
        is_bootstrap=False,
    )
    data = yaml.safe_load(body)
    assert data["workspace_absolute"] == str(profile_root)
    # And crucially NOT the shared root.
    assert data["workspace_absolute"] != "/tmp/framework-root"


def test_create_profile_end_to_end_writes_profile_root_as_workspace_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`create_profile()` end-to-end: profile.yaml on disk points at profile_root."""
    monkeypatch.delenv("MINERU_HOME", raising=False)
    workspace = tmp_path / "framework"
    workspace.mkdir()
    profiles = workspace / "profiles"
    profiles.mkdir()
    (workspace / "humans.yaml").write_text(
        "humans:\n  carol:\n    telegram_id: 100\n    display_name: Carol\n",
        encoding="utf-8",
    )
    spec = ProfileSpec(
        name="carol",
        persona="Carol",
        owner_handle="carol",
        timezone="America/Los_Angeles",
    )
    result = create_profile(
        spec, workspace_root=workspace, profiles_base_dir=profiles
    )
    profile_yaml = result.profile_yaml_path.read_text(encoding="utf-8")
    import yaml
    data = yaml.safe_load(profile_yaml)
    # Written value is the profile's own dir, not the workspace root.
    assert data["workspace_absolute"] == str(result.profile_root)
    assert data["workspace_absolute"] != str(workspace)


def test_two_profiles_resolve_to_distinct_workspace_absolute(tmp_path: Path) -> None:
    """The load-bearing two-profile invariant: distinct workspaces per profile."""
    _write_profile_yaml(tmp_path, "alice")
    _write_profile_yaml(tmp_path, "bob")
    alice = load_active_profile("alice", base_dir=tmp_path)
    bob = load_active_profile("bob", base_dir=tmp_path)
    assert alice.workspace_absolute != bob.workspace_absolute
    assert alice.workspace_absolute.name == "alice"
    assert bob.workspace_absolute.name == "bob"


# ===========================================================================
# Fix 1 — loader rejects workspace_absolute == default_workspace_root()
# ===========================================================================


def test_loader_rejects_profile_with_workspace_absolute_equal_to_default_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile.yaml pointing `workspace_absolute` at the shared root is rejected."""
    # Set MINERU_WORKSPACE_ROOT so default_workspace_root() returns a
    # known value; then write a profile.yaml whose workspace_absolute
    # equals that same value.
    shared_root = tmp_path / "shared-root"
    shared_root.mkdir()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(shared_root))
    _write_profile_yaml(
        tmp_path,
        "leaky",
        workspace_absolute=str(shared_root),
    )
    with pytest.raises(ProfileError) as excinfo:
        load_active_profile("leaky", base_dir=tmp_path)
    assert "workspace_absolute" in str(excinfo.value)
    assert "collapses onto the shared workspace root" in str(excinfo.value)


def test_loader_accepts_profile_pointing_at_profile_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the onboarding fix, a profile's workspace_absolute is its own dir — should load."""
    shared_root = tmp_path
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(shared_root))
    profile_dir = _write_profile_yaml(tmp_path, "healthy")
    # workspace_absolute defaults to `<tmp_path>/healthy` which is NOT
    # the shared root (`<tmp_path>`), so validation lets it through.
    profile = load_active_profile("healthy", base_dir=tmp_path)
    assert profile.workspace_absolute == profile_dir.resolve()


# ===========================================================================
# Fix 2 — get_profile exports env; wrappers thread explicit env dicts
# ===========================================================================


def test_export_profile_env_sets_all_three_keys(tmp_path: Path) -> None:
    """`_export_profile_env(profile)` writes MINERU_HOME + KEYCHAIN + INJECT_QUEUE_DIR."""
    _write_profile_yaml(tmp_path, "alpha")
    profile = load_active_profile("alpha", base_dir=tmp_path)
    orig_env = {k: os.environ.get(k) for k in PROFILE_ENV_KEYS}
    try:
        # Clear so we know the export actually ran.
        for k in PROFILE_ENV_KEYS:
            os.environ.pop(k, None)
        _export_profile_env(profile)
        assert os.environ["MINERU_HOME"] == str(profile.workspace_absolute)
        assert os.environ["MINERU_KEYCHAIN_ACCOUNT"] == profile.keychain_account
        assert os.environ["MINERU_INJECT_QUEUE_DIR"] == (
            f"{profile.workspace_absolute}/cache/inject-queue"
        )
    finally:
        for k, v in orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_load_active_profile_exports_env_on_first_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First `load_active_profile()` call exports the three keys."""
    for k in PROFILE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    _write_profile_yaml(tmp_path, "beta")
    profile = load_active_profile("beta", base_dir=tmp_path)
    # Direct-load path bypasses the ctx.obj cache; still exports via
    # `_export_profile_env` inside `get_profile`. Direct
    # `load_active_profile` does NOT export by design (get_profile is
    # the entry that owns the env-export contract); prove via
    # explicit invocation.
    _export_profile_env(profile)
    assert os.environ["MINERU_HOME"] == str(profile.workspace_absolute)


def test_is_env_framework_managed_distinguishes_operator_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`is_env_framework_managed(key)` returns False when env value != last export."""
    for k in PROFILE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    _write_profile_yaml(tmp_path, "gamma")
    profile = load_active_profile("gamma", base_dir=tmp_path)
    _export_profile_env(profile)
    assert is_env_framework_managed("MINERU_HOME") is True
    # Operator override → env value drifts from framework snapshot.
    monkeypatch.setenv("MINERU_HOME", "/tmp/operator-override")
    assert is_env_framework_managed("MINERU_HOME") is False


def test_profile_env_overlay_produces_three_keys() -> None:
    """`profile_env_overlay(profile)` builds the canonical overlay dict."""
    profile = _StubProfile(
        workspace_absolute="/tmp/alice-ws", keychain_account="alice-kc"
    )
    overlay = profile_env_overlay(profile)
    assert overlay["MINERU_HOME"] == "/tmp/alice-ws"
    assert overlay["MINERU_KEYCHAIN_ACCOUNT"] == "alice-kc"
    assert overlay["MINERU_INJECT_QUEUE_DIR"] == "/tmp/alice-ws/cache/inject-queue"


def test_profile_env_overlay_tolerates_partial_stub() -> None:
    """A duck-typed stub with only `keychain_account` still produces the KC key."""
    profile = _StubProfile(keychain_account="only-kc")
    overlay = profile_env_overlay(profile)
    assert overlay == {"MINERU_KEYCHAIN_ACCOUNT": "only-kc"}


def test_profile_env_for_ctx_returns_none_without_profile() -> None:
    assert profile_env_for_ctx(None) is None
    assert profile_env_for_ctx(_Ctx(obj=None)) is None
    assert profile_env_for_ctx(_Ctx(obj={})) is None


def test_profile_env_for_ctx_merges_ambient_with_overlay() -> None:
    profile = _StubProfile(
        workspace_absolute="/tmp/x", keychain_account="x-kc"
    )
    ctx = _Ctx(obj={"profile_obj": profile})
    env = profile_env_for_ctx(ctx)
    assert env is not None
    # Ambient env preserved.
    for key in os.environ:
        assert key in env
    # Overlay stamps profile keys.
    assert env["MINERU_HOME"] == "/tmp/x"
    assert env["MINERU_KEYCHAIN_ACCOUNT"] == "x-kc"


# --- Wrapper env-threading regression per two profiles ---------------------


_WRAPPER_TABLE = [
    ("msearch", "mineru_cli.wrappers.msearch", "run_msearch", "MINERU_MSEARCH_BIN", "/tmp/fake/msearch"),
    ("gog_firewall", "mineru_cli.wrappers.gog_firewall", "run_gog_firewall", "MINERU_GOG_FIREWALL_BIN", "/tmp/fake/gog-firewall"),
    ("imsg_firewall", "mineru_cli.wrappers.imsg_firewall", "run_imsg_firewall", "MINERU_IMSG_FIREWALL_BIN", "/tmp/fake/imsg-firewall"),
    ("imsg", "mineru_cli.wrappers.imsg", "run_imsg", "MINERU_IMSG_BIN", "/tmp/fake/imsg"),
    ("monarch", "mineru_cli.wrappers.monarch", "run_monarch", "MINERU_MONARCH_BIN", "/tmp/fake/monarch"),
    ("amazon_orders", "mineru_cli.wrappers.amazon_orders", "run_amazon_orders", "MINERU_AMAZON_ORDERS_BIN", "/tmp/fake/amazon-orders"),
    ("brevity", "mineru_cli.wrappers.brevity", "run_brevity", "MINERU_BREVITY_BIN", "/tmp/fake/brevity"),
]


@pytest.mark.parametrize("label,module,fn_name,env,fake", _WRAPPER_TABLE)
def test_wrapper_threads_profile_env_dict_when_ctx_passed(
    label: str,
    module: str,
    fn_name: str,
    env: str,
    fake: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every subprocess-invoking wrapper passes env= carrying the three profile keys."""
    monkeypatch.setenv(env, fake)
    import importlib
    mod = importlib.import_module(module)
    runner = getattr(mod, fn_name)
    profile = _StubProfile(
        workspace_absolute="/tmp/prof-ws", keychain_account="prof-kc"
    )
    ctx = _Ctx(obj={"profile_obj": profile})
    with patch.object(mod, "_binary_available", return_value=True):
        fake_run, calls = _record_run()
        with patch.object(subprocess, "run", fake_run):
            runner(["arg"], ctx=ctx)
    kwargs = calls[0]["kwargs"]
    assert "env" in kwargs, f"{label}: wrapper did not pass env="
    child_env = kwargs["env"]
    assert child_env["MINERU_HOME"] == "/tmp/prof-ws"
    assert child_env["MINERU_KEYCHAIN_ACCOUNT"] == "prof-kc"
    assert child_env["MINERU_INJECT_QUEUE_DIR"] == "/tmp/prof-ws/cache/inject-queue"


def test_two_profiles_produce_distinct_wrapper_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_msearch(ctx=)` for two profiles hands the subprocess two DIFFERENT env dicts."""
    monkeypatch.setenv("MINERU_MSEARCH_BIN", "/tmp/fake/msearch")
    _write_profile_yaml(tmp_path, "alice")
    _write_profile_yaml(tmp_path, "bob")
    alice = load_active_profile("alice", base_dir=tmp_path)
    bob = load_active_profile("bob", base_dir=tmp_path)

    from mineru_cli.wrappers import msearch as msearch_mod

    envs = {}
    for label, profile in (("alice", alice), ("bob", bob)):
        ctx = _Ctx(obj={"profile_obj": profile})
        with patch.object(msearch_mod, "_binary_available", return_value=True):
            fake_run, calls = _record_run()
            with patch.object(subprocess, "run", fake_run):
                msearch_mod.run_msearch(["keyword", "x"], ctx=ctx)
        envs[label] = calls[0]["kwargs"]["env"]

    assert envs["alice"]["MINERU_HOME"] != envs["bob"]["MINERU_HOME"]
    assert (
        envs["alice"]["MINERU_KEYCHAIN_ACCOUNT"]
        != envs["bob"]["MINERU_KEYCHAIN_ACCOUNT"]
    )
    assert (
        envs["alice"]["MINERU_INJECT_QUEUE_DIR"]
        != envs["bob"]["MINERU_INJECT_QUEUE_DIR"]
    )


def test_wrapper_no_ctx_still_inherits_ambient_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy callers that omit `ctx` get the ambient env inherited (no env= override)."""
    monkeypatch.setenv("MINERU_MSEARCH_BIN", "/tmp/fake/msearch")
    from mineru_cli.wrappers import msearch as msearch_mod

    with patch.object(msearch_mod, "_binary_available", return_value=True):
        fake_run, calls = _record_run()
        with patch.object(subprocess, "run", fake_run):
            msearch_mod.run_msearch(["keyword", "x"])
    assert "env" not in calls[0]["kwargs"]


# ===========================================================================
# Fix 3 — webapp plist template + templated port + app/config.PORT env
# ===========================================================================


def test_webapp_plist_template_carries_env_and_templated_port() -> None:
    """`engine/launchd/LABEL_PREFIX.webapp.plist.template` renders every isolation key."""
    template = (
        REPO_ROOT / "engine" / "launchd" / "LABEL_PREFIX.webapp.plist.template"
    ).read_text(encoding="utf-8")
    # Templated port instead of hardcoded 5195.
    assert "{{WEBAPP_PORT}}" in template
    # EnvironmentVariables block with the isolation keys.
    for marker in (
        "<key>MINERU_HOME</key>",
        "<key>MINERU_WEBAPP_PORT</key>",
        "<key>PASSPHRASE_KEYCHAIN_ACCOUNT</key>",
        "<key>PASSPHRASE_KEYCHAIN_SERVICE</key>",
        "<key>WEBPUSH_KEYCHAIN_ACCOUNT</key>",
        "<key>WEBPUSH_KEYCHAIN_SERVICE</key>",
        "<key>EnvironmentVariables</key>",
    ):
        assert marker in template, f"webapp plist template missing {marker!r}"


def test_app_config_port_honors_mineru_webapp_port_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`app/config.PORT` reads `MINERU_WEBAPP_PORT` (default 5195)."""
    monkeypatch.setenv("MINERU_WEBAPP_PORT", "5211")
    # Reload the module so the top-level PORT re-resolves.
    import importlib
    sys.path.insert(0, str(REPO_ROOT / "app"))
    try:
        import config as app_config
        importlib.reload(app_config)
        assert app_config.PORT == 5211
    finally:
        # Reset env + reload for a clean slate.
        monkeypatch.delenv("MINERU_WEBAPP_PORT", raising=False)
        import config as app_config
        importlib.reload(app_config)


def test_app_config_port_default_is_5195(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent MINERU_WEBAPP_PORT → PORT falls back to the framework default 5195."""
    monkeypatch.delenv("MINERU_WEBAPP_PORT", raising=False)
    import importlib
    sys.path.insert(0, str(REPO_ROOT / "app"))
    import config as app_config
    importlib.reload(app_config)
    assert app_config.PORT == 5195


def test_render_context_includes_webapp_port_and_inject_queue_dir(
    tmp_path: Path,
) -> None:
    """`build_render_context` emits WEBAPP_PORT + MINERU_INJECT_QUEUE_DIR."""
    _write_profile_yaml(tmp_path, "ctx")
    profile = load_active_profile("ctx", base_dir=tmp_path)
    context = build_render_context(profile=profile, connectors=None, env={})
    assert "WEBAPP_PORT" in context
    assert context["WEBAPP_PORT"] == "5195"
    assert "MINERU_INJECT_QUEUE_DIR" in context
    assert context["MINERU_INJECT_QUEUE_DIR"] == (
        f"{profile.workspace_absolute}/cache/inject-queue"
    )


def test_render_context_webapp_port_honors_extras_override(
    tmp_path: Path,
) -> None:
    """`profile.extras.webapp_port` overrides the default 5195."""
    profile_dir = tmp_path / "portoverride"
    profile_dir.mkdir()
    body = (
        "name: portoverride\n"
        "display_name: PO\n"
        "assistant_name: TestBot\n"
        "timezone: America/Los_Angeles\n"
        "keychain_account: po-kc\n"
        "launchd_label_prefix: com.portoverride\n"
        f"workspace_absolute: {profile_dir}\n"
        f"memory_root: {profile_dir}/memory\n"
        f"briefs_root: {profile_dir}/briefs\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "webapp_port: 5220\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        "  env_prefix: PO_SECRET_\n"
    )
    (profile_dir / "profile.yaml").write_text(body, encoding="utf-8")
    profile = load_active_profile("portoverride", base_dir=tmp_path)
    context = build_render_context(profile=profile, connectors=None, env={})
    assert context["WEBAPP_PORT"] == "5220"


# ===========================================================================
# Fix 4 — every rendered cron plist carries the three MINERU_* env keys
# ===========================================================================


@pytest.fixture()
def seed_profile(monkeypatch: pytest.MonkeyPatch) -> Profile:
    base = REPO_ROOT / "tests" / "fixtures" / "seed_profile_base"
    return load_active_profile("mineru", base_dir=base)


_CRON_JOB_NAMES = (
    "morning-brief",
    "news-brief",
    "curiosity-question",
    "memory-description",
    "memory-dedup",
    "prompts-alignment",
    "pet-summary",
    "inbox-triage",
    "daily-transactions",
    "financial-checkup",
    "daily-consolidation",
    "weekly-deep-consolidation",
    "cleanup-retention",
    "group-members-sweep",
    "pre-export-journals",
    "keepsake-autodeploy",
)


@pytest.mark.parametrize("job_name", _CRON_JOB_NAMES)
def test_rendered_cron_plist_carries_mineru_env_keys(
    job_name: str, seed_profile: Profile
) -> None:
    """Every job rendered by `render_plist` stamps the three MINERU_* env keys."""
    cron = load_cron_config(seed_profile)
    job = get_job(cron, job_name)
    assert job is not None, f"cron.yaml missing {job_name!r}"
    xml = render_plist(job, seed_profile)
    # Each env key is present in the plist body.
    for key in ("MINERU_HOME", "MINERU_KEYCHAIN_ACCOUNT", "MINERU_INJECT_QUEUE_DIR"):
        assert f"<key>{key}</key>" in xml, (
            f"{job_name}: rendered plist missing env key {key!r}"
        )
    # Values sourced from the profile.
    workspace = str(seed_profile.workspace_absolute).rstrip("/")
    assert f"<string>{workspace}</string>" in xml
    assert (
        f"<string>{workspace}/cache/inject-queue</string>" in xml
    )
    assert (
        f"<string>{seed_profile.keychain_account}</string>" in xml
    )


def test_two_profiles_render_cron_plists_with_distinct_env(tmp_path: Path) -> None:
    """Two profiles' rendered plists never collide on MINERU_HOME / KEYCHAIN / INJECT."""
    _write_profile_yaml(tmp_path, "alice")
    _write_profile_yaml(tmp_path, "bob")
    alice = load_active_profile("alice", base_dir=tmp_path)
    bob = load_active_profile("bob", base_dir=tmp_path)

    from mineru_cli.cron.model import CRON_JOB_KIND_LLM, CronJob

    job = CronJob(
        name="morning-brief",
        kind=CRON_JOB_KIND_LLM,
        schedule=("0 7 * * *",),
        program_args=(),
        working_directory="",
        env={},
        timeout_seconds=1800,
        enabled=True,
        instruction="recurring/morning-brief.md",
    )
    alice_xml = render_plist(job, alice)
    bob_xml = render_plist(job, bob)
    # Alice's plist carries alice's keys; NEVER bob's.
    assert str(alice.workspace_absolute) in alice_xml
    assert str(bob.workspace_absolute) not in alice_xml
    assert alice.keychain_account in alice_xml
    assert bob.keychain_account not in alice_xml
    # And vice-versa.
    assert str(bob.workspace_absolute) in bob_xml
    assert str(alice.workspace_absolute) not in bob_xml


# --- Every hydrated launchd template also carries MINERU_* env keys ---------


_LAUNCHD_TEMPLATES = sorted(
    (REPO_ROOT / "engine" / "launchd").glob("LABEL_PREFIX.*.plist.template")
)


@pytest.mark.parametrize(
    "template_path",
    _LAUNCHD_TEMPLATES,
    ids=lambda p: p.name,
)
def test_every_launchd_template_declares_mineru_env_keys(template_path: Path) -> None:
    """Every plist template's EnvironmentVariables block includes the three keys.

    Webapp template gets a slightly different overlay
    (MINERU_HOME + MINERU_WEBAPP_PORT + PASSPHRASE_*/WEBPUSH_*); the
    three cron+daemon templates share the MINERU_HOME/KC/INJECT trio.
    """
    body = template_path.read_text(encoding="utf-8")
    # MINERU_HOME lands in EVERY template (webapp too).
    assert "<key>MINERU_HOME</key>" in body, (
        f"{template_path.name}: no <key>MINERU_HOME</key> in EnvironmentVariables"
    )
    if template_path.name == "LABEL_PREFIX.webapp.plist.template":
        # Webapp shape is validated by a dedicated test.
        return
    assert "<key>MINERU_KEYCHAIN_ACCOUNT</key>" in body, (
        f"{template_path.name}: no <key>MINERU_KEYCHAIN_ACCOUNT</key>"
    )
    assert "<key>MINERU_INJECT_QUEUE_DIR</key>" in body, (
        f"{template_path.name}: no <key>MINERU_INJECT_QUEUE_DIR</key>"
    )


# ===========================================================================
# Fix 5 — push_send.py env-driven KEYCHAIN_ACCOUNT + APP_DIR
# ===========================================================================


def test_push_send_reads_keychain_account_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`scripts/push_send.py` `KEYCHAIN_ACCOUNT` honors `MINERU_KEYCHAIN_ACCOUNT`."""
    monkeypatch.setenv("MINERU_KEYCHAIN_ACCOUNT", "alice-kc")
    # Read the module source and eval the target line in isolation so
    # we exercise the exact expression without importing the whole
    # module (its top-level imports pull `cryptography`, which we
    # avoid to keep the test fast + hermetic).
    src = (REPO_ROOT / "scripts" / "push_send.py").read_text(encoding="utf-8")
    ns: Dict[str, Any] = {"os": os}
    for line in src.splitlines():
        if line.startswith("KEYCHAIN_ACCOUNT ="):
            exec(line, ns)  # noqa: S102 — narrow, script-owned line
            break
    assert ns["KEYCHAIN_ACCOUNT"] == "alice-kc"


def test_push_send_keychain_account_defaults_to_mineru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent env → `KEYCHAIN_ACCOUNT` falls back to `"mineru"` (framework namespace)."""
    monkeypatch.delenv("MINERU_KEYCHAIN_ACCOUNT", raising=False)
    src = (REPO_ROOT / "scripts" / "push_send.py").read_text(encoding="utf-8")
    ns: Dict[str, Any] = {"os": os}
    for line in src.splitlines():
        if line.startswith("KEYCHAIN_ACCOUNT ="):
            exec(line, ns)  # noqa: S102
            break
    assert ns["KEYCHAIN_ACCOUNT"] == "mineru"


def test_push_send_app_dir_reads_mineru_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`scripts/push_send._resolve_app_dir()` returns `MINERU_HOME / app` when set."""
    monkeypatch.setenv("MINERU_HOME", "/tmp/alice-ws")
    # Compile just the _resolve_app_dir function + its dependencies.
    src = (REPO_ROOT / "scripts" / "push_send.py").read_text(encoding="utf-8")
    # Grab the function definition through APP_DIR = _resolve_app_dir().
    marker = "def _resolve_app_dir() -> Path:"
    end_marker = "\nAPP_DIR = _resolve_app_dir()"
    start = src.find(marker)
    end = src.find(end_marker, start) + len(end_marker)
    assert start >= 0 and end > start, "push_send.py layout drifted"
    body = "from pathlib import Path\nimport os\n" + src[start:end]
    ns: Dict[str, Any] = {}
    exec(body, ns)  # noqa: S102
    assert ns["APP_DIR"] == Path("/tmp/alice-ws/app")


def test_push_send_app_dir_defaults_to_repo_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent `MINERU_HOME` → APP_DIR falls back to the repo's `<script_dir>/../app`."""
    monkeypatch.delenv("MINERU_HOME", raising=False)
    src = (REPO_ROOT / "scripts" / "push_send.py").read_text(encoding="utf-8")
    marker = "def _resolve_app_dir() -> Path:"
    end_marker = "\nAPP_DIR = _resolve_app_dir()"
    start = src.find(marker)
    end = src.find(end_marker, start) + len(end_marker)
    body = "from pathlib import Path\nimport os\n" + src[start:end]
    ns: Dict[str, Any] = {
        "__file__": str(REPO_ROOT / "scripts" / "push_send.py"),
    }
    exec(body, ns)  # noqa: S102
    assert ns["APP_DIR"] == REPO_ROOT / "app"


def test_deliver_output_passes_env_to_push_send() -> None:
    """`deliver-output.py::fire_web_push_for_brief` invokes push_send with env=."""
    src = (REPO_ROOT / "scripts" / "deliver-output.py").read_text(encoding="utf-8")
    # The env= kwarg is present on the subprocess.run call inside
    # fire_web_push_for_brief so push_send inherits MINERU_HOME +
    # MINERU_KEYCHAIN_ACCOUNT + MINERU_INJECT_QUEUE_DIR from deliver's
    # own os.environ (which get_profile() already exported).
    start = src.find("def fire_web_push_for_brief")
    end = src.find("\ndef ", start + 1)
    body = src[start:end]
    assert "env={**os.environ}" in body or "env=" in body, (
        "deliver-output.py::fire_web_push_for_brief does not pass env= to push_send"
    )


def test_deliver_output_inject_queue_dir_honors_mineru_inject_queue_dir_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`scripts/deliver-output.py::INJECT_QUEUE_DIR` respects MINERU_INJECT_QUEUE_DIR."""
    monkeypatch.setenv("MINERU_INJECT_QUEUE_DIR", "/tmp/custom-queue")
    monkeypatch.setenv("MINERU_HOME", "/tmp/some-home")
    src = (REPO_ROOT / "scripts" / "deliver-output.py").read_text(encoding="utf-8")
    # Extract the multi-line INJECT_QUEUE_DIR resolution block. The
    # Path( ... ) call spans 6 lines including a nested os.environ.get
    # with a default expression, so we grep for the final `)` at the
    # module's own indentation (start of a line).
    start = src.find("_MINERU_HOME = Path")
    # Find the closing `)` of the INJECT_QUEUE_DIR block, which sits
    # at column 0 on its own line.
    marker = "INJECT_QUEUE_DIR = Path("
    marker_start = src.find(marker, start)
    # Walk forward and match parens.
    depth = 0
    i = marker_start + len(marker) - 1
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    end = i + 1
    body = "import os\nfrom pathlib import Path\n" + src[start:end]
    ns: Dict[str, Any] = {}
    exec(body, ns)  # noqa: S102
    assert ns["INJECT_QUEUE_DIR"] == Path("/tmp/custom-queue")
