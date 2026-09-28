"""Tests for the memory-maintenance verb implementations.

Covers the five verbs the stub-triage report tagged BUILD:
`warm-resume`, `tree`, `reindex`, `consolidate`, `backup`.

The pure-Python implementations under `mineru_cli.memory_ops` are
exercised directly against a `tmp_path` memory tree; the CLI wiring is
covered end-to-end via `CliRunner` on `mineru_cli.app.app`. `reindex`
is covered via a `run_msearch` monkeypatch (no live binary needed).
"""

from __future__ import annotations

import tarfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mineru_cli.app import app
from mineru_cli.memory_ops import (
    build_memory_tree,
    build_warm_resume,
    consolidate_daily_fragments,
    create_backup,
)
from mineru_cli.memory_ops.consolidate import (
    STATUS_DISTILLED,
    STATUS_NO_FRAGMENTS,
    STATUS_PARTIAL_NO_LLM,
)
from mineru_cli.memory_ops.distiller_claude import DistillerError


runner = CliRunner()


# ------------------------------------------------------------------ helpers


def _write(path: Path, body: str) -> Path:
    """Convenience: `mkdir -p` the parent and write text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _seed_profile_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tz: str = "America/Los_Angeles",
) -> tuple[Path, Path]:
    """Build a synthetic active profile on disk and point env at it.

    Layout (matches the production `<workspace_root>/profiles/<name>/`
    convention, so the profile's `workspace_absolute` is DISTINCT from
    the shared workspace root — a loader invariant enforced by the
    step-5 audit Finding 6 check):

        <tmp_path>/shared_root/                      <- MINERU_WORKSPACE_ROOT
        <tmp_path>/shared_root/profiles/             <- MINERU_PROFILE_ROOT
        <tmp_path>/shared_root/profiles/mineru/      <- workspace_absolute
        <tmp_path>/shared_root/profiles/mineru/memory/  <- memory_root

    Returns (memory_root, workspace_absolute) so tests can seed files
    under either root without recomputing paths.
    """
    shared_root = tmp_path / "shared_root"
    profile_base = shared_root / "profiles"
    workspace_absolute = profile_base / "mineru"
    memory_root = workspace_absolute / "memory"
    workspace_absolute.mkdir(parents=True, exist_ok=True)
    memory_root.mkdir(parents=True, exist_ok=True)
    (workspace_absolute / "profile.yaml").write_text(
        (
            "name: mineru\n"
            "display_name: Example User\n"
            "assistant_name: Mineru\n"
            f"timezone: {tz}\n"
            "keychain_account: mineru\n"
            "launchd_label_prefix: com.mineru\n"
            f"workspace_absolute: {workspace_absolute}\n"
            f"memory_root: {memory_root}\n"
            f"briefs_root: {workspace_absolute}\n"
            "journal_apple_notes_folder: Daily Journals\n"
            "secrets:\n"
            "  backends:\n"
            "    - env\n"
            "    - keychain\n"
            "  env_prefix: MINERU_SECRET_\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINERU_PROFILE", "mineru")
    monkeypatch.setenv("MINERU_PROFILE_ROOT", str(profile_base))
    monkeypatch.setenv("MINERU_WORKSPACE_ROOT", str(shared_root))
    return memory_root, workspace_absolute


# ============================================================ warm-resume ==


def test_warm_resume_selects_recent_days_and_today(tmp_path: Path) -> None:
    """3 most recent consolidated days (excl today) + today's session fragments."""
    root = tmp_path / "mem"
    daily = root / "daily"
    # Consolidated days spread across the window and a stale one.
    _write(daily / "2026-09-16.md", "will-be-shown-in-today\n")  # today, consolidated (skipped in recent)
    _write(daily / "2026-09-15.md", "yesterday-consolidated\n")
    _write(daily / "2026-09-14.md", "two-days-ago-consolidated\n")
    _write(daily / "2026-09-13.md", "three-days-ago-consolidated\n")
    _write(daily / "2026-09-01.md", "way-too-old\n")
    # Today's session fragments (both slug forms).
    _write(daily / "2026-09-16-morning-notes.md", "morning-frag-body\n")
    _write(daily / "2026-09-16_08-30-00.md", "timestamped-frag-body\n")
    # Artifacts that must be ignored.
    _write(daily / "2026-09-15.raw.md", "should-not-appear\n")
    _write(daily / "2026-09-15.denoise.log", "should-not-appear\n")
    _write(daily / "not-a-date.md", "should-not-appear\n")

    fixed_now = datetime(2026, 9, 16, 10, 23, tzinfo=timezone.utc)
    bundle = build_warm_resume(root, timezone="UTC", now=fixed_now)

    assert "<session_warmup>" in bundle
    assert "</session_warmup>" in bundle
    assert '<recent_days count="3">' in bundle
    assert "yesterday-consolidated" in bundle
    assert "two-days-ago-consolidated" in bundle
    assert "three-days-ago-consolidated" in bundle
    # Today's consolidated file must NOT be listed as a recent day.
    assert "will-be-shown-in-today" not in bundle
    # Stale (older than window) must NOT appear.
    assert "way-too-old" not in bundle
    # Today's fragments both listed, in filename order.
    assert '<today count="2">' in bundle
    assert "morning-frag-body" in bundle
    assert "timestamped-frag-body" in bundle
    # Artifacts and non-date filenames must be filtered out.
    assert "should-not-appear" not in bundle
    # Header carries the requested date + weekday.
    assert "Wednesday" in bundle
    assert "September" in bundle
    assert "2026" in bundle
    # Order: recent-days section precedes today.
    assert bundle.index("<recent_days") < bundle.index("<today")


