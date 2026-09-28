"""End-to-end tests for the legacy shim safety net (F6).

Exercises `bin_shims/msearch` and its shared `_shim_lib.sh` as real
subprocesses. We deliberately do NOT mock at the Python level here:
the whole point of the shim is that it's self-contained Bash with an
import-failure fallback, so the tests must prove the Bash actually does
what it says.

Coverage:
  - Health probe passes; shim exec's `mineru memory <args>` (byte-
    identical stdout to a direct `mineru memory <args>` run).
  - `mineru` missing from PATH; shim falls through to the live original
    binary at `$MINERU_HOME/bin/msearch`.
  - `mineru` on PATH but hangs on `--help`; the 2-second timeout kicks
    in and the shim falls through cleanly. Wall time bounded so a
    regression (hang forever) fails loudly instead of hanging CI.
  - `mineru` on PATH but broken (non-zero exit on `--help`); shim
    falls through to the original binary.
  - Both the shim script and the shared library are executable and
    contain no Python invocation on the fallback path (belt-and-braces).
  - The one-liner authoring pattern: a fresh shim built from a template
    with only two knobs (MINERU_VERB + FALLBACK_BIN) works end-to-end.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SHIMS_DIR = REPO_ROOT / "bin_shims"
SHIM_LIB = SHIMS_DIR / "_shim_lib.sh"
MSEARCH_SHIM = SHIMS_DIR / "msearch"
GOG_FIREWALL_SHIM = SHIMS_DIR / "gog-firewall"
IMSG_FIREWALL_SHIM = SHIMS_DIR / "imsg-firewall"
MONARCH_SHIM = SHIMS_DIR / "monarch"

# The live original binary that the fallback path exec's. If this is
# missing on the test host, skip the fallback-path tests instead of
# failing them — the tests only make sense against a real workstation.
_LIVE_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
LIVE_MSEARCH = _LIVE_MINERU_HOME / "bin" / "msearch"
LIVE_GOG_FIREWALL = _LIVE_MINERU_HOME / "bin" / "gog-firewall"
LIVE_IMSG_FIREWALL = _LIVE_MINERU_HOME / "bin" / "imsg-firewall"
LIVE_MONARCH = _LIVE_MINERU_HOME / "bin" / "monarch"

# Venv-installed mineru we probe for. Located via shutil.which after we
# activate the venv's bin dir on PATH.
VENV_BIN_DIR = REPO_ROOT / ".venv" / "bin"


def _require_venv_mineru() -> str:
    """Return absolute path to the venv mineru or skip if missing."""
    candidate = VENV_BIN_DIR / "mineru"
    if not candidate.exists():
        pytest.skip(f"venv mineru not installed at {candidate}; run `pip install -e .`")
    return str(candidate)


def _path_with_venv() -> str:
    """PATH that includes the venv's bin dir (so `mineru` resolves)."""
    base = os.environ.get("PATH", "/usr/bin:/bin")
    return f"{VENV_BIN_DIR}:{base}"


def _path_without_mineru() -> str:
    """PATH with no `mineru` reachable (poison the CLI lookup)."""
    return "/usr/bin:/bin"


# The engine's own shipped msearch (bin/msearch -> scripts/msearch/msearch).
# Healthy-path shim tests point `MINERU_MSEARCH_BIN` at this so they run
# against the repo's engine, not a live `~/.mineru` install.
REPO_MSEARCH_BIN = REPO_ROOT / "bin" / "msearch"


