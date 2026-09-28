"""Two distinct profiles must be fully isolated (Phase 1 audit).

The Phase 1 done-criterion: two profiles resolve to DISTINCT keychain
accounts, DISTINCT launchd label prefixes, DISTINCT filesystem roots,
and DISTINCT secrets env prefixes. No shared state that would let one
profile see or clobber the other's data.

These tests do not touch the live Keychain or `~/Library/LaunchAgents`
— they exercise the profile loader + secrets bridge + plist renderer
in-process and assert that the derived namespaces don't collide.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.cron.model import CRON_JOB_KIND_LLM, CronJob
from mineru_cli.cron.plist import render_plist
from mineru_cli.profile import (
    Profile,
    load_active_profile,
    secrets_config_from_profile,
)
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)
from mineru_cli.secrets import build_resolver


def _write_profile(base: Path, name: str, **overrides: str) -> Path:
    """Materialize a valid profile.yaml under `<base>/<name>/`.

    Every namespace field defaults to something distinct-per-profile so a
    caller doesn't have to remember to override them: `keychain_account`
    becomes `<name>-kc`, `launchd_label_prefix` becomes `com.<name>`,
    paths become `/tmp/<name>-*`.
    """
    fields = {
        "name": name,
        "display_name": name.capitalize(),
        "assistant_name": "TestBot",
        "timezone": "America/Los_Angeles",
        "keychain_account": f"{name}-kc",
        "launchd_label_prefix": f"com.{name}",
        "workspace_absolute": f"/tmp/{name}-workspace",
        "memory_root": f"/tmp/{name}-workspace/memory",
        "briefs_root": f"/tmp/{name}-workspace/briefs",
        "journal_apple_notes_folder": "Daily Journals",
    }
    fields.update(overrides)
    body_lines = [f"{k}: {v}" for k, v in fields.items()]
    body_lines.extend(
        [
            "secrets:",
            "  backends: [env, keychain]",
            f"  env_prefix: {name.upper()}_SECRET_",
        ]
    )
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.yaml").write_text(
        "\n".join(body_lines) + "\n", encoding="utf-8"
    )
    return profile_dir


def _two_profiles(base: Path) -> tuple[Profile, Profile]:
    _write_profile(base, "alice")
    _write_profile(base, "bob")
    alice = load_active_profile("alice", base_dir=base)
    bob = load_active_profile("bob", base_dir=base)
    return alice, bob


# --- Keychain account isolation ---------------------------------------


def test_two_profiles_distinct_keychain_account(tmp_path: Path) -> None:
    alice, bob = _two_profiles(tmp_path)
    assert alice.keychain_account != bob.keychain_account
    assert alice.keychain_account == "alice-kc"
    assert bob.keychain_account == "bob-kc"


def test_two_profiles_distinct_secrets_config_keychain_account(
    tmp_path: Path,
) -> None:
    """The SecretsConfig bridge carries each profile's Keychain account."""
    alice, bob = _two_profiles(tmp_path)
    alice_cfg = secrets_config_from_profile(alice)
    bob_cfg = secrets_config_from_profile(bob)
    assert alice_cfg.keychain_account == "alice-kc"
    assert bob_cfg.keychain_account == "bob-kc"


def test_two_profiles_env_backend_uses_distinct_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SecretsResolver built from each profile hits each profile's env prefix.

    Set the same secret under BOTH prefixes with different values;
    each profile's resolver must read only its own value.
    """
    alice, bob = _two_profiles(tmp_path)
    monkeypatch.setenv("ALICE_SECRET_MY_KEY", "value-for-alice")
    monkeypatch.setenv("BOB_SECRET_MY_KEY", "value-for-bob")
    alice_resolver = build_resolver(secrets_config_from_profile(alice))
    bob_resolver = build_resolver(secrets_config_from_profile(bob))
    assert alice_resolver.resolve("my-key").value == "value-for-alice"
    assert bob_resolver.resolve("my-key").value == "value-for-bob"


# --- launchd label isolation ------------------------------------------


def test_two_profiles_distinct_launchd_label_prefix(tmp_path: Path) -> None:
    alice, bob = _two_profiles(tmp_path)
    assert alice.launchd_label_prefix != bob.launchd_label_prefix
    assert alice.launchd_label_prefix == "com.alice"
    assert bob.launchd_label_prefix == "com.bob"


def test_render_plist_bakes_in_per_profile_label_prefix(
    tmp_path: Path,
) -> None:
    """The rendered plist Label uses the ACTIVE profile's prefix, not a shared one.

    A regression that hardcoded `com.mineru` in the renderer would let
    two profiles' plists collide at install time; this test pins the
    per-profile label into the rendered XML.
    """
    alice, bob = _two_profiles(tmp_path)
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
    assert "<string>com.alice.morning-brief</string>" in alice_xml
    assert "<string>com.bob.morning-brief</string>" in bob_xml
    # And each profile's XML must NOT carry the OTHER profile's label.
    assert "com.bob" not in alice_xml
    assert "com.alice" not in bob_xml


def test_render_plist_bakes_in_per_profile_workspace_path(
    tmp_path: Path,
) -> None:
    """WorkingDirectory + trigger-script paths reflect each profile's workspace."""
    alice, bob = _two_profiles(tmp_path)
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
    assert "/tmp/alice-workspace" in alice_xml
    assert "/tmp/bob-workspace" in bob_xml
    assert "/tmp/bob-workspace" not in alice_xml
    assert "/tmp/alice-workspace" not in bob_xml


