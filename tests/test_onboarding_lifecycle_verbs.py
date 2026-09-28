"""Tests for the 5 onboarding-lifecycle verbs.

Covers:
  - `secrets set <name>`             — writes to Keychain via getpass or
                                       --from-stdin, never echoes value.
  - `secrets list`                    — enumerates KEYCHAIN_SERVICES
                                       names from connectors.yaml, never
                                       reads or prints values.
  - `profile validate`                — schema + required-secrets +
                                       REPLACE_ME placeholder check.
  - `profile export --out <path>`     — portable .tar.gz bundle,
                                       excludes secrets + cache + logs.
  - `profile import <bundle>`         — validates + installs a bundle
                                       into `<profiles_base>/<name>/`,
                                       refuses duplicate names.

All tests use a temporary workspace (`tmp_path` fixture + monkeypatched
MINERU_WORKSPACE_ROOT / MINERU_PROFILE_ROOT) so they NEVER touch the
live workspace. Real subprocess calls to `security` are replaced by
in-memory stubs.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)
from mineru_cli.secrets.writer import (
    KeychainWriteError,
    KeychainWriteResult,
    REDACTED_SENTINEL,
    write_keychain_secret,
)
from mineru_cli.profile.bundle import (
    BundleError,
    EXCLUDED_DIR_NAMES,
    EXCLUDED_FILE_BASENAMES,
    build_export_plan,
    install_bundle_contents,
    open_bundle,
    write_export_archive,
)


runner = CliRunner()


# --------------------------- fixtures --------------------------------------


def _write_profile(base: Path, name: str, **overrides: str) -> Path:
    """Materialize a minimal-required profile.yaml under `<base>/<name>/`."""
    profile_dir = base / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "name": name,
        "display_name": name.capitalize(),
        "assistant_name": "TestBot",
        "timezone": "America/Los_Angeles",
        "keychain_account": f"{name}-acct",
        "launchd_label_prefix": f"com.{name}",
        "workspace_absolute": f"/tmp/{name}-workspace",
        "memory_root": f"/tmp/{name}-workspace/memory",
        "briefs_root": f"/tmp/{name}-workspace/briefs",
        "journal_apple_notes_folder": "Daily Journals",
    }
    body.update(overrides)
    lines = [f"{k}: {v}" for k, v in body.items()]
    lines.append("secrets:")
    lines.append("  backends:")
    lines.append("    - env")
    lines.append("    - keychain")
    lines.append(f"  env_prefix: {name.upper()}_SECRET_")
    (profile_dir / "profile.yaml").write_text("\n".join(lines) + "\n")
    return profile_dir


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every profile-resolution env var at `tmp_path` so tests don't
    touch the real workspace. Sets BOTH workspace root and profiles base
    to `tmp_path` (the legacy-override pattern) so profiles live at
    `tmp_path/<name>/profile.yaml` — matches the `_write_profile` helper
    used throughout `test_profile.py`.

    Returns the profiles base dir (== tmp_path).
    """
    for var in (
        PROFILE_NAME_ENV_VAR,
        PROFILE_BASE_DIR_ENV_VAR,
        WORKSPACE_ROOT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(tmp_path))
    return tmp_path


def _activate_profile(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, name)


def _fake_security_runner(
    return_map: Dict[Tuple[str, str], Tuple[int, str]] = None,
    default: Tuple[int, str] = (0, ""),
    write_hits: Optional[list] = None,
    fail_writes: bool = False,
):
    """Fake `subprocess.run` accepting both `find-` and `add-generic-password`.

    `return_map` maps `(account, name)` → `(rc, stdout)` for READS
    (find-generic-password). WRITES record their (account, name, value)
    triple into `write_hits` (if provided) and return `default_write` unless
    `fail_writes` is True (then rc=1).
    """
    return_map = return_map or {}
    write_hits = write_hits if write_hits is not None else []

    def fake_run(cmd, *args, **kwargs):
        assert isinstance(cmd, list) and len(cmd) >= 7
        op = cmd[1]  # "find-generic-password" or "add-generic-password"

        class R:
            def __init__(self, rc, stdout):
                self.returncode = rc
                self.stdout = stdout if isinstance(stdout, bytes) else stdout.encode()
                self.stderr = b""

        if op == "find-generic-password":
            # argv: [security, find-generic-password, -a, ACCT, -s, NAME, -w]
            account = cmd[3]
            name = cmd[5]
            rc, stdout = return_map.get((account, name), (44, ""))
            if isinstance(stdout, str) and stdout:
                stdout = stdout + "\n"
            return R(rc, stdout)
        if op == "add-generic-password":
            # argv: [security, add-generic-password, -U, -a, ACCT, -s, NAME, -w, VALUE]
            assert cmd[2] == "-U"
            account = cmd[4]
            name = cmd[6]
            value = cmd[8]
            write_hits.append((account, name, value))
            if fail_writes:
                return R(45, "")
            return R(0, "")
        raise AssertionError(f"unexpected security op: {op!r}")

    return fake_run, write_hits


