"""Tests for the per-profile allowlist exporter + `mineru access sync/status`.

Covers the Arc 2 step-4 engine side of the Landline daemon handshake:

  - `resolve_allowlist_ids(access, humans)`: pure join, validation
    (owner present, no dupes, positive ints), deterministic ordering.
  - `format_allowlist_value` + `parse_keychain_allowlist`: round-trip
    of the canonical Keychain wire format, plus the daemon parser's
    tolerance for whitespace / junk tokens / empty string (fail-closed).
  - `mineru access sync`: DRY-RUN is the default; `--apply` shells out
    to `security add-generic-password -U` via an INJECTED subprocess
    runner (NEVER touches the live Keychain).
  - `mineru access status`: reads the Keychain slot via a mocked
    subprocess runner, diffs against resolved YAML, prints drift.
  - `mineru access add`/`mineru access remove`: DRY-RUN default,
    `--apply` writes access.yaml + refreshes the Keychain slot
    (subprocess mocked).
  - Per-profile Keychain-account isolation: two profiles with
    different `keychain_account` values write two distinct Keychain
    slots; a third profile whose keychain_account matches profile A's
    does not silently overwrite profile B's slot.

CRITICAL RULE: no test in this file may ever invoke the real
`/usr/bin/security` binary. Every subprocess call is either handed an
injected `runner=` callable or covered by the module-level
`_fail_if_security_binary_called` autouse fixture below.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from mineru_cli.access import (
    AccessConfig,
    AccessEntry,
    AccessTier,
    AllowlistDiff,
    AllowlistExportError,
    KeychainReadResult,
    KeychainWriteResult,
    TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE,
    diff_allowlist,
    format_allowlist_value,
    parse_keychain_allowlist,
    read_keychain_allowlist,
    resolve_allowlist_ids,
    write_keychain_allowlist,
)
from mineru_cli.access.exporter import (
    SECURITY_BINARY,
    _default_runner,
)
from mineru_cli.app import app
from mineru_cli.humans import load_humans_registry
from mineru_cli.humans.schema import Human, HumansRegistry
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# Safety net: refuse to invoke the real `/usr/bin/security` binary. Any
# test that forgets to mock is caught here (the whole file is autouse-
# fixtured so a leaked subprocess call fails loudly instead of touching
# the operator's live Keychain).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fail_if_security_binary_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to invoke the real `security` binary from any test.

    Wraps `_default_runner` so a test that forgets to pass `runner=`
    into `read_keychain_allowlist` / `write_keychain_allowlist` (and
    would therefore fall through to the real subprocess) raises a
    hard assertion instead of silently touching the operator's login
    keychain.

    Tests that legitimately need the runner path monkeypatch it back
    with their own stub via `patch("mineru_cli.access.exporter._default_runner", ...)`.
    """
    def _hard_fail(*args, **kwargs):
        raise AssertionError(
            "test invoked the real `security` binary via _default_runner; "
            "pass runner= or patch _default_runner explicitly."
        )
    monkeypatch.setattr(
        "mineru_cli.access.exporter._default_runner", _hard_fail
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def _humans(*pairs: Tuple[str, int]) -> HumansRegistry:
    """Build a HumansRegistry from (handle, telegram_id) pairs."""
    entries = {
        h: Human(handle=h, telegram_id=tid, display_name=h.capitalize())
        for h, tid in pairs
    }
    return HumansRegistry(entries_by_handle=entries)


def _access(
    profile_name: str,
    owner: str,
    authorized: List[Tuple[str, AccessTier]],
) -> AccessConfig:
    entries = [AccessEntry(human=h, tier=t) for h, t in authorized]
    return AccessConfig(
        profile_name=profile_name, owner=owner, authorized=entries
    )


class _FakeCompleted:
    """Stand-in for `subprocess.CompletedProcess` used by injected runners."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRunner:
    """Callable subprocess-runner stub that records every call.

    Also lets a test rig a queue of `(returncode, stdout, stderr)` triples
    the runner returns in order. Handy for a sync flow that first reads
    the Keychain (existing slot) then writes it.
    """

    def __init__(
        self,
        results: Optional[List[Tuple[int, str, str]]] = None,
        raise_on_call: Optional[Exception] = None,
    ):
        self.calls: List[Dict[str, Any]] = []
        self.results: List[Tuple[int, str, str]] = list(results or [])
        self.raise_on_call = raise_on_call

    def __call__(self, argv, *args, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": dict(kwargs)})
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if not self.results:
            return _FakeCompleted(0, "", "")
        rc, stdout, stderr = self.results.pop(0)
        return _FakeCompleted(rc, stdout, stderr)


def _write_seed_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile_name: str = "alice",
    keychain_account: str = "alice-acct",
    owner_handle: str = "alice",
    owner_telegram_id: int = 1001,
    extra_humans: Optional[List[Tuple[str, int]]] = None,
    authorized: Optional[List[Tuple[str, AccessTier]]] = None,
) -> Path:
    """Materialize a self-contained profile + humans.yaml under tmp_path.

    Sets `MINERU_WORKSPACE_ROOT` + `MINERU_PROFILE_ROOT` to `tmp_path`
    so both the humans loader (reads workspace root) and the profile
    loader (reads profiles base dir) resolve there. Sets
    `MINERU_PROFILE` to `profile_name` so `get_profile(ctx)` picks the
    right one without a `current` symlink.
    """
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, profile_name)

    # humans.yaml
    humans_body = {"humans": {}}
    humans_body["humans"][owner_handle] = {
        "telegram_id": owner_telegram_id,
        "display_name": owner_handle.capitalize(),
    }
    for handle, tid in (extra_humans or []):
        humans_body["humans"][handle] = {
            "telegram_id": tid,
            "display_name": handle.capitalize(),
        }
    (tmp_path / "humans.yaml").write_text(
        yaml.safe_dump(humans_body, sort_keys=False), encoding="utf-8"
    )

    # profile.yaml
    profile_dir = tmp_path / profile_name
    profile_dir.mkdir()
    (profile_dir / "profile.yaml").write_text(
        f"name: {profile_name}\n"
        f"display_name: {profile_name.capitalize()}\n"
        "assistant_name: TestBot\n"
        "timezone: America/Los_Angeles\n"
        f"keychain_account: {keychain_account}\n"
        f"launchd_label_prefix: com.{profile_name}\n"
        f"workspace_absolute: {tmp_path}/{profile_name}\n"
        f"memory_root: {tmp_path}/{profile_name}/memory\n"
        f"briefs_root: {tmp_path}/{profile_name}/briefs\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        f"  env_prefix: {profile_name.upper()}_SECRET_\n",
        encoding="utf-8",
    )
    # access.yaml
    lines = [f"owner: {owner_handle}\n", "authorized:\n"]
    if authorized is None:
        # Default: owner-only.
        lines.append(f"  - {{human: {owner_handle}, tier: owner}}\n")
    else:
        for handle, tier in authorized:
            lines.append(f"  - {{human: {handle}, tier: {tier.value}}}\n")
    (profile_dir / "access.yaml").write_text("".join(lines), encoding="utf-8")
    return profile_dir


# ---------------------------------------------------------------------------
# resolve_allowlist_ids: pure join + validation
# ---------------------------------------------------------------------------


def test_resolve_allowlist_owner_only() -> None:
    humans = _humans(("alice", 111))
    access = _access("alice", "alice", [("alice", AccessTier.OWNER)])
    assert resolve_allowlist_ids(access, humans) == [111]


def test_resolve_allowlist_owner_first_and_guests_excluded(
    capsys: pytest.CaptureFixture,
) -> None:
    """Cat-B critical (2026-09-04 step-5 audit, Finding 11): GUEST-tier
    entries are EXCLUDED from the exported allowlist because the daemon
    guard has no tier concept and would admit them with owner-equivalent
    access. Owner ids still flow through; each skipped guest produces a
    loud warning."""
    humans = _humans(("alice", 111), ("bob", 222), ("carol", 333))
    access = _access(
        "alice", "alice",
        [
            ("alice", AccessTier.OWNER),
            ("bob", AccessTier.GUEST),
            ("carol", AccessTier.GUEST),
        ],
    )
    ids = resolve_allowlist_ids(access, humans)
    assert ids == [111], "guests must be excluded until real scoping lands"
    captured = capsys.readouterr()
    # One warning per resolve call, naming every skipped guest.
    assert "NOT admitting" in captured.err
    assert "'bob'" in captured.err
    assert "'carol'" in captured.err
    # And the OWNER-equivalent-access risk is spelled out.
    assert "OWNER-equivalent" in captured.err


def test_resolve_allowlist_owner_materialized_even_if_only_guest_authorized(
    capsys: pytest.CaptureFixture,
) -> None:
    """Even when the caller hands in a hand-built AccessConfig whose
    `authorized` list is guest-only, the owner is materialized FIRST and
    every guest is excluded with a warning."""
    humans = _humans(("alice", 111), ("bob", 222))
    access = _access(
        "alice", "alice", [("bob", AccessTier.GUEST)]  # owner absent
    )
    assert resolve_allowlist_ids(access, humans) == [111]
    assert "'bob'" in capsys.readouterr().err


def test_resolve_allowlist_rejects_owner_missing_from_humans() -> None:
    humans = _humans(("bob", 222))
    access = _access("alice", "alice", [("alice", AccessTier.OWNER)])
    with pytest.raises(AllowlistExportError) as exc:
        resolve_allowlist_ids(access, humans)
    assert "alice" in str(exc.value)
    assert "humans.yaml" in str(exc.value)


def test_resolve_allowlist_silently_skips_guest_missing_from_humans(
    capsys: pytest.CaptureFixture,
) -> None:
    """Since guests are now excluded wholesale, a guest whose handle no
    longer resolves in humans.yaml no longer blocks the sync — it's just
    another entry that never makes it into the Keychain. It DOES still
    show up in the "not admitting" warning so the operator can spot the
    stale handle."""
    humans = _humans(("alice", 111))
    access = _access(
        "alice", "alice",
        [("alice", AccessTier.OWNER), ("ghost", AccessTier.GUEST)],
    )
    ids = resolve_allowlist_ids(access, humans)
    assert ids == [111]
    assert "'ghost'" in capsys.readouterr().err


def test_resolve_allowlist_rejects_duplicate_telegram_id_between_owner_tier_entries() -> None:
    """Two OWNER-tier entries pointing at the same telegram_id is still a
    humans.yaml bug and must surface — the exporter refuses to silently
    dedup. (The GUEST-tier variant of this bug is now hidden because
    guests are excluded before the dup check; guarding the OWNER-tier
    variant is what keeps the dedup discipline intact for the ids that
    DO reach the Keychain.)"""
    humans = _humans(("alice", 111), ("alice2", 111))
    access = _access(
        "alice", "alice",
        # Hand-built: two OWNER-tier entries with the same telegram_id.
        [("alice", AccessTier.OWNER), ("alice2", AccessTier.OWNER)],
    )
    with pytest.raises(AllowlistExportError) as exc:
        resolve_allowlist_ids(access, humans)
    assert "111" in str(exc.value)
    assert "alice" in str(exc.value)


def test_resolve_allowlist_rejects_zero_telegram_id() -> None:
    humans = _humans(("alice", 0))
    access = _access("alice", "alice", [("alice", AccessTier.OWNER)])
    with pytest.raises(AllowlistExportError) as exc:
        resolve_allowlist_ids(access, humans)
    assert "non-positive" in str(exc.value).lower() or "positive" in str(exc.value).lower()


def test_resolve_allowlist_rejects_negative_telegram_id() -> None:
    humans = _humans(("alice", -1))
    access = _access("alice", "alice", [("alice", AccessTier.OWNER)])
    with pytest.raises(AllowlistExportError) as exc:
        resolve_allowlist_ids(access, humans)
    assert "-1" in str(exc.value)


def test_resolve_allowlist_rejects_bool_telegram_id() -> None:
    """A `true` value in humans.yaml (bool subclasses int) must NOT
    silently write "1" to the Keychain."""
    # We can't build a bool via the loader (it rejects), but a hand-
    # built HumansRegistry with a bool telegram_id must still fail loud
    # at the exporter boundary.
    reg = HumansRegistry(
        entries_by_handle={
            "alice": Human(
                handle="alice", telegram_id=True, display_name="Alice"  # type: ignore
            )
        }
    )
    access = _access("alice", "alice", [("alice", AccessTier.OWNER)])
    with pytest.raises(AllowlistExportError) as exc:
        resolve_allowlist_ids(access, reg)
    assert "alice" in str(exc.value)


def test_resolve_allowlist_rejects_bogus_types() -> None:
    with pytest.raises(AllowlistExportError):
        resolve_allowlist_ids("not-an-access-config", _humans())  # type: ignore
    with pytest.raises(AllowlistExportError):
        resolve_allowlist_ids(
            _access("a", "a", []), "not-a-humans-registry"  # type: ignore
        )


# ---------------------------------------------------------------------------
# format_allowlist_value + parse_keychain_allowlist round-trip
# ---------------------------------------------------------------------------


def test_format_empty_is_empty_string() -> None:
    assert format_allowlist_value([]) == ""


def test_format_single_id_is_bare_number() -> None:
    assert format_allowlist_value([111]) == "111"


def test_format_multi_id_is_canonical_comma_string() -> None:
    assert format_allowlist_value([111, 222, 333]) == "111,222,333"


def test_parse_empty_string_yields_empty_list() -> None:
    """Empty string parses to empty — the fail-closed signal."""
    assert parse_keychain_allowlist("") == []


def test_parse_whitespace_around_commas_tolerated() -> None:
    """Matches `landline.runtime.guard._parse_int_set` tolerance."""
    assert parse_keychain_allowlist(" 111 , 222 ") == [111, 222]


def test_parse_junk_tokens_silently_skipped() -> None:
    assert parse_keychain_allowlist("111,not-an-int,222") == [111, 222]


def test_parse_all_junk_yields_empty_list() -> None:
    """A fully-junk Keychain slot parses to empty — daemon fails closed."""
    assert parse_keychain_allowlist("only,junk,tokens") == []


def test_parse_deduplicates_preserving_order() -> None:
    assert parse_keychain_allowlist("111,222,111,333") == [111, 222, 333]


def test_round_trip_canonical_form() -> None:
    ids = [111, 222, 333]
    assert parse_keychain_allowlist(format_allowlist_value(ids)) == ids


# ---------------------------------------------------------------------------
# read_keychain_allowlist: hit / miss / locked / missing binary / timeout
# ---------------------------------------------------------------------------


def test_read_keychain_hit_returns_stripped_value() -> None:
    runner_stub = _RecordingRunner([(0, "111,222\n", "")])
    result = read_keychain_allowlist("mineru", runner=runner_stub)
    assert result.present is True
    assert result.rc == 0
    assert result.raw == "111,222"
    # Argv shape: canonical `security find-generic-password -a <acct> -s <svc> -w`.
    argv = runner_stub.calls[0]["argv"]
    assert argv[0] == SECURITY_BINARY
    assert argv[1] == "find-generic-password"
    assert argv[2] == "-a"
    assert argv[3] == "mineru"
    assert argv[4] == "-s"
    assert argv[5] == TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE
    assert argv[6] == "-w"
    # `stdin` is detached so a keychain prompt cannot block the CLI.
    assert runner_stub.calls[0]["kwargs"].get("stdin") == subprocess.DEVNULL


def test_read_keychain_miss_returns_present_false_rc_44() -> None:
    runner_stub = _RecordingRunner([(44, "", "not found")])
    result = read_keychain_allowlist("mineru", runner=runner_stub)
    assert result.present is False
    assert result.rc == 44
    assert result.raw == ""


def test_read_keychain_missing_binary_returns_present_false() -> None:
    runner_stub = _RecordingRunner(raise_on_call=FileNotFoundError("no security"))
    result = read_keychain_allowlist("mineru", runner=runner_stub)
    assert result.present is False
    assert result.rc == -1
    assert "not available" in result.stderr_snippet


def test_read_keychain_timeout_returns_present_false() -> None:
    runner_stub = _RecordingRunner(
        raise_on_call=subprocess.TimeoutExpired(cmd="sec", timeout=5.0)
    )
    result = read_keychain_allowlist("mineru", runner=runner_stub, timeout_seconds=5.0)
    assert result.present is False
    assert result.rc == -2
    assert "timed out" in result.stderr_snippet


def test_read_keychain_rejects_empty_account() -> None:
    with pytest.raises(AllowlistExportError):
        read_keychain_allowlist("")


# ---------------------------------------------------------------------------
# write_keychain_allowlist: happy path, argv shape, redaction discipline
# ---------------------------------------------------------------------------


def test_write_keychain_success_argv_shape() -> None:
    runner_stub = _RecordingRunner([(0, "", "")])
    result = write_keychain_allowlist("mineru", "111,222", runner=runner_stub)
    assert result.ok is True
    assert result.rc == 0
    argv = runner_stub.calls[0]["argv"]
    # security add-generic-password -U -a <acct> -s <svc> -w <value>
    assert argv[0] == SECURITY_BINARY
    assert argv[1] == "add-generic-password"
    assert argv[2] == "-U"
    assert argv[3] == "-a"
    assert argv[4] == "mineru"
    assert argv[5] == "-s"
    assert argv[6] == TELEGRAM_ALLOWLIST_KEYCHAIN_SERVICE
    assert argv[7] == "-w"
    assert argv[8] == "111,222"
    # The returned argv_shape has the value redacted for logging.
    assert result.argv_shape[-1] == "<REDACTED>"


def test_write_keychain_failure_reports_rc() -> None:
    runner_stub = _RecordingRunner([(1, "", "some error")])
    result = write_keychain_allowlist("mineru", "111", runner=runner_stub)
    assert result.ok is False
    assert result.rc == 1
    assert "some error" in result.stderr_snippet


def test_write_keychain_missing_binary_returns_ok_false() -> None:
    runner_stub = _RecordingRunner(raise_on_call=FileNotFoundError("gone"))
    result = write_keychain_allowlist("mineru", "111", runner=runner_stub)
    assert result.ok is False
    assert result.rc == -1


def test_write_keychain_timeout_returns_ok_false() -> None:
    runner_stub = _RecordingRunner(
        raise_on_call=subprocess.TimeoutExpired(cmd="sec", timeout=5.0)
    )
    result = write_keychain_allowlist(
        "mineru", "111", runner=runner_stub, timeout_seconds=5.0
    )
    assert result.ok is False
    assert result.rc == -2


def test_write_keychain_rejects_empty_account() -> None:
    with pytest.raises(AllowlistExportError):
        write_keychain_allowlist("", "111")


def test_write_keychain_rejects_non_string_value() -> None:
    with pytest.raises(AllowlistExportError):
        write_keychain_allowlist("mineru", 111)  # type: ignore


# ---------------------------------------------------------------------------
# diff_allowlist: in-sync / drift / non-canonical
# ---------------------------------------------------------------------------


def test_diff_in_sync_matches_canonical() -> None:
    kc = KeychainReadResult(raw="111,222", present=True, rc=0)
    d = diff_allowlist([111, 222], kc)
    assert d.in_sync is True
    assert d.missing_from_keychain == []
    assert d.extra_in_keychain == []


def test_diff_missing_from_keychain() -> None:
    kc = KeychainReadResult(raw="111", present=True, rc=0)
    d = diff_allowlist([111, 222], kc)
    assert d.in_sync is False
    assert d.missing_from_keychain == [222]


def test_diff_extra_in_keychain() -> None:
    kc = KeychainReadResult(raw="111,222,999", present=True, rc=0)
    d = diff_allowlist([111, 222], kc)
    assert d.in_sync is False
    assert d.extra_in_keychain == [999]


def test_diff_non_canonical_flagged_as_drift_even_when_sets_match() -> None:
    """Same ids, but the raw string has extra whitespace — sync would
    still rewrite it, so the diff correctly reports drift."""
    kc = KeychainReadResult(raw=" 111 , 222 ", present=True, rc=0)
    d = diff_allowlist([111, 222], kc)
    assert d.in_sync is False
    assert d.missing_from_keychain == []
    assert d.extra_in_keychain == []
    assert d.canonical_value == "111,222"


def test_diff_keychain_absent_treats_as_empty_set() -> None:
    kc = KeychainReadResult(raw="", present=False, rc=44)
    d = diff_allowlist([111], kc)
    assert d.in_sync is False
    assert d.missing_from_keychain == [111]


# ---------------------------------------------------------------------------
# CLI: `mineru access sync` (dry-run default + --apply)
# ---------------------------------------------------------------------------


def test_cli_sync_dry_run_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default `mineru access sync` prints the plan and never invokes the
    write path."""
    _write_seed_workspace(tmp_path, monkeypatch, owner_telegram_id=111)

    runner_stub = _RecordingRunner(
        [(44, "", "not found")]  # only the read is expected
    )
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "sync"])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "resolved ids:" in result.output
    assert "canonical value:" in result.output
    assert "'111'" in result.output  # canonical value shows quoted repr
    # Only ONE subprocess call: the read. No `add-generic-password` write.
    assert len(runner_stub.calls) == 1
    assert runner_stub.calls[0]["argv"][1] == "find-generic-password"