# --- Filesystem roots isolation ---------------------------------------


def test_two_profiles_distinct_filesystem_roots(tmp_path: Path) -> None:
    alice, bob = _two_profiles(tmp_path)
    fields = ("workspace_absolute", "memory_root", "briefs_root")
    for field in fields:
        alice_v = getattr(alice, field)
        bob_v = getattr(bob, field)
        assert alice_v != bob_v, (
            f"{field}: alice={alice_v!r} bob={bob_v!r} — profiles share a root"
        )


def test_two_profiles_distinct_profile_root(tmp_path: Path) -> None:
    alice, bob = _two_profiles(tmp_path)
    assert alice.profile_root != bob.profile_root
    assert alice.profile_root.name == "alice"
    assert bob.profile_root.name == "bob"


# --- Every namespacing field is per-profile, none is engine-wide ----


_NAMESPACING_FIELDS = (
    "keychain_account",
    "launchd_label_prefix",
    "workspace_absolute",
    "memory_root",
    "briefs_root",
    "secrets_env_prefix",
)


def test_every_namespacing_field_is_per_profile(tmp_path: Path) -> None:
    """No namespacing field silently collapses to a single engine-wide value.

    A regression that hardcoded any of these in the loader or the plist
    renderer would surface as an equal value across two distinct profiles.
    """
    alice, bob = _two_profiles(tmp_path)
    collisions = [
        field
        for field in _NAMESPACING_FIELDS
        if getattr(alice, field) == getattr(bob, field)
    ]
    assert collisions == [], (
        f"namespacing collapse: {collisions} identical across profiles"
    )


# --- Consumer-level: `mineru memory search` isolation -----------------
#
# The wire tests above prove the profile loader + resolver don't share
# state, but Finding 1 of the Fable security review pointed at a REAL
# consumer-level leak: `mineru memory` was hitting the msearch engine's
# default workspace ($MINERU_HOME) regardless of --profile, so
# `mineru --profile mira memory search foo` returned the operator's hits.
# The fix routes `--workspace <profile.workspace_absolute>` through
# to msearch. These tests plant two toy memory trees under two
# profiles and assert each profile's search sees only its own tree.
# Discipline note: we RECORD the argv sent to the msearch wrapper (the
# unit boundary we control), rather than shelling out to the real
# msearch binary — the engine's index / cache behavior is not the
# subject of this security test. The routed --workspace argument IS.


