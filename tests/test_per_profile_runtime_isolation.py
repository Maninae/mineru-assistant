"""Per-profile runtime isolation tests (multi-tenant Arc 2, step 3).

Covers the four connector seams the runtime-isolation spec asks for
and the iMessage enable-gate:

  1. Browser CLI (`bin/browser`) honors MINERU_BROWSER_PORT and
     MINERU_BROWSER_PID_FILE so a second profile's browser server
     stops colliding with the primary's on 9471.
  2. Gog: `run_gog_firewall(args, ctx=ctx)` prepends `--account=<email>`
     when the active profile carries a `google_account`, in the correct
     GLOBAL-flag-before-subcommand position, and every gog-using verb
     routes through the wrapper (no bypass site).
  3. Slack: `run_slack_read` / `run_slack_refresh_users` accept a `ctx`
     and export `SLACK_KEYCHAIN_ACCOUNT` from the profile's
     `keychain_account` into the subprocess env.
  4. `scripts/deliver-output.py` reads `MINERU_KEYCHAIN_ACCOUNT` for
     the Keychain lookup default, and `run_deliver_output(ctx=ctx)`
     exports it from the profile.
  5. Every `mineru imessage <verb>` fails LOUD when the active profile
     has `imessage_enabled: false` (a real privacy leak otherwise —
     macOS has ONE Apple ID / chat.db per user, and a second profile
     invoking any imessage verb would silently read the owner's
     message history).

Discipline: EVERY subprocess boundary is mocked. No live Keychain
access. No live Telegram / Slack / gog / iMessage traffic. No live
`~/.mineru`. Two-profile invariants mirror `tests/test_profile_namespacing.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.profile import Profile, load_active_profile
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
    ProfileError,
)
from mineru_cli.wrappers import gog_firewall as gf_wrapper
from mineru_cli.wrappers import slack_read as sr_wrapper
from mineru_cli.wrappers import slack_refresh_users as sru_wrapper
from mineru_cli.wrappers import deliver_output as do_wrapper
from mineru_cli.wrappers.gog_firewall import (
    _inject_account_flag,
    run_gog_firewall,
)
from mineru_cli.wrappers.slack_read import run_slack_read
from mineru_cli.wrappers.slack_refresh_users import run_slack_refresh_users
from mineru_cli.wrappers.deliver_output import run_deliver_output


# ---------------------------------------------------------------------------
# Shared fixtures + helpers
# ---------------------------------------------------------------------------


class _StubProfile:
    """Duck-typed stand-in for `Profile` in wrapper-level unit tests.

    The wrappers only read `.google_account` and `.keychain_account`
    off the profile via `getattr`, so an object with those attributes
    is enough — we don't need the full frozen dataclass here.
    """

    def __init__(
        self,
        *,
        google_account: Optional[str] = None,
        keychain_account: Optional[str] = None,
        imessage_enabled: bool = True,
    ) -> None:
        self.google_account = google_account
        self.keychain_account = keychain_account
        self.imessage_enabled = imessage_enabled


class _Ctx:
    """Duck-typed Typer.Context for wrapper-level unit tests."""

    def __init__(self, obj: Optional[Dict[str, Any]] = None) -> None:
        self.obj = obj


class _CompletedStub:
    """Mimics subprocess.CompletedProcess but only exposes returncode."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _make_run_recorder(returncode: int = 0):
    """Return (fake_run, calls_list)."""
    calls: List[Dict[str, Any]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": list(cmd), "args": args, "kwargs": kwargs})
        return _CompletedStub(returncode)

    return fake_run, calls


def _write_profile(
    base: Path,
    name: str,
    *,
    google_account: Optional[str] = None,
    imessage_enabled: Optional[bool] = None,
    **overrides: str,
) -> Path:
    """Materialize a two-profile-shaped profile.yaml under `<base>/<name>/`."""
    lines = [
        f"name: {name}",
        f"display_name: {name.capitalize()}",
        "assistant_name: TestBot",
        "timezone: America/Los_Angeles",
        f"keychain_account: {name}-kc",
        f"launchd_label_prefix: com.{name}",
        f"workspace_absolute: /tmp/{name}-ws",
        f"memory_root: /tmp/{name}-ws/memory",
        f"briefs_root: /tmp/{name}-ws/briefs",
        "journal_apple_notes_folder: Daily Journals",
    ]
    for k, v in overrides.items():
        lines.append(f"{k}: {v}")
    if google_account is not None:
        lines.append(f"google_account: {google_account}")
    if imessage_enabled is not None:
        lines.append(f"imessage_enabled: {'true' if imessage_enabled else 'false'}")
    lines.append("secrets:")
    lines.append("  backends: [env, keychain]")
    lines.append(f"  env_prefix: {name.upper()}_SECRET_")
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return profile_dir


# ===========================================================================
# 1. Profile-schema: google_account + imessage_enabled first-class fields
# ===========================================================================


def test_profile_loader_lifts_google_account_to_first_class_field(
    tmp_path: Path,
) -> None:
    """Top-level `google_account:` in profile.yaml -> `Profile.google_account`."""
    _write_profile(tmp_path, "alice", google_account="alice@example.com")
    profile = load_active_profile("alice", base_dir=tmp_path)
    assert profile.google_account == "alice@example.com"
    # And NOT also on extras (single source of truth).
    assert "google_account" not in profile.extras


def test_profile_loader_google_account_absent_defaults_to_none(
    tmp_path: Path,
) -> None:
    """A profile.yaml with no google_account gets None on the field."""
    _write_profile(tmp_path, "bare")
    profile = load_active_profile("bare", base_dir=tmp_path)
    assert profile.google_account is None


def test_profile_loader_google_account_legacy_extras_fallback(
    tmp_path: Path,
) -> None:
    """Older hand-authored files that nested `extras: {google_account: ...}` still load.

    The loader prefers the top-level key but accepts the legacy nested
    location as a fallback so existing installs don't have to migrate.
    """
    body = (
        "name: legacy\n"
        "display_name: Legacy\n"
        "assistant_name: TestBot\n"
        "timezone: America/Los_Angeles\n"
        "keychain_account: legacy-kc\n"
        "launchd_label_prefix: com.legacy\n"
        "workspace_absolute: /tmp/legacy-ws\n"
        "memory_root: /tmp/legacy-ws/memory\n"
        "briefs_root: /tmp/legacy-ws/briefs\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "extras:\n"
        "  google_account: legacy@example.com\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        "  env_prefix: LEGACY_SECRET_\n"
    )
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "profile.yaml").write_text(body, encoding="utf-8")
    profile = load_active_profile("legacy", base_dir=tmp_path)
    assert profile.google_account == "legacy@example.com"