def test_cli_sync_apply_writes_via_add_generic_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--apply` shells out to `security add-generic-password -U` with
    the exact contract shape (service, account, canonical value)."""
    _write_seed_workspace(
        tmp_path, monkeypatch,
        keychain_account="alice-acct",
        owner_telegram_id=111,
    )

    runner_stub = _RecordingRunner(
        [
            (44, "", "not found"),   # read: absent (fresh install)
            (0, "", ""),              # write: OK
        ]
    )
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "sync", "--apply"])
    assert result.exit_code == 0, result.output
    assert "APPLIED" in result.output
    # Two calls: one read, one write.
    assert len(runner_stub.calls) == 2
    write_argv = runner_stub.calls[1]["argv"]
    assert write_argv[0] == SECURITY_BINARY
    assert write_argv[1] == "add-generic-password"
    assert write_argv[2] == "-U"
    assert write_argv[3] == "-a"
    assert write_argv[4] == "alice-acct"
    assert write_argv[5] == "-s"
    assert write_argv[6] == "telegram-allowed-chat-ids"
    assert write_argv[7] == "-w"
    assert write_argv[8] == "111"


def test_cli_sync_apply_reports_keychain_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(tmp_path, monkeypatch)
    runner_stub = _RecordingRunner(
        [(44, "", "not found"), (1, "", "denied")]
    )
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "sync", "--apply"])
    assert result.exit_code == 2
    assert "keychain write failed" in (result.output + (result.stderr or "")).lower()


def test_cli_sync_json_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(tmp_path, monkeypatch, owner_telegram_id=111)
    runner_stub = _RecordingRunner([(44, "", "")])
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "sync", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["resolved_ids"] == [111]
    assert payload["canonical_value"] == "111"
    assert payload["action"] == "dry-run"
    assert payload["keychain_service"] == "telegram-allowed-chat-ids"


def test_cli_sync_in_sync_state_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(tmp_path, monkeypatch, owner_telegram_id=111)
    # Keychain already carries the canonical value.
    runner_stub = _RecordingRunner([(0, "111\n", "")])
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "sync"])
    assert result.exit_code == 0, result.output
    assert "in sync" in result.output.lower()


# ---------------------------------------------------------------------------
# CLI: `mineru access status` (diff shown, never writes)
# ---------------------------------------------------------------------------


def test_cli_status_reports_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status drift with a guest in access.yaml: post Finding-11 fix the
    guest is EXCLUDED from `resolved_ids`, so `missing` is empty. The
    stale (revoked) 999 in the Keychain is still surfaced as `extra`, so
    the operator still sees DRIFT."""
    _write_seed_workspace(
        tmp_path, monkeypatch,
        owner_telegram_id=111,
        extra_humans=[("bob", 222)],
        authorized=[
            ("alice", AccessTier.OWNER),
            ("bob", AccessTier.GUEST),
        ],
    )
    # Daemon slot has the owner + a stale (revoked) id.
    runner_stub = _RecordingRunner([(0, "111,999\n", "")])
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "status"])
    assert result.exit_code == 0, result.output
    assert "extra on daemon" in result.output
    assert "999" in result.output
    assert "DRIFT" in result.output
    # Guest bob is excluded — 222 must NOT surface as "missing on daemon"
    # because it never enters the resolved allowlist post-fix.
    assert "222" not in result.output
    # status never writes — only one call.
    assert len(runner_stub.calls) == 1
    assert runner_stub.calls[0]["argv"][1] == "find-generic-password"


def test_cli_status_in_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(tmp_path, monkeypatch, owner_telegram_id=111)
    runner_stub = _RecordingRunner([(0, "111\n", "")])
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "status"])
    assert result.exit_code == 0, result.output
    assert "in sync" in result.output.lower()


def test_cli_status_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(tmp_path, monkeypatch, owner_telegram_id=111)
    runner_stub = _RecordingRunner([(0, "", "")])
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["resolved_ids"] == [111]
    assert payload["keychain_present"] is True
    assert payload["keychain_service"] == "telegram-allowed-chat-ids"
    # Ids parse via daemon semantics.
    assert payload["keychain_ids"] == []
    assert payload["missing_from_keychain"] == [111]


def test_cli_status_keychain_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a non-macOS host or shimmed security binary, status still
    reports a clean diff (keychain treated as empty set)."""
    _write_seed_workspace(tmp_path, monkeypatch)
    runner_stub = _RecordingRunner(
        raise_on_call=FileNotFoundError("no security")
    )
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "status"])
    assert result.exit_code == 0, result.output
    assert "not available" in result.output.lower()


# ---------------------------------------------------------------------------
# CLI: `mineru access add` / `mineru access remove`
# ---------------------------------------------------------------------------


def test_cli_access_add_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _write_seed_workspace(
        tmp_path, monkeypatch,
        owner_telegram_id=111,
        extra_humans=[("bob", 222)],
    )
    # Only the exporter READ fires during dry-run — writes never do.
    # `access add` in dry-run should not touch subprocess at all
    # (nothing to read or write). Even so, wire a stub to catch any
    # accidental subprocess call.
    runner_stub = _RecordingRunner()
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "add", "bob"])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    # access.yaml not mutated.
    body = (profile_dir / "access.yaml").read_text(encoding="utf-8")
    assert "bob" not in body
    # No subprocess calls in dry-run.
    assert runner_stub.calls == []


def test_cli_access_add_guest_apply_writes_yaml_but_excludes_guest_from_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cat-B critical (2026-09-04 step-5 audit, Finding 11).

    `mineru access add --tier guest --apply` (the default `--tier` is
    guest) writes the guest into access.yaml but MUST NOT put the guest's
    telegram_id into the exported/Keychain allowlist — guest-tier scoping
    is not yet enforced by the daemon, and admitting a guest here would
    silently grant OWNER-equivalent daemon access. A loud stderr warning
    must be surfaced naming the excluded handle. Owner ids stay included.
    """
    profile_dir = _write_seed_workspace(
        tmp_path, monkeypatch,
        keychain_account="alice-acct",
        owner_telegram_id=111,
        extra_humans=[("bob", 222)],
    )
    runner_stub = _RecordingRunner([(0, "", "")])
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        # Default tier is guest; the CLI honors that here.
        result = runner.invoke(app, ["access", "add", "bob", "--apply"])
    assert result.exit_code == 0, result.output
    # access.yaml still records bob (guest) — the yaml is the operator's
    # source of truth for who the profile MIGHT admit later.
    body = (profile_dir / "access.yaml").read_text(encoding="utf-8")
    assert "bob" in body
    assert "tier: guest" in body
    # Keychain write fired ONCE with the OWNER-ONLY canonical value.
    # Guest bob's id 222 is EXCLUDED from the write payload.
    assert len(runner_stub.calls) == 1
    write_argv = runner_stub.calls[0]["argv"]
    assert write_argv[4] == "alice-acct"
    assert write_argv[6] == "telegram-allowed-chat-ids"
    assert write_argv[8] == "111", (
        "guest telegram_id must NOT enter the Keychain allowlist "
        f"(got: {write_argv[8]!r})"
    )
    # A warning naming the excluded guest handle is surfaced.
    combined = (result.output or "") + (result.stderr or "")
    assert "bob" in combined
    assert "NOT admitting" in combined or "guest" in combined.lower()




def test_cli_access_add_rejects_unknown_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(tmp_path, monkeypatch)
    result = runner.invoke(app, ["access", "add", "ghost", "--apply"])
    assert result.exit_code == 2
    assert "ghost" in (result.output + (result.stderr or ""))


def test_cli_access_add_rejects_duplicate_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(tmp_path, monkeypatch, owner_telegram_id=111)
    # Try to add the owner again.
    result = runner.invoke(app, ["access", "add", "alice", "--apply"])
    assert result.exit_code == 2
    assert "already authorized" in (result.output + (result.stderr or "")).lower()


def test_cli_access_add_rejects_owner_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(
        tmp_path, monkeypatch,
        owner_telegram_id=111, extra_humans=[("bob", 222)],
    )
    result = runner.invoke(
        app, ["access", "add", "bob", "--tier", "owner", "--apply"]
    )
    assert result.exit_code == 2
    assert "owner" in (result.output + (result.stderr or "")).lower()


def test_cli_access_remove_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _write_seed_workspace(
        tmp_path, monkeypatch,
        owner_telegram_id=111,
        extra_humans=[("bob", 222)],
        authorized=[("alice", AccessTier.OWNER), ("bob", AccessTier.GUEST)],
    )
    runner_stub = _RecordingRunner()
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "remove", "bob"])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    body = (profile_dir / "access.yaml").read_text(encoding="utf-8")
    assert "bob" in body  # not written
    assert runner_stub.calls == []


def test_cli_access_remove_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _write_seed_workspace(
        tmp_path, monkeypatch,
        keychain_account="alice-acct",
        owner_telegram_id=111,
        extra_humans=[("bob", 222)],
        authorized=[("alice", AccessTier.OWNER), ("bob", AccessTier.GUEST)],
    )
    runner_stub = _RecordingRunner([(0, "", "")])
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(app, ["access", "remove", "bob", "--apply"])
    assert result.exit_code == 0, result.output
    body = (profile_dir / "access.yaml").read_text(encoding="utf-8")
    assert "bob" not in body
    # Keychain rewritten with just the owner.
    assert len(runner_stub.calls) == 1
    assert runner_stub.calls[0]["argv"][8] == "111"


def test_cli_access_remove_refuses_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(tmp_path, monkeypatch)
    result = runner.invoke(app, ["access", "remove", "alice", "--apply"])
    assert result.exit_code == 2
    assert "owner" in (result.output + (result.stderr or "")).lower()


def test_cli_access_remove_rejects_unknown_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_workspace(tmp_path, monkeypatch)
    result = runner.invoke(app, ["access", "remove", "ghost", "--apply"])
    assert result.exit_code == 2
    assert "not currently authorized" in (
        result.output + (result.stderr or "")
    ).lower()


# ---------------------------------------------------------------------------
# Per-profile keychain-account isolation
# ---------------------------------------------------------------------------


def test_per_profile_keychain_account_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two profiles with distinct `keychain_account` values must write to
    two distinct Keychain slots — the -a arg reflects each profile's own
    account, not a shared default.

    This is the load-bearing property that makes a multi-agent Mac safe:
    if two co-existing profiles collapsed to `-a mineru`, syncing profile
    B would overwrite profile A's slot and re-authorize A's daemon with
    B's user set.
    """
    # Materialize workspace with TWO profiles + one humans.yaml.
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))

    (tmp_path / "humans.yaml").write_text(
        yaml.safe_dump(
            {"humans": {
                "alice": {"telegram_id": 1001, "display_name": "Alice"},
                "bob":   {"telegram_id": 2002, "display_name": "Bob"},
            }},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    def _write_profile(name: str, keychain_account: str, owner: str) -> None:
        d = tmp_path / name
        d.mkdir()
        (d / "profile.yaml").write_text(
            f"name: {name}\n"
            f"display_name: {name.capitalize()}\n"
            "assistant_name: TestBot\n"
            "timezone: America/Los_Angeles\n"
            f"keychain_account: {keychain_account}\n"
            f"launchd_label_prefix: com.{name}\n"
            f"workspace_absolute: {tmp_path}/{name}\n"
            f"memory_root: {tmp_path}/{name}/memory\n"
            f"briefs_root: {tmp_path}/{name}/briefs\n"
            "journal_apple_notes_folder: Daily Journals\n"
            "secrets:\n"
            "  backends: [env, keychain]\n"
            f"  env_prefix: {name.upper()}_SECRET_\n",
            encoding="utf-8",
        )
        (d / "access.yaml").write_text(
            f"owner: {owner}\nauthorized:\n  - {{human: {owner}, tier: owner}}\n",
            encoding="utf-8",
        )

    _write_profile("alice-profile", "alice-acct", "alice")
    _write_profile("bob-profile", "bob-acct", "bob")

    # --- sync alice: writes to alice-acct.
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "alice-profile")
    stub_alice = _RecordingRunner([(44, "", ""), (0, "", "")])
    with patch(
        "mineru_cli.access.exporter._default_runner",
        stub_alice,
    ):
        result = runner.invoke(app, ["access", "sync", "--apply"])
    assert result.exit_code == 0, result.output
    write_alice = stub_alice.calls[1]["argv"]
    assert write_alice[4] == "alice-acct"
    assert write_alice[8] == "1001"

    # --- sync bob: writes to bob-acct with a DIFFERENT value.
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "bob-profile")
    stub_bob = _RecordingRunner([(44, "", ""), (0, "", "")])
    with patch(
        "mineru_cli.access.exporter._default_runner",
        stub_bob,
    ):
        result = runner.invoke(app, ["access", "sync", "--apply"])
    assert result.exit_code == 0, result.output
    write_bob = stub_bob.calls[1]["argv"]
    assert write_bob[4] == "bob-acct"
    assert write_bob[8] == "2002"

    # Load-bearing property: neither write ever mentioned the OTHER
    # profile's keychain account or telegram id.
    for arg in write_alice:
        assert "bob" not in arg
        assert "2002" not in arg
    for arg in write_bob:
        assert "alice" not in arg
        assert "1001" not in arg


# ---------------------------------------------------------------------------
# profile init wire-up: post-onboarding preview
# ---------------------------------------------------------------------------


def test_profile_init_previews_landline_keychain_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly-scaffolded profile prints the Landline Keychain allowlist
    preview in dry-run form; the actual `security add-generic-password`
    write never happens from inside `profile init`."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))

    runner_stub = _RecordingRunner()
    # Any call to the exporter's default runner from inside init is a
    # bug — init previews only, never writes.
    with patch(
        "mineru_cli.access.exporter._default_runner",
        runner_stub,
    ):
        result = runner.invoke(
            app,
            [
                "profile", "init",
                "--name", "test-agent",
                "--persona", "TestAgent",
                "--owner-new-handle", "sam",
                "--owner-new-display", "Sam Rivera",
                "--owner-new-telegram", "111",
                "--no-input",
                "--skip-integrations",
            ],
        )
    assert result.exit_code == 0, result.stderr
    # Preview text present.
    assert "Landline Keychain allowlist preview" in result.output
    assert "telegram-allowed-chat-ids" in result.output
    assert "111" in result.output
    # Next-steps block names the explicit sync command.
    assert "mineru access sync" in result.output
    # No subprocess writes to the Keychain from inside init.
    assert runner_stub.calls == []


# ===========================================================================
# 2026-09-16 audit §2A F3 file rename: `humans.yaml` -> `people.yaml`.
# Load-bearing invariant: `resolve_allowlist_ids` (the pure join that
# feeds `format_allowlist_value` and thence the Keychain slot the
# Landline daemon reads) MUST produce byte-identical output regardless
# of which physical filename holds the registry. The daemon reads the
# Keychain slot by service+account, so any drift in the exported wire
# format across the rename would silently break access enforcement.
# These tests pin the invariant with real filesystem fixtures.
# ===========================================================================


def test_keychain_payload_identical_across_people_yaml_and_humans_yaml_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_allowlist_ids` -> `format_allowlist_value` is filename-agnostic.

    Build TWO isolated workspaces with identical YAML contents; one
    writes the CANONICAL `people.yaml`, the other writes the LEGACY
    `humans.yaml`. Load each registry via the default loader (which
    exercises the resolver's fallback logic), resolve the same
    `AccessConfig` against each, and assert the exported Keychain wire
    string is byte-identical.
    """
    body = (
        "humans:\n"
        "  alice:\n"
        "    telegram_id: 111\n"
        "    display_name: Alice\n"
        "  bob:\n"
        "    telegram_id: 222\n"
        "    display_name: Bob\n"
    )

    canonical_ws = tmp_path / "canonical"
    canonical_ws.mkdir()
    (canonical_ws / "people.yaml").write_text(body, encoding="utf-8")

    legacy_ws = tmp_path / "legacy"
    legacy_ws.mkdir()
    (legacy_ws / "humans.yaml").write_text(body, encoding="utf-8")

    # Load each via the default resolver so the fallback is exercised.
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(canonical_ws))
    canonical_reg = load_humans_registry()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(legacy_ws))
    legacy_reg = load_humans_registry()

    # Hand-build a matching AccessConfig for each; owner is alice, bob
    # is OWNER-tier authorized (mimicking a multi-owner household).
    from mineru_cli.access.schema import AccessConfig, AccessEntry, AccessTier

    access = AccessConfig(
        profile_name="jane",
        owner="alice",
        authorized=[
            AccessEntry(human="alice", tier=AccessTier.OWNER),
            AccessEntry(human="bob", tier=AccessTier.OWNER),
        ],
    )
    canonical_ids = resolve_allowlist_ids(access, canonical_reg)
    legacy_ids = resolve_allowlist_ids(access, legacy_reg)
    assert canonical_ids == legacy_ids == [111, 222], (
        f"resolver drift across rename: canonical={canonical_ids}, "
        f"legacy={legacy_ids}"
    )

    canonical_wire = format_allowlist_value(canonical_ids)
    legacy_wire = format_allowlist_value(legacy_ids)
    assert canonical_wire == legacy_wire == "111,222", (
        f"Keychain wire drift across rename: canonical={canonical_wire!r}, "
        f"legacy={legacy_wire!r}"
    )


def test_access_seam_reads_via_people_yaml_when_both_files_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace with BOTH files present reads the canonical `people.yaml`.

    A stale legacy `humans.yaml` left behind by a pre-rename operator
    must not shadow the canonical file for the access-seam load path.
    This is the mirror-refresh contract in reverse: writers refresh
    both files so a reader that happens to walk the fallback path
    sees the same contents; but readers ALWAYS prefer the canonical
    path when it exists.
    """
    # DIFFERENT contents in each file to unambiguously prove the reader
    # picked the canonical one.
    (tmp_path / "people.yaml").write_text(
        "humans:\n"
        "  canonical_alice:\n"
        "    telegram_id: 500\n"
        "    display_name: Alice\n",
        encoding="utf-8",
    )
    (tmp_path / "humans.yaml").write_text(
        "humans:\n"
        "  stale_alice:\n"
        "    telegram_id: 999\n"
        "    display_name: Stale Alice\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    reg = load_humans_registry()
    assert reg.handles() == ["canonical_alice"]
    assert 999 not in [h.telegram_id for h in reg]

    from mineru_cli.access.schema import AccessConfig, AccessEntry, AccessTier

    access = AccessConfig(
        profile_name="p",
        owner="canonical_alice",
        authorized=[AccessEntry(human="canonical_alice", tier=AccessTier.OWNER)],
    )
    ids = resolve_allowlist_ids(access, reg)
    assert ids == [500]  # NOT 999 — the stale legacy file was ignored.
    assert format_allowlist_value(ids) == "500"
