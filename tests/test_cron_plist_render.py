"""Tests for `mineru_cli.cron.plist.render_plist` (P4-02).

Two contract layers:

  1. **Golden-file parity.** Every one of the 15 jobs in
     `profiles/mineru/cron.yaml` renders to a byte-for-byte match against
     `tests/fixtures/plists/<name>.plist`. The fixtures are checked in
     so a copy tweak (whitespace shift, XML-element reorder, cron
     humanizer regression) fails loud.
  2. **Semantic rules.** The unit tests exercise every discriminating
     rule the module docstring calls out: cron parsing (`*` handling,
     DoW 7 -> 0, DoM without DoW), single-vs-multi
     `StartCalendarInterval`, `xml.sax.saxutils.escape` on hostile
     input, and — the load-bearing safety property — `render_plist`
     never opens a file, never spawns a subprocess, never touches
     `~/Library/LaunchAgents`.

No test writes under `$MINERU_HOME` or `~/Library/LaunchAgents`. All state
lives under `tmp_path` or the worktree's `profiles/mineru/` +
`tests/fixtures/plists/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mineru_cli.cron import (
    CRON_JOB_KIND_LLM,
    CRON_JOB_KIND_SCRIPT,
    CronConfig,
    CronJob,
    PlistRenderError,
    get_job,
    load_cron_config,
    render_plist,
)
from mineru_cli.cron.plist import (
    _parse_cron_string_to_calendar_dict,
)
from mineru_cli.profile import load_active_profile
from mineru_cli.profile.schema import Profile


# --- Fixtures --------------------------------------------------------------


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "plists"

# Synthetic seed profile shipped under tests/fixtures/ (the engine repo does
# NOT ship a live `profiles/` tree). The golden plists in `_FIXTURE_DIR` are
# rendered from THIS seed's `workspace_absolute` + `launchd_label_prefix`.
_SEED_PROFILE_BASE = Path(__file__).resolve().parent / "fixtures" / "seed_profile_base"


# Full inventory pinned from profiles/mineru/cron.yaml. Duplicated (not
# imported) from tests/test_cron_config.py so a rename in one file can
# only ever break its own tests; the goldens under fixtures/plists/ are
# the load-bearing name list, not this constant.
_LIVE_JOB_NAMES = (
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
)


@pytest.fixture()
def seed_profile() -> Profile:
    """Load the synthetic `mineru` seed shipped under tests/fixtures/."""
    return load_active_profile("mineru", base_dir=_SEED_PROFILE_BASE)


@pytest.fixture()
def seed_cron(seed_profile: Profile) -> CronConfig:
    """Load `profiles/mineru/cron.yaml` via the real loader."""
    return load_cron_config(seed_profile)


# --- 1. Golden-file parity across the 15 jobs -----------------------------


class TestGoldenFileParity:
    """Every shipped job renders byte-for-byte against its fixture."""

    @pytest.mark.parametrize("job_name", _LIVE_JOB_NAMES)
    def test_render_matches_fixture(
        self,
        seed_cron: CronConfig,
        seed_profile: Profile,
        job_name: str,
    ) -> None:
        job = get_job(seed_cron, job_name)
        assert job is not None, f"job {job_name!r} missing from cron.yaml"

        expected_path = _FIXTURE_DIR / f"{job_name}.plist"
        assert expected_path.exists(), (
            f"fixture {expected_path} missing — run the P4-02 fixture "
            "generator (see the plist module docstring) to regenerate."
        )
        expected = expected_path.read_text(encoding="utf-8")

        actual = render_plist(job, seed_profile)
        assert actual == expected, (
            f"rendered plist for {job_name!r} diverged from fixture at "
            f"{expected_path}. If the divergence is intentional, "
            "regenerate the fixture. Otherwise fix the renderer."
        )

    def test_fixture_directory_has_no_extras(self) -> None:
        """A stale fixture from a deleted job would slip past the parametrize."""
        expected = {f"{name}.plist" for name in _LIVE_JOB_NAMES}
        actual = {p.name for p in _FIXTURE_DIR.glob("*.plist")}
        assert actual == expected, (
            f"fixture directory {_FIXTURE_DIR} has "
            f"missing={expected - actual!r} and/or extra={actual - expected!r} "
            "files. Regenerate to match the shipped job roster."
        )


# --- 2. Label + prefix contract -------------------------------------------


class TestLabelPrefix:
    """The plist Label MUST be `<profile.launchd_label_prefix>.<job.name>`."""

    def test_label_uses_profile_prefix(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        job = get_job(seed_cron, "morning-brief")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        assert (
            f"<string>{seed_profile.launchd_label_prefix}.morning-brief</string>"
            in rendered
        )


# --- 3. ProgramArguments — trigger-script default vs. verbatim script -----


class TestProgramArguments:
    def test_llm_job_uses_trigger_script(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        job = get_job(seed_cron, "morning-brief")
        assert job is not None
        assert job.kind == CRON_JOB_KIND_LLM
        rendered = render_plist(job, seed_profile)
        workspace = str(seed_profile.workspace_absolute).rstrip("/")
        # The two argv elements the trigger-script format produces.
        assert "<string>/bin/bash</string>" in rendered
        assert (
            f"<string>{workspace}/scripts/trigger-morning-brief-claude-code.sh</string>"
            in rendered
        )

    def test_script_job_argv_is_verbatim(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        job = get_job(seed_cron, "pre-export-journals")
        assert job is not None
        assert job.kind == CRON_JOB_KIND_SCRIPT
        rendered = render_plist(job, seed_profile)
        # The three tokens from the loader survive verbatim, including
        # the trailing "30" (retention window in days).
        for token in job.program_args:
            assert f"<string>{token}</string>" in rendered

    def test_script_job_bypasses_trigger_script_template(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        """A script job's argv must NEVER be wrapped with trigger-<name>.sh."""
        job = get_job(seed_cron, "cleanup-retention")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        assert "trigger-cleanup-retention-claude-code.sh" not in rendered


