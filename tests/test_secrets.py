"""Tests for the pluggable secrets seam (F2 foundation).

Covers:
  - EnvBackend picks up `<prefix><UPPER_SNAKE(name)>` env vars and misses
    on absent.
  - KeychainBackend shells out with the value in stdout (never argv),
    handles non-zero exits as misses, and treats a missing `security`
    binary as a miss. Also: locked keychain (rc 36) surfaces an
    actionable warning; a timed-out `security` returns None with a named
    warning; a non-UTF-8 stored value returns None with a warning; a
    leading-dash name is rejected before argv construction.
  - OnePasswordBackend raises `NotImplementedError` at get-time and is
    walked past by the resolver.
  - SecretsResolver returns the first-hit backend; env wins over
    keychain when both are present.
  - SecretsResolver.audit never populates value.
  - `SecretResolution.value` is redacted in `repr()`, `str()`,
    `f"{r}"`, and `%s` formatting — the last defence against an
    accidental log of a resolution object.
  - `_` inside a secret name is rejected so `env_var_for` is unambiguous.
  - The `mineru secrets get` CLI prints only the value on stdout, exits 2
    on miss with a stderr hint, and never echoes secret material on miss.
  - The `mineru secrets audit --json` CLI returns
    `[{name, resolved_by, present}]` rows with no value key.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.secrets import (
    EnvBackend,
    KeychainBackend,
    OnePasswordBackend,
    SecretResolution,
    SecretsConfig,
    SecretsResolver,
    build_resolver,
    env_var_for,
)


# ----------------------- EnvBackend ------------------------------------


def test_env_var_for_maps_dashes_and_dots_to_underscore() -> None:
    assert env_var_for("telegram-bot-token", "MINERU_SECRET_") == (
        "MINERU_SECRET_TELEGRAM_BOT_TOKEN"
    )
    assert env_var_for("foo.bar", "PFX_") == "PFX_FOO_BAR"
    assert env_var_for("op:vault/item", "X_") == "X_OP_VAULT_ITEM"


def test_env_backend_returns_value_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_SECRET_TELEGRAM_BOT_TOKEN", "hunter2")
    backend = EnvBackend()
    assert backend.get("telegram-bot-token") == "hunter2"


def test_env_backend_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINERU_SECRET_UNSET_KEY", raising=False)
    backend = EnvBackend()
    assert backend.get("unset-key") is None


def test_env_backend_empty_string_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_SECRET_EMPTY_KEY", "")
    backend = EnvBackend()
    assert backend.get("empty-key") is None


def test_env_backend_rejects_bad_names() -> None:
    backend = EnvBackend()
    with pytest.raises(ValueError):
        backend.get("has space")
    with pytest.raises(ValueError):
        backend.get("`rm -rf /`")


def test_env_backend_custom_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_FOO", "bar")
    backend = EnvBackend(env_prefix="CUSTOM_")
    assert backend.get("foo") == "bar"


def test_secret_name_rejects_leading_dash() -> None:
    """Leading `-` would smuggle into argv as an option flag (`-w`, `--foo`).

    The regex requires the first character to be alphanumeric so
    `KeychainBackend.get('-w')` cannot accidentally hand `security` a
    fresh copy of its own `-w` flag or an unknown `--long-flag`.
    """
    backend = EnvBackend()
    for bad in ["-w", "-h", "--foo", "-abc"]:
        with pytest.raises(ValueError):
            backend.get(bad)


def test_secret_name_rejects_underscore() -> None:
    """`_` inside a secret name is reserved (env_var_for folds `-./:` to `_`).

    Rejecting `_` at validation time keeps the env-var mapping
    unambiguous: `foo-bar` and `foo_bar` can never both exist to collide
    on `MINERU_SECRET_FOO_BAR`, because the second is rejected before it
    reaches the backend.
    """
    backend = EnvBackend()
    for bad in ["foo_bar", "_leading", "trailing_"]:
        with pytest.raises(ValueError):
            backend.get(bad)


# ----------------------- KeychainBackend -------------------------------


def _make_keychain_run_stub(return_map: dict):
    """Build a fake `subprocess.run` that maps (account, name) -> (rc, stdout).

    `stdout` in `return_map` is bytes-like (str is auto-encoded); the stub
    matches the real `KeychainBackend.get(text=False)` path which reads
    bytes and decodes them itself. Records every call so tests can assert
    the VALUE never crosses argv.
    """
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": kwargs})
        # cmd shape: [security, find-generic-password, -a, ACCOUNT, -s, NAME, -w]
        account = cmd[3]
        name = cmd[5]
        rc, stdout = return_map.get((account, name), (44, ""))
        if isinstance(stdout, str):
            # `security -w` appends a trailing newline on hit.
            stdout_bytes = (stdout + "\n").encode("utf-8") if stdout else b""
        else:
            stdout_bytes = stdout

        class R:
            returncode = rc
            stdout_data = stdout_bytes

            def __init__(self):
                self.stdout = self.stdout_data
                self.stderr = b""

        return R()

    return fake_run, calls


def test_keychain_backend_hit_returns_value_stripped_of_newline() -> None:
    fake_run, calls = _make_keychain_run_stub({("mineru", "some-secret"): (0, "abc")})
    with patch.object(subprocess, "run", fake_run):
        backend = KeychainBackend(account="mineru")
        assert backend.get("some-secret") == "abc"
    # Argv discipline: value ("abc") never appears in argv.
    for c in calls:
        for token in c["cmd"]:
            assert "abc" not in token, f"value leaked into argv: {c['cmd']}"


def test_keychain_backend_miss_returns_none() -> None:
    fake_run, _ = _make_keychain_run_stub({})  # empty map -> rc 44 for any name
    with patch.object(subprocess, "run", fake_run):
        backend = KeychainBackend(account="mineru")
        assert backend.get("does-not-exist") is None


def test_keychain_backend_missing_binary_returns_none() -> None:
    def raising_run(*args, **kwargs):
        raise FileNotFoundError("no such binary")

    with patch.object(subprocess, "run", raising_run):
        backend = KeychainBackend(binary="/nonexistent/security")
        assert backend.get("anything") is None


def test_keychain_backend_argv_uses_account_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run, calls = _make_keychain_run_stub({("myacct", "widget"): (0, "v")})
    with patch.object(subprocess, "run", fake_run):
        backend = KeychainBackend(account="myacct")
        backend.get("widget")
    assert len(calls) == 1
    cmd = calls[0]["cmd"]
    # Argv shape: [security, find-generic-password, -a, ACCOUNT, -s, NAME, -w]
    assert cmd[-1] == "-w"
    assert cmd[2] == "-a" and cmd[3] == "myacct"
    assert cmd[4] == "-s" and cmd[5] == "widget"


def test_keychain_backend_rejects_bad_names() -> None:
    backend = KeychainBackend()
    with pytest.raises(ValueError):
        backend.get("bad name with space")


def test_keychain_backend_locked_returns_none_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """rc 36 (SecAuthFailed) is DISTINCT from rc 44 (item-not-found).

    The comment block in backends.py has documented this since day one:
    a locked keychain is an actionable state ("unlock your login
    keychain") that must not be silently indistinguishable from a real
    miss. We surface a WARNING-level log naming the item; the resolver
    still walks on so the CLI can proceed.
    """
    fake_run, _ = _make_keychain_run_stub({("mineru", "locked-item"): (36, "")})
    with patch.object(subprocess, "run", fake_run), caplog.at_level(logging.WARNING):
        backend = KeychainBackend(account="mineru")
        result = backend.get("locked-item")
    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log for locked keychain (rc 36)"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "locked" in joined.lower()
    assert "locked-item" in joined


def test_keychain_backend_timeout_returns_none_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A wedged `security` binary must not hang the CLI.

    `subprocess.run(..., timeout=KEYCHAIN_SUBPROCESS_TIMEOUT_SECONDS)`
    raises `TimeoutExpired`; the backend swallows it, emits a WARNING
    naming the item, and returns None so the resolver walks on.
    """

    def timing_out_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=5.0)

    with patch.object(subprocess, "run", timing_out_run), caplog.at_level(logging.WARNING):
        backend = KeychainBackend(account="mineru")
        result = backend.get("wedged-item")
    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log for keychain timeout"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "timed out" in joined.lower()
    assert "wedged-item" in joined