# =========================================================================
# writer.write_keychain_secret — the direct-API tests
# =========================================================================


def test_writer_hit_writes_expected_argv_shape() -> None:
    write_hits: list = []
    fake_run, _ = _fake_security_runner(write_hits=write_hits)
    result = write_keychain_secret(
        name="telegram-bot-token",
        value="hunter2",
        account="acct",
        runner=fake_run,
    )
    assert result.ok is True
    assert result.rc == 0
    assert len(write_hits) == 1
    account, name, value = write_hits[0]
    assert account == "acct"
    assert name == "telegram-bot-token"
    assert value == "hunter2"


def test_writer_argv_shape_redacts_value() -> None:
    """`argv_shape` on the result is safe to log — value goes in as sentinel."""
    write_hits: list = []
    fake_run, _ = _fake_security_runner(write_hits=write_hits)
    r = write_keychain_secret(
        name="foo", value="SUPER-SECRET", account="acct", runner=fake_run
    )
    assert "SUPER-SECRET" not in " ".join(r.argv_shape)
    assert REDACTED_SENTINEL in r.argv_shape
    # Sanity: the sentinel lives in the -w slot (right after "-w").
    assert r.argv_shape[-2] == "-w" and r.argv_shape[-1] == REDACTED_SENTINEL


def test_writer_rejects_empty_value() -> None:
    with pytest.raises(KeychainWriteError) as exc:
        write_keychain_secret(name="foo", value="", account="acct")
    assert "empty" in str(exc.value).lower()


def test_writer_rejects_empty_account() -> None:
    with pytest.raises(KeychainWriteError):
        write_keychain_secret(name="foo", value="v", account="")


def test_writer_rejects_bad_name() -> None:
    with pytest.raises(ValueError):
        write_keychain_secret(name="has space", value="v", account="a")


def test_writer_missing_binary_returns_result_not_raises() -> None:
    def raising(*args, **kwargs):
        raise FileNotFoundError("no security binary")

    r = write_keychain_secret(
        name="foo", value="v", account="a", runner=raising
    )
    assert r.ok is False
    assert r.rc == -1
    assert "not available" in r.stderr_snippet


def test_writer_timeout_returns_result_not_raises() -> None:
    def timing_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=5.0)

    r = write_keychain_secret(
        name="foo", value="v", account="a", runner=timing_out
    )
    assert r.ok is False
    assert r.rc == -2
    assert "timed out" in r.stderr_snippet.lower()


def test_writer_nonzero_rc_is_not_ok() -> None:
    write_hits: list = []
    fake_run, _ = _fake_security_runner(write_hits=write_hits, fail_writes=True)
    r = write_keychain_secret(name="foo", value="v", account="a", runner=fake_run)
    assert r.ok is False
    assert r.rc == 45


def test_writer_uses_devnull_stdin_and_timeout() -> None:
    seen: Dict = {}

    def spy_run(cmd, *args, **kwargs):
        seen["kwargs"] = kwargs

        class R:
            returncode = 0
            stdout = b""
            stderr = b""

        return R()

    write_keychain_secret(
        name="foo", value="v", account="a", runner=spy_run, timeout_seconds=1.5
    )
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL
    assert seen["kwargs"].get("timeout") == 1.5


# =========================================================================
# CLI: mineru secrets set
# =========================================================================