@pytest.fixture
def synthetic_profile_env(tmp_path: Path) -> dict:
    """Env activating a throwaway profile over a minimal synthetic workspace.

    The healthy shim path runs a REAL `mineru memory tags` — which shells
    out to msearch and indexes the active profile's `workspace_absolute`.
    The engine repo ships no live `current` symlink, and its committed seed
    profile points `workspace_absolute` at a fictional path, so that real
    run has nowhere to index and exits non-zero. Build a tmp profile whose
    workspace holds one tagged memory note, and point msearch at the repo's
    own shipped engine (not the live `~/.mineru`), so the healthy path runs
    green with zero personal data. Mirrors the conftest autouse fixture but
    over a workspace that actually exists on disk.
    """
    workspace = tmp_path / "ws"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "memory" / "note.md").write_text(
        "---\ntags: [sample, demo]\n---\n# Sample note\nhello world\n",
        encoding="utf-8",
    )
    base = tmp_path / "profiles"
    profile_dir = base / "mineru"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.yaml").write_text(
        "name: mineru\n"
        "display_name: Example User\n"
        "assistant_name: Mineru\n"
        "timezone: America/Los_Angeles\n"
        "keychain_account: mineru\n"
        "launchd_label_prefix: com.mineru\n"
        f"workspace_absolute: {workspace}\n"
        f"memory_root: {workspace}/memory\n"
        f"briefs_root: {workspace}\n"
        "journal_apple_notes_folder: Daily Journals\n"
        "secrets:\n"
        "  backends: [env, keychain]\n"
        "  env_prefix: MINERU_SECRET_\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = _path_with_venv()
    env["MINERU_PROFILE"] = "mineru"
    env["MINERU_PROFILE_ROOT"] = str(base)
    env["MINERU_MSEARCH_BIN"] = str(REPO_MSEARCH_BIN)
    return env


# --------------------------------------------------------------- basics ----


def test_shim_files_exist_and_are_executable() -> None:
    assert MSEARCH_SHIM.exists(), f"missing shim: {MSEARCH_SHIM}"
    assert SHIM_LIB.exists(), f"missing shim lib: {SHIM_LIB}"
    for f in (MSEARCH_SHIM, SHIM_LIB):
        mode = f.stat().st_mode
        assert mode & stat.S_IXUSR, f"{f} must be user-executable"


def test_shim_and_lib_are_pure_bash() -> None:
    """Fallback path must not depend on Python (invariant from README).

    Coarse but effective: neither file should shell out to `python`, and
    the shim body itself should not import any interpreter beyond bash.
    """
    for f in (MSEARCH_SHIM, SHIM_LIB):
        text = f.read_text()
        # First shebang line must be bash.
        assert text.splitlines()[0].endswith("bash"), (
            f"{f} shebang must be #!/usr/bin/env bash"
        )
        # No python invocations anywhere.
        for forbidden in ("python3", "python "):
            assert forbidden not in text, (
                f"{f} must be self-contained bash; found {forbidden!r}"
            )


# ------------------------------------------------------- healthy path ----