def test_keychain_backend_non_utf8_value_returns_none_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stored non-UTF-8 secret must not silently vanish.

    The old `text=True` path raised `UnicodeDecodeError` at read time and
    got swallowed by the resolver's blanket `except Exception`. Now we
    read bytes and decode explicitly; a decode error yields a WARNING
    naming the item + a None so the operator can diagnose.
    """
    # 0xFF 0xFE is not valid UTF-8; simulate a stored non-UTF-8 blob.
    non_utf8_stdout = b"\xff\xfe\x00\x00\n"
    fake_run, _ = _make_keychain_run_stub(
        {("mineru", "binary-item"): (0, non_utf8_stdout)}
    )
    with patch.object(subprocess, "run", fake_run), caplog.at_level(logging.WARNING):
        backend = KeychainBackend(account="mineru")
        result = backend.get("binary-item")
    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log for non-UTF-8 keychain item"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "utf-8" in joined.lower() or "utf8" in joined.lower()
    assert "binary-item" in joined


def test_keychain_backend_passes_timeout_and_devnull_stdin() -> None:
    """The subprocess call must always carry a timeout and detached stdin.

    These are load-bearing hardening: without `timeout`, a wedged
    `security` blocks the CLI indefinitely; without `stdin=DEVNULL`, a
    subprocess flag path that decided to prompt could still block on it.
    """
    seen = {}

    def spy_run(cmd, *args, **kwargs):
        seen["kwargs"] = kwargs

        class R:
            returncode = 44
            stdout = b""
            stderr = b""

        return R()

    with patch.object(subprocess, "run", spy_run):
        KeychainBackend(account="mineru").get("anything")
    assert "timeout" in seen["kwargs"], "keychain call must set a timeout"
    assert seen["kwargs"]["timeout"] > 0
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL


# ----------------------- OnePasswordBackend ----------------------------


def test_onepassword_backend_raises_not_implemented() -> None:
    backend = OnePasswordBackend()
    with pytest.raises(NotImplementedError) as exc:
        backend.get("anything")
    # Docstring guarantee: names the config key and op command template.
    assert "op read" in str(exc.value)


def test_onepassword_backend_describe_stable() -> None:
    assert "1password" in OnePasswordBackend().describe()


def test_onepassword_config_key_is_1password() -> None:
    assert OnePasswordBackend.CONFIG_KEY == "1password"


# ----------------------- SecretsResolver -------------------------------


class _StaticBackend:
    """Deterministic in-memory backend for resolver-order tests."""

    def __init__(self, label: str, data: dict) -> None:
        self._label = label
        self._data = data

    def get(self, name):
        return self._data.get(name)

    def describe(self) -> str:
        return self._label


def test_resolver_returns_first_hit_in_order() -> None:
    first = _StaticBackend("first", {"foo": "from-first"})
    second = _StaticBackend("second", {"foo": "from-second"})
    resolver = SecretsResolver([first, second])
    got = resolver.resolve("foo")
    assert got.present is True
    assert got.value == "from-first"
    assert got.resolved_by == "first"


def test_resolver_env_wins_over_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    # env has it
    monkeypatch.setenv("MINERU_SECRET_MYKEY", "from-env")
    # keychain also has it (but should NOT win)
    fake_run, _ = _make_keychain_run_stub({("mineru", "mykey"): (0, "from-keychain")})
    with patch.object(subprocess, "run", fake_run):
        resolver = build_resolver(SecretsConfig())  # ["env", "keychain"]
        got = resolver.resolve("mykey")
    assert got.value == "from-env"
    assert got.resolved_by == "env(MINERU_SECRET_*)"


def test_resolver_falls_back_to_keychain_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINERU_SECRET_MYKEY", raising=False)
    fake_run, _ = _make_keychain_run_stub({("mineru", "mykey"): (0, "from-keychain")})
    with patch.object(subprocess, "run", fake_run):
        resolver = build_resolver(SecretsConfig())
        got = resolver.resolve("mykey")
    assert got.value == "from-keychain"
    assert "keychain" in got.resolved_by


def test_resolver_missing_returns_absent_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINERU_SECRET_NOPE", raising=False)
    fake_run, _ = _make_keychain_run_stub({})
    with patch.object(subprocess, "run", fake_run):
        resolver = build_resolver(SecretsConfig())
        got = resolver.resolve("nope")
    assert got.present is False
    assert got.value is None
    assert got.resolved_by is None


def test_resolver_walks_past_onepassword_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_SECRET_ONLY_ENV", "hit")
    resolver = build_resolver(
        SecretsConfig(backends=["1password", "env"])
    )
    got = resolver.resolve("only-env")
    assert got.present is True
    assert got.value == "hit"


def test_resolver_audit_never_populates_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_SECRET_ALPHA", "alpha-value")
    monkeypatch.delenv("MINERU_SECRET_BETA", raising=False)
    fake_run, _ = _make_keychain_run_stub({})
    with patch.object(subprocess, "run", fake_run):
        resolver = build_resolver(SecretsConfig())
        rows = resolver.audit(["alpha", "beta"])
    assert len(rows) == 2
    for row in rows:
        assert row.value is None
    assert rows[0].present is True
    assert rows[0].resolved_by == "env(MINERU_SECRET_*)"
    assert rows[1].present is False
    assert rows[1].resolved_by is None


def test_build_resolver_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError):
        build_resolver(SecretsConfig(backends=["env", "nonsense"]))


# ----------------------- SecretResolution repr/str redaction -----------


SECRET_MARKER = "SUPER-SECRET-TOKEN-DO-NOT-LEAK"


def test_secret_resolution_repr_redacts_value() -> None:
    """`repr(resolution)` must never include the secret payload.

    This is the last defence against an accidental log line like
    `logger.info("got %r", result)` — a very easy foot-gun. The
    dataclass field for `value` is `repr=False` so the auto-generated
    `__repr__` never mentions it.
    """
    r = SecretResolution(
        name="foo",
        present=True,
        resolved_by="env(MINERU_SECRET_*)",
        value=SECRET_MARKER,
    )
    assert SECRET_MARKER not in repr(r)


def test_secret_resolution_str_redacts_value() -> None:
    """`str(resolution)` must never include the payload.

    Overridden `__str__` returns a shape that mirrors the safe repr and
    explicitly shows `<redacted>` for value. Covers `str(r)`, `f"{r}"`,
    and `"%s" % r`.
    """
    r = SecretResolution(
        name="foo",
        present=True,
        resolved_by="env(MINERU_SECRET_*)",
        value=SECRET_MARKER,
    )
    assert SECRET_MARKER not in str(r)
    assert SECRET_MARKER not in f"{r}"
    assert SECRET_MARKER not in ("%s" % r)
    assert "<redacted>" in str(r)


def test_secret_resolution_format_specifiers_redact_value() -> None:
    """`{r!r}` and `{r!s}` also redact.

    Guards against a future log-format string that uses `!r` (which
    calls `repr()`) or `!s` (which calls `str()`) on a resolution.
    """
    r = SecretResolution(
        name="foo", present=True, resolved_by="env", value=SECRET_MARKER
    )
    assert SECRET_MARKER not in f"{r!r}"
    assert SECRET_MARKER not in f"{r!s}"


def test_secret_resolution_to_audit_row_excludes_value_key() -> None:
    """The explicit projection used by the CLI must not carry the value key.

    Distinct from `dataclasses.asdict`, which would include `value=None`
    even on audit rows. `to_audit_row()` produces exactly the JSON shape
    the CLI documents: `{name, present, resolved_by}`.
    """
    r = SecretResolution(
        name="foo", present=True, resolved_by="env", value=SECRET_MARKER
    )
    row = r.to_audit_row()
    assert set(row.keys()) == {"name", "present", "resolved_by"}
    assert SECRET_MARKER not in json.dumps(row)


# ----------------------- CLI: mineru secrets get / audit ----------------


runner = CliRunner()


def test_cli_get_prints_value_only_on_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_SECRET_MY_TEST_KEY", "the-value")
    result = runner.invoke(app, ["secrets", "get", "my-test-key"])
    assert result.exit_code == 0
    # stdout is exactly "the-value\n" — no framing, no prompt, no leaks.
    assert result.stdout == "the-value\n"


def test_cli_get_missing_exits_2_and_hints_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINERU_SECRET_MISSING_KEY", raising=False)
    fake_run, _ = _make_keychain_run_stub({})
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["secrets", "get", "missing-key"])
    assert result.exit_code == 2
    assert result.stdout == ""  # no accidental echo of anything on stdout
    assert "no backend resolved" in result.stderr
    assert "missing-key" in result.stderr


def test_cli_audit_json_returns_rows_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_SECRET_ALPHA", "should-not-appear")
    monkeypatch.delenv("MINERU_SECRET_BETA", raising=False)
    fake_run, _ = _make_keychain_run_stub({})
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["secrets", "audit", "--json", "alpha", "beta"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 2
    # Shape check — exactly {name, resolved_by, present} per row, no value.
    for row in payload:
        assert set(row.keys()) == {"name", "resolved_by", "present"}
    assert payload[0] == {
        "name": "alpha",
        "resolved_by": "env(MINERU_SECRET_*)",
        "present": True,
    }
    assert payload[1] == {"name": "beta", "resolved_by": None, "present": False}
    # And the secret VALUE never appears anywhere in the output.
    assert "should-not-appear" not in result.stdout
    assert "should-not-appear" not in result.stderr


def test_cli_audit_no_names_prints_hint() -> None:
    result = runner.invoke(app, ["secrets", "audit"])
    assert result.exit_code == 0
    assert "no names given" in result.stdout.lower()


def test_cli_audit_text_mode_shows_presence_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_SECRET_ALPHA", "secret-alpha")
    monkeypatch.delenv("MINERU_SECRET_BETA", raising=False)
    fake_run, _ = _make_keychain_run_stub({})
    with patch.object(subprocess, "run", fake_run):
        result = runner.invoke(app, ["secrets", "audit", "alpha", "beta"])
    assert result.exit_code == 0
    assert "alpha" in result.stdout
    assert "present" in result.stdout
    assert "beta" in result.stdout
    assert "MISSING" in result.stdout
    # And the value never appears.
    assert "secret-alpha" not in result.stdout


# ----------------------- Static PII / value-leak grep ------------------


PKG_ROOT = Path(__file__).resolve().parent.parent / "mineru_cli" / "secrets"


def test_no_open_for_write_in_secrets_package() -> None:
    """No code path in the secrets package writes a file.

    We reject any call site that could persist a secret to disk. If a
    future increment needs to persist metadata (never a value), thread
    it through the profile layer instead of adding a write here.
    """
    for py in PKG_ROOT.glob("*.py"):
        text = py.read_text()
        # Reject any pattern that opens a file for writing / appending.
        for bad in ('open(', 'Path.write_text', '.write_text('):
            if bad == 'open(' and 'open(' in text:
                # allow it only if never with a mode that writes
                for line in text.splitlines():
                    if 'open(' in line and any(
                        m in line for m in ('"w"', "'w'", '"a"', "'a'", '"wb"', "'wb'", '"x"', "'x'")
                    ):
                        pytest.fail(f"{py.name}: write-mode open() found: {line!r}")
            elif bad != 'open(' and bad in text:
                pytest.fail(f"{py.name}: forbidden write API: {bad}")


def test_no_logging_of_values_in_secrets_package() -> None:
    """No log/print statement in secrets/*.py mentions a value variable.

    We only log backend `describe()` labels and secret NAMES, never
    `value`, `result.value`, or `completed.stdout`. This is a coarse
    grep: any hit means someone added a leak and needs to justify it.
    """
    forbidden_substrings = [
        "logger.info(value",
        "logger.warning(value",
        "logger.error(value",
        "logger.debug(value",
        "print(value",
        "logger.info(result.value",
        "logger.warning(result.value",
        "print(result.value",
        "logger.info(completed.stdout",
        "print(completed.stdout",
    ]
    for py in PKG_ROOT.glob("*.py"):
        text = py.read_text()
        for bad in forbidden_substrings:
            assert bad not in text, f"{py.name}: potential value-leak in log/print: {bad}"


# ============================================================================
# `secrets ls` -> `secrets list` rename (2026-09-16 audit §2C)
# ============================================================================


def test_secrets_list_canonical_verb_is_registered() -> None:
    """`mineru secrets list` renders help (canonical stub verb post-rename)."""
    from typer.testing import CliRunner as _Runner  # local — file is import-light

    from mineru_cli.app import app as _app

    result = _Runner().invoke(_app, ["secrets", "list", "--help"])
    assert result.exit_code == 0, result.output


def test_secrets_ls_alias_still_emits_deprecation_notice() -> None:
    """The hidden `ls` alias still fires the DEPRECATED notice.

    Post-2026-09-16 (this increment wired `secrets list` for real), the
    alias now dispatches into the REAL `list` body — not the stub — so
    the exit code is whatever `list` would return for the operator's
    environment (0 on a valid profile, 2 on a profile-load miss). The
    load-bearing behavior of the alias is the stderr DEPRECATED line,
    which must fire on every invocation regardless of exit code.
    """
    from typer.testing import CliRunner as _Runner

    from mineru_cli.app import app as _app

    result = _Runner().invoke(_app, ["secrets", "ls"])
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "DEPRECATED:" in combined, combined
    assert "secrets ls" in combined
    assert "secrets list" in combined