def _prep_active_profile_for_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "alice"
) -> Path:
    _isolate_env(monkeypatch, tmp_path)
    profile_dir = _write_profile(tmp_path, name)
    _activate_profile(monkeypatch, name)
    return profile_dir


def test_cli_secrets_set_from_stdin_writes_value_to_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_active_profile_for_cli(tmp_path, monkeypatch)
    write_hits: list = []
    fake_run, _ = _fake_security_runner(write_hits=write_hits)
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(
            app,
            ["secrets", "set", "telegram-bot-token", "--from-stdin"],
            input="hunter2\n",
        )
    assert result.exit_code == 0, result.output
    assert len(write_hits) == 1
    account, name, value = write_hits[0]
    assert name == "telegram-bot-token"
    assert value == "hunter2"
    assert account == "alice-acct"
    # Verifier hint prints, but no secret material shows up.
    assert "hunter2" not in result.stdout
    assert "hunter2" not in (result.stderr or "")
    assert "audit telegram-bot-token" in result.stdout


def test_cli_secrets_set_from_stdin_strips_exactly_one_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`echo VALUE | ... --from-stdin` and `echo -n VALUE | ...` both write VALUE."""
    _prep_active_profile_for_cli(tmp_path, monkeypatch)
    for stdin_text, expected in [
        ("VALUE\n", "VALUE"),
        ("VALUE", "VALUE"),
        ("VALUE\n\n", "VALUE\n"),
    ]:
        write_hits: list = []
        fake_run, _ = _fake_security_runner(write_hits=write_hits)
        with patch.object(subprocess, "run", fake_run):
            result = runner.invoke(
                app,
                ["secrets", "set", "foo", "--from-stdin"],
                input=stdin_text,
            )
        assert result.exit_code == 0, result.output
        assert write_hits[0][2] == expected, (stdin_text, write_hits[0])


def test_cli_secrets_set_from_stdin_refuses_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_active_profile_for_cli(tmp_path, monkeypatch)
    write_hits: list = []
    fake_run, _ = _fake_security_runner(write_hits=write_hits)
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(
            app,
            ["secrets", "set", "foo", "--from-stdin"],
            input="",
        )
    assert result.exit_code == 2
    assert write_hits == []  # nothing hit the keychain
    combined = result.output + (result.stderr or "")
    assert "empty" in combined.lower()


def test_cli_secrets_set_from_stdin_refuses_bad_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_active_profile_for_cli(tmp_path, monkeypatch)
    fake_run, hits = _fake_security_runner()
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(
            app,
            ["secrets", "set", "has space", "--from-stdin"],
            input="v\n",
        )
    assert result.exit_code == 2
    assert hits == []


def test_cli_secrets_set_from_stdin_reports_keychain_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_active_profile_for_cli(tmp_path, monkeypatch)
    fake_run, _ = _fake_security_runner(fail_writes=True)
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(
            app,
            ["secrets", "set", "foo", "--from-stdin"],
            input="v\n",
        )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "failed to write" in combined.lower()


def test_cli_secrets_set_never_prints_value_on_any_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sweep: success + failure paths never echo the value."""
    _prep_active_profile_for_cli(tmp_path, monkeypatch)
    marker = "DO-NOT-LEAK-XYZ-1234"

    # success
    fake_run, _ = _fake_security_runner()
    with patch.object(subprocess, "run", fake_run):
        r_ok = runner.invoke(
            app, ["secrets", "set", "foo", "--from-stdin"], input=marker
        )
    assert r_ok.exit_code == 0
    assert marker not in r_ok.stdout
    assert marker not in (r_ok.stderr or "")

    # failure at the keychain
    fake_run, _ = _fake_security_runner(fail_writes=True)
    with patch.object(subprocess, "run", fake_run):
        r_fail = runner.invoke(
            app, ["secrets", "set", "foo", "--from-stdin"], input=marker
        )
    assert r_fail.exit_code == 2
    assert marker not in r_fail.stdout
    assert marker not in (r_fail.stderr or "")


# =========================================================================
# CLI: mineru secrets list
# =========================================================================


def _write_connectors(profile_dir: Path, body: str) -> Path:
    p = profile_dir / "connectors.yaml"
    p.write_text(body)
    return p


