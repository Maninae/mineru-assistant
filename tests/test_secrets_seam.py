"""F7 tests for the pluggable secrets seam.

Focused on the F7 done-criteria contracts (a superset of these lives in
`test_secrets.py`; this file exists to make the F7 claims individually
grep-visible and to exercise the KeychainBackend via a fake `security`
binary on PATH — the F7 done-criteria's stated approach for keeping the
real Keychain out of the test loop):

  - EnvBackend picks up `<prefix><UPPER_SNAKE(name)>`; empty is absent.
  - KeychainBackend runs a fake `security` binary via PATH, reads the
    value from stdout, and never passes the value through argv.
  - OnePasswordBackend raises `NotImplementedError` at get-time with the
    documented message shape, and the resolver walks past it.
  - `env` beats `keychain` in the default chain when both are present.
  - `build_resolver` rejects an unknown backend name.
  - Full CLI: `mineru secrets get` writes only the value (env source),
    and the SecretResolver's chain-order guarantee holds when both
    backends have the same name (env wins).

Why the fake `security` binary via PATH:
  The F7 done-criteria explicitly require no reads of real Keychain
  items. The natural, spoofable seam is the `security` binary itself:
  we drop a fake at `tmp/security`, prepend `tmp/` to PATH, and
  construct KeychainBackend with `binary="security"` (bare name), which
  causes subprocess to resolve it via PATH — hitting our fake, not
  `/usr/bin/security`.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Iterable

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
)


# --- Fake `security` binary helpers ----------------------------------------


def _write_fake_security_binary(
    bin_dir: Path,
    *,
    hit_map: Iterable[tuple[str, str, str]] = (),
    miss_exit: int = 44,
) -> Path:
    """Drop a fake `security` binary at `bin_dir/security`.

    Behavior: parses the `-a <account> -s <name> -w` argv shape
    (`find-generic-password`) and, if the (account, name) is in
    `hit_map`, prints the value on stdout + exits 0. Otherwise exits
    with `miss_exit` (macOS's `SecKeychainItemNotFound` is 44).

    `hit_map` is a tuple of `(account, name, value)`.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script_path = bin_dir / "security"
    # Build a case statement that matches "$acct|$name" -> value. POSIX sh
    # so no bash-isms; simple and portable.
    case_body = "\n".join(
        f'    "{acct}|{name}") printf %s "{value}"; exit 0 ;;'
        for acct, name, value in hit_map
    )
    script_path.write_text(
        "#!/bin/sh\n"
        "# Fake macOS `security` binary for mineru CLI foundation tests (F7).\n"
        "# Only implements the `find-generic-password -a A -s N -w` shape\n"
        "# used by mineru_cli.secrets.KeychainBackend.\n"
        'if [ "$1" != "find-generic-password" ]; then\n'
        '  echo "fake security: unexpected subcommand $1" >&2\n'
        f"  exit {miss_exit}\n"
        'fi\n'
        "# Parse -a ACCOUNT -s NAME -w in any order (mineru only ever passes\n"
        "# them in one order, but be permissive so a future flag reshape\n"
        "# doesn't silently break the fake).\n"
        "acct=\"\"; name=\"\"\n"
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -a) acct="$2"; shift 2 ;;\n'
        '    -s) name="$2"; shift 2 ;;\n'
        '    -w) shift ;;\n'
        '    *) shift ;;\n'
        '  esac\n'
        'done\n'
        'case "$acct|$name" in\n'
        f"{case_body}\n"
        f'  *) exit {miss_exit} ;;\n'
        "esac\n"
    )
    mode = script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    script_path.chmod(mode)
    return script_path


@pytest.fixture
def fake_security_bin_dir(tmp_path: Path) -> Path:
    """A tmp dir with a `security` fake that knows one canned hit.

    (account="mineru", name="my-key") -> "keychain-value"
    Any other lookup exits 44 (miss).
    """
    _write_fake_security_binary(
        tmp_path,
        hit_map=[("mineru", "my-key", "keychain-value")],
    )
    return tmp_path


# --- EnvBackend contracts -------------------------------------------------