def _isolate_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every profile-resolution env var so the test starts clean."""
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def _stub_run_msearch(recorded: List[List[str]]):
    """Return a stub for `mineru_cli.verbs.memory.run_msearch` that records argv.

    The stub returns 0 so the verb exits cleanly; the recorded argv is
    the security-relevant surface: it must carry the ACTIVE profile's
    workspace root, so downstream msearch would scope its walk to the
    right memory tree.
    """

    def fake(args):
        recorded.append(list(args))
        return 0

    return fake


def _write_toy_memory_tree(profile_dir: Path, name: str) -> None:
    """Materialize a `memory/` subtree next to a profile.yaml.

    The content is intentionally profile-distinctive (`f'{name}-only'`)
    so a leak surfaces as the wrong tree's content bleeding into search
    results. `msearch` is not shelled out here; the argv routing is
    what the security test pins.
    """
    memory_dir = profile_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "note.md").write_text(
        f"---\ntags: [{name}-only]\n---\n\n"
        f"marker: {name}-secret-marker\n",
        encoding="utf-8",
    )


def _minimal_profile_body_for(name: str, workspace_root: Path) -> str:
    """profile.yaml body pointing every filesystem root at `workspace_root`."""
    lines = [
        f"name: {name}",
        f"display_name: {name.capitalize()}",
        "assistant_name: TestBot",
        "timezone: America/Los_Angeles",
        f"keychain_account: {name}-kc",
        f"launchd_label_prefix: com.{name}",
        f"workspace_absolute: {workspace_root}",
        f"memory_root: {workspace_root}/memory",
        f"briefs_root: {workspace_root}/briefs",
        "journal_apple_notes_folder: Daily Journals",
        "secrets:",
        "  backends: [env, keychain]",
        f"  env_prefix: {name.upper()}_SECRET_",
    ]
    return "\n".join(lines) + "\n"


def _setup_isolated_profiles(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build two workspace roots + a profiles/ tree with alice + bob.

    Returns (workspace_root, alice_workspace, bob_workspace).
    """
    workspace_root = tmp_path
    profiles = workspace_root / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)

    alice_workspace = tmp_path / "alice-ws"
    bob_workspace = tmp_path / "bob-ws"
    alice_workspace.mkdir()
    bob_workspace.mkdir()

    alice_profile = profiles / "alice"
    alice_profile.mkdir()
    (alice_profile / "profile.yaml").write_text(
        _minimal_profile_body_for("alice", alice_workspace), encoding="utf-8"
    )
    _write_toy_memory_tree(alice_workspace, "alice")

    bob_profile = profiles / "bob"
    bob_profile.mkdir()
    (bob_profile / "profile.yaml").write_text(
        _minimal_profile_body_for("bob", bob_workspace), encoding="utf-8"
    )
    _write_toy_memory_tree(bob_workspace, "bob")

    return workspace_root, alice_workspace, bob_workspace