# --- 4. StartCalendarInterval — single dict vs. array of dicts ------------


class TestStartCalendarInterval:
    def test_single_schedule_renders_as_dict(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        job = get_job(seed_cron, "morning-brief")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        # After the StartCalendarInterval key the next open-tag must be
        # <dict>, not <array>. Guard with a substring match on the
        # canonical rendering.
        marker = "<key>StartCalendarInterval</key>\n"
        idx = rendered.find(marker)
        assert idx != -1
        tail = rendered[idx + len(marker):]
        assert tail.lstrip().startswith("<dict>")

    def test_multi_schedule_renders_as_array_of_dicts(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        job = get_job(seed_cron, "prompts-alignment")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        marker = "<key>StartCalendarInterval</key>\n"
        idx = rendered.find(marker)
        assert idx != -1
        tail = rendered[idx + len(marker):]
        assert tail.lstrip().startswith("<array>")
        # Two Weekday entries expected (Tue + Fri).
        assert rendered.count("<key>Weekday</key>") == 2

    def test_pet_summary_has_three_weekday_dicts(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        job = get_job(seed_cron, "pet-summary")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        # Mon + Wed + Fri.
        assert rendered.count("<key>Weekday</key>") == 3

    def test_cleanup_retention_uses_day_not_weekday(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        """`7 4 2 * *` -> Day=2, no Weekday."""
        job = get_job(seed_cron, "cleanup-retention")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        assert "<key>Day</key>" in rendered
        assert "<key>Weekday</key>" not in rendered

    def test_humanized_comment_covers_multi_instance(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        """The comment above StartCalendarInterval joins per-schedule humanizations."""
        job = get_job(seed_cron, "prompts-alignment")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        assert (
            "<!-- weekly on Tuesday at 22:00, weekly on Friday at 22:00 -->"
            in rendered
        )


# --- 5. TimeOut, WorkingDirectory, EnvironmentVariables --------------------


class TestScalarFields:
    def test_timeout_is_emitted_for_default_1800(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        # memory-dedup carries the default 1800s. Per the module contract
        # we emit even the default so behavior is deterministic under
        # launchd instead of relying on the launchd default.
        job = get_job(seed_cron, "memory-dedup")
        assert job is not None
        assert job.timeout_seconds == 1800
        rendered = render_plist(job, seed_profile)
        assert "<key>TimeOut</key>\n    <integer>1800</integer>" in rendered

    def test_timeout_reflects_per_job_override(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        # daily-consolidation runs 7200s in the live plist; the config
        # matches.
        job = get_job(seed_cron, "daily-consolidation")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        assert "<integer>7200</integer>" in rendered

    def test_working_directory_defaults_to_profile_workspace(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        # No shipped job sets working_directory explicitly, so every
        # rendering falls back to the profile workspace.
        for job_name in _LIVE_JOB_NAMES:
            job = get_job(seed_cron, job_name)
            assert job is not None
            rendered = render_plist(job, seed_profile)
            assert (
                f"<key>WorkingDirectory</key>\n    <string>"
                f"{seed_profile.workspace_absolute}</string>"
                in rendered
            ), f"working_directory fallback missed for {job_name!r}"

    def test_env_dict_preserves_insertion_order(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        # PATH comes before HOME in the YAML `default_env`. Rendering
        # must keep PATH first so operators eyeball the same order they
        # wrote.
        job = get_job(seed_cron, "morning-brief")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        path_key_idx = rendered.find("<key>PATH</key>")
        home_key_idx = rendered.find("<key>HOME</key>")
        assert 0 <= path_key_idx < home_key_idx

    def test_minimal_env_daily_consolidation(
        self, seed_cron: CronConfig, seed_profile: Profile
    ) -> None:
        # daily-consolidation overrides env to PATH-only (no HOME). The
        # rendered plist must NOT emit a HOME key at all.
        job = get_job(seed_cron, "daily-consolidation")
        assert job is not None
        rendered = render_plist(job, seed_profile)
        # Zoom in to the EnvironmentVariables block so a HOME occurring
        # elsewhere (label prefix, path) doesn't produce a false-negative.
        env_block_start = rendered.find("<key>EnvironmentVariables</key>")
        env_block = rendered[env_block_start:]
        assert "<key>HOME</key>" not in env_block


# --- 6. Cron parsing edge cases (unit-level, not plist-level) --------------


class TestParseCron:
    """Direct unit tests for `_parse_cron_string_to_calendar_dict`."""

    def test_daily_at_seven(self) -> None:
        assert _parse_cron_string_to_calendar_dict("0 7 * * *") == {
            "Hour": 7,
            "Minute": 0,
        }

    def test_monday_at_two_twentyseven(self) -> None:
        assert _parse_cron_string_to_calendar_dict("27 2 * * 1") == {
            "Hour": 2,
            "Minute": 27,
            "Weekday": 1,
        }

    def test_second_of_month(self) -> None:
        # cleanup-retention shape: DOM present, DOW absent, no Weekday.
        assert _parse_cron_string_to_calendar_dict("7 4 2 * *") == {
            "Hour": 4,
            "Minute": 7,
            "Day": 2,
        }

    def test_sunday_zero_and_seven_are_equivalent(self) -> None:
        assert _parse_cron_string_to_calendar_dict("0 0 * * 0")["Weekday"] == 0
        assert _parse_cron_string_to_calendar_dict("0 0 * * 7")["Weekday"] == 0

    def test_rejects_four_field_string(self) -> None:
        with pytest.raises(PlistRenderError, match="not a 5-field cron string"):
            _parse_cron_string_to_calendar_dict("0 7 * *")

    def test_rejects_range(self) -> None:
        with pytest.raises(PlistRenderError, match="not a plain integer"):
            _parse_cron_string_to_calendar_dict("0 7 * * 1-5")

    def test_rejects_step(self) -> None:
        with pytest.raises(PlistRenderError, match="not a plain integer"):
            _parse_cron_string_to_calendar_dict("*/15 * * * *")

    def test_rejects_list(self) -> None:
        with pytest.raises(PlistRenderError, match="not a plain integer"):
            _parse_cron_string_to_calendar_dict("0 0 * * 1,3,5")

    def test_rejects_hour_out_of_range(self) -> None:
        with pytest.raises(PlistRenderError, match="hour 24"):
            _parse_cron_string_to_calendar_dict("0 24 * * *")

    def test_rejects_minute_out_of_range(self) -> None:
        with pytest.raises(PlistRenderError, match="minute 60"):
            _parse_cron_string_to_calendar_dict("60 0 * * *")

    def test_rejects_dow_out_of_range(self) -> None:
        with pytest.raises(PlistRenderError, match="day-of-week"):
            _parse_cron_string_to_calendar_dict("0 0 * * 8")

    def test_rejects_day_out_of_range(self) -> None:
        with pytest.raises(PlistRenderError, match="day-of-month"):
            _parse_cron_string_to_calendar_dict("0 0 32 * *")


# --- 7. XML safety --------------------------------------------------------


class TestXmlEscaping:
    """Hostile strings must be escaped, not injected verbatim into XML."""

    def _job_with_env(self, env: dict) -> CronJob:
        return CronJob(
            name="hostile",
            kind=CRON_JOB_KIND_SCRIPT,
            schedule=("0 0 * * *",),
            program_args=("/bin/true",),
            env=env,
        )

    def test_env_value_with_ampersand_is_escaped(
        self, seed_profile: Profile
    ) -> None:
        job = self._job_with_env({"THING": "a&b"})
        rendered = render_plist(job, seed_profile)
        assert "<string>a&amp;b</string>" in rendered
        assert "<string>a&b</string>" not in rendered

    def test_env_value_with_angle_brackets_is_escaped(
        self, seed_profile: Profile
    ) -> None:
        job = self._job_with_env({"THING": "<x>"})
        rendered = render_plist(job, seed_profile)
        assert "<string>&lt;x&gt;</string>" in rendered

    def test_env_key_with_angle_bracket_is_escaped(
        self, seed_profile: Profile
    ) -> None:
        # A hand-built CronJob can smuggle a hostile key; the renderer
        # must still escape it (the loader would reject this via YAML
        # shape, but the safety-net matters).
        job = self._job_with_env({"<hostile>": "safe"})
        rendered = render_plist(job, seed_profile)
        assert "<key>&lt;hostile&gt;</key>" in rendered


# --- 8. Filesystem-neutrality property ------------------------------------


class TestFilesystemNeutral:
    """`render_plist` must not open a file or spawn a subprocess.

    Guards against a future refactor that reaches for `Path.write_text`
    or `subprocess.run` inside the renderer. The property is
    load-bearing: `mineru cron install` intentionally gates every write
    behind `--dry-run`, and a renderer that writes to disk on the side
    would silently break that contract.
    """

    def test_render_does_not_open_files(
        self,
        seed_cron: CronConfig,
        seed_profile: Profile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import builtins

        opened: list[tuple[str, str]] = []
        real_open = builtins.open

        def _tracking_open(path, mode="r", *args, **kwargs):
            opened.append((str(path), str(mode)))
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _tracking_open)

        job = get_job(seed_cron, "morning-brief")
        assert job is not None
        render_plist(job, seed_profile)

        # Zero opens during a render. If a future edit reads a template
        # from disk, this fails and forces the reviewer to think about
        # whether it belongs inside a pure renderer.
        assert opened == [], f"render_plist opened files: {opened!r}"

    def test_render_does_not_spawn_subprocess(
        self,
        seed_cron: CronConfig,
        seed_profile: Profile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import subprocess

        calls: list = []

        def _blow_up(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError(
                "render_plist must not spawn a subprocess"
            )

        monkeypatch.setattr(subprocess, "run", _blow_up)
        monkeypatch.setattr(subprocess, "Popen", _blow_up)

        job = get_job(seed_cron, "pet-summary")
        assert job is not None
        render_plist(job, seed_profile)
        assert calls == []


# --- 9. Error paths on hand-built CronJobs --------------------------------


class TestRenderErrors:
    """The renderer itself double-checks even when the loader would reject."""

    def test_script_job_empty_program_args_raises(
        self, seed_profile: Profile
    ) -> None:
        # Sneak past the loader by constructing directly. The renderer
        # still fails loud so a hand-built fixture cannot silently
        # produce an argv-less plist.
        job = CronJob(
            name="empty",
            kind=CRON_JOB_KIND_SCRIPT,
            schedule=("0 0 * * *",),
            program_args=(),
        )
        with pytest.raises(PlistRenderError, match="empty program_args"):
            render_plist(job, seed_profile)

    def test_bad_schedule_string_raises(
        self, seed_profile: Profile
    ) -> None:
        job = CronJob(
            name="broken",
            kind=CRON_JOB_KIND_LLM,
            schedule=("0 0 * * 1-5",),
            model="claude-opus-4-6",
            instruction="recurring/x.md",
        )
        with pytest.raises(PlistRenderError, match="not a plain integer"):
            render_plist(job, seed_profile)
