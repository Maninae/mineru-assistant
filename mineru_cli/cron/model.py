"""Data model for `mineru cron` — the declarative schedule for one profile.

Frozen dataclasses that describe:

  - `PreStep`            one inline shell command run BEFORE the job's LLM
                         invocation (e.g. Ollama pre-warm, Apple Notes
                         export, CC-session extraction). Only meaningful
                         on LLM jobs.
  - `CronJob`            one scheduled unit; kind='llm' or 'script'.
  - `CronDefaults`       top-level defaults block from cron.yaml (model,
                         timeout, env, working directory).
  - `CronConfig`         the whole loaded cron.yaml plus provenance.

The data model is intentionally the ONLY thing this module ships. The
loader in `mineru_cli.cron.config` validates + hydrates from YAML; the
plist-materializer + verb code lives elsewhere and is fed a `CronConfig`.

Why frozen: a resolver / verb can hold a reference without worrying
about drift mid-invocation. To pick up a changed cron.yaml, call
`load_cron_config` again.

Why absolute paths in `program_args`: the live plists use absolute paths
(e.g. `/opt/homebrew/bin/python3`, `$MINERU_HOME/scripts/...`)
so launchd can dispatch without a PATH lookup. The loader preserves
that shape — the model stores exactly what launchd will run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Legal `kind` values. `enum.Enum` would be nicer but the loader consumes
# raw YAML strings and the surface here is small; a tuple + validation
# check keeps the model dependency-free.
CRON_JOB_KIND_LLM = "llm"
CRON_JOB_KIND_SCRIPT = "script"
CRON_JOB_KINDS: Tuple[str, ...] = (CRON_JOB_KIND_LLM, CRON_JOB_KIND_SCRIPT)


@dataclass(frozen=True)
class PreStep:
    """One inline shell command to run BEFORE the LLM invocation.

    Fields:
      cmd:        argv list. `argv[0]` should be an absolute path (the
                  live trigger scripts do this; keeps launchd's minimal
                  PATH from misfiring). Workspace-relative script paths
                  are also accepted (loader validation checks shape).
      allow_fail: if True, a non-zero exit from this step logs a warning
                  and CONTINUES (matches the live trigger scripts' use
                  of `|| { echo warning; }` around the pre-steps). If
                  False, a failure aborts the job.
      comment:    free-text intent — surfaces in `mineru cron plist`
                  output so an operator debugging the generated plist
                  can see WHY the pre-step exists.
    """

    cmd: Tuple[str, ...]
    allow_fail: bool = False
    comment: str = ""


@dataclass(frozen=True)
class CronJob:
    """One scheduled job. Kind='llm' (Claude Code) or 'script' (plain).

    Fields that apply to BOTH kinds:
      name:               launchd label suffix (`<prefix>.<name>.plist`)
                          and log directory name.
      kind:               'llm' | 'script'; validated against CRON_JOB_KINDS.
      schedule:           list of 5-field cron strings. A single-instance
                          job carries a 1-element list. Multi-instance
                          jobs (e.g. a 3x/week job on Mon/Wed/Fri and a 2x/week job on Tue/Fri)
                          carry a list of N. The loader normalizes both
                          `str` and `List[str]` YAML shapes to this.
      enabled:            when False, `mineru cron install` skips this job
                          (but `mineru cron list` still shows it, so the
                          operator sees what exists on paper).
      timeout_seconds:    plist TimeOut key. Defaults to 1800 (matches
                          most live plists); morning-brief / financial /
                          news / inbox all use 3600; consolidation jobs
                          use 7200.
      working_directory:  plist WorkingDirectory. Defaults to the profile
                          workspace absolute path at consumption time.
      env:                plist EnvironmentVariables. Full dict; the
                          loader applies `defaults.env` as a fallback
                          when the per-job block is omitted.

    Fields only meaningful when kind='llm':
      model:                 Claude model id (e.g. `claude-opus-4-6`).
                             REQUIRED; a script job MUST NOT set it.
      instruction:           path to the recurring/*.md file, relative to
                             the profile workspace. REQUIRED for LLM jobs;
                             MUST NOT be set on script jobs.
      expected_output_glob:  glob (workspace-relative) that must match a
                             file with mtime within the last 2 hours after
                             the LLM finishes; if it doesn't, `run_cc_job`
                             fires a failure alert. `None` for jobs whose
                             output is in-place (memory-description) or
                             free-form (weekly-deep-consolidation).
      idempotency_marker:    workspace-relative path with a supported
                             template placeholder (`{today}`, `{yesterday}`)
                             that, when it already exists and its mtime is
                             today's local date, causes the trigger to
                             skip cleanly. `None` when no guard is used.
      custom_prompt:         True when the job does NOT use the standard
                             `Read <instruction> and execute the job.`
                             prompt. Consumers must then read the
                             instruction file inline (see the live
                             daily-consolidation + weekly-deep-consolidation
                             trigger scripts). Legal ONLY on LLM jobs.
      custom_prompt_suffix:  optional per-job template string appended to
                             the instruction file's raw contents when
                             `custom_prompt: true`. `{today}` /
                             `{yesterday}` placeholders are resolved by
                             the runner using system-TZ dates, matching
                             the live trigger scripts' inline date pins
                             (e.g. daily-consolidation's YESTERDAY
                             "IMPORTANT: The target date..." block).
                             `None` is legal — weekly-deep-consolidation
                             uses a bare `cat <instr>` prompt with no
                             suffix. Meaningful ONLY when
                             `custom_prompt: true`; the loader rejects
                             the pairing custom_prompt=false + non-None
                             suffix so a typo can't silently degrade.
      pre_steps:             inline shell steps to run before the LLM. Only
                             meaningful on LLM jobs.

    Fields only meaningful when kind='script':
      program_args:          full argv (interpreter + script + args) launchd
                             will invoke via `ProgramArguments`. REQUIRED
                             for script jobs; MUST NOT be set on LLM jobs.
    """

    name: str
    kind: str
    schedule: Tuple[str, ...]
    enabled: bool = True
    # LLM-only
    model: Optional[str] = None
    instruction: Optional[str] = None
    expected_output_glob: Optional[str] = None
    idempotency_marker: Optional[str] = None
    custom_prompt: bool = False
    custom_prompt_suffix: Optional[str] = None
    pre_steps: Tuple[PreStep, ...] = ()
    # SCRIPT-only
    program_args: Tuple[str, ...] = ()
    # Shared
    timeout_seconds: int = 1800
    working_directory: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CronDefaults:
    """Top-level defaults block from cron.yaml.

    Every field has a sensible fallback so a cron.yaml that omits the
    entire `defaults:` block loads cleanly (all per-job entries then
    have to specify their env / working_directory / timeout / model
    explicitly, which is legal but noisy).

    Fields:
      default_model:              LLM jobs that omit `model:` inherit this.
      default_timeout:            jobs that omit `timeout_seconds:` inherit this.
      default_env:                jobs that omit `env:` inherit this dict.
      default_working_directory:  jobs that omit `working_directory:`
                                  inherit this. Consumers may further
                                  fall back to `profile.workspace_absolute`
                                  when this is empty (the loader does not
                                  auto-fill it, so the config file stays
                                  the single source of truth).
    """

    default_model: str = "claude-opus-4-6"
    default_timeout: int = 1800
    default_env: Dict[str, str] = field(default_factory=dict)
    default_working_directory: str = ""


@dataclass(frozen=True)
class CronConfig:
    """The whole loaded cron.yaml plus provenance.

    Fields:
      jobs:        tuple of `CronJob` in the order they appeared in YAML.
      defaults:    the resolved defaults block (never None; missing YAML
                   block yields the field-default `CronDefaults()`).
      source_path: absolute path to the loaded cron.yaml file — mirrors
                   `Profile.profile_yaml_path` for symmetry.
    """

    jobs: Tuple[CronJob, ...]
    defaults: CronDefaults
    source_path: Path


# Convenient lookup: `get_job(cfg, 'morning-brief')` for verbs / plist
# materializer. O(N) over the job list; N is 16 today so this is fine
# and the returned object is the same immutable dataclass instance the
# loader built.
def get_job(config: CronConfig, name: str) -> Optional[CronJob]:
    """Return the job named `name`, or None if absent.

    Kept alongside the data model so a caller has a stable public API
    for lookups without reaching into the internals. Verbs that need to
    fail loud on a miss should do `if get_job(cfg, name) is None: raise ...`
    at their own layer — this helper stays neutral.
    """
    for job in config.jobs:
        if job.name == name:
            return job
    return None


def all_job_names(config: CronConfig) -> List[str]:
    """Return every job name in declaration order.

    Handy for `mineru cron list` and for guard-tests that need to
    assert the full inventory hasn't regressed.
    """
    return [job.name for job in config.jobs]
