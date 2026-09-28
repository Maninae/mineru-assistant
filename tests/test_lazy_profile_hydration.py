"""Regression tests for the F3 lazy-hydration profile redesign (2026-08-28).

The previous eager-hydration design had a subtle bypass. The root
callback tried to skip profile loading on `--help` invocations via a
heuristic that scanned argv for the token `--help`. That heuristic
could not distinguish a help FLAG from an option VALUE, so any option
whose value happened to be the string `--help` (`--caption "--help"`,
`--label "--help"`, `--chat-id "-h"`, etc.) silently skipped profile
validation. On `mineru --profile bogus telegram photo /tmp/x --caption
"--help"` the verb fell through to the DEFAULT profile — the send
actually fired, the sha256'd bytes landed under
`$MINERU_HOME/cache/telegram_sent_images/` (the operator's live workspace), and
the transport used the default Keychain account. A real cross-profile
isolation leak that also masked a bogus-profile shell as success.

The redesign moves profile hydration off the callback entirely (see
`mineru_cli.profile.get_profile`). Click renders `--help` BEFORE the
verb callback runs, so a verb's `get_profile(ctx)` is never reached on
a help path (by construction) and a real command with a bogus profile
fails loud at the first verb-side call. These tests pin the fix:

  1. `mineru --profile bogus telegram photo <tmp> --caption "--help"`
     (+ variants with `-h`, `--label "--help"`, `--chat-id "-h"`, and
     a positional token that looks like `--help`) exits NON-zero and
     writes NOTHING under `$MINERU_HOME`.
  2. `mineru --help`, `mineru <noun> --help`, `mineru <noun> <verb>
     --help` still render on a fresh clone (already covered elsewhere,
     re-pinned here so a regression fires in this file too).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app


runner = CliRunner()


# The live workspace the F3 leak wrote into. Any regression that
# re-opens the bypass would create a file under this root.
LIVE_MINERU_ROOT = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))


@pytest.fixture
def sample_photo(tmp_path: Path) -> Path:
    p = tmp_path / "regression-probe.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0MINERU_F3_REGRESSION_PROBE_v1")
    return p


@pytest.fixture(autouse=True)
def no_writes_to_live_mineru(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prefix-based guard: any `Path.mkdir` under $MINERU_HOME fails the test.

    Mirrors `no_writes_to_live_cache_dir` in `test_telegram_image_cache.py`
    but broadened to the entire `$MINERU_HOME` tree (the F3 leak could have
    written a bytes file, a sidecar, or an inject-queue entry — the
    exact subpath varied by verb, so we guard the whole root).
    """
    original_mkdir = Path.mkdir
    # `.resolve(strict=False)` canonicalizes macOS firmlinks (/tmp ->
    # /private/tmp) so a MINERU_HOME under /tmp still prefix-matches the
    # resolved mkdir target below.
    live_prefix_str = str(LIVE_MINERU_ROOT.expanduser().resolve())

    def guarded_mkdir(self, *args, **kwargs):
        resolved = self.expanduser()
        try:
            resolved_abs = resolved.resolve()
        except (OSError, RuntimeError):
            resolved_abs = resolved
        target_str = str(resolved_abs)
        under_live = (
            target_str == live_prefix_str
            or target_str.startswith(live_prefix_str + os.sep)
        )
        if under_live:
            raise AssertionError(
                f"F3 regression: test attempted mkdir under {LIVE_MINERU_ROOT}: "
                f"{self}. A bogus `--profile` must never fall through to the "
                "default workspace."
            )
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)


def _snapshot_live_mineru() -> set:
    """Return the set of paths currently under `$MINERU_HOME` (or empty).

    The test asserts this set is unchanged after invoking a bogus-profile
    command, so any new file created under `$MINERU_HOME` during the
    invocation trips the assertion loudly.
    """
    root = LIVE_MINERU_ROOT.expanduser()
    if not root.exists():
        return set()
    return set(root.rglob("*"))


# ============================================================================
# F3 REGRESSION — bogus profile must fail loud even when an option VALUE
# looks like a help flag. The old argv-scan heuristic could not tell the
# two apart; the lazy-hydration redesign is help-flag-detection-free.
# ============================================================================