def test_healthy_shim_matches_mineru_memory_output(
    synthetic_profile_env: dict,
) -> None:
    """`bin_shims/msearch tags` must behave identically to `mineru memory tags`."""
    _require_venv_mineru()
    env = synthetic_profile_env

    shim = subprocess.run(
        [str(MSEARCH_SHIM), "tags", "--count"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    direct = subprocess.run(
        [str(VENV_BIN_DIR / "mineru"), "memory", "tags", "--count"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert shim.returncode == direct.returncode == 0, (
        f"shim rc={shim.returncode}, direct rc={direct.returncode}\n"
        f"shim stderr:\n{shim.stderr}\n"
        f"direct stderr:\n{direct.stderr}"
    )
    assert shim.stdout == direct.stdout, "shim and direct stdout must match byte-for-byte"


# ------------------------------------------------------ fallback path ----


def test_fallback_when_mineru_missing_from_path() -> None:
    """No `mineru` on PATH → shim falls through to the live original binary."""
    if not LIVE_MSEARCH.exists():
        pytest.skip(f"live msearch missing at {LIVE_MSEARCH}; nothing to fall back to")

    env = os.environ.copy()
    env["PATH"] = _path_without_mineru()

    result = subprocess.run(
        [str(MSEARCH_SHIM), "tags", "--count"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"fallback exit={result.returncode}\nstderr:\n{result.stderr}"
    )
    # The live msearch emits JSON with a top-level "mode": "tags".
    assert '"mode": "tags"' in result.stdout


def test_fallback_when_mineru_broken(tmp_path: Path) -> None:
    """`mineru` on PATH but exits non-zero on --help → shim falls through."""
    if not LIVE_MSEARCH.exists():
        pytest.skip(f"live msearch missing at {LIVE_MSEARCH}")

    fake_dir = tmp_path / "broken-mineru-bin"
    fake_dir.mkdir()
    fake = fake_dir / "mineru"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # Simulates a broken mineru wrapper subpackage.
            echo "mineru: fatal import error (test fake)" >&2
            exit 1
            """
        )
    )
    fake.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_dir}:/usr/bin:/bin"

    result = subprocess.run(
        [str(MSEARCH_SHIM), "tags", "--count"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"fallback exit={result.returncode}\nstderr:\n{result.stderr}"
    )
    assert '"mode": "tags"' in result.stdout


def test_fallback_when_mineru_hangs(tmp_path: Path) -> None:
    """`mineru --help` hangs → 2s timeout kicks in, shim falls through cleanly.

    Wall-time-bounded so a broken timeout implementation (hang forever)
    fails this test loudly instead of hanging CI.
    """
    if not LIVE_MSEARCH.exists():
        pytest.skip(f"live msearch missing at {LIVE_MSEARCH}")

    hang_dir = tmp_path / "hanging-mineru-bin"
    hang_dir.mkdir()
    fake = hang_dir / "mineru"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # Simulates a mineru whose --help hangs forever.
            sleep 30
            exit 0
            """
        )
    )
    fake.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{hang_dir}:/usr/bin:/bin"

    started = time.monotonic()
    result = subprocess.run(
        [str(MSEARCH_SHIM), "tags", "--count"],
        env=env,
        capture_output=True,
        text=True,
        # subprocess timeout > shim's own 2s cap + generous fallback exec budget
        # but well under sleep-30. If the shim hangs, subprocess.TimeoutExpired.
        timeout=10,
    )
    elapsed = time.monotonic() - started

    # Health probe caps at 2s; add a small budget for spawning perl,
    # falling through, and running the live msearch. If we're much
    # longer than that, the timeout mechanism regressed.
    assert elapsed < 8, f"shim took {elapsed:.2f}s (2s timeout not enforced?)"
    assert result.returncode == 0, (
        f"fallback exit={result.returncode}\nstderr:\n{result.stderr}"
    )
    assert '"mode": "tags"' in result.stdout


# --------------------------------------- one-liner authoring pattern ----


def test_new_shim_from_template_works(
    tmp_path: Path, synthetic_profile_env: dict
) -> None:
    """The 5-line authoring pattern from the README must actually work.

    We build a mock shim that dispatches to a fake `mineru` (echoes argv)
    with a fake fallback binary (echoes 'fallback'). Both healthy and
    fallback paths should hit their respective targets. This proves the
    shared library is genuinely reusable for future shims (gog-firewall,
    imsg-firewall, etc.) with just the two-knob configuration.
    """
    # Fake fallback binary. Marker in stdout so we can tell paths apart.
    fallback = tmp_path / "fallback-bin"
    fallback.write_text('#!/usr/bin/env bash\necho "FALLBACK_HIT: $*"\n')
    fallback.chmod(0o755)

    # New shim, ~5 lines of real code, matching the README template.
    new_shim = tmp_path / "newshim"
    new_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            SCRIPT_DIR="{SHIMS_DIR}"
            source "$SCRIPT_DIR/_shim_lib.sh"

            MINERU_VERB=(memory tags)
            FALLBACK_BIN={fallback}

            shim_run "$@"
            """
        )
    )
    new_shim.chmod(0o755)

    # --- Healthy path: dispatches to `mineru memory tags ...` ---
    if VENV_BIN_DIR.joinpath("mineru").exists():
        env = synthetic_profile_env
        healthy = subprocess.run(
            [str(new_shim), "--count"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Should look like a real msearch tags result (non-empty stdout,
        # exit 0). We don't compare full output here — this test's job
        # is the pattern, not the msearch semantics.
        assert healthy.returncode == 0, healthy.stderr

    # --- Fallback path: no mineru on PATH, hits our fake fallback ---
    env = os.environ.copy()
    env["PATH"] = _path_without_mineru()
    fallback_run = subprocess.run(
        [str(new_shim), "arg1", "arg2"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert fallback_run.returncode == 0, fallback_run.stderr
    assert "FALLBACK_HIT: arg1 arg2" in fallback_run.stdout


# ---------------------------------------------------- error surfaces -----


def test_shim_lib_errors_when_verb_or_fallback_missing(tmp_path: Path) -> None:
    """A shim that forgets to set MINERU_VERB or FALLBACK_BIN must fail loud."""
    # Missing FALLBACK_BIN.
    broken = tmp_path / "broken-shim-a"
    broken.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            source "{SHIM_LIB}"
            MINERU_VERB=(memory)
            shim_run "$@"
            """
        )
    )
    broken.chmod(0o755)
    result = subprocess.run(
        [str(broken)], capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0
    assert "FALLBACK_BIN" in result.stderr


# ------------------------------- MINERU_SHIM_HEALTH_TIMEOUT validation ----
#
# The env var is interpolated straight into a `bash -c` health probe, so
# validation is load-bearing security AND correctness. Zero disables the
# alarm (`perl -e 'alarm 0'` clears the pending signal) and hands the shim
# an unbounded wait, breaking the file-level "shim NEVER hangs forever"
# invariant. Non-digit input could smuggle a shell metacharacter payload.


def _build_test_shim_dispatch_to_hanging_mineru(
    tmp_path: Path, timeout_value: str
) -> tuple[Path, dict[str, str], float]:
    """Set up a shim + hanging fake mineru + env with MINERU_SHIM_HEALTH_TIMEOUT.

    Returns (shim path, env dict, subprocess.timeout budget). The env
    resolves `mineru` to a hanging script that sleeps 30s on `--help`,
    and points the shim's fallback at a fast echo binary — so a working
    timeout falls through under 8s, while a broken timeout blocks until
    the subprocess timeout kills the test.
    """
    if not LIVE_MSEARCH.exists():
        pytest.skip(f"live msearch missing at {LIVE_MSEARCH}")

    hang_dir = tmp_path / "hanging-mineru-bin"
    hang_dir.mkdir()
    fake = hang_dir / "mineru"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            sleep 30
            exit 0
            """
        )
    )
    fake.chmod(0o755)

    fallback = tmp_path / "fast-fallback"
    fallback.write_text('#!/usr/bin/env bash\necho "FALLBACK_HIT"\n')
    fallback.chmod(0o755)

    shim = tmp_path / "shim-under-test"
    shim.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            source "{SHIM_LIB}"
            MINERU_VERB=(memory)
            FALLBACK_BIN={fallback}
            shim_run "$@"
            """
        )
    )
    shim.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{hang_dir}:/usr/bin:/bin"
    env["MINERU_SHIM_HEALTH_TIMEOUT"] = timeout_value
    # subprocess timeout: generous enough to let the sanitized 2s cap
    # + fallback exec complete, but well under the 30s hang.
    return shim, env, 8.0