def test_profile_loader_google_account_bad_shape_fails_loud(
    tmp_path: Path,
) -> None:
    _write_profile(tmp_path, "bad", google_account="not-an-email")
    with pytest.raises(ProfileError) as excinfo:
        load_active_profile("bad", base_dir=tmp_path)
    assert "google_account" in str(excinfo.value)


def test_profile_loader_imessage_enabled_default_true(tmp_path: Path) -> None:
    _write_profile(tmp_path, "seed")
    profile = load_active_profile("seed", base_dir=tmp_path)
    assert profile.imessage_enabled is True


def test_profile_loader_imessage_enabled_explicit_false(tmp_path: Path) -> None:
    _write_profile(tmp_path, "nonowner", imessage_enabled=False)
    profile = load_active_profile("nonowner", base_dir=tmp_path)
    assert profile.imessage_enabled is False


def test_profile_loader_imessage_enabled_non_bool_fails_loud(
    tmp_path: Path,
) -> None:
    (tmp_path / "typo").mkdir()
    body = (
        "name: typo\n"
        "display_name: Typo\n"
        "assistant_name: TestBot\n"
        "timezone: America/Los_Angeles\n"
        "keychain_account: typo-kc\n"
        "launchd_label_prefix: com.typo\n"
        "workspace_absolute: /tmp/typo-ws\n"
        "memory_root: /tmp/typo-ws/memory\n"
        "briefs_root: /tmp/typo-ws/briefs\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "imessage_enabled: 'no'\n"  # string, not bool
        "secrets:\n"
        "  backends: [env, keychain]\n"
        "  env_prefix: TYPO_SECRET_\n"
    )
    (tmp_path / "typo" / "profile.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ProfileError) as excinfo:
        load_active_profile("typo", base_dir=tmp_path)
    assert "imessage_enabled" in str(excinfo.value)


# ===========================================================================
# 2. gog --account injection (per-profile Google account)
# ===========================================================================


def test_inject_account_flag_no_account_returns_argv_unchanged() -> None:
    assert _inject_account_flag(None, ["gmail", "search", "x"]) == [
        "gmail", "search", "x",
    ]
    assert _inject_account_flag("", ["gmail", "search", "x"]) == [
        "gmail", "search", "x",
    ]


def test_inject_account_flag_prepends_at_position_zero() -> None:
    """`--account` is a gog GLOBAL flag; MUST land BEFORE the subcommand."""
    argv = _inject_account_flag("alice@example.com", ["gmail", "search", "q"])
    assert argv[0] == "--account=alice@example.com"
    assert argv[1] == "gmail"
    assert argv[2] == "search"


def test_inject_account_flag_respects_operator_override_split_form() -> None:
    """An operator-supplied `--account VAL` wins; we don't double-inject."""
    argv = _inject_account_flag(
        "alice@example.com", ["--account", "override@example.com", "gmail", "search"]
    )
    assert argv == ["--account", "override@example.com", "gmail", "search"]


def test_inject_account_flag_respects_operator_override_equals_form() -> None:
    argv = _inject_account_flag(
        "alice@example.com", ["--account=override@example.com", "gmail", "search"]
    )
    assert argv == ["--account=override@example.com", "gmail", "search"]


def test_run_gog_firewall_injects_account_from_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_gog_firewall(args, ctx=ctx)` prepends `--account=` from the profile."""
    monkeypatch.setenv("MINERU_GOG_FIREWALL_BIN", "/tmp/fake/gog-firewall")
    ctx = _Ctx(obj={"profile_obj": _StubProfile(google_account="alice@example.com")})
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_gog_firewall(["gmail", "search", "q"], ctx=ctx)
    assert len(calls) == 1
    cmd = calls[0]["cmd"]
    # argv[0] is the firewall binary; argv[1] is the injected --account=;
    # argv[2] is the subcommand.
    assert cmd[0].endswith("gog-firewall")
    assert cmd[1] == "--account=alice@example.com"
    assert cmd[2] == "gmail"
    assert cmd[3] == "search"


def test_run_gog_firewall_omits_account_when_profile_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile with no google_account -> no --account injected."""
    monkeypatch.setenv("MINERU_GOG_FIREWALL_BIN", "/tmp/fake/gog-firewall")
    ctx = _Ctx(obj={"profile_obj": _StubProfile(google_account=None)})
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_gog_firewall(["gmail", "search", "q"], ctx=ctx)
    cmd = calls[0]["cmd"]
    for token in cmd:
        assert not token.startswith("--account"), (
            f"unexpected --account injection: {cmd!r}"
        )
    assert cmd[1:] == ["gmail", "search", "q"]


def test_run_gog_firewall_no_ctx_omits_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling without ctx keeps the argv untouched — back-compat with legacy callers."""
    monkeypatch.setenv("MINERU_GOG_FIREWALL_BIN", "/tmp/fake/gog-firewall")
    with patch.object(gf_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_gog_firewall(["gmail", "search", "q"])
    cmd = calls[0]["cmd"]
    assert cmd[1:] == ["gmail", "search", "q"]


def test_two_profiles_resolve_to_distinct_gog_accounts(tmp_path: Path) -> None:
    """Two-profile invariant: distinct `google_account` values, distinct `--account=` flags."""
    _write_profile(tmp_path, "alice", google_account="alice@example.com")
    _write_profile(tmp_path, "bob", google_account="bob@example.com")
    alice = load_active_profile("alice", base_dir=tmp_path)
    bob = load_active_profile("bob", base_dir=tmp_path)
    assert alice.google_account != bob.google_account
    argv_alice = _inject_account_flag(alice.google_account, ["gmail", "search"])
    argv_bob = _inject_account_flag(bob.google_account, ["gmail", "search"])
    assert argv_alice[0] == "--account=alice@example.com"
    assert argv_bob[0] == "--account=bob@example.com"


def test_no_gog_bypass_site_every_gog_verb_routes_through_wrapper() -> None:
    """Regression: every gog-using verb file must call the wrapper, never raw `gog`.

    A bypass site is a file under `mineru_cli/verbs/` that references
    the `gog` binary (a raw `gog` subprocess.run, a `/opt/homebrew/bin/gog`
    absolute path, or a bare `"gog"` literal in a subprocess argv) instead
    of importing `run_gog_firewall`. Since a missed callsite would mean a
    profile silently reads the WRONG Google account, we lock the invariant
    in a test — any regression that adds a raw gog callsite fails here.
    """
    verbs_dir = Path(__file__).resolve().parent.parent / "mineru_cli" / "verbs"
    gog_verb_files = [
        "gmail.py",
        "calendar.py",
        "drive.py",
        "docs.py",
        "contacts.py",
        # 2026-09-16 audit §2B: `people.py` verb module renamed to
        # `directory.py`. The canonical + hidden-alias verbs both live
        # in the same file, so a single grep still covers the whole
        # Workspace-directory surface.
        "directory.py",
        "tasks.py",
        "sheets.py",
        "groups.py",
    ]
    for name in gog_verb_files:
        raw = (verbs_dir / name).read_text(encoding="utf-8")
        # Strip triple-quoted docstrings before grepping so prose mentions
        # of `/opt/homebrew/bin/gog` (the read-redacted-email escape hatch,
        # the firewall contract docstring) don't count as bypass sites.
        text = _strip_triple_quoted(raw)
        assert "/opt/homebrew/bin/gog" not in text, (
            f"{name}: code references raw gog binary — bypass site regression"
        )
        # No `subprocess.run(["gog", ...])` shape in code.
        assert "subprocess.run([\"gog\"" not in text, (
            f"{name}: constructs a raw gog subprocess argv — bypass site"
        )
        assert "subprocess.run(['gog'" not in text, (
            f"{name}: constructs a raw gog subprocess argv — bypass site"
        )
    # Belt-and-braces: every code-level `run_gog_firewall(` in the whole
    # verbs tree threads `ctx=ctx`. We count occurrences and assert none
    # of them is left without a ctx keyword arg.
    for name in gog_verb_files:
        text = (verbs_dir / name).read_text(encoding="utf-8")
        # Strip triple-quoted docstrings so mentions inside prose don't
        # count against the invariant.
        stripped_text = _strip_triple_quoted(text)
        callsite_count = stripped_text.count("run_gog_firewall(")
        # Callsite count in code MUST match `ctx=ctx` occurrences, because
        # every code callsite in every gog-using verb file was threaded.
        ctx_count = stripped_text.count("ctx=ctx")
        # Some files have extra `ctx=ctx` uses (e.g. inside other wrapper
        # calls), so we assert LOWER-bound: at least as many `ctx=ctx` as
        # `run_gog_firewall(` callsites.
        assert ctx_count >= callsite_count, (
            f"{name}: {callsite_count} run_gog_firewall calls but only "
            f"{ctx_count} ctx=ctx — a bypass site slipped through."
        )


def _strip_triple_quoted(text: str) -> str:
    """Drop everything inside triple-quoted strings (very simple state machine).

    Sufficient for the mineru_cli.verbs source; no exotic escapes or
    single-quote triple-strings are used.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    in_triple = False
    while i < n:
        if text[i : i + 3] == '"""':
            in_triple = not in_triple
            i += 3
            continue
        if not in_triple:
            out.append(text[i])
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# 2b. bin/gog-firewall internal helpers: the injected `--account=<email>`
#     global-flag prefix MUST be stripped before the argv reaches the
#     read-side pipeline (`is_read_command`, `clean`, `_get_gog_surface`),
#     which key off (noun, verb) at index 0/1. Without this strip, every
#     WRITE with an injected `--account=` prefix would be misclassified as
#     a READ and every per-service cleaner would silently no-op, passing
#     raw un-stripped gog JSON through — see the docstring on
#     `_strip_gog_global_flags` in bin/gog-firewall for the failure mode.
# ---------------------------------------------------------------------------


def _load_bin_gog_firewall_module():
    """Load bin/gog-firewall as an importable module for unit testing.

    The script has no `.py` extension and lives outside the package, so
    we hand SourceFileLoader an explicit path. Loading it also
    side-effect-adds scripts/firewall to sys.path (needed for the
    `from gog_cleaner import ...` line at the top of the script).
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    script_path = Path(__file__).resolve().parent.parent / "bin" / "gog-firewall"
    loader = SourceFileLoader("mineru_bin_gog_firewall", str(script_path))
    spec = importlib.util.spec_from_loader("mineru_bin_gog_firewall", loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_strip_gog_global_flags_drops_account_equals_prefix() -> None:
    """`--account=you@x.com <noun> <verb>` -> `[<noun>, <verb>]`."""
    mod = _load_bin_gog_firewall_module()
    stripped = mod._strip_gog_global_flags(
        ["--account=alice@example.com", "gmail", "get", "msg-id"]
    )
    assert stripped == ["gmail", "get", "msg-id"]


def test_strip_gog_global_flags_drops_split_account_prefix() -> None:
    """`--account you@x.com <noun> <verb>` -> `[<noun>, <verb>]` (two-token form)."""
    mod = _load_bin_gog_firewall_module()
    stripped = mod._strip_gog_global_flags(
        ["--account", "alice@example.com", "gmail", "send", "--to", "x"]
    )
    assert stripped == ["gmail", "send", "--to", "x"]


def test_strip_gog_global_flags_drops_profile_prefix() -> None:
    """`--profile <name>` is also a gog global flag; strip both forms."""
    mod = _load_bin_gog_firewall_module()
    assert mod._strip_gog_global_flags(
        ["--profile=work", "gmail", "search", "q"]
    ) == ["gmail", "search", "q"]
    assert mod._strip_gog_global_flags(
        ["--profile", "work", "gmail", "search", "q"]
    ) == ["gmail", "search", "q"]


def test_strip_gog_global_flags_noop_when_no_prefix() -> None:
    """No leading global-flag -> args returned unchanged."""
    mod = _load_bin_gog_firewall_module()
    assert mod._strip_gog_global_flags(["gmail", "search", "q"]) == [
        "gmail", "search", "q",
    ]


def test_strip_preserves_write_classification_for_prefixed_argv() -> None:
    """REGRESSION (reviewer's must-fix): a WRITE with an injected `--account=` prefix
    must still be classified as a WRITE by is_read_command.

    Without the strip, `is_read_command(["--account=a@b.com","gmail","send",...])`
    returns True (because service='--account=...' is unknown, falls through
    to the fail-safe True), silently sending every write through the read
    pipeline. With the strip, classification behaves identically to the
    unprefixed argv.
    """
    mod = _load_bin_gog_firewall_module()
    from gog_cleaner import is_read_command

    write_argv = ["gmail", "send", "--to", "x@example.com"]
    prefixed = ["--account=alice@example.com"] + write_argv
    # Baseline: bare write argv is correctly classified as a write.
    assert is_read_command(write_argv) is False
    # After strip, classification is preserved for the prefixed argv.
    assert is_read_command(mod._strip_gog_global_flags(prefixed)) is False


def test_strip_preserves_clean_output_for_prefixed_argv() -> None:
    """REGRESSION (reviewer's empirical repro): clean() must strip Gmail
    metadata (threadId, historyId, internalDate, sizeEstimate, payload)
    for both bare and `--account=`-prefixed argvs — otherwise per-profile
    isolation silently passes raw un-stripped gog JSON downstream.
    """
    import json as _json

    mod = _load_bin_gog_firewall_module()
    from gog_cleaner import clean

    # A gmail-get shaped sample. clean_gmail_get drops payload/historyId/
    # internalDate/sizeEstimate and lifts body/headers/id/snippet.
    sample = _json.dumps({
        "body": "hi",
        "headers": {"from": "a@b.com", "subject": "s"},
        "message": {
            "id": "msg1",
            "snippet": "hi there",
            "payload": {"headers": [], "parts": []},
            "historyId": "999",
            "internalDate": "1700000000",
            "sizeEstimate": 1234,
            "labelIds": ["INBOX"],
        },
    })
    get_argv = ["gmail", "get", "msg-id"]
    prefixed = ["--account=alice@example.com"] + get_argv

    bare_cleaned = clean(get_argv, sample)
    # After strip, prefixed argv cleans to the same result as bare.
    prefixed_cleaned = clean(mod._strip_gog_global_flags(prefixed), sample)
    assert bare_cleaned == prefixed_cleaned

    # And the cleaner actually STRIPPED noise (not a pass-through).
    cleaned_obj = _json.loads(bare_cleaned)
    # `message` retains id + snippet, but the noise fields are gone.
    assert "message" in cleaned_obj
    msg = cleaned_obj["message"]
    for noise_field in ("payload", "historyId", "internalDate", "sizeEstimate"):
        assert noise_field not in msg, (
            f"clean_gmail_get failed to strip {noise_field!r}: {msg!r}"
        )


def test_strip_preserves_gog_surface_for_prefixed_argv() -> None:
    """`_get_gog_surface` maps (service, action) at index 0/1 to a boundary
    validator surface name. The `--account=` prefix at index 0 would
    otherwise silently disable the validator for every read.
    """
    mod = _load_bin_gog_firewall_module()

    bare = ["gmail", "search", "q"]
    prefixed = ["--account=alice@example.com"] + bare
    assert mod._get_gog_surface(bare) == mod._get_gog_surface(
        mod._strip_gog_global_flags(prefixed)
    )
    # And the returned surface is the real gmail-search validator, not None.
    assert mod._get_gog_surface(bare) == "gog.gmail_search"


# ===========================================================================
# 3. Slack keychain env (per-profile Slack token)
# ===========================================================================


def test_run_slack_read_exports_slack_keychain_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_SLACK_READ_BIN", "/tmp/fake/slack-read")
    ctx = _Ctx(obj={"profile_obj": _StubProfile(keychain_account="alice-kc")})
    with patch.object(sr_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_read(["C02", "20"], ctx=ctx)
    env = calls[0]["kwargs"].get("env")
    assert env is not None, "env not passed"
    assert env["SLACK_KEYCHAIN_ACCOUNT"] == "alice-kc"
    # The rest of the env is inherited from os.environ.
    for key in os.environ:
        assert key in env


def test_run_slack_read_no_ctx_inherits_ambient_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy callers that don't pass ctx get the ambient env unchanged."""
    monkeypatch.setenv("MINERU_SLACK_READ_BIN", "/tmp/fake/slack-read")
    with patch.object(sr_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_read(["C02", "20"])
    assert "env" not in calls[0]["kwargs"], "should not force an env override"


def test_two_profiles_produce_distinct_slack_keychain_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-profile invariant: distinct SLACK_KEYCHAIN_ACCOUNT per profile."""
    monkeypatch.setenv("MINERU_SLACK_READ_BIN", "/tmp/fake/slack-read")
    _write_profile(tmp_path, "alice")
    _write_profile(tmp_path, "bob")
    alice = load_active_profile("alice", base_dir=tmp_path)
    bob = load_active_profile("bob", base_dir=tmp_path)
    envs: Dict[str, Dict[str, str]] = {}
    for label, profile in (("alice", alice), ("bob", bob)):
        ctx = _Ctx(obj={"profile_obj": profile})
        with patch.object(sr_wrapper, "_binary_available", return_value=True):
            fake_run, calls = _make_run_recorder(returncode=0)
            with patch.object(subprocess, "run", fake_run):
                run_slack_read(["C02"], ctx=ctx)
        envs[label] = calls[0]["kwargs"]["env"]
    assert envs["alice"]["SLACK_KEYCHAIN_ACCOUNT"] == "alice-kc"
    assert envs["bob"]["SLACK_KEYCHAIN_ACCOUNT"] == "bob-kc"
    assert envs["alice"]["SLACK_KEYCHAIN_ACCOUNT"] != envs["bob"]["SLACK_KEYCHAIN_ACCOUNT"]


def test_run_slack_refresh_users_exports_slack_keychain_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_SLACK_REFRESH_USERS_BIN", "/tmp/fake/slack-refresh-users")
    ctx = _Ctx(obj={"profile_obj": _StubProfile(keychain_account="bob-kc")})
    with patch.object(sru_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_slack_refresh_users([], ctx=ctx)
    env = calls[0]["kwargs"].get("env")
    assert env is not None
    assert env["SLACK_KEYCHAIN_ACCOUNT"] == "bob-kc"


# ===========================================================================
# 4. deliver-output.py: MINERU_KEYCHAIN_ACCOUNT env
# ===========================================================================


def test_run_deliver_output_exports_mineru_keychain_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_DELIVER_OUTPUT_BIN", "/tmp/fake/deliver-output.py")
    ctx = _Ctx(obj={"profile_obj": _StubProfile(keychain_account="alice-kc")})
    with patch.object(do_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_deliver_output(["--raw", "hi"], ctx=ctx)
    env = calls[0]["kwargs"].get("env")
    assert env is not None
    assert env["MINERU_KEYCHAIN_ACCOUNT"] == "alice-kc"


def test_run_deliver_output_no_ctx_inherits_ambient_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_DELIVER_OUTPUT_BIN", "/tmp/fake/deliver-output.py")
    with patch.object(do_wrapper, "_binary_available", return_value=True):
        fake_run, calls = _make_run_recorder(returncode=0)
        with patch.object(subprocess, "run", fake_run):
            run_deliver_output(["--raw", "hi"])
    assert "env" not in calls[0]["kwargs"]


def test_deliver_output_script_default_account_honors_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deliver-output.py script's keychain_get default reads MINERU_KEYCHAIN_ACCOUNT.

    Compiles the script's `keychain_get` default via a fresh import
    with the env set; ensures the default account matches the env.
    """
    monkeypatch.setenv("MINERU_KEYCHAIN_ACCOUNT", "alice-kc")
    # Load the script's source and eval the `_DEFAULT_KEYCHAIN_ACCOUNT`
    # line in isolation. Full import would trigger a bunch of side effects
    # (imports Path, etc.), so this narrow check is cleaner.
    repo = Path(__file__).resolve().parent.parent
    src = (repo / "scripts" / "deliver-output.py").read_text(encoding="utf-8")
    ns: Dict[str, Any] = {"os": os}
    for line in src.splitlines():
        if line.startswith("_DEFAULT_KEYCHAIN_ACCOUNT"):
            exec(line, ns)  # noqa: S102 — narrow, script-owned line
            break
    assert ns["_DEFAULT_KEYCHAIN_ACCOUNT"] == "alice-kc"


# ===========================================================================
# 5. iMessage enable gate (C1) — every imessage verb fails loud when disabled
# ===========================================================================


runner = CliRunner()


def _isolate_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def _setup_owner_and_nonowner_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """Two profiles: `owner` (imessage enabled by default) + `nonowner` (disabled)."""
    _isolate_profile_env(monkeypatch)
    workspace_root = tmp_path
    profiles = workspace_root / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    _write_profile(profiles, "owner")
    _write_profile(profiles, "nonowner", imessage_enabled=False)
    for name in ("owner", "nonowner"):
        (Path(f"/tmp/{name}-ws")).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace_root))
    return workspace_root, profiles / "owner", profiles / "nonowner"


# Read verbs (imsg-firewall) and write verbs (imsg) both go through the gate.
_IMESSAGE_READ_INVOCATIONS: List[List[str]] = [
    ["imessage", "chats"],
    ["imessage", "history", "42"],
    ["imessage", "group", "42"],
    ["imessage", "search", "hi"],
    ["imessage", "whois", "--address", "+15551234567"],
    ["imessage", "nickname", "--address", "+15551234567"],
    ["imessage", "status"],
]
_IMESSAGE_WRITE_INVOCATIONS: List[List[str]] = [
    ["imessage", "send", "--to", "+15551234567", "--text", "hi"],
    ["imessage", "react"],
    ["imessage", "edit"],
    ["imessage", "unsend"],
    ["imessage", "delete"],
    ["imessage", "mark-read"],
    ["imessage", "typing"],
    ["imessage", "notify"],
    ["imessage", "chat", "create"],
    ["imessage", "chat", "rename"],
    ["imessage", "chat", "photo"],
    ["imessage", "chat", "add"],
    ["imessage", "chat", "remove"],
    ["imessage", "chat", "leave"],
    ["imessage", "chat", "delete"],
]


@pytest.mark.parametrize(
    "verb_argv",
    _IMESSAGE_READ_INVOCATIONS + _IMESSAGE_WRITE_INVOCATIONS,
    ids=lambda argv: " ".join(argv),
)
def test_imessage_verb_fails_loud_when_profile_has_imessage_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb_argv: List[str]
) -> None:
    """Every imessage verb refuses on a profile with `imessage_enabled: false`."""
    _setup_owner_and_nonowner_profiles(tmp_path, monkeypatch)
    # Patch BOTH wrappers so if the gate ever regresses and the verb tries
    # to shell out, the test surfaces it explicitly (rather than a spurious
    # subprocess error). The wrapper never runs on this path.
    called: List[str] = []

    def fake_read(args, **kwargs):
        called.append("read")
        return 0

    def fake_write(args, **kwargs):
        called.append("write")
        return 0

    with patch(
        "mineru_cli.verbs.imessage.run_imsg_firewall", fake_read
    ), patch("mineru_cli.verbs.imessage.run_imsg", fake_write):
        result = runner.invoke(app, ["--profile", "nonowner", *verb_argv])
    assert result.exit_code != 0, result.output
    assert called == [], f"gate regressed: subprocess would have fired ({called!r})"
    combined = (result.output or "") + (result.stderr or "" if result.stderr_bytes else "")
    assert "imessage_enabled" in combined or "iMessage disabled" in combined, (
        f"error message must name the disabled field: {combined!r}"
    )


def test_imessage_verbs_still_work_on_owner_profile_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate does not over-fire — an enabled profile's read verb still routes."""
    _setup_owner_and_nonowner_profiles(tmp_path, monkeypatch)
    calls: List[List[str]] = []

    def fake_read(args, **kwargs):
        calls.append(list(args))
        return 0

    with patch("mineru_cli.verbs.imessage.run_imsg_firewall", fake_read):
        result = runner.invoke(app, ["--profile", "owner", "imessage", "chats"])
    assert result.exit_code == 0, result.output
    assert calls == [["chats"]]


# ===========================================================================
# 6. Browser CLI (bin/browser) honors env
# ===========================================================================

def bin_browser_header_line_count(src: str) -> int:
    """Lines of bin/browser up to (not including) `SERVER_SCRIPT =`.

    That prefix holds the imports plus the SERVER_URL / PID_FILE definitions
    and is safe to exec; anchoring on a sentinel keeps the probe stable when
    the usage docstring above it grows.
    """
    lines = src.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("SERVER_SCRIPT ="):
            return index
    raise AssertionError("bin/browser no longer defines SERVER_SCRIPT at module level")


def test_bin_browser_reads_env_for_server_url_and_pid_file() -> None:
    """`bin/browser` module-level SERVER_URL + PID_FILE derive from env vars."""
    repo = Path(__file__).resolve().parent.parent
    bin_browser = repo / "bin" / "browser"
    src = bin_browser.read_text(encoding="utf-8")

    # A grep-level invariant: the bare "127.0.0.1:9471" literal and the
    # hardcoded /tmp pid path should NOT appear as plain string assignments
    # anymore. The env knobs are the only source of truth.
    assert 'SERVER_URL = "http://127.0.0.1:9471"' not in src, (
        "bin/browser still has a hardcoded SERVER_URL — regression"
    )
    assert "MINERU_BROWSER_PORT" in src, (
        "bin/browser must consult MINERU_BROWSER_PORT"
    )
    assert "MINERU_BROWSER_PID_FILE" in src, (
        "bin/browser must consult MINERU_BROWSER_PID_FILE"
    )

    # Behavior probe: import-execute the module namespace with env set and
    # unset, and assert SERVER_URL + PID_FILE flip accordingly.
    for env_port, env_pid in (
        (None, None),
        ("9481", "/tmp/mineru-browser-alt.pid"),
    ):
        env: Dict[str, str] = {}
        if env_port is not None:
            env["MINERU_BROWSER_PORT"] = env_port
        if env_pid is not None:
            env["MINERU_BROWSER_PID_FILE"] = env_pid

        # Emulate the first ~50 lines of bin/browser (module-level) — we
        # can't `import` a shebang script cleanly, but we can execute the
        # top of it in a fresh namespace.
        namespace: Dict[str, Any] = {
            "__file__": str(bin_browser),
            "__name__": "__browser_probe__",
        }
        # Prepare an isolated env.
        orig_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update({"PATH": orig_env.get("PATH", "")})
            os.environ.update(env)
            # Read just the first 80 source lines (through the SERVER_URL /
            # PID_FILE definitions); executing the whole file would try to
            # dispatch a CLI.
            head = "\n".join(src.splitlines()[:bin_browser_header_line_count(src)])
            exec(compile(head, str(bin_browser), "exec"), namespace)  # noqa: S102
        finally:
            os.environ.clear()
            os.environ.update(orig_env)

        expected_port = int(env_port) if env_port is not None else 9471
        expected_pid = env_pid or "/tmp/mineru-browser-server.pid"
        assert namespace["SERVER_URL"] == f"http://127.0.0.1:{expected_port}"
        assert namespace["PID_FILE"] == expected_pid


def test_two_profiles_can_bind_distinct_browser_ports_and_pid_files() -> None:
    """Invariant: setting the env twice with different values yields distinct URLs."""
    repo = Path(__file__).resolve().parent.parent
    bin_browser = repo / "bin" / "browser"
    src = bin_browser.read_text(encoding="utf-8")
    head = "\n".join(src.splitlines()[:bin_browser_header_line_count(src)])

    resolved: Dict[str, tuple[str, str]] = {}
    for label, port, pid in (
        ("alice", "9481", "/tmp/mineru-browser-alice.pid"),
        ("bob", "9482", "/tmp/mineru-browser-bob.pid"),
    ):
        orig = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update({"PATH": orig.get("PATH", "")})
            os.environ["MINERU_BROWSER_PORT"] = port
            os.environ["MINERU_BROWSER_PID_FILE"] = pid
            ns: Dict[str, Any] = {
                "__file__": str(bin_browser),
                "__name__": "__browser_probe__",
            }
            exec(compile(head, str(bin_browser), "exec"), ns)  # noqa: S102
        finally:
            os.environ.clear()
            os.environ.update(orig)
        resolved[label] = (ns["SERVER_URL"], ns["PID_FILE"])

    assert resolved["alice"][0] != resolved["bob"][0]
    assert resolved["alice"][1] != resolved["bob"][1]
    assert resolved["alice"] == ("http://127.0.0.1:9481", "/tmp/mineru-browser-alice.pid")
    assert resolved["bob"] == ("http://127.0.0.1:9482", "/tmp/mineru-browser-bob.pid")


# ===========================================================================
# 7. Onboarding: NON-bootstrap profiles get imessage_enabled: false
# ===========================================================================


def test_onboarding_render_profile_yaml_non_bootstrap_writes_imessage_disabled() -> None:
    """`_render_profile_yaml(..., is_bootstrap=False)` sets `imessage_enabled: false`."""
    from mineru_cli.profile.onboarding import ProfileSpec, _render_profile_yaml

    spec = ProfileSpec(
        name="carol",
        persona="Carol",
        owner_handle="carol",
        timezone="America/Los_Angeles",
    )
    body = _render_profile_yaml(
        spec,
        Path("/tmp/carol/memory"),
        Path("/tmp/carol/briefs"),
        Path("/tmp/carol"),
        is_bootstrap=False,
    )
    import yaml as _yaml

    data = _yaml.safe_load(body)
    assert data["imessage_enabled"] is False


def test_onboarding_render_profile_yaml_bootstrap_omits_imessage_field() -> None:
    """The bootstrap (owner) profile renders WITHOUT the field so the loader
    default (True) applies — matching every existing hand-authored profile.yaml."""
    from mineru_cli.profile.onboarding import ProfileSpec, _render_profile_yaml

    spec = ProfileSpec(
        name="alice",
        persona="Alice",
        owner_handle="alice",
        timezone="America/Los_Angeles",
    )
    body = _render_profile_yaml(
        spec,
        Path("/tmp/alice/memory"),
        Path("/tmp/alice/briefs"),
        Path("/tmp/alice"),
        is_bootstrap=True,
    )
    import yaml as _yaml

    data = _yaml.safe_load(body)
    assert "imessage_enabled" not in data


# ===========================================================================
# 8. Two-profile end-to-end: every namespacing field is per-profile
# ===========================================================================


def test_two_profiles_distinct_gog_account_and_slack_env_and_imessage_gate(
    tmp_path: Path,
) -> None:
    """Two profiles must resolve to DISTINCT gog-account, Slack env, imessage state."""
    _write_profile(
        tmp_path, "alice", google_account="alice@example.com"
    )
    _write_profile(
        tmp_path,
        "bob",
        google_account="bob@example.com",
        imessage_enabled=False,
    )
    alice = load_active_profile("alice", base_dir=tmp_path)
    bob = load_active_profile("bob", base_dir=tmp_path)

    # gog account
    assert alice.google_account != bob.google_account
    # Slack keychain namespace
    assert alice.keychain_account != bob.keychain_account
    # iMessage gate
    assert alice.imessage_enabled is True
    assert bob.imessage_enabled is False