def test_cli_secrets_list_shows_names_from_connectors_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _prep_active_profile_for_cli(tmp_path, monkeypatch)
    _write_connectors(
        profile_dir,
        """
KEYCHAIN_SERVICES:
  - service: telegram-bot-token
    purpose: Chat bot API token
  - service: webapp-passphrase
    purpose: Web app passphrase gate
""",
    )
    # env sets one so we exercise the "present" branch on one row
    monkeypatch.setenv("ALICE_SECRET_TELEGRAM_BOT_TOKEN", "not-shown")
    fake_run, _ = _fake_security_runner()  # keychain empty
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["secrets", "list"])
    assert result.exit_code == 0, result.output
    assert "telegram-bot-token" in result.stdout
    assert "webapp-passphrase" in result.stdout
    assert "present" in result.stdout
    assert "MISSING" in result.stdout
    assert "Chat bot API token" in result.stdout
    # Value never in the output.
    assert "not-shown" not in result.stdout


def test_cli_secrets_list_json_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _prep_active_profile_for_cli(tmp_path, monkeypatch)
    _write_connectors(
        profile_dir,
        """
KEYCHAIN_SERVICES:
  - service: alpha
    purpose: A
  - service: beta
""",
    )
    fake_run, _ = _fake_security_runner()
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["secrets", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 2
    names = {row["name"] for row in payload}
    assert names == {"alpha", "beta"}
    for row in payload:
        # Value key must be structurally absent — this is a name/presence
        # listing, never a value dump.
        assert "value" not in row
        assert "present" in row


def test_cli_secrets_list_empty_when_no_connectors_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_active_profile_for_cli(tmp_path, monkeypatch)
    fake_run, _ = _fake_security_runner()
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["secrets", "list"])
    assert result.exit_code == 0
    assert "no secrets configured" in result.stdout.lower()


def test_cli_secrets_list_empty_when_block_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _prep_active_profile_for_cli(tmp_path, monkeypatch)
    _write_connectors(profile_dir, "OTHER_KEY: value\n")
    result = runner.invoke(app, ["secrets", "list"])
    assert result.exit_code == 0


def test_cli_secrets_list_never_reads_a_value_from_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `find-generic-password -w` should return VALUE material to us —
    we only care about presence (rc 0 vs 44). Assert we never expose it."""
    profile_dir = _prep_active_profile_for_cli(tmp_path, monkeypatch)
    _write_connectors(
        profile_dir,
        "KEYCHAIN_SERVICES:\n  - service: alpha\n    purpose: p\n",
    )
    marker = "KEYCHAIN-STORED-SUPER-SECRET"
    # Return the marker on find — the audit call still exercises resolve,
    # but the CLI must not print it.
    fake_run, _ = _fake_security_runner(
        return_map={("alice-acct", "alpha"): (0, marker)}
    )
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["secrets", "list"])
    assert result.exit_code == 0
    assert marker not in result.stdout
    assert marker not in (result.stderr or "")


# =========================================================================
# CLI: mineru profile validate
# =========================================================================


def test_cli_profile_validate_ok_when_everything_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _prep_active_profile_for_cli(tmp_path, monkeypatch)
    _write_connectors(
        profile_dir,
        """
KEYCHAIN_SERVICES:
  - service: alpha
    purpose: A
FOO: bar
""",
    )
    monkeypatch.setenv("ALICE_SECRET_ALPHA", "value-not-echoed")
    fake_run, _ = _fake_security_runner()
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["profile", "validate"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.stdout
    assert "value-not-echoed" not in result.stdout


def test_cli_profile_validate_flags_missing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _prep_active_profile_for_cli(tmp_path, monkeypatch)
    _write_connectors(
        profile_dir,
        "KEYCHAIN_SERVICES:\n  - service: absent-one\n    purpose: p\n",
    )
    # env doesn't have it; keychain returns 44 (miss) for every read.
    fake_run, _ = _fake_security_runner()
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["profile", "validate"])
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "absent-one" in combined
    assert "absent" in combined.lower() or "secret" in combined.lower()


def test_cli_profile_validate_flags_replace_me_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _prep_active_profile_for_cli(tmp_path, monkeypatch)
    _write_connectors(
        profile_dir,
        """