def test_shim_health_timeout_zero_snaps_to_default_and_does_not_hang(
    tmp_path: Path,
) -> None:
    """MINERU_SHIM_HEALTH_TIMEOUT=0 must NOT disable the alarm.

    `perl -e 'alarm 0'` clears pending alarms, so if the shim accepted
    `0` verbatim the health probe would block forever on the hanging
    fake `mineru --help`. The lib snaps `0` back to the 2s default and
    falls through cleanly to the fallback.
    """
    shim, env, budget = _build_test_shim_dispatch_to_hanging_mineru(
        tmp_path, timeout_value="0"
    )
    started = time.monotonic()
    result = subprocess.run(
        [str(shim)], env=env, capture_output=True, text=True, timeout=budget
    )
    elapsed = time.monotonic() - started
    assert elapsed < budget - 1, (
        f"MINERU_SHIM_HEALTH_TIMEOUT=0 hung the shim ({elapsed:.2f}s); "
        "the lib must reject 0 and snap to the 2s default."
    )
    assert result.returncode == 0
    assert "FALLBACK_HIT" in result.stdout


def test_shim_health_timeout_shell_injection_payload_is_defused(
    tmp_path: Path,
) -> None:
    """A shell-metacharacter payload in the env var must not execute.

    A hostile / mis-inherited `MINERU_SHIM_HEALTH_TIMEOUT='2; touch /tmp/pwn'`
    must snap to the default (non-digits present) and the sentinel file
    must never appear.
    """
    sentinel = tmp_path / "pwn-marker"
    payload = f"2; touch {sentinel}"
    shim, env, budget = _build_test_shim_dispatch_to_hanging_mineru(
        tmp_path, timeout_value=payload
    )
    result = subprocess.run(
        [str(shim)], env=env, capture_output=True, text=True, timeout=budget
    )
    assert result.returncode == 0
    assert "FALLBACK_HIT" in result.stdout
    assert not sentinel.exists(), (
        "shell-metacharacter payload in MINERU_SHIM_HEALTH_TIMEOUT was "
        "executed — validation regressed."
    )


