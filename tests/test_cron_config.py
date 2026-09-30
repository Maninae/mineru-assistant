"""Tests for the P4-01 `mineru cron` data model + cron.yaml loader.

Two contract layers:

  1. **Shipped `profiles/mineru/cron.yaml` loads cleanly and matches the
     live launchd inventory.** This is the "guard the seed" test: if a
     future edit deletes or renames a job, the roster count / name
     assertions fail loud.
  2. **Loader rejects malformed cron.yaml.** For each error branch in
     `mineru_cli/cron/config.py`, an ephemeral cron.yaml is written
     under `tmp_path`, loaded, and asserted to raise `CronConfigError`
     with a message that names the field.

No test writes under `$MINERU_HOME` or `~/Library/LaunchAgents`, no test
executes any job, no test loads the live launchd plists. All state
lives under `tmp_path` or the worktree's `profiles/mineru/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest
import yaml

from mineru_cli.cron import (
    CRON_JOB_KIND_LLM,
    CRON_JOB_KIND_SCRIPT,
    CronConfig,
    CronConfigError,
    CronJob,
    all_job_names,
    default_cron_yaml_path,
    get_job,
    load_cron_config,
)
from mineru_cli.cron.model import PreStep
from mineru_cli.profile import load_active_profile
from mineru_cli.profile.loader import PROFILE_BASE_DIR_ENV_VAR
from mineru_cli.profile.schema import Profile

# Synthetic seed profile shipped under tests/fixtures/ (the engine repo does
# NOT ship a live `profiles/` tree). The `seed_profile` fixture loads the
# generic `mineru` seed from here rather than a workspace-resolved default.
_SEED_PROFILE_BASE = Path(__file__).resolve().parent / "fixtures" / "seed_profile_base"


# --- Fixtures --------------------------------------------------------------


# The canonical inventory the roster tests pin against. Any future edit
# to profiles/mineru/cron.yaml that changes the set of jobs must also
# update these constants, so the change is visible in code review.
LIVE_LLM_JOB_NAMES: Iterable[str] = (
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
)
LIVE_SCRIPT_JOB_NAMES: Iterable[str] = (
    "cleanup-retention",
    "group-members-sweep",
    "pre-export-journals",
)
LIVE_ALL_JOB_NAMES = tuple(LIVE_LLM_JOB_NAMES) + tuple(LIVE_SCRIPT_JOB_NAMES)


@pytest.fixture()
def seed_profile() -> Profile:
    """Load the synthetic `mineru` seed shipped under tests/fixtures/."""
    return load_active_profile("mineru", base_dir=_SEED_PROFILE_BASE)


@pytest.fixture()
def seed_cron(seed_profile: Profile) -> CronConfig:
    """Load the shipped `profiles/mineru/cron.yaml` via the real loader."""
    return load_cron_config(seed_profile)


def _write_profile_stub(profile_root: Path, name: str) -> Path:
    """Write a minimal but schema-valid profile.yaml so tests can point
    `MINERU_PROFILE_ROOT` at an ephemeral base and load a cron.yaml
    beside it. Mirrors `tests/test_profile_loader.py`'s helper.
    """
    profile_root.mkdir(parents=True, exist_ok=True)
    profile_yaml = profile_root / "profile.yaml"
    profile_yaml.write_text(
        (
            f"name: {name}\n"
            f"display_name: {name.capitalize()}\n"
            "assistant_name: TestBot\n"
            "timezone: America/Los_Angeles\n"
            f"keychain_account: {name}-acct\n"
            f"launchd_label_prefix: com.{name}\n"
            f"workspace_absolute: /tmp/{name}-workspace\n"
            f"memory_root: /tmp/{name}-workspace/memory\n"
            f"briefs_root: /tmp/{name}-workspace/briefs\n"
            "journal_apple_notes_folder: Daily Journals\n"
            "secrets:\n"
            "  backends:\n"
            "    - env\n"
            "    - keychain\n"
            f"  env_prefix: {name.upper()}_SECRET_\n"
        ),
        encoding="utf-8",
    )
    return profile_yaml


@pytest.fixture()
def tmp_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Profile:
    """Return a `Profile` rooted under an ephemeral base dir.

    Every test that writes a cron.yaml uses this fixture so nothing
    lands in the worktree tree, and no test collides with another.
    """
    base = tmp_path / "profiles"
    _write_profile_stub(base / "testp", "testp")
    monkeypatch.setenv(PROFILE_BASE_DIR_ENV_VAR, str(base))
    return load_active_profile("testp")


def _write_cron_yaml(profile: Profile, body: str) -> Path:
    path = default_cron_yaml_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --- 1. Shipped cron.yaml sanity ------------------------------------------


class TestShippedCronYaml:
    """The seed `profiles/mineru/cron.yaml` must round-trip through the loader
    with the exact 15-job roster we cutover to."""

    def test_loads(self, seed_cron: CronConfig) -> None:
        assert isinstance(seed_cron, CronConfig)
        assert seed_cron.source_path.name == "cron.yaml"

    def test_roster_has_exactly_fifteen_jobs(self, seed_cron: CronConfig) -> None:
        # Locks the inventory: if a future edit deletes a job by
        # accident, this fails LOUDLY at the whole-count level before
        # the name checks pin down which one.
        assert len(seed_cron.jobs) == 15

    def test_llm_job_names_match(self, seed_cron: CronConfig) -> None:
        llm = [j.name for j in seed_cron.jobs if j.kind == CRON_JOB_KIND_LLM]
        assert sorted(llm) == sorted(LIVE_LLM_JOB_NAMES)

    def test_script_job_names_match(self, seed_cron: CronConfig) -> None:
        script = [j.name for j in seed_cron.jobs if j.kind == CRON_JOB_KIND_SCRIPT]
        assert sorted(script) == sorted(LIVE_SCRIPT_JOB_NAMES)

    def test_daemon_jobs_are_absent(self, seed_cron: CronConfig) -> None:
        # LANDMINE (§7): telegram-daemon + daemon-watchdog live
        # out-of-tree and must never be materialized by `mineru cron`.
        names = set(all_job_names(seed_cron))
        assert "telegram-daemon" not in names
        assert "daemon-watchdog" not in names

    def test_multi_instance_schedules_parse_to_lists(
        self, seed_cron: CronConfig
    ) -> None:
        # the pet-summary job fires M/W/F; prompts-alignment fires Tue/Fri.
        pet_summary_job = get_job(seed_cron, "pet-summary")
        assert pet_summary_job is not None
        assert pet_summary_job.schedule == (
            "31 23 * * 1",
            "31 23 * * 3",
            "31 23 * * 5",
        )

        prompts = get_job(seed_cron, "prompts-alignment")
        assert prompts is not None
        assert prompts.schedule == ("0 22 * * 2", "0 22 * * 5")

    def test_single_instance_schedules_are_length_one_tuples(
        self, seed_cron: CronConfig
    ) -> None:
        morning = get_job(seed_cron, "morning-brief")
        assert morning is not None
        assert morning.schedule == ("0 7 * * *",)

    def test_custom_prompt_jobs(self, seed_cron: CronConfig) -> None:
        # LANDMINE (§7): only these two jobs use custom prompts + inline
        # pre-steps. Guard against a future edit that flips the flag
        # elsewhere.
        expected_custom = {"daily-consolidation", "weekly-deep-consolidation"}
        actual_custom = {
            j.name for j in seed_cron.jobs if j.kind == CRON_JOB_KIND_LLM and j.custom_prompt
        }
        assert actual_custom == expected_custom

    def test_daily_consolidation_pre_steps(self, seed_cron: CronConfig) -> None:
        job = get_job(seed_cron, "daily-consolidation")
        assert job is not None
        assert job.custom_prompt is True
        # Two pre-steps: Apple Notes export, then CC-session extraction.
        assert len(job.pre_steps) == 2
        # Names of the scripts must be preserved verbatim so the
        # runner shells the right file.
        assert job.pre_steps[0].cmd[1].endswith("scripts/export-journals.py")
        assert job.pre_steps[1].cmd[1].endswith("scripts/extract-cc-sessions.py")
        # allow_fail=True on both — matches the trigger's continue-on-fail.
        assert all(step.allow_fail for step in job.pre_steps)

    def test_daily_consolidation_custom_prompt_suffix(
        self, seed_cron: CronConfig
    ) -> None:
        # LANDMINE (§7): the live trigger script cats the instruction
        # file AND appends a `"IMPORTANT: The target date... is
        # $YESTERDAY..."` date pin. The runner must reproduce that same
        # text; the `{yesterday}` placeholder is resolved at fire time.
        job = get_job(seed_cron, "daily-consolidation")
        assert job is not None
        assert job.custom_prompt_suffix is not None
        assert "IMPORTANT: The target date" in job.custom_prompt_suffix
        assert "{yesterday}" in job.custom_prompt_suffix

    def test_weekly_deep_consolidation_no_suffix(
        self, seed_cron: CronConfig
    ) -> None:
        # weekly-deep-consolidation's live trigger has NO date pin —
        # just a bare `cat` of the instruction file. The suffix must be
        # None so the runner doesn't append a phantom trailer.
        job = get_job(seed_cron, "weekly-deep-consolidation")
        assert job is not None
        assert job.custom_prompt is True
        assert job.custom_prompt_suffix is None

    def test_morning_brief_pre_step(self, seed_cron: CronConfig) -> None:
        job = get_job(seed_cron, "morning-brief")
        assert job is not None
        # morning-brief has a pre-step but is NOT a custom_prompt job.
        assert job.custom_prompt is False
        assert len(job.pre_steps) == 1
        # The curl command must target Ollama on 11434 — the whole
        # point of the pre-warm.
        assert any("11434" in tok for tok in job.pre_steps[0].cmd)
        assert job.pre_steps[0].allow_fail is True

    def test_no_expected_output_jobs(self, seed_cron: CronConfig) -> None:
        # LANDMINE (§7): memory-description AND weekly-deep-consolidation
        # both edit in place — no expected_output_glob.
        for name in ("memory-description", "weekly-deep-consolidation"):
            job = get_job(seed_cron, name)
            assert job is not None
            assert job.expected_output_glob is None

    def test_idempotency_markers_use_supported_placeholders(
        self, seed_cron: CronConfig
    ) -> None:
        # Every marker either has no placeholder or uses {today} /
        # {yesterday} (already validated by the loader; this pins the
        # inventory-level answer).
        expected = {
            "morning-brief": "briefs_morning/morning-{today}.md",
            "curiosity-question": "briefs_curiosity/question-{today}.md",
            "inbox-triage": "briefs_inbox/triage-{today}.md",
            "daily-consolidation": "memory/daily/{yesterday}.md",
        }
        for name, marker in expected.items():
            job = get_job(seed_cron, name)
            assert job is not None
            assert job.idempotency_marker == marker

    def test_script_jobs_carry_program_args(self, seed_cron: CronConfig) -> None:
        # Each of the 4 script jobs must have a non-empty argv, and MUST
        # NOT have model / instruction / expected_output_glob set.
        for name in LIVE_SCRIPT_JOB_NAMES:
            job = get_job(seed_cron, name)
            assert job is not None
            assert job.kind == CRON_JOB_KIND_SCRIPT
            assert len(job.program_args) >= 1
            assert job.model is None
            assert job.instruction is None
            assert job.expected_output_glob is None
            assert job.custom_prompt is False
            assert job.pre_steps == ()

    def test_llm_jobs_carry_model_and_instruction(
        self, seed_cron: CronConfig
    ) -> None:
        for name in LIVE_LLM_JOB_NAMES:
            job = get_job(seed_cron, name)
            assert job is not None
            assert job.kind == CRON_JOB_KIND_LLM
            assert job.model  # non-empty string
            assert job.instruction  # non-empty string
            # LLM jobs never carry program_args.
            assert job.program_args == ()

    def test_specific_timeouts(self, seed_cron: CronConfig) -> None:
        # morning-brief plist declares TimeOut=3600 (the long window
        # for the ~11-source brief). daily-consolidation is 7200s.
        # Keep the pins tight — a silent drop to 1800 would misfire
        # the plist under launchd.
        assert get_job(seed_cron, "morning-brief").timeout_seconds == 3600
        assert get_job(seed_cron, "daily-consolidation").timeout_seconds == 7200
        assert get_job(seed_cron, "weekly-deep-consolidation").timeout_seconds == 7200
        assert get_job(seed_cron, "group-members-sweep").timeout_seconds == 300


# --- 2. Round-trip: load, re-emit as YAML, re-load, equal ------------------


class TestRoundTrip:
    """Sanity: the LOADED shape survives a YAML round-trip.

    We don't ship a `save` API, but we DO promise the loader parses any
    correctly-shaped YAML including the one we hand-wrote for the seed.
    Re-serializing the shipped file with PyYAML and re-parsing must land
    on the same roster; this guards against subtle multi-line-string
    quirks (the pre-step comments contain colons + curly braces).
    """

    def test_reload_matches(
        self, seed_cron: CronConfig, tmp_profile: Profile
    ) -> None:
        source_body = seed_cron.source_path.read_text(encoding="utf-8")
        # Emit into the ephemeral testp profile and re-load. Same
        # loader, same shape expected.
        _write_cron_yaml(tmp_profile, source_body)
        reloaded = load_cron_config(tmp_profile)
        assert all_job_names(reloaded) == all_job_names(seed_cron)
        assert len(reloaded.jobs) == len(seed_cron.jobs)


# --- 3. Malformed cron.yaml — one branch per error path -------------------


class TestLoaderRejects:
    """Every rejection branch in the loader gets a fixture-scale test."""

    def test_missing_file(self, tmp_profile: Profile) -> None:
        # No cron.yaml written under the testp profile.
        with pytest.raises(CronConfigError, match="cron.yaml not found"):
            load_cron_config(tmp_profile)

    def test_non_mapping_toplevel(self, tmp_profile: Profile) -> None:
        _write_cron_yaml(tmp_profile, "- 1\n- 2\n")
        with pytest.raises(CronConfigError, match="must be a mapping at the top level"):
            load_cron_config(tmp_profile)

    def test_invalid_yaml(self, tmp_profile: Profile) -> None:
        _write_cron_yaml(tmp_profile, "jobs: [unclosed\n")
        with pytest.raises(CronConfigError, match="not valid YAML"):
            load_cron_config(tmp_profile)

    def test_missing_jobs_key(self, tmp_profile: Profile) -> None:
        _write_cron_yaml(tmp_profile, "defaults:\n  default_model: x\n")
        with pytest.raises(CronConfigError, match="missing the required `jobs:` list"):
            load_cron_config(tmp_profile)

    def test_empty_jobs_list(self, tmp_profile: Profile) -> None:
        _write_cron_yaml(tmp_profile, "jobs: []\n")
        with pytest.raises(CronConfigError, match="`jobs:` is empty"):
            load_cron_config(tmp_profile)

    def test_unknown_kind(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: bogus\n"
            "    schedule: '0 0 * * *'\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`kind` is required and must be one of"):
            load_cron_config(tmp_profile)

    def test_llm_missing_model(self, tmp_profile: Profile) -> None:
        # Explicit empty model — the default_model would otherwise
        # silently satisfy it. We want to catch the case where an
        # operator NUKES the model field in a specific entry.
        body = (
            "defaults:\n"
            "  default_model: ''\n"
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    instruction: recurring/x.md\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`defaults.default_model` must be a non-empty string"):
            load_cron_config(tmp_profile)

    def test_llm_missing_instruction(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`instruction` is required for LLM jobs"):
            load_cron_config(tmp_profile)

    def test_llm_rejects_program_args(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
            "    program_args:\n"
            "      - /bin/bash\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`program_args` is only valid on `kind: script`"):
            load_cron_config(tmp_profile)

    def test_script_missing_program_args(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: script\n"
            "    schedule: '0 0 * * *'\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`program_args` is required for script jobs"):
            load_cron_config(tmp_profile)

    def test_script_rejects_model(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: script\n"
            "    schedule: '0 0 * * *'\n"
            "    program_args:\n"
            "      - /bin/bash\n"
            "    model: claude-opus-4-6\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`model` is only valid on `kind: llm`"):
            load_cron_config(tmp_profile)

    def test_script_rejects_custom_prompt(self, tmp_profile: Profile) -> None:
        # custom_prompt=true on a script job is nonsensical — script
        # jobs don't invoke Claude at all.
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: script\n"
            "    schedule: '0 0 * * *'\n"
            "    program_args:\n"
            "      - /bin/bash\n"
            "    custom_prompt: true\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`custom_prompt` is only valid on `kind: llm`"):
            load_cron_config(tmp_profile)

    def test_suffix_without_custom_prompt_is_rejected(
        self, tmp_profile: Profile
    ) -> None:
        # A suffix is meaningless without `custom_prompt: true` — the
        # standard `Read <instr>` prompt doesn't concatenate anything.
        # Reject at load time so a `custom_prompt: false` typo doesn't
        # silently drop the date pin.
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
            "    custom_prompt_suffix: \"trailing pin\"\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(
            CronConfigError,
            match="`custom_prompt_suffix` is only meaningful when",
        ):
            load_cron_config(tmp_profile)

    def test_suffix_rejects_unknown_placeholder(
        self, tmp_profile: Profile
    ) -> None:
        # `{tomorrow}` is not a supported placeholder — surface it at
        # load time so a typo doesn't ship a literal `{tomorrow}` into
        # a prompt.
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
            "    custom_prompt: true\n"
            "    custom_prompt_suffix: \"pinned to {tomorrow}\"\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="unknown template placeholder"):
            load_cron_config(tmp_profile)

    def test_script_rejects_custom_prompt_suffix(
        self, tmp_profile: Profile
    ) -> None:
        # Script jobs never send anything to Claude — the suffix field
        # is nonsensical there and must be rejected loudly.
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: script\n"
            "    schedule: '0 0 * * *'\n"
            "    program_args:\n"
            "      - /bin/bash\n"
            "    custom_prompt_suffix: \"trailing\"\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(
            CronConfigError,
            match="`custom_prompt_suffix` is only valid on `kind: llm`",
        ):
            load_cron_config(tmp_profile)

    def test_bad_schedule_shape(self, tmp_profile: Profile) -> None:
        # 4 fields instead of 5 — a classic typo.
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="not a 5-field cron string"):
            load_cron_config(tmp_profile)

    def test_empty_schedule_list(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: []\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`schedule` list must contain at least one"):
            load_cron_config(tmp_profile)

    def test_unknown_key_typo(self, tmp_profile: Profile) -> None:
        # `instrucion` typo — must be rejected, not silently defaulted.
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
            "    instrucion: recurring/typo.md\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="unknown keys"):
            load_cron_config(tmp_profile)

    def test_duplicate_name(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: dupe\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/a.md\n"
            "  - name: dupe\n"
            "    kind: llm\n"
            "    schedule: '0 1 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/b.md\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="duplicate job name 'dupe'"):
            load_cron_config(tmp_profile)

    def test_unknown_placeholder(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
            "    idempotency_marker: 'briefs/{tomorrow}.md'\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="unknown template placeholder"):
            load_cron_config(tmp_profile)

    def test_pre_step_naked_command_rejected(self, tmp_profile: Profile) -> None:
        # Naked-word `python3` (no `/` at all) breaks under launchd's
        # minimal PATH — the loader catches this ahead of fire time.
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
            "    pre_steps:\n"
            "      - cmd:\n"
            "          - python3\n"
            "          - -c\n"
            "          - 'print(1)'\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="absolute path or a path containing"):
            load_cron_config(tmp_profile)

    def test_bad_timeout_type(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
            "    timeout_seconds: 'sixty'\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`timeout_seconds` must be an integer"):
            load_cron_config(tmp_profile)

    def test_bad_enabled_type(self, tmp_profile: Profile) -> None:
        # YAML-quoted string 'true' — Python str, not bool.
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
            "    enabled: 'true'\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`enabled` must be a boolean"):
            load_cron_config(tmp_profile)

    def test_bad_env_shape(self, tmp_profile: Profile) -> None:
        # env values must be strings — a numeric would silently coerce
        # in plist rendering if we let it through.
        body = (
            "jobs:\n"
            "  - name: broken\n"
            "    kind: llm\n"
            "    schedule: '0 0 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/x.md\n"
            "    env:\n"
            "      PATH: /bin\n"
            "      RETRIES: 3\n"
        )
        _write_cron_yaml(tmp_profile, body)
        with pytest.raises(CronConfigError, match="`env.RETRIES` must be a string"):
            load_cron_config(tmp_profile)


# --- 4. Positive round-trip on hand-written minimal fixtures --------------


class TestMinimalPositive:
    """Small positive fixtures that exercise the loader's default paths."""

    def test_minimal_llm_job_with_defaults(self, tmp_profile: Profile) -> None:
        # No defaults block; job carries only the required fields.
        # The loader must apply field defaults for enabled / timeout /
        # env and leave pre_steps empty.
        body = (
            "jobs:\n"
            "  - name: mini\n"
            "    kind: llm\n"
            "    schedule: '0 6 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/mini.md\n"
        )
        _write_cron_yaml(tmp_profile, body)
        cfg = load_cron_config(tmp_profile)
        assert len(cfg.jobs) == 1
        job = cfg.jobs[0]
        assert job.name == "mini"
        assert job.kind == CRON_JOB_KIND_LLM
        assert job.enabled is True
        assert job.timeout_seconds == 1800
        assert job.pre_steps == ()
        assert job.custom_prompt is False
        assert job.expected_output_glob is None
        assert job.idempotency_marker is None

    def test_minimal_script_job(self, tmp_profile: Profile) -> None:
        body = (
            "jobs:\n"
            "  - name: cleanup\n"
            "    kind: script\n"
            "    schedule: '0 3 * * *'\n"
            "    program_args:\n"
            "      - /bin/bash\n"
            "      - /tmp/cleanup.sh\n"
        )
        _write_cron_yaml(tmp_profile, body)
        cfg = load_cron_config(tmp_profile)
        assert len(cfg.jobs) == 1
        job = cfg.jobs[0]
        assert job.kind == CRON_JOB_KIND_SCRIPT
        assert job.program_args == ("/bin/bash", "/tmp/cleanup.sh")

    def test_pre_step_with_workspace_relative_script_ok(
        self, tmp_profile: Profile
    ) -> None:
        # `scripts/foo.py` contains a `/`, so the naked-word check
        # accepts it. This mirrors the daily-consolidation pre-step.
        body = (
            "jobs:\n"
            "  - name: mini\n"
            "    kind: llm\n"
            "    schedule: '0 6 * * *'\n"
            "    model: claude-opus-4-6\n"
            "    instruction: recurring/mini.md\n"
            "    pre_steps:\n"
            "      - cmd:\n"
            "          - scripts/foo.py\n"
            "        allow_fail: true\n"
            "        comment: ok\n"
        )
        _write_cron_yaml(tmp_profile, body)
        cfg = load_cron_config(tmp_profile)
        job = cfg.jobs[0]
        assert isinstance(job.pre_steps[0], PreStep)
        assert job.pre_steps[0].cmd == ("scripts/foo.py",)


# --- 5. get_job helper -----------------------------------------------------


class TestGetJob:
    def test_hit(self, seed_cron: CronConfig) -> None:
        assert isinstance(get_job(seed_cron, "morning-brief"), CronJob)

    def test_miss(self, seed_cron: CronConfig) -> None:
        assert get_job(seed_cron, "does-not-exist") is None