def test_warm_resume_missing_daily_dir_is_safe(tmp_path: Path) -> None:
    """A memory root with no daily/ dir still returns a well-formed bundle."""
    fixed_now = datetime(2026, 9, 16, 10, 23, tzinfo=timezone.utc)
    bundle = build_warm_resume(tmp_path / "nope", now=fixed_now)
    assert '<recent_days count="0">' in bundle
    assert '<today count="0">' in bundle
    assert bundle.endswith("</session_warmup>\n")


def test_warm_resume_recent_window_boundary_exclusive_at_today(tmp_path: Path) -> None:
    """Today's date is excluded from `<recent_days>` even when consolidated."""
    root = tmp_path / "mem"
    daily = root / "daily"
    _write(daily / "2026-09-16.md", "today-body\n")
    fixed_now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    bundle = build_warm_resume(root, now=fixed_now)
    assert '<recent_days count="0">' in bundle
    assert "today-body" not in bundle  # consolidated-today doesn't appear anywhere in this bundle


def test_warm_resume_verb_writes_bundle_via_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_root, _ = _seed_profile_env(monkeypatch, tmp_path)
    _write(memory_root / "daily" / "2026-01-15-yesterday-notes.md", "hello-from-fragment\n")
    result = runner.invoke(app, ["memory", "warm-resume"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "<session_warmup>" in result.stdout
    # No fragments for today (today != 2026-01-15) but the bundle must still be well-formed.
    assert "<today" in result.stdout


# ==================================================================== tree ==


def test_tree_annotates_files_with_frontmatter_description(tmp_path: Path) -> None:
    root = tmp_path / "mem"
    _write(
        root / "restaurants.md",
        "---\n"
        'description: "50+ local spots"\n'
        "tags: [food]\n"
        "---\n"
        "# Places\n",
    )
    _write(root / "no_frontmatter.md", "# just a body\n")
    _write(
        root / "people" / "family.md",
        "---\ndescription: Family notes\n---\ncontent\n",
    )
    # Excluded from listing but summarized.
    _write(root / "daily" / "2026-09-16-frag.md", "x")
    _write(root / "monthly" / "2026-09.md", "y")
    # Ignored artifact.
    _write(root / "__pycache__" / "junk.pyc", "z")

    rendered = build_memory_tree(root)
    # Header shape.
    assert rendered.startswith("## Current Structure\n\n```\n")
    # Description surfaces inline for annotated file.
    assert "restaurants.md  # 50+ local spots" in rendered
    # Unannotated file listed but with no description comment.
    assert "|-- no_frontmatter.md" in rendered
    assert "no_frontmatter.md  #" not in rendered
    # Subdirectory + nested description.
    assert "|-- people" in rendered
    assert "family.md  # Family notes" in rendered
    # Summarized dirs collapsed to summary lines.
    assert "|-- daily/  # 1 session logs" in rendered
    assert "|-- monthly/  # 1 monthly summaries" in rendered
    # Individual daily/monthly files are NOT listed inline.
    assert "2026-09-16-frag.md" not in rendered
    assert "2026-09.md" not in rendered
    # Ignored artifacts skipped.
    assert "__pycache__" not in rendered


def test_tree_include_all_lists_daily_files(tmp_path: Path) -> None:
    root = tmp_path / "mem"
    _write(root / "daily" / "2026-09-16-a.md", "hi")
    rendered = build_memory_tree(root, summarized_dirs=())
    assert "2026-09-16-a.md" in rendered


def test_tree_missing_root_returns_stub(tmp_path: Path) -> None:
    rendered = build_memory_tree(tmp_path / "does_not_exist")
    assert "no memory tree" in rendered
    assert rendered.endswith("```\n")


def test_tree_verb_via_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_root, _ = _seed_profile_env(monkeypatch, tmp_path)
    _write(memory_root / "hello.md", "---\ndescription: greet\n---\nbody\n")
    result = runner.invoke(app, ["memory", "tree"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "hello.md  # greet" in result.stdout


# ================================================================ reindex ==


def test_reindex_verb_invokes_msearch_with_no_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`memory reindex` shells out to msearch with `--no-cache`."""
    _seed_profile_env(monkeypatch, tmp_path)
    recorded: List[List[str]] = []

    def fake(args):
        recorded.append(list(args))
        return 0

    with patch("mineru_cli.verbs.memory.run_msearch", fake):
        result = runner.invoke(app, ["memory", "reindex"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(recorded) == 1
    argv = recorded[0]
    assert argv[0] == "tags"
    assert "--no-cache" in argv
    # Workspace forwarded to the profile's workspace root.
    assert "--workspace" in argv
    ws_index = argv.index("--workspace")
    assert Path(argv[ws_index + 1]).exists()


def test_reindex_verb_propagates_engine_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_profile_env(monkeypatch, tmp_path)

    def failing(args):
        return 3

    with patch("mineru_cli.verbs.memory.run_msearch", failing):
        result = runner.invoke(app, ["memory", "reindex"])
    assert result.exit_code == 3


# ============================================================ consolidate ==


def test_consolidate_status_partial_alias_matches_no_fragments() -> None:
    """Back-compat: legacy `STATUS_PARTIAL_NO_LLM` alias resolves to the new
    `STATUS_NO_FRAGMENTS` constant. External callers that imported the old
    name continue to compare successfully."""
    assert STATUS_PARTIAL_NO_LLM == STATUS_NO_FRAGMENTS


def test_consolidate_writes_raw_bundle_with_injected_distiller(tmp_path: Path) -> None:
    """Raw bundle scanning + writing is unchanged; the injected distiller
    receives the concatenated bundle and the consolidated `.md` is written
    from its return value."""
    root = tmp_path / "mem"
    daily = root / "daily"
    _write(daily / "2026-09-15-morning.md", "morning content\n")
    _write(daily / "2026-09-15_18-30-00.md", "evening content\n")
    _write(daily / "2026-09-16-other.md", "wrong day, must be skipped\n")
    _write(daily / "2026-09-15.raw.md", "old raw, must be skipped in scan\n")

    captured: List[str] = []

    def fake_distiller(raw: str) -> str:
        captured.append(raw)
        return "DISTILLED SUMMARY\n"

    result = consolidate_daily_fragments(
        root, date(2026, 9, 15), distiller=fake_distiller
    )
    assert result.fragments_found == 2
    assert result.distillation_status == STATUS_DISTILLED
    assert result.consolidated_path is not None
    assert result.raw_bundle_path is not None

    body = result.raw_bundle_path.read_text(encoding="utf-8")
    assert '<daily_bundle date="2026-09-15" fragments="2">' in body
    assert "morning content" in body
    assert "evening content" in body
    assert "wrong day" not in body
    assert 'file="2026-09-15-morning.md"' in body
    # Filename ordering: morning fragment before evening (alphabetical stem
    # covers both slug forms deterministically).
    assert body.index("morning content") < body.index("evening content")

    # The distiller sees the same bundle that was written to disk, and its
    # return value lands verbatim in the consolidated file.
    assert captured == [body]
    assert result.consolidated_path.read_text(encoding="utf-8") == "DISTILLED SUMMARY\n"


def test_consolidate_default_distiller_invoked_when_none_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `distiller=None`, the module-level default (headless-Claude) is
    used. Verified without invoking real `claude`: patch the default symbol
    on the consolidate module and confirm it is what runs."""
    root = tmp_path / "mem"
    daily = root / "daily"
    _write(daily / "2026-09-15-a.md", "raw a\n")

    calls: List[str] = []

    def fake_default(raw: str) -> str:
        calls.append(raw)
        return "DEFAULT-DISTILLED\n"

    monkeypatch.setattr(
        "mineru_cli.memory_ops.consolidate.default_claude_cli_distiller",
        fake_default,
    )
    result = consolidate_daily_fragments(root, date(2026, 9, 15))
    assert result.distillation_status == STATUS_DISTILLED
    assert result.consolidated_path is not None
    assert result.consolidated_path.read_text(encoding="utf-8") == "DEFAULT-DISTILLED\n"
    assert len(calls) == 1
    assert "raw a" in calls[0]


def test_consolidate_distiller_failure_does_not_write_consolidated(
    tmp_path: Path,
) -> None:
    """If the distiller raises, the raw bundle survives on disk but the
    consolidated `.md` is NOT written — a distiller failure must never
    leave a corrupt consolidated file behind."""
    root = tmp_path / "mem"
    daily = root / "daily"
    _write(daily / "2026-09-15-a.md", "raw a\n")

    def boom(raw: str) -> str:
        raise DistillerError("simulated distiller crash")

    with pytest.raises(DistillerError, match="simulated distiller crash"):
        consolidate_daily_fragments(root, date(2026, 9, 15), distiller=boom)

    assert (daily / "2026-09-15.raw.md").exists()
    assert not (daily / "2026-09-15.md").exists()


def test_consolidate_with_no_fragments_writes_nothing(tmp_path: Path) -> None:
    """No fragments -> STATUS_NO_FRAGMENTS, no files written, distiller
    is never invoked (would have raised if it had been)."""
    root = tmp_path / "mem"
    (root / "daily").mkdir(parents=True)

    def would_raise(raw: str) -> str:  # pragma: no cover - must not be called
        raise AssertionError("distiller must not run on the no-fragments path")

    result = consolidate_daily_fragments(root, date(2026, 9, 15), distiller=would_raise)
    assert result.fragments_found == 0
    assert result.raw_bundle_path is None
    assert result.consolidated_path is None
    assert result.distillation_status == STATUS_NO_FRAGMENTS


def test_consolidate_verb_writes_consolidated_file_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI verb runs end-to-end: raw bundle written, default distiller
    (patched) called, consolidated `.md` written, exit 0."""
    memory_root, _ = _seed_profile_env(monkeypatch, tmp_path)
    _write(memory_root / "daily" / "2026-09-15-frag.md", "body\n")
    monkeypatch.setattr(
        "mineru_cli.memory_ops.consolidate.default_claude_cli_distiller",
        lambda raw: "CONSOLIDATED-BODY\n",
    )
    result = runner.invoke(app, ["memory", "consolidate", "--date", "2026-09-15"])
    assert result.exit_code == 0, result.stdout + result.stderr
    raw_path = memory_root / "daily" / "2026-09-15.raw.md"
    md_path = memory_root / "daily" / "2026-09-15.md"
    assert raw_path.exists()
    assert md_path.exists()
    assert md_path.read_text(encoding="utf-8") == "CONSOLIDATED-BODY\n"
    assert "PARTIAL" not in (result.stdout + result.stderr)


def test_consolidate_verb_exits_3_when_distiller_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DistillerError bubbles up as CLI exit 3 with a clean stderr
    message; the consolidated file is NOT written; the raw bundle IS
    preserved for the operator to inspect."""
    memory_root, _ = _seed_profile_env(monkeypatch, tmp_path)
    _write(memory_root / "daily" / "2026-09-15-frag.md", "body\n")

    def boom(raw: str) -> str:
        raise DistillerError("claude not found on PATH")

    monkeypatch.setattr(
        "mineru_cli.memory_ops.consolidate.default_claude_cli_distiller",
        boom,
    )
    result = runner.invoke(app, ["memory", "consolidate", "--date", "2026-09-15"])
    assert result.exit_code == 3
    combined = result.stdout + result.stderr
    assert "distiller failed" in combined.lower()
    assert "claude not found on PATH" in combined
    assert (memory_root / "daily" / "2026-09-15.raw.md").exists()
    assert not (memory_root / "daily" / "2026-09-15.md").exists()


def test_consolidate_verb_exits_1_when_no_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_root, _ = _seed_profile_env(monkeypatch, tmp_path)
    (memory_root / "daily").mkdir(parents=True)
    result = runner.invoke(app, ["memory", "consolidate", "--date", "2026-09-15"])
    assert result.exit_code == 1
    assert "no session fragments" in (result.stderr + result.stdout).lower()


def test_consolidate_verb_rejects_bad_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_profile_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["memory", "consolidate", "--date", "yesterday"])
    assert result.exit_code == 2
    assert "bad date" in (result.stderr + result.stdout).lower()


def test_consolidate_verb_rejects_date_and_missing_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--date and --missing target incompatible modes (single day vs
    backfill sweep) and must not be combined."""
    _seed_profile_env(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["memory", "consolidate", "--date", "2026-09-15", "--missing"]
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "mutually exclusive" in combined.lower()


def test_consolidate_verb_missing_mode_backfills_all_gap_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--missing consolidates every day with fragments but no `.md`,
    oldest first. Days already consolidated are skipped."""
    memory_root, _ = _seed_profile_env(monkeypatch, tmp_path)
    daily = memory_root / "daily"
    # Two gap days (fragments, no consolidated md).
    _write(daily / "2026-09-10-a.md", "day-10-body\n")
    _write(daily / "2026-09-12_08-00-00.md", "day-12-body\n")
    # One day already consolidated — must be skipped.
    _write(daily / "2026-09-11-a.md", "day-11-body\n")
    _write(daily / "2026-09-11.md", "PRE-EXISTING day-11 consolidation\n")

    calls: List[str] = []

    def fake_distiller(raw: str) -> str:
        calls.append(raw)
        return f"DISTILLED CALL {len(calls)}\n"

    monkeypatch.setattr(
        "mineru_cli.memory_ops.consolidate.default_claude_cli_distiller",
        fake_distiller,
    )
    result = runner.invoke(app, ["memory", "consolidate", "--missing"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Two gap days processed, in ascending order.
    assert len(calls) == 2
    assert "day-10-body" in calls[0]
    assert "day-12-body" in calls[1]
    # Newly consolidated files exist.
    assert (daily / "2026-09-10.md").read_text(encoding="utf-8") == "DISTILLED CALL 1\n"
    assert (daily / "2026-09-12.md").read_text(encoding="utf-8") == "DISTILLED CALL 2\n"
    # Pre-existing consolidated day was NOT overwritten.
    assert (
        (daily / "2026-09-11.md").read_text(encoding="utf-8")
        == "PRE-EXISTING day-11 consolidation\n"
    )


def test_consolidate_verb_missing_mode_no_gap_days_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--missing on a clean tree exits 0 with a friendly "nothing to
    do" message."""
    memory_root, _ = _seed_profile_env(monkeypatch, tmp_path)
    daily = memory_root / "daily"
    _write(daily / "2026-09-10-a.md", "body\n")
    _write(daily / "2026-09-10.md", "already-done\n")
    result = runner.invoke(app, ["memory", "consolidate", "--missing"])
    assert result.exit_code == 0
    assert "no days missing" in (result.stdout + result.stderr).lower()


def test_find_dates_missing_consolidation_returns_only_gap_days(
    tmp_path: Path,
) -> None:
    """The pure helper the CLI's --missing mode uses: fragments-but-no-md
    dates only, in ascending order. Artifacts and non-date filenames are
    ignored."""
    from mineru_cli.memory_ops import find_dates_missing_consolidation

    root = tmp_path / "mem"
    daily = root / "daily"
    # Two gap days.
    _write(daily / "2026-09-10-a.md", "x")
    _write(daily / "2026-09-12_08-00-00.md", "x")
    # Already-consolidated day.
    _write(daily / "2026-09-11-a.md", "x")
    _write(daily / "2026-09-11.md", "already-done")
    # Artifacts + noise to ignore.
    _write(daily / "2026-09-13.raw.md", "raw-only, no fragment")
    _write(daily / "not-a-date.md", "x")
    _write(daily / "2026-09-10.denoise.log", "log")

    result = find_dates_missing_consolidation(root)
    assert result == [date(2026, 9, 10), date(2026, 9, 12)]


def test_recurring_template_references_shared_prompt() -> None:
    """The recurring nightly consolidation template must reference the
    same canonical distillation prompt the CLI verb loads. This is the
    "one prompt file, both sites read it" single-source-of-truth
    invariant for the framework."""
    template = (
        Path(__file__).parent.parent
        / "engine"
        / "recurring"
        / "consolidate-daily-memories.md.template"
    ).read_text(encoding="utf-8")
    # References the same prompt FILE the CLI-side default distiller loads
    # (the template reads it by path through the engine link, since the
    # workspace has no importable mineru_cli on its python path).
    from mineru_cli.memory_ops.distiller_claude import PROMPT_PATH

    assert PROMPT_PATH.name in template
    assert "mineru_cli/memory_ops/prompts/" + PROMPT_PATH.name in template
    # And carries the TODO for the eventual route-through-verb refactor.
    assert "mineru memory consolidate" in template


# ================================================================== backup ==


def test_backup_writes_tarball_and_reports_contents(tmp_path: Path) -> None:
    root = tmp_path / "mem"
    _write(root / "top.md", "top-body")
    _write(root / "nested" / "child.md", "child-body")
    result = create_backup(
        root,
        backup_dir=tmp_path / "backups",
        profile_name="alice",
    )
    assert result.archive_path.exists()
    assert result.archive_path.name.startswith("memory-alice-")
    assert result.archive_path.name.endswith(".tar.gz")
    assert result.byte_count > 0
    assert result.file_count >= 3  # root dir + 2 files + nested dir
    # Verify archive contents.
    with tarfile.open(result.archive_path, "r:gz") as tf:
        names = tf.getnames()
    assert any(n.endswith("/top.md") or n.endswith("top.md") for n in names)
    assert any(n.endswith("/nested/child.md") for n in names)


def test_backup_never_overwrites(tmp_path: Path) -> None:
    """Two calls at the same UTC second land on unique paths (.1 suffix)."""
    root = tmp_path / "mem"
    _write(root / "x.md", "x")
    fixed = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    first = create_backup(root, backup_dir=tmp_path / "b", now=fixed)
    second = create_backup(root, backup_dir=tmp_path / "b", now=fixed)
    assert first.archive_path != second.archive_path
    assert first.archive_path.exists()
    assert second.archive_path.exists()


def test_backup_raises_on_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        create_backup(tmp_path / "nope", backup_dir=tmp_path / "b")


def test_backup_verb_via_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_root, workspace_absolute = _seed_profile_env(monkeypatch, tmp_path)
    _write(memory_root / "hello.md", "world")
    result = runner.invoke(app, ["memory", "backup"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Default output directory is <workspace_absolute>/backups/.
    backup_dir = workspace_absolute / "backups"
    assert backup_dir.exists()
    archives = list(backup_dir.glob("memory-mineru-*.tar.gz"))
    assert len(archives) == 1
    # The reported path in stdout must match the archive on disk.
    assert str(archives[0]) in result.stdout


def test_backup_verb_honors_out_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_root, _ = _seed_profile_env(monkeypatch, tmp_path)
    _write(memory_root / "hello.md", "world")
    custom_dir = tmp_path / "elsewhere" / "snapshots"
    result = runner.invoke(app, ["memory", "backup", "--out", str(custom_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    archives = list(custom_dir.glob("memory-mineru-*.tar.gz"))
    assert len(archives) == 1


# =========================================================== stub un-hide ==


def test_maintenance_verbs_are_not_hidden() -> None:
    """The five BUILD verbs must be discoverable in `memory --help`."""
    result = runner.invoke(app, ["memory", "--help"])
    assert result.exit_code == 0
    for verb in ("warm-resume", "tree", "reindex", "consolidate", "backup"):
        assert verb in result.stdout, f"{verb!r} missing from `memory --help`"