def test_env_backend_returns_value_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_SECRET_TELEGRAM_BOT_TOKEN", "value-from-env")
    backend = EnvBackend()
    assert backend.get("telegram-bot-token") == "value-from-env"


def test_env_backend_returns_none_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINERU_SECRET_ANYTHING", raising=False)
    assert EnvBackend().get("anything") is None


def test_env_backend_empty_env_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_SECRET_EMPTY", "")
    assert EnvBackend().get("empty") is None


# --- KeychainBackend contracts (via fake `security` on PATH) --------------


def test_keychain_backend_hit_via_fake_security_on_path(
    fake_security_bin_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeychainBackend hits our fake `security` on PATH and returns the value.

    The F7 done-criteria demand no reads of real Keychain items. Placing
    a fake `security` binary on PATH and constructing the backend with
    `binary="security"` (bare name) triggers PATH resolution inside
    subprocess — hitting the fake, never `/usr/bin/security`.
    """
    # Prepend fake bin dir; the real /usr/bin/security still comes later
    # in PATH but subprocess resolves the first hit.
    monkeypatch.setenv("PATH", f"{fake_security_bin_dir}:{os.environ.get('PATH', '')}")
    backend = KeychainBackend(account="mineru", binary="security")
    assert backend.get("my-key") == "keychain-value"


def test_keychain_backend_miss_via_fake_security(
    fake_security_bin_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A miss from the fake `security` (exit 44) resolves as None (walk on)."""
    monkeypatch.setenv("PATH", f"{fake_security_bin_dir}:{os.environ.get('PATH', '')}")
    backend = KeychainBackend(account="mineru", binary="security")
    assert backend.get("nonexistent-key") is None


def test_keychain_backend_value_never_appears_in_argv(
    fake_security_bin_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: the secret value must never cross argv.

    Directly invoke the fake with the argv the real code would build,
    then confirm the value string is nowhere in that argv. This is the
    same class of leak that the well-known 2022 argv-token incident taught.
    """
    monkeypatch.setenv("PATH", f"{fake_security_bin_dir}:{os.environ.get('PATH', '')}")

    # Instead of introspecting the real KeychainBackend's subprocess call,
    # patch subprocess.run to record what it was invoked with.
    captured: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    backend = KeychainBackend(account="mineru", binary="security")
    got = backend.get("my-key")
    assert got == "keychain-value"
    # Value must not be present anywhere in the argv the wrapper built.
    for cmd in captured:
        for arg in cmd:
            assert "keychain-value" not in arg, (
                f"KeychainBackend leaked value into argv: {cmd!r}"
            )


def test_keychain_backend_missing_security_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing `security` binary is treated as a miss, not a raise.

    We point PATH at an empty dir and use `binary="security"` (bare
    name). subprocess will raise `FileNotFoundError`, which the backend
    swallows and returns None so the resolver walks on.
    """
    empty = Path(os.environ.get("TMPDIR", "/tmp")) / "mineru_f7_empty_path"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
    backend = KeychainBackend(account="mineru", binary="security")
    assert backend.get("anything") is None


# --- OnePasswordBackend contracts (stub) ----------------------------------


def test_onepassword_backend_get_raises_not_implemented() -> None:
    """OnePasswordBackend is a documented stub; `get()` MUST raise.

    The docstring guarantees the raised message names the `op read`
    command template so future maintainers know where to look. This
    test pins that contract.
    """
    backend = OnePasswordBackend()
    with pytest.raises(NotImplementedError) as exc:
        backend.get("anything")
    # The documented message names the op-read command template and the
    # profile-secrets keys, so operators can trace it to the class docstring.
    assert "op read" in str(exc.value)
    assert "documented stub" in str(exc.value)


def test_onepassword_backend_config_key_is_1password() -> None:
    """`build_resolver` matches the profile's `1password` backend key here."""
    assert OnePasswordBackend.CONFIG_KEY == "1password"


def test_onepassword_backend_describe_is_stable_label() -> None:
    """`describe()` returns an audit-safe label — no secret material."""
    assert "1password" in OnePasswordBackend().describe()


def test_resolver_walks_past_onepassword_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stub backend in the chain is a no-op, not a fatal error.

    Chain `[1password, env]` with env populated: resolver walks past
    the stub's `NotImplementedError` and resolves via env.
    """
    monkeypatch.setenv("MINERU_SECRET_LATER_KEY", "from-env")
    resolver = build_resolver(SecretsConfig(backends=["1password", "env"]))
    result = resolver.resolve("later-key")
    assert result.present is True
    assert result.value == "from-env"


# --- Chain precedence: env wins over keychain -----------------------------


def test_env_wins_over_keychain_when_both_present(
    fake_security_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default chain `[env, keychain]`: env is first, env wins.

    The whole point of the env-first ordering is that a shell export
    shadows a Keychain item during CI/dev. If this ever regresses, the
    default overrides would silently stop working.
    """
    # Env has one value.
    monkeypatch.setenv("MINERU_SECRET_MY_KEY", "env-wins")
    # Fake security on PATH has a different value under the same name.
    monkeypatch.setenv("PATH", f"{fake_security_bin_dir}:{os.environ.get('PATH', '')}")
    # Build the resolver with the default backends but point KeychainBackend
    # at the bare-name `security` so it hits the fake.
    resolver = SecretsResolver(
        [
            EnvBackend(env_prefix="MINERU_SECRET_"),
            KeychainBackend(account="mineru", binary="security"),
        ]
    )
    result = resolver.resolve("my-key")
    assert result.value == "env-wins", (
        f"env-first precedence regressed: got {result.value!r}"
    )
    assert "env" in (result.resolved_by or "")


def test_resolver_falls_back_to_keychain_when_env_absent(
    fake_security_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINERU_SECRET_MY_KEY", raising=False)
    monkeypatch.setenv("PATH", f"{fake_security_bin_dir}:{os.environ.get('PATH', '')}")
    resolver = SecretsResolver(
        [
            EnvBackend(env_prefix="MINERU_SECRET_"),
            KeychainBackend(account="mineru", binary="security"),
        ]
    )
    result = resolver.resolve("my-key")
    assert result.value == "keychain-value"
    assert "keychain" in (result.resolved_by or "")


def test_resolver_all_backends_miss_returns_absent_resolution(
    fake_security_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINERU_SECRET_GHOST", raising=False)
    monkeypatch.setenv("PATH", f"{fake_security_bin_dir}:{os.environ.get('PATH', '')}")
    resolver = SecretsResolver(
        [
            EnvBackend(env_prefix="MINERU_SECRET_"),
            KeychainBackend(account="mineru", binary="security"),
        ]
    )
    result = resolver.resolve("ghost")
    assert isinstance(result, SecretResolution)
    assert result.present is False
    assert result.value is None
    assert result.resolved_by is None


def test_build_resolver_rejects_unknown_backend_name() -> None:
    """A typo in `profile.yaml`'s `secrets.backends` fails LOUD.

    A silent misconfiguration (unknown name -> skipped) would give an
    operator a false sense of security about what's in the chain.
    """
    with pytest.raises(ValueError) as exc:
        build_resolver(SecretsConfig(backends=["env", "kychen"]))
    assert "kychen" in str(exc.value)


# --- CLI: `mineru secrets get` end-to-end --------------------------------


runner = CliRunner()


def test_cli_secrets_get_writes_only_the_value_on_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mineru secrets get <name>` writes JUST the value + a trailing \\n."""
    monkeypatch.setenv("MINERU_SECRET_MY_TEST_KEY", "value-x")
    result = runner.invoke(app, ["secrets", "get", "my-test-key"])
    assert result.exit_code == 0
    assert result.stdout == "value-x\n"


def test_cli_secrets_get_missing_exits_2_and_never_leaks_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A miss exits 2 with a hint on stderr; no value material is echoed."""
    monkeypatch.delenv("MINERU_SECRET_ABSENT_KEY", raising=False)
    # Empty PATH so KeychainBackend has no `security` to hit either.
    empty = Path(os.environ.get("TMPDIR", "/tmp")) / "mineru_f7_empty_path"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
    result = runner.invoke(app, ["secrets", "get", "absent-key"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "absent-key" in result.stderr
    assert "no backend resolved" in result.stderr