def test_shim_health_timeout_non_digit_defaults_to_two_seconds(
    tmp_path: Path,
) -> None:
    """Garbage like `abc` must fall back to the 2s default cleanly."""
    shim, env, budget = _build_test_shim_dispatch_to_hanging_mineru(
        tmp_path, timeout_value="abc"
    )
    started = time.monotonic()
    result = subprocess.run(
        [str(shim)], env=env, capture_output=True, text=True, timeout=budget
    )
    elapsed = time.monotonic() - started
    assert elapsed < budget - 1
    assert result.returncode == 0
    assert "FALLBACK_HIT" in result.stdout


# ============================================================================
# P4-06 shims: gog-firewall (polymorphic), imsg-firewall, monarch.
#
# Same three-path coverage as msearch (healthy / fallback / hang) plus the
# polymorphism-specific tests for gog-firewall. Uses the same subprocess
# discipline: no Python-level mocking of the shim path, since the point
# of the safety net is that it's pure Bash.
# ============================================================================


# ------------------------------- files exist + are pure Bash ----


@pytest.mark.parametrize(
    "shim_path",
    [GOG_FIREWALL_SHIM, IMSG_FIREWALL_SHIM, MONARCH_SHIM],
    ids=["gog-firewall", "imsg-firewall", "monarch"],
)
def test_new_shim_files_exist_and_are_executable(shim_path: Path) -> None:
    assert shim_path.exists(), f"missing shim: {shim_path}"
    mode = shim_path.stat().st_mode
    assert mode & stat.S_IXUSR, f"{shim_path} must be user-executable"


@pytest.mark.parametrize(
    "shim_path",
    [GOG_FIREWALL_SHIM, IMSG_FIREWALL_SHIM, MONARCH_SHIM],
    ids=["gog-firewall", "imsg-firewall", "monarch"],
)
def test_new_shims_are_pure_bash(shim_path: Path) -> None:
    """Same invariant as msearch: no Python on the fallback path."""
    text = shim_path.read_text()
    assert text.splitlines()[0].endswith("bash"), (
        f"{shim_path} shebang must be #!/usr/bin/env bash"
    )
    for forbidden in ("python3", "python "):
        assert forbidden not in text, (
            f"{shim_path} must be self-contained bash; found {forbidden!r}"
        )


# ------------------------------------------ healthy path parity ----
#
# For each shim we assert byte-identical stdout to a direct `mineru <verb>`
# call for at least one flavor. `--help` is the smoke test: it never
# touches an engine (so no live network / Keychain / cookies needed),
# always exits 0 on a healthy CLI, and thus tests the shim's routing
# rather than the underlying tool's behavior.


def _healthy_run(argv: list[str], env: dict[str, str] | None = None):
    _require_venv_mineru()
    run_env = os.environ.copy() if env is None else env
    run_env["PATH"] = _path_with_venv()
    return subprocess.run(
        argv, env=run_env, capture_output=True, text=True, timeout=10
    )


def _assert_shim_matches_direct(
    shim_argv: list[str], direct_argv: list[str]
) -> None:
    shim_result = _healthy_run(shim_argv)
    direct_result = _healthy_run(direct_argv)
    assert shim_result.returncode == direct_result.returncode == 0, (
        f"shim rc={shim_result.returncode}, direct rc={direct_result.returncode}\n"
        f"shim stderr:\n{shim_result.stderr}\n"
        f"direct stderr:\n{direct_result.stderr}"
    )
    assert shim_result.stdout == direct_result.stdout, (
        "shim and direct stdout must match byte-for-byte"
    )