TAILSCALE_HOSTNAME: REPLACE_ME
NESTED:
  ok: fine
  broken: replace_me_here
LIST:
  - one
  - REPLACE_ME_2
""",
    )
    fake_run, _ = _fake_security_runner()
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["profile", "validate"])
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "TAILSCALE_HOSTNAME" in combined
    assert "NESTED.broken" in combined
    assert "LIST[1]" in combined


def test_cli_profile_validate_json_reports_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = _prep_active_profile_for_cli(tmp_path, monkeypatch)
    _write_connectors(
        profile_dir,
        "TAILSCALE_HOSTNAME: REPLACE_ME\nKEYCHAIN_SERVICES:\n  - service: missing\n",
    )
    fake_run, _ = _fake_security_runner()
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["profile", "validate", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["profile"] == "alice"
    kinds = {p["kind"] for p in payload["problems"]}
    assert "secret" in kinds
    assert "connectors" in kinds


def test_cli_profile_validate_flags_missing_connectors_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_active_profile_for_cli(tmp_path, monkeypatch)  # no connectors.yaml
    fake_run, _ = _fake_security_runner()
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["profile", "validate"])
    assert result.exit_code == 2


# =========================================================================
# CLI: mineru profile export
# =========================================================================


def _seed_profile_with_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile_dir = _prep_active_profile_for_cli(tmp_path, monkeypatch)
    _write_connectors(profile_dir, "FOO: bar\n")
    (profile_dir / "cron.yaml").write_text("defaults: {}\n")
    (profile_dir / "custom_verbs.yaml").write_text("verbs: []\n")
    # Files that MUST be excluded:
    (profile_dir / "secrets.yaml").write_text("secret: DO_NOT_LEAK\n")
    (profile_dir / ".env").write_text("SECRET=DO_NOT_LEAK\n")
    (profile_dir / "id_ed25519").write_text("PRIVATE_KEY_MATERIAL\n")
    (profile_dir / "notes.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    # Dirs that MUST be pruned:
    (profile_dir / "cache").mkdir()
    (profile_dir / "cache" / "leaked.txt").write_text("cache-leak\n")
    (profile_dir / "logs").mkdir()
    (profile_dir / "logs" / "run.log").write_text("log-leak\n")
    (profile_dir / "secrets").mkdir()
    (profile_dir / "secrets" / "tokens.json").write_text("DO_NOT_LEAK\n")
    return profile_dir


def test_cli_profile_export_writes_tarball_at_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_profile_with_content(tmp_path, monkeypatch)
    out = tmp_path / "bundles" / "alice.tar.gz"
    result = runner.invoke(app, ["profile", "export", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    # Bundle body sanity: it's a real gzipped tar with expected members.
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    # Top-level dir is the profile name.
    assert "alice/profile.yaml" in names
    assert "alice/connectors.yaml" in names
    assert "alice/cron.yaml" in names


def test_cli_profile_export_excludes_secrets_cache_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_profile_with_content(tmp_path, monkeypatch)
    out = tmp_path / "b.tar.gz"
    result = runner.invoke(app, ["profile", "export", "--out", str(out)])
    assert result.exit_code == 0, result.output
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    # None of the excluded files or dirs leaked into the bundle.
    for bad in [
        "alice/secrets.yaml",
        "alice/.env",
        "alice/id_ed25519",
        "alice/notes.pem",
        "alice/cache/leaked.txt",
        "alice/logs/run.log",
        "alice/secrets/tokens.json",
    ]:
        assert bad not in names, f"excluded path leaked into bundle: {bad}"
    # Belt-and-braces: crack the archive open and grep the raw bytes for
    # the marker strings we planted in every excluded file.
    with tarfile.open(out, "r:gz") as tar:
        raw = io.BytesIO()
        tar.extractall(tmp_path / "extracted")
    for planted in [
        tmp_path / "extracted",
    ]:
        if planted.exists():
            for f in planted.rglob("*"):
                if f.is_file():
                    content = f.read_text(errors="ignore")
                    assert "DO_NOT_LEAK" not in content
                    assert "PRIVATE_KEY_MATERIAL" not in content
                    assert "cache-leak" not in content
                    assert "log-leak" not in content


def test_cli_profile_export_writes_owner_only_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_profile_with_content(tmp_path, monkeypatch)
    out = tmp_path / "b.tar.gz"
    out.write_text("stale world-readable bundle\n")
    out.chmod(0o644)
    old_umask = os.umask(0o022)
    try:
        result = runner.invoke(app, ["profile", "export", "--out", str(out)])
    finally:
        os.umask(old_umask)
    assert result.exit_code == 0, result.output
    assert (out.stat().st_mode & 0o777) == 0o600
    # No partial temp file is left beside the bundle.
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".b.tar.gz")) == []


@pytest.mark.parametrize(
    "rel_path",
    [
        "gh_token.txt",
        "API_TOKENS.yaml",
        "browser-cookies.sqlite",
        "session.json",
        "chat_session_state.json",
        "credentials.json",
        "credentials",
        "server.key",
        "cert.PEM",
        "openai_apikey.txt",
        ".env.local",
        "tokens/any.md",
        "cookie_jar/data.bin",
    ],
)
def test_export_plan_excludes_secret_shaped_names(tmp_path: Path, rel_path: str) -> None:
    profile_root = tmp_path / "alice"
    profile_root.mkdir()
    (profile_root / "profile.yaml").write_text("name: alice\n")
    (profile_root / "session_notes.md").write_text("fine\n")
    target = profile_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("DO_NOT_LEAK\n")
    plan = build_export_plan("alice", profile_root, tmp_path / "out.tar.gz")
    assert rel_path not in plan.included
    assert "profile.yaml" in plan.included
    # `*session*.json` is json-only; a markdown note about sessions still travels.
    assert "session_notes.md" in plan.included


def test_cli_profile_export_lists_included_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_profile_with_content(tmp_path, monkeypatch)
    out = tmp_path / "b.tar.gz"
    result = runner.invoke(app, ["profile", "export", "--out", str(out)])
    assert result.exit_code == 0
    assert "profile.yaml" in result.stdout
    assert "connectors.yaml" in result.stdout
    # Excluded summary calls out at least one blocked path with reason.
    assert "excluded" in result.stdout
    assert "secrets.yaml" in result.stdout


def test_cli_profile_export_bundle_roundtrips_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Export → open_bundle → validated contents. Sanity that the format
    the exporter emits is exactly what the importer expects."""
    _seed_profile_with_content(tmp_path, monkeypatch)
    out = tmp_path / "roundtrip.tar.gz"
    r = runner.invoke(app, ["profile", "export", "--out", str(out)])
    assert r.exit_code == 0
    with tempfile.TemporaryDirectory() as td:
        contents = open_bundle(out, Path(td))
        assert contents.profile_name == "alice"
        assert (contents.profile_dir / "profile.yaml").exists()