@pytest.mark.parametrize(
    ("args_after_path",),
    [
        # An option VALUE that spells `--help` — the exact reproduction
        # from the Fable re-review.
        (["--caption", "--help"],),
        # `-h` short form, same category of bug.
        (["--caption", "-h"],),
        # `--label` (another string option) whose value looks like help.
        (["--label", "--help"],),
        (["--label", "-h"],),
        # `--chat-id` (another string option) whose value looks like help.
        (["--chat-id", "--help"],),
        (["--chat-id", "-h"],),
        # Combined: caption + label + chat-id all disguised.
        (["--caption", "--help", "--label", "-h", "--chat-id", "--help"],),
    ],
)
def test_bogus_profile_photo_with_helpish_option_value_fails_loud(
    tmp_path: Path,
    sample_photo: Path,
    monkeypatch: pytest.MonkeyPatch,
    args_after_path,
) -> None:
    """`--profile bogus` must fail loud regardless of option-value spelling.

    Under the old eager-hydration + argv-scan-for-`--help` heuristic,
    each of these argv shapes silently skipped profile validation and
    fell through to the default profile. The lazy-hydration redesign
    removes the heuristic entirely — `get_profile(ctx)` runs inside the
    verb, after Click has done its own arg parsing, so an option value
    that happens to be `--help` is just a string.
    """
    # Point workspace root at an empty tmp dir so `bogus` is guaranteed
    # missing (no seed profile, no `current` symlink to muddy the
    # signal).
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)

    before = _snapshot_live_mineru()

    result = runner.invoke(
        app,
        [
            "--profile", "bogus",
            "telegram", "photo",
            str(sample_photo),
            *args_after_path,
        ],
    )

    assert result.exit_code != 0, (
        f"Bogus --profile must fail loud (rc=0 was the F3 leak). "
        f"argv-after-path={args_after_path!r}, output={result.output!r}"
    )
    # The error message names --profile / bogus.
    output = (result.output or "") + (result.stderr or "")
    assert "bogus" in output or "profile" in output.lower(), (
        f"error should name the failing profile: {output!r}"
    )

    # The critical property: nothing new landed under $MINERU_HOME.
    after = _snapshot_live_mineru()
    new_paths = after - before
    assert not new_paths, (
        f"F3 regression: bogus-profile invocation created {len(new_paths)} "
        f"file(s) under {LIVE_MINERU_ROOT}: {sorted(str(p) for p in new_paths)}"
    )


def test_bogus_profile_telegram_send_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`send <text>` also fails loud on bogus profile — no subprocess spawn.

    `mineru telegram send` shells out to `deliver-output.py`; under the
    old design the root callback caught bogus profiles for it. Under
    lazy hydration the verb itself must call `get_profile(ctx)` first
    so a bogus profile never reaches the subprocess spawn.
    """
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)

    before = _snapshot_live_mineru()
    result = runner.invoke(
        app, ["--profile", "bogus", "telegram", "send", "hello"]
    )
    assert result.exit_code != 0
    after = _snapshot_live_mineru()
    assert after == before, (
        f"send: bogus-profile invocation touched {LIVE_MINERU_ROOT}: "
        f"{sorted(str(p) for p in (after - before))}"
    )


def test_bogus_profile_telegram_inject_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`inject <label> <content>` fails loud on bogus profile.

    Inject drops a JSON queue file that used to fall back to
    `$MINERU_HOME/cache/inject-queue/` under the old default; the F2 fix
    already made this path fail loud, and this test pins that behavior
    for the bogus-profile case too.
    """
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in (
        "MINERU_PROFILE", "MINERU_PROFILE_ROOT",
        "MINERU_INJECT_QUEUE_DIR",
    ):
        monkeypatch.delenv(var, raising=False)

    before = _snapshot_live_mineru()
    result = runner.invoke(
        app,
        ["--profile", "bogus", "telegram", "inject", "regression", "content"],
    )
    assert result.exit_code != 0
    after = _snapshot_live_mineru()
    assert after == before


# ============================================================================
# Help paths still render on a fresh clone (no `current` symlink at all).
# Duplicates test_help_tree.py coverage; kept here so this file also
# proves the lazy redesign preserved the fresh-clone UX.
# ============================================================================


def test_root_help_still_works_on_empty_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output


def test_noun_verb_help_still_works_on_empty_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["telegram", "photo", "--help"])
    assert result.exit_code == 0, result.output
    # Sanity: the help body renders (not just a bare exit-0).
    assert "photo" in result.stdout


def test_noun_verb_dash_h_still_works_on_empty_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-h` at any level also renders on a fresh clone."""
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(tmp_path))
    for var in ("MINERU_PROFILE", "MINERU_PROFILE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["telegram", "photo", "-h"])
    assert result.exit_code == 0, result.output


# ============================================================================
# Deleted-heuristic guard: the argv-scan machinery must NOT come back.
# ============================================================================


def test_help_detection_heuristic_is_deleted() -> None:
    """The `_is_help_invocation` heuristic + argv snapshot are retired.

    A regression that re-introduced either would re-open the F3 bypass
    (an option value spelled `--help` would once again skip profile
    validation). Pin their absence so the intent survives the next
    refactor.
    """
    from mineru_cli import app as app_module
    from mineru_cli.verbs import custom as custom_module

    assert not hasattr(app_module, "_is_help_invocation"), (
        "_is_help_invocation is retired; do not re-add. See "
        "mineru_cli.profile.get_profile for the correct hydration seam."
    )
    assert not hasattr(app_module, "_HELP_TOKENS"), (
        "_HELP_TOKENS is retired; the argv-scan heuristic that used it "
        "could not tell a help flag from an option value."
    )
    assert not hasattr(custom_module, "MINERU_SUB_ARGV_META_KEY"), (
        "MINERU_SUB_ARGV_META_KEY (the argv snapshot key) is retired; "
        "no downstream --help detection is needed under lazy hydration."
    )
    # The `invoke()` override on `CustomVerbTyperGroup` that populated
    # the meta stash was the mechanical carrier of the bug.
    assert "invoke" not in custom_module.CustomVerbTyperGroup.__dict__, (
        "CustomVerbTyperGroup.invoke override is retired; it only "
        "existed to populate the argv-snapshot stash."
    )