def test_healthy_gog_firewall_gmail_matches_mineru_gmail() -> None:
    """`gog-firewall gmail --help` must match `mineru gmail --help`."""
    mineru_bin = _require_venv_mineru()
    _assert_shim_matches_direct(
        [str(GOG_FIREWALL_SHIM), "gmail", "--help"],
        [mineru_bin, "gmail", "--help"],
    )


def test_healthy_gog_firewall_calendar_matches_mineru_calendar() -> None:
    """POLYMORPHIC proof: `gog-firewall calendar --help` → `mineru calendar --help`.

    This is the LANDMINE test: if the shim were the naive `MINERU_VERB=(gmail)`
    form, this would silently misroute `calendar` to the gmail app and diverge.
    """
    mineru_bin = _require_venv_mineru()
    _assert_shim_matches_direct(
        [str(GOG_FIREWALL_SHIM), "calendar", "--help"],
        [mineru_bin, "calendar", "--help"],
    )


def test_healthy_imsg_firewall_matches_mineru_imessage() -> None:
    """`imsg-firewall --help` must match `mineru imessage --help`."""
    mineru_bin = _require_venv_mineru()
    _assert_shim_matches_direct(
        [str(IMSG_FIREWALL_SHIM), "--help"],
        [mineru_bin, "imessage", "--help"],
    )


def test_healthy_monarch_matches_mineru_finance() -> None:
    """`monarch --help` must match `mineru finance --help`."""
    mineru_bin = _require_venv_mineru()
    _assert_shim_matches_direct(
        [str(MONARCH_SHIM), "--help"],
        [mineru_bin, "finance", "--help"],
    )


# ------------------------------- polymorphism: full routing table ----


@pytest.mark.parametrize(
    "verb",
    ["gmail", "calendar", "drive", "docs", "sheets",
     "contacts", "tasks", "people", "groups"],
)
def test_gog_firewall_polymorphic_routing_full_table(verb: str) -> None:
    """Every routing key in the README table must reach the matching mineru noun.

    We compare `gog-firewall <verb> --help` byte-for-byte with
    `mineru <verb> --help`. `--help` is deterministic and cheap, and if
    the shim ever misrouted (dropped arg1, wrong noun, wrong exec), the
    stdout would diverge.
    """
    mineru_bin = _require_venv_mineru()
    _assert_shim_matches_direct(
        [str(GOG_FIREWALL_SHIM), verb, "--help"],
        [mineru_bin, verb, "--help"],
    )