# =========================================================================
# CLI: mineru profile import
# =========================================================================


def _build_bundle(
    tmp_path: Path,
    profile_name: str,
    *,
    yaml_name_override: Optional[str] = None,
    extra_files: Optional[Dict[str, str]] = None,
) -> Path:
    """Build a valid bundle at `<tmp_path>/<profile_name>.tar.gz`.

    `yaml_name_override` lets a test create a bundle whose profile.yaml
    declares a different name from the top-level dir (to prove the
    importer rejects the mismatch).
    """
    src = tmp_path / f"src-{profile_name}"
    src.mkdir()
    _write_profile(src, yaml_name_override or profile_name)
    (src / (yaml_name_override or profile_name) / "connectors.yaml").write_text(
        "FOO: bar\n"
    )
    if extra_files:
        for rel, content in extra_files.items():
            f = src / (yaml_name_override or profile_name) / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
    # Repack under the ACTUAL top-level dir name (which is `profile_name`
    # regardless of yaml_name_override; that's the mismatch we test).
    stored_dirname = profile_name
    if yaml_name_override and yaml_name_override != profile_name:
        # Rename the on-disk dir before packing so the archive's top
        # level is `profile_name` but its inner profile.yaml says
        # `yaml_name_override`.
        os.rename(src / yaml_name_override, src / stored_dirname)
    out = tmp_path / f"{profile_name}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(src / stored_dirname, arcname=stored_dirname)
    return out