def test_memory_search_routes_active_profile_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mineru --profile <name> memory search` forwards --workspace <profile.workspace_absolute>.

    This is the crux Finding 1 called out: two profiles must never
    share a memory tree at the engine level. The verb layer forwards
    `--workspace <root>` to msearch, so msearch's `resolve_paths` walks
    each profile's OWN `memory/` tree.
    """
    _isolate_profile_env(monkeypatch)
    workspace_root, alice_ws, bob_ws = _setup_isolated_profiles(tmp_path)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace_root))

    runner = CliRunner()
    recorded_alice: List[List[str]] = []
    recorded_bob: List[List[str]] = []

    with patch(
        "mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded_alice)
    ):
        result = runner.invoke(app, ["--profile", "alice", "memory", "search", "marker"])
    assert result.exit_code == 0, result.output
    with patch(
        "mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded_bob)
    ):
        result = runner.invoke(app, ["--profile", "bob", "memory", "search", "marker"])
    assert result.exit_code == 0, result.output

    assert len(recorded_alice) == 1 and len(recorded_bob) == 1

    # alice's argv routes alice's workspace, bob's routes bob's, and
    # NEITHER routes the other's tree.
    alice_argv = recorded_alice[0]
    bob_argv = recorded_bob[0]

    assert "--workspace" in alice_argv, (
        f"alice argv missing --workspace: {alice_argv!r}"
    )
    assert alice_argv[alice_argv.index("--workspace") + 1] == str(alice_ws)
    assert str(bob_ws) not in alice_argv, (
        f"alice argv leaks bob's workspace: {alice_argv!r}"
    )

    assert "--workspace" in bob_argv, (
        f"bob argv missing --workspace: {bob_argv!r}"
    )
    assert bob_argv[bob_argv.index("--workspace") + 1] == str(bob_ws)
    assert str(alice_ws) not in bob_argv, (
        f"bob argv leaks alice's workspace: {bob_argv!r}"
    )


def test_memory_search_workspace_precedes_user_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Profile-scoped `--workspace` sits BEFORE user-supplied extras.

    argparse's "last-flag-wins" for `--workspace` means an explicit
    user override still works (last on the command line wins), while
    the default is the profile's own workspace. Pinning this order
    guards against a regression that appends the profile arg after
    the user's tail (which would silently override an explicit
    `--workspace /some/other`).
    """
    _isolate_profile_env(monkeypatch)
    workspace_root, alice_ws, _ = _setup_isolated_profiles(tmp_path)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace_root))

    runner = CliRunner()
    recorded: List[List[str]] = []
    with patch("mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded)):
        result = runner.invoke(
            app,
            ["--profile", "alice", "memory", "search", "marker", "--pretty"],
        )
    assert result.exit_code == 0
    argv = recorded[0]
    # Position 0/1 = verb + term; the profile workspace pair must sit
    # right after them, BEFORE any user extras.
    assert argv[0] == "keyword"
    assert argv[1] == "marker"
    assert argv[2] == "--workspace"
    assert argv[3] == str(alice_ws)
    # user extras land after the profile pair.
    assert "--pretty" in argv[4:]


def test_memory_tags_and_query_also_route_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`memory tags` and `memory query` isolate the workspace too, not just search."""
    _isolate_profile_env(monkeypatch)
    workspace_root, alice_ws, _ = _setup_isolated_profiles(tmp_path)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace_root))

    runner = CliRunner()
    for cli, sub_verb in (
        (["--profile", "alice", "memory", "tags"], "tags"),
        (["--profile", "alice", "memory", "query", "what is marker"], "query"),
    ):
        recorded: List[List[str]] = []
        with patch(
            "mineru_cli.verbs.memory.run_msearch", _stub_run_msearch(recorded)
        ):
            result = runner.invoke(app, cli)
        assert result.exit_code == 0, (sub_verb, result.output)
        argv = recorded[0]
        assert argv[0] == sub_verb
        assert "--workspace" in argv
        assert argv[argv.index("--workspace") + 1] == str(alice_ws)


# --- Consumer-level: telegram cache dirs isolate per profile -----------
#
# Finding 2 pointed at `verbs/telegram.py` (inject-queue dir) and
# `wrappers/telegram_image_cache.py` (sent-image cache dir), both of
# which hardcoded `Path.home() / ".mineru" / ...`. Profile B writes
# would land in the operator's cache. The fix derives BOTH from the active
# profile's workspace.


def test_two_profiles_resolve_to_distinct_telegram_cache_dirs(
    tmp_path: Path,
) -> None:
    """The sent-image cache root is per profile, never a shared engine default.

    We invoke the wrapper helper `cache_dir_for_workspace` on each
    profile's `workspace_absolute` and assert (a) the dirs differ, and
    (b) neither string contains the pre-fix `$MINERU_HOME/` marker.
    """
    from mineru_cli.wrappers.telegram_image_cache import cache_dir_for_workspace

    alice, bob = _two_profiles(tmp_path)
    alice_cache = cache_dir_for_workspace(alice.workspace_absolute)
    bob_cache = cache_dir_for_workspace(bob.workspace_absolute)
    assert alice_cache != bob_cache, (
        f"telegram cache dirs collapsed: {alice_cache} == {bob_cache}"
    )
    assert str(alice_cache).startswith(str(alice.workspace_absolute)), alice_cache
    assert str(bob_cache).startswith(str(bob.workspace_absolute)), bob_cache
    # Reject the pre-fix hardcoded `$MINERU_HOME/` regression.
    home_mineru = f"{Path.home()}/.mineru/"
    assert home_mineru not in str(alice_cache)
    assert home_mineru not in str(bob_cache)


def test_two_profiles_resolve_to_distinct_inject_queue_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mineru telegram inject` under two profiles writes to two DIFFERENT queues.

    We assert the physical write path per profile: alice's inject file
    lands under alice's workspace, bob's under bob's. A regression that
    re-hardcodes `$MINERU_HOME/cache/inject-queue` would surface as both
    profiles writing to the same directory.
    """
    _isolate_profile_env(monkeypatch)
    workspace_root, alice_ws, bob_ws = _setup_isolated_profiles(tmp_path)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace_root))
    # Explicitly clear the test-only env override so the profile-derived
    # path is exercised (not the tmp override).
    monkeypatch.delenv("MINERU_INJECT_QUEUE_DIR", raising=False)

    runner = CliRunner()
    result_a = runner.invoke(
        app, ["--profile", "alice", "telegram", "inject", "note", "hello-alice"]
    )
    assert result_a.exit_code == 0, result_a.output
    result_b = runner.invoke(
        app, ["--profile", "bob", "telegram", "inject", "note", "hello-bob"]
    )
    assert result_b.exit_code == 0, result_b.output

    alice_queue = alice_ws / "cache" / "inject-queue"
    bob_queue = bob_ws / "cache" / "inject-queue"
    alice_files = sorted(p.name for p in alice_queue.iterdir())
    bob_files = sorted(p.name for p in bob_queue.iterdir())
    assert len(alice_files) == 1
    assert len(bob_files) == 1
    # And each queue holds only its OWN profile's payload.
    import json as _json
    alice_payload = _json.loads((alice_queue / alice_files[0]).read_text())
    bob_payload = _json.loads((bob_queue / bob_files[0]).read_text())
    assert alice_payload["content"] == "hello-alice"
    assert bob_payload["content"] == "hello-bob"
    # Belt-and-braces: the pre-fix hardcoded path is absent.
    home_queue = Path.home() / ".mineru" / "cache" / "inject-queue"
    if home_queue.exists():
        # If it exists on the tester's machine (the operator's own workspace),
        # confirm we did NOT drop these payloads into it.
        for fname in alice_files + bob_files:
            assert not (home_queue / fname).exists()