def test_gog_firewall_unknown_verb_falls_through_to_fallback(
    tmp_path: Path,
) -> None:
    """Unknown gog verb → fall through to live fallback with ORIGINAL argv.

    This is the compound-safety-net rule: a route we haven't wired yet
    must not error. We prove it with a fake fallback that echoes its
    args, so we can verify the original argv (including arg1) survives.
    """
    fallback = tmp_path / "fake-gog-firewall"
    fallback.write_text(
        '#!/usr/bin/env bash\n'
        'echo "FALLBACK_HIT: $*"\n'
    )
    fallback.chmod(0o755)

    shim = tmp_path / "shim-under-test"
    shim.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            source "{SHIM_LIB}"
            route() {{
                case "$1" in
                    gmail) ROUTED_VERB=(gmail) ;;
                    *)     ROUTED_VERB=() ;;
                esac
            }}
            FALLBACK_BIN={fallback}
            shim_route "$FALLBACK_BIN" "$@"
            """
        )
    )
    shim.chmod(0o755)

    env = os.environ.copy()
    # Keep mineru on PATH — we want to prove that even when the CLI is
    # healthy, an unknown verb still falls through. Health OK, route
    # miss → fallback. That's the compound-safety-net contract.
    env["PATH"] = _path_with_venv()

    result = subprocess.run(
        [str(shim), "unknown-verb", "sub", "--flag"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"unknown-verb fall-through failed: rc={result.returncode}\n"
        f"stderr:\n{result.stderr}"
    )
    # Original argv (including the unknown verb) must survive verbatim.
    assert "FALLBACK_HIT: unknown-verb sub --flag" in result.stdout


def test_gog_firewall_no_args_falls_through_to_fallback(tmp_path: Path) -> None:
    """`gog-firewall` with zero args must not crash; falls through with empty argv."""
    fallback = tmp_path / "fake-gog-firewall"
    fallback.write_text(
        '#!/usr/bin/env bash\n'
        'echo "FALLBACK_HIT_NOARGS"\n'
    )
    fallback.chmod(0o755)

    shim = tmp_path / "shim-under-test"
    shim.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            source "{SHIM_LIB}"
            route() {{
                case "$1" in
                    gmail) ROUTED_VERB=(gmail) ;;
                    *)     ROUTED_VERB=() ;;
                esac
            }}
            FALLBACK_BIN={fallback}
            shim_route "$FALLBACK_BIN" "$@"
            """
        )
    )
    shim.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = _path_with_venv()
    result = subprocess.run(
        [str(shim)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "FALLBACK_HIT_NOARGS" in result.stdout


# ------------------------------------------ mineru-unhealthy fallback ----
#
# Point PATH away from the venv so `mineru` is unreachable. Each shim
# must reach its live fallback binary. Skip if the fallback is missing
# on this host — the tests only make sense against a real workstation.


def _run_shim_vs_direct(
    shim_argv: list[str], direct_argv: list[str]
) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess]:
    """Run the shim and the live binary directly with the SAME sanitized PATH.

    Both invocations get an identical env with `mineru` NOT reachable —
    which forces the shim's health probe to fail and drop into the
    fallback path. The shim MUST behave exactly like the direct call:
    same exit code, same stdout+stderr. That is the exec-parity contract
    (`exec "$FALLBACK_BIN" "$@"`).
    """
    env = os.environ.copy()
    env["PATH"] = _path_without_mineru()
    shim_result = subprocess.run(
        shim_argv, env=env, capture_output=True, text=True, timeout=15
    )
    direct_result = subprocess.run(
        direct_argv, env=env, capture_output=True, text=True, timeout=15
    )
    return shim_result, direct_result


def _assert_exec_parity(
    shim: subprocess.CompletedProcess, direct: subprocess.CompletedProcess
) -> None:
    """Shim must be byte-identical to a direct fallback call (exec semantics)."""
    assert shim.returncode == direct.returncode, (
        f"exit-code parity broken: shim rc={shim.returncode}, "
        f"direct rc={direct.returncode}\n"
        f"shim stderr:\n{shim.stderr}\n"
        f"direct stderr:\n{direct.stderr}"
    )
    assert shim.stdout == direct.stdout, "stdout parity broken"
    assert shim.stderr == direct.stderr, "stderr parity broken"
    # Sanity: the fallback (not mineru) must have run — mineru's `--help`
    # output always contains "Usage: mineru".
    combined = shim.stdout + shim.stderr
    assert "Usage: mineru" not in combined, (
        "mineru appears to have run instead of the live fallback:\n" + combined
    )


def test_imsg_firewall_fallback_when_mineru_missing() -> None:
    if not LIVE_IMSG_FIREWALL.exists():
        pytest.skip(f"live imsg-firewall missing at {LIVE_IMSG_FIREWALL}")
    shim, direct = _run_shim_vs_direct(
        [str(IMSG_FIREWALL_SHIM), "--help"],
        [str(LIVE_IMSG_FIREWALL), "--help"],
    )
    _assert_exec_parity(shim, direct)


def test_monarch_fallback_when_mineru_missing() -> None:
    if not LIVE_MONARCH.exists():
        pytest.skip(f"live monarch missing at {LIVE_MONARCH}")
    shim, direct = _run_shim_vs_direct(
        [str(MONARCH_SHIM), "--help"],
        [str(LIVE_MONARCH), "--help"],
    )
    _assert_exec_parity(shim, direct)
    # Positive fingerprint: monarch's own Usage banner on stdout.
    assert "Usage: monarch" in shim.stdout


def test_gog_firewall_fallback_when_mineru_missing() -> None:
    """Polymorphic shim, known verb, mineru unhealthy → live fallback.

    Even though the routing table would send `gmail` to `mineru gmail`
    on a healthy CLI, an unhealthy CLI must still fall through with the
    ORIGINAL argv preserved — the live gog-firewall receives
    `gmail --help`, exactly what the user typed.
    """
    if not LIVE_GOG_FIREWALL.exists():
        pytest.skip(f"live gog-firewall missing at {LIVE_GOG_FIREWALL}")
    shim, direct = _run_shim_vs_direct(
        [str(GOG_FIREWALL_SHIM), "gmail", "--help"],
        [str(LIVE_GOG_FIREWALL), "gmail", "--help"],
    )
    _assert_exec_parity(shim, direct)


# ------------------------------------------ 2s hang cap parity ----
#
# Prove the shared health-probe timeout still holds on the three new
# shims: a `mineru --help` that hangs 30s must fall through in under 8s
# (2s probe cap + fallback-exec budget).


def _hang_test_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Build a PATH where `mineru` hangs 30s on any invocation."""
    hang_dir = tmp_path / "hanging-mineru-bin"
    hang_dir.mkdir()
    fake = hang_dir / "mineru"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            sleep 30
            exit 0
            """
        )
    )
    fake.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{hang_dir}:/usr/bin:/bin"
    return env, hang_dir