def test_cli_profile_import_installs_bundle_into_profiles_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_base = _isolate_env(monkeypatch, tmp_path)
    # Bundle staging lives OUTSIDE profiles_base to avoid the walker
    # treating it as a stray profile.
    stage = tmp_path / "stage"
    stage.mkdir()
    bundle = _build_bundle(stage, "bob")
    result = runner.invoke(app, ["profile", "import", str(bundle)])
    assert result.exit_code == 0, result.output
    installed = profiles_base / "bob"
    assert installed.is_dir()
    assert (installed / "profile.yaml").exists()
    assert (installed / "connectors.yaml").exists()
    assert "activate with" in result.stdout


def test_cli_profile_import_refuses_duplicate_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_base = _isolate_env(monkeypatch, tmp_path)
    # Pre-existing profile with the same name on the target machine.
    _write_profile(profiles_base, "bob")
    stage = tmp_path / "stage"
    stage.mkdir()
    bundle = _build_bundle(stage, "bob")
    result = runner.invoke(app, ["profile", "import", str(bundle)])
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "already exists" in combined.lower() or "refusing" in combined.lower()


def test_cli_profile_import_rejects_top_level_yaml_name_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    # Top-level dir = "bob", but internal profile.yaml declares name: "eve".
    bundle = _build_bundle(stage, "bob", yaml_name_override="eve")
    result = runner.invoke(app, ["profile", "import", str(bundle)])
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "name" in combined.lower()


def test_cli_profile_import_rejects_missing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["profile", "import", str(tmp_path / "nope.tar.gz")]
    )
    assert result.exit_code == 2


def test_cli_profile_import_rejects_path_traversal_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    # Hand-craft a malicious archive that tries to write outside its root.
    stage = tmp_path / "stage"
    stage.mkdir()
    bundle = stage / "evil.tar.gz"
    # A file named `../evil.txt` at the archive root — path traversal.
    payload = b"evil"
    with tarfile.open(bundle, "w:gz") as tar:
        info = tarfile.TarInfo(name="../evil.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    result = runner.invoke(app, ["profile", "import", str(bundle)])
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "traversal" in combined.lower() or "escape" in combined.lower()


def test_cli_profile_import_rejects_bundle_without_profile_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    stage = tmp_path / "stage"
    (stage / "src" / "eve").mkdir(parents=True)
    (stage / "src" / "eve" / "random.txt").write_text("no profile yaml here\n")
    bundle = stage / "empty.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(stage / "src" / "eve", arcname="eve")
    result = runner.invoke(app, ["profile", "import", str(bundle)])
    assert result.exit_code == 2


def test_cli_profile_export_import_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full round-trip. Export from workspace A, import into workspace B."""
    # -------- workspace A: seed and export --------
    workspace_a = tmp_path / "A"
    workspace_a.mkdir()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace_a))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(workspace_a))
    monkeypatch.setenv(PROFILE_NAME_ENV_VAR, "alice")
    profile_dir = _write_profile(workspace_a, "alice")
    _write_connectors(profile_dir, "FOO: bar\n")
    (profile_dir / "cron.yaml").write_text("defaults: {}\n")
    out = tmp_path / "alice.tar.gz"
    r_exp = runner.invoke(app, ["profile", "export", "--out", str(out)])
    assert r_exp.exit_code == 0, r_exp.output

    # -------- workspace B: import fresh --------
    workspace_b = tmp_path / "B"
    workspace_b.mkdir()
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(workspace_b))
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(workspace_b))
    monkeypatch.delenv(PROFILE_NAME_ENV_VAR, raising=False)
    r_imp = runner.invoke(app, ["profile", "import", str(out)])
    assert r_imp.exit_code == 0, r_imp.output
    installed = workspace_b / "alice"
    assert (installed / "profile.yaml").exists()
    assert (installed / "connectors.yaml").exists()
    assert (installed / "cron.yaml").exists()
    # Verify the imported profile.yaml is byte-equivalent to the source.
    src_yaml = (profile_dir / "profile.yaml").read_text()
    dst_yaml = (installed / "profile.yaml").read_text()
    assert src_yaml == dst_yaml