def _assert_hang_cap(shim_argv: list[str], tmp_path: Path) -> None:
    """Shared assertion: shim must fall through within ~2s + fallback budget.

    Wall-time-bounded so a broken timeout implementation (hang forever)
    fails loudly instead of hanging CI. We don't assert on the fallback's
    exit code here — a live binary may exit non-zero when it can't find
    its own dependencies on the sanitized PATH, and that's fine; the
    invariant under test is the health-probe timeout, not the fallback's
    happy path.
    """
    env, _ = _hang_test_env(tmp_path)
    started = time.monotonic()
    result = subprocess.run(
        shim_argv, env=env, capture_output=True, text=True, timeout=10
    )
    elapsed = time.monotonic() - started
    assert elapsed < 8, f"shim took {elapsed:.2f}s (2s hang cap regressed?)"
    # Sanity: some output happened (the fallback binary actually ran).
    assert (result.stdout + result.stderr).strip(), (
        f"no output from shim or fallback:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_imsg_firewall_shim_honors_2s_hang_cap(tmp_path: Path) -> None:
    if not LIVE_IMSG_FIREWALL.exists():
        pytest.skip(f"live imsg-firewall missing at {LIVE_IMSG_FIREWALL}")
    _assert_hang_cap([str(IMSG_FIREWALL_SHIM), "--help"], tmp_path)


def test_monarch_shim_honors_2s_hang_cap(tmp_path: Path) -> None:
    if not LIVE_MONARCH.exists():
        pytest.skip(f"live monarch missing at {LIVE_MONARCH}")
    _assert_hang_cap([str(MONARCH_SHIM), "--help"], tmp_path)


def test_gog_firewall_shim_honors_2s_hang_cap(tmp_path: Path) -> None:
    """Polymorphic shim path must honor the same 2s health-probe cap."""
    if not LIVE_GOG_FIREWALL.exists():
        pytest.skip(f"live gog-firewall missing at {LIVE_GOG_FIREWALL}")
    _assert_hang_cap([str(GOG_FIREWALL_SHIM), "gmail", "--help"], tmp_path)


# ------------------------------------------ shim_route error surfaces ----


def test_shim_route_errors_when_route_function_missing(tmp_path: Path) -> None:
    """A polymorphic shim that forgets to define `route()` must fail loud."""
    fallback = tmp_path / "fake-fallback"
    fallback.write_text('#!/usr/bin/env bash\necho hi\n')
    fallback.chmod(0o755)

    broken = tmp_path / "broken-polymorphic-shim"
    broken.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            source "{SHIM_LIB}"
            # DELIBERATELY OMITTING `route()` definition.
            shim_route "{fallback}" "$@"
            """
        )
    )
    broken.chmod(0o755)
    result = subprocess.run(
        [str(broken), "gmail"], capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0
    assert "route" in result.stderr.lower()


def test_shim_route_errors_when_called_with_no_fallback(tmp_path: Path) -> None:
    """shim_route needs the fallback path as its first positional arg."""
    broken = tmp_path / "broken-shim-no-fallback"
    broken.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            source "{SHIM_LIB}"
            route() {{ ROUTED_VERB=(); }}
            shim_route
            """
        )
    )
    broken.chmod(0o755)
    result = subprocess.run(
        [str(broken)], capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0
    assert "fallback" in result.stderr.lower()
