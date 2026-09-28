"""Loader for `profiles/<name>/cron.yaml`.

Reads the cron.yaml alongside `profile.yaml`, validates it against the
dataclass model in `mineru_cli.cron.model`, and returns an immutable
`CronConfig`. This module is READ-ONLY and PURELY DECLARATIVE — no live
side effects (no launchd writes, no plist materialization, no CC
invocation). Consumers (`verbs/cron.py`, the plist template) are the
places where any of that happens.

Design notes:

  - The loader intentionally re-implements small validation rather than
    reaching for pydantic. The project's declared dependency floor is
    `typer + PyYAML` (pyproject.toml); adding pydantic would balloon
    the pipx install and force a hard version bump. The Profile loader
    at `mineru_cli/profile/loader.py` sets the precedent.
  - Every failure raises `CronConfigError` with the exact field name +
    absolute path in the message. Silent coercion (str-cast, empty-list
    tolerance) is forbidden per CODING_STYLE.md — a bad cron.yaml must
    surface at load time, not while launchd is trying to fire.
  - Schedule strings are validated to a 5-field cron shape only; deeper
    semantic checks (weekday range, hour range) are left to the plist
    materializer where the numeric fields are already being parsed for
    the StartCalendarInterval dict.
  - The loader does NOT touch `~/Library/LaunchAgents`, does NOT read the
    live plists, and does NOT execute anything. It parses YAML, builds
    the model, and returns.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from mineru_cli.cron.model import (
    CRON_JOB_KIND_LLM,
    CRON_JOB_KIND_SCRIPT,
    CRON_JOB_KINDS,
    CronConfig,
    CronDefaults,
    CronJob,
    PreStep,
    get_job as _model_get_job,
)
from mineru_cli.profile.schema import Profile


# --- Constants -------------------------------------------------------------


# Default cron.yaml filename inside a profile directory.
CRON_YAML_FILENAME = "cron.yaml"

# A cron string is exactly 5 whitespace-separated fields. We do NOT
# validate the individual fields further here (ranges, step values,
# lists) — the plist materializer parses those. Rejecting the wrong
# shape at load time still catches the most common typo (a 4-field or
# 6-field string) which would otherwise silently misfire in launchd.
_CRON_FIELD_RE = re.compile(r"^\S+(\s+\S+){4}$")

# Template placeholders permitted inside `idempotency_marker` and
# `expected_output_glob`. The consumer (`mineru cron run`, the guard
# helper) resolves these at execution time using the system-TZ date,
# matching the live `date +%Y-%m-%d` / `date -v-1d '+%Y-%m-%d'` calls.
# We only VALIDATE that the placeholders present in the config are ones
# the runner will know how to resolve; unrecognized placeholders would
# silently pass through and appear literally in a filename.
_KNOWN_MARKER_PLACEHOLDERS = frozenset({"today", "yesterday"})
_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


# --- Errors ----------------------------------------------------------------


class CronConfigError(RuntimeError):
    """Raised on any loader failure (missing file, bad YAML, invalid job).

    Message convention mirrors `ProfileError`: name the absolute path and
    the dotted field so a downstream Typer handler can render it verbatim.
    """


# --- Public API ------------------------------------------------------------


def default_cron_yaml_path(profile: Profile) -> Path:
    """Return the cron.yaml path for `profile`.

    Kept as a helper so tests can pin the exact path the loader consults
    against a fixture. Uses `profile.profile_root` (the directory that
    holds profile.yaml) so a fork-per-profile layout Just Works — the
    cron.yaml is a sibling of profile.yaml.
    """
    return profile.profile_root / CRON_YAML_FILENAME


def load_cron_config(profile: Profile) -> CronConfig:
    """Read + validate `<profile_root>/cron.yaml`, return `CronConfig`.

    Args:
        profile: an already-loaded `Profile` (from
            `mineru_cli.profile.load_active_profile`). We DO NOT re-read
            profile.yaml here — the caller is expected to have hydrated
            it already, and mixing profile + cron loads in one call
            invites subtle drift.

    Returns:
        Immutable `CronConfig`.

    Raises:
        CronConfigError: cron.yaml is missing, unreadable, malformed, or
            declares an invalid job.
    """
    cron_yaml = default_cron_yaml_path(profile)

    if not cron_yaml.exists():
        raise CronConfigError(
            f"profile {profile.name!r}: cron.yaml not found at {cron_yaml}. "
            "Create it (see profiles/mineru/cron.yaml for the seed) — the "
            "cron surface has no implicit defaults."
        )

    try:
        raw_text = cron_yaml.read_text(encoding="utf-8")
    except OSError as exc:
        raise CronConfigError(
            f"profile {profile.name!r}: could not read {cron_yaml}: "
            f"{type(exc).__name__}"
        ) from exc

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise CronConfigError(
            f"profile {profile.name!r}: cron.yaml at {cron_yaml} is not valid "
            f"YAML ({type(exc).__name__}). Fix the file and retry."
        ) from exc

    if not isinstance(data, dict):
        raise CronConfigError(
            f"profile {profile.name!r}: cron.yaml at {cron_yaml} must be a "
            f"mapping at the top level; got {type(data).__name__}."
        )

    defaults = _parse_defaults(data.get("defaults"), source=cron_yaml)
    jobs = _parse_jobs(
        data.get("jobs"),
        defaults=defaults,
        source=cron_yaml,
    )

    return CronConfig(
        jobs=jobs,
        defaults=defaults,
        source_path=cron_yaml,
    )


# Public re-export so callers only need one import. Same semantics as
# `mineru_cli.cron.model.get_job`.
def get_job(config: CronConfig, name: str) -> Optional[CronJob]:
    """Return the job named `name`, or None. Convenience re-export."""
    return _model_get_job(config, name)


# --- Internals: defaults ---------------------------------------------------


def _parse_defaults(raw: Any, *, source: Path) -> CronDefaults:
    """Validate + hydrate the top-level `defaults:` block.

    Missing block: return `CronDefaults()` (field defaults). Present but
    non-mapping: raise — a scalar or list under `defaults:` is a config
    error the operator wants to see immediately, not silently ignore.
    """
    if raw is None:
        return CronDefaults()
    if not isinstance(raw, dict):
        raise CronConfigError(
            f"cron.yaml at {source}: `defaults:` must be a mapping; "
            f"got {type(raw).__name__}."
        )

    default_model = raw.get("default_model", CronDefaults.default_model)
    if not isinstance(default_model, str) or not default_model:
        raise CronConfigError(
            f"cron.yaml at {source}: `defaults.default_model` must be a "
            f"non-empty string; got {default_model!r}."
        )

    default_timeout = raw.get("default_timeout", CronDefaults.default_timeout)
    if not isinstance(default_timeout, int) or isinstance(default_timeout, bool):
        # `bool` is a subclass of `int` in Python; a stray `true`/`false`
        # would otherwise slip through the int check and produce a plist
        # TimeOut of 0 or 1. Reject explicitly.
        raise CronConfigError(
            f"cron.yaml at {source}: `defaults.default_timeout` must be an "
            f"integer (seconds); got {type(default_timeout).__name__}."
        )
    if default_timeout <= 0:
        raise CronConfigError(
            f"cron.yaml at {source}: `defaults.default_timeout` must be "
            f"positive; got {default_timeout}."
        )

    default_env = _parse_env_dict(
        raw.get("default_env", {}), dotted="defaults.default_env", source=source
    )

    default_wd = raw.get("default_working_directory", "")
    if not isinstance(default_wd, str):
        raise CronConfigError(
            f"cron.yaml at {source}: `defaults.default_working_directory` "
            f"must be a string; got {type(default_wd).__name__}."
        )

    return CronDefaults(
        default_model=default_model,
        default_timeout=default_timeout,
        default_env=default_env,
        default_working_directory=default_wd,
    )


def _parse_env_dict(raw: Any, *, dotted: str, source: Path) -> Dict[str, str]:
    """Validate an env-vars mapping (all keys + values must be strings)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CronConfigError(
            f"cron.yaml at {source}: `{dotted}` must be a mapping "
            f"of string keys to string values; got {type(raw).__name__}."
        )
    out: Dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise CronConfigError(
                f"cron.yaml at {source}: `{dotted}` has a non-string / empty "
                f"key {key!r}."
            )
        if not isinstance(value, str):
            raise CronConfigError(
                f"cron.yaml at {source}: `{dotted}.{key}` must be a string; "
                f"got {type(value).__name__}."
            )
        out[key] = value
    return out


# --- Internals: jobs -------------------------------------------------------


def _parse_jobs(
    raw: Any, *, defaults: CronDefaults, source: Path
) -> Tuple[CronJob, ...]:
    """Validate + hydrate the `jobs:` list.

    A missing `jobs:` key is a config error — cron.yaml exists to describe
    jobs, and an empty file would silently install no plists on a live
    `mineru cron install`. Better to fail loud than to have the operator
    wonder why nothing scheduled.
    """
    if raw is None:
        raise CronConfigError(
            f"cron.yaml at {source} is missing the required `jobs:` list."
        )
    if not isinstance(raw, list):
        raise CronConfigError(
            f"cron.yaml at {source}: `jobs:` must be a list; "
            f"got {type(raw).__name__}."
        )
    if not raw:
        raise CronConfigError(
            f"cron.yaml at {source}: `jobs:` is empty. Add at least one job "
            "or delete the cron.yaml entirely if you truly want no schedule."
        )

    seen_names: set[str] = set()
    jobs: List[CronJob] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CronConfigError(
                f"cron.yaml at {source}: `jobs[{index}]` must be a mapping; "
                f"got {type(entry).__name__}."
            )
        job = _parse_one_job(
            entry, index=index, defaults=defaults, source=source
        )
        if job.name in seen_names:
            raise CronConfigError(
                f"cron.yaml at {source}: duplicate job name {job.name!r} "
                f"at `jobs[{index}]` (already declared earlier). Names must "
                "be unique — they map 1:1 to launchd labels."
            )
        seen_names.add(job.name)
        jobs.append(job)

    return tuple(jobs)


def _parse_one_job(
    entry: Dict[str, Any], *, index: int, defaults: CronDefaults, source: Path
) -> CronJob:
    """Validate one entry from the `jobs:` list.

    Field-by-field with a `_where` prefix in every error message so an
    operator can immediately locate which entry failed. `entry` may
    include unknown keys — we raise on them rather than silently ignore
    so a typo (`schdule:`) fails loud instead of silently defaulting the
    real field.
    """
    _where = f"cron.yaml at {source}: `jobs[{index}]`"

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CronConfigError(f"{_where}: `name` is required and must be a non-empty string.")
    name = name.strip()

    _where_named = f"cron.yaml at {source}: job {name!r} (`jobs[{index}]`)"

    kind = entry.get("kind")
    if kind not in CRON_JOB_KINDS:
        raise CronConfigError(
            f"{_where_named}: `kind` is required and must be one of "
            f"{list(CRON_JOB_KINDS)}; got {kind!r}."
        )

    schedule = _parse_schedule(entry.get("schedule"), where=_where_named)
    enabled = _parse_bool(entry.get("enabled", True), field="enabled", where=_where_named)

    # Per-job env: fall back to defaults.default_env if omitted so a
    # cron.yaml that just says `env:` on the block that diverges keeps
    # the common case terse. A present-but-empty block resets to {}
    # (operator explicitly wants no env — legal, even if unusual).
    if "env" in entry:
        env = _parse_env_dict(entry["env"], dotted="env", source=source)
    else:
        env = dict(defaults.default_env)

    if "timeout_seconds" in entry:
        timeout_raw = entry["timeout_seconds"]
        if not isinstance(timeout_raw, int) or isinstance(timeout_raw, bool):
            raise CronConfigError(
                f"{_where_named}: `timeout_seconds` must be an integer; "
                f"got {type(timeout_raw).__name__}."
            )
        if timeout_raw <= 0:
            raise CronConfigError(
                f"{_where_named}: `timeout_seconds` must be positive; "
                f"got {timeout_raw}."
            )
        timeout = timeout_raw
    else:
        timeout = defaults.default_timeout

    working_directory = entry.get("working_directory")
    if working_directory is not None and not isinstance(working_directory, str):
        raise CronConfigError(
            f"{_where_named}: `working_directory` must be a string or null; "
            f"got {type(working_directory).__name__}."
        )

    # --- Kind-specific field validation ------------------------------------
    if kind == CRON_JOB_KIND_LLM:
        job = _parse_llm_job(
            entry,
            name=name,
            schedule=schedule,
            enabled=enabled,
            env=env,
            timeout=timeout,
            working_directory=working_directory,
            defaults=defaults,
            where=_where_named,
        )
    else:
        job = _parse_script_job(
            entry,
            name=name,
            schedule=schedule,
            enabled=enabled,
            env=env,
            timeout=timeout,
            working_directory=working_directory,
            where=_where_named,
        )

    _reject_unknown_keys(entry, kind=kind, where=_where_named)
    return job


def _parse_llm_job(
    entry: Dict[str, Any],
    *,
    name: str,
    schedule: Tuple[str, ...],
    enabled: bool,
    env: Dict[str, str],
    timeout: int,
    working_directory: Optional[str],
    defaults: CronDefaults,
    where: str,
) -> CronJob:
    """Validate + build a `kind='llm'` `CronJob`.

    LLM contract: `model` + `instruction` are required. `program_args`
    MUST NOT be present (that field is script-only; a stray value would
    silently ride along and confuse the plist materializer).
    `custom_prompt` and `pre_steps` are optional and default off / empty.
    """
    if "program_args" in entry:
        raise CronConfigError(
            f"{where}: `program_args` is only valid on `kind: script` jobs. "
            "For an LLM job, drop `program_args` and set `instruction:` + "
            "`model:` instead."
        )

    model = entry.get("model", defaults.default_model)
    if not isinstance(model, str) or not model:
        raise CronConfigError(
            f"{where}: `model` is required for LLM jobs and must be a "
            f"non-empty string; got {model!r}."
        )

    instruction = entry.get("instruction")
    if not isinstance(instruction, str) or not instruction:
        raise CronConfigError(
            f"{where}: `instruction` is required for LLM jobs and must be a "
            f"non-empty string (workspace-relative path to the recurring/*.md "
            f"file); got {instruction!r}."
        )

    expected_output_glob = entry.get("expected_output_glob")
    if expected_output_glob is not None and not isinstance(expected_output_glob, str):
        raise CronConfigError(
            f"{where}: `expected_output_glob` must be a string or null; "
            f"got {type(expected_output_glob).__name__}."
        )
    if isinstance(expected_output_glob, str):
        _validate_marker_placeholders(
            expected_output_glob, field="expected_output_glob", where=where
        )

    idempotency_marker = entry.get("idempotency_marker")
    if idempotency_marker is not None and not isinstance(idempotency_marker, str):
        raise CronConfigError(
            f"{where}: `idempotency_marker` must be a string or null; "
            f"got {type(idempotency_marker).__name__}."
        )
    if isinstance(idempotency_marker, str):
        _validate_marker_placeholders(
            idempotency_marker, field="idempotency_marker", where=where
        )

    custom_prompt = _parse_bool(
        entry.get("custom_prompt", False), field="custom_prompt", where=where
    )

    custom_prompt_suffix = entry.get("custom_prompt_suffix")
    if custom_prompt_suffix is not None and not isinstance(custom_prompt_suffix, str):
        raise CronConfigError(
            f"{where}: `custom_prompt_suffix` must be a string or null; "
            f"got {type(custom_prompt_suffix).__name__}."
        )
    if isinstance(custom_prompt_suffix, str):
        _validate_marker_placeholders(
            custom_prompt_suffix,
            field="custom_prompt_suffix",
            where=where,
        )
        if not custom_prompt:
            # A suffix without `custom_prompt: true` is meaningless —
            # the standard `Read <instr>` prompt doesn't concatenate it.
            # A typo like `custom_prompt: false` would silently drop the
            # suffix; reject at load time so the operator sees it.
            raise CronConfigError(
                f"{where}: `custom_prompt_suffix` is only meaningful when "
                "`custom_prompt: true`. Set `custom_prompt: true` or drop "
                "the suffix."
            )

    pre_steps = _parse_pre_steps(entry.get("pre_steps"), where=where)
    if pre_steps and not custom_prompt:
        # Not an error per se — a job MIGHT legitimately have a pre-step
        # without a custom prompt (e.g. morning-brief's Ollama pre-warm
        # runs before the standard `Read <instr>` prompt). So we allow
        # this. The check is deliberately absent; the docstring on
        # `CronJob.pre_steps` explains why.
        pass

    return CronJob(
        name=name,
        kind=CRON_JOB_KIND_LLM,
        schedule=schedule,
        enabled=enabled,
        model=model,
        instruction=instruction,
        expected_output_glob=expected_output_glob,
        idempotency_marker=idempotency_marker,
        custom_prompt=custom_prompt,
        custom_prompt_suffix=custom_prompt_suffix,
        pre_steps=pre_steps,
        program_args=(),
        timeout_seconds=timeout,
        working_directory=working_directory,
        env=env,
    )


def _parse_script_job(
    entry: Dict[str, Any],
    *,
    name: str,
    schedule: Tuple[str, ...],
    enabled: bool,
    env: Dict[str, str],
    timeout: int,
    working_directory: Optional[str],
    where: str,
) -> CronJob:
    """Validate + build a `kind='script'` `CronJob`.

    Script contract: `program_args` is required (the full argv launchd
    invokes). `model`, `instruction`, `expected_output_glob`,
    `idempotency_marker`, `custom_prompt`, and `pre_steps` MUST NOT be
    present — the trigger is a plain script and none of those knobs
    apply.
    """
    for llm_only in (
        "model",
        "instruction",
        "expected_output_glob",
        "idempotency_marker",
        "custom_prompt",
        "custom_prompt_suffix",
        "pre_steps",
    ):
        if llm_only in entry:
            raise CronConfigError(
                f"{where}: `{llm_only}` is only valid on `kind: llm` jobs. "
                f"A script job runs a plain argv, no LLM invocation."
            )

    program_args_raw = entry.get("program_args")
    if not isinstance(program_args_raw, list) or not program_args_raw:
        raise CronConfigError(
            f"{where}: `program_args` is required for script jobs and must "
            f"be a non-empty list of strings; got {program_args_raw!r}."
        )
    program_args: List[str] = []
    for i, arg in enumerate(program_args_raw):
        if not isinstance(arg, str) or not arg:
            raise CronConfigError(
                f"{where}: `program_args[{i}]` must be a non-empty string; "
                f"got {arg!r}."
            )
        program_args.append(arg)

    return CronJob(
        name=name,
        kind=CRON_JOB_KIND_SCRIPT,
        schedule=schedule,
        enabled=enabled,
        model=None,
        instruction=None,
        expected_output_glob=None,
        idempotency_marker=None,
        custom_prompt=False,
        pre_steps=(),
        program_args=tuple(program_args),
        timeout_seconds=timeout,
        working_directory=working_directory,
        env=env,
    )


def _parse_pre_steps(raw: Any, *, where: str) -> Tuple[PreStep, ...]:
    """Validate + hydrate the `pre_steps:` list on an LLM job.

    Missing / None -> empty tuple. Present -> must be a list of mappings,
    each with a non-empty `cmd:` list of strings, an optional bool
    `allow_fail:`, and an optional string `comment:`. `argv[0]` must be
    an absolute path OR a workspace-relative script path (`scripts/...`,
    `bin/...`, `python3` alone — absolute preferred). The check keeps
    launchd's minimal-PATH-in-cron-context surprise from happening.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CronConfigError(
            f"{where}: `pre_steps` must be a list of mappings; "
            f"got {type(raw).__name__}."
        )

    steps: List[PreStep] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CronConfigError(
                f"{where}: `pre_steps[{i}]` must be a mapping; "
                f"got {type(entry).__name__}."
            )
        cmd_raw = entry.get("cmd")
        if not isinstance(cmd_raw, list) or not cmd_raw:
            raise CronConfigError(
                f"{where}: `pre_steps[{i}].cmd` must be a non-empty list of "
                f"strings; got {cmd_raw!r}."
            )
        cmd: List[str] = []
        for j, arg in enumerate(cmd_raw):
            if not isinstance(arg, str) or not arg:
                raise CronConfigError(
                    f"{where}: `pre_steps[{i}].cmd[{j}]` must be a non-empty "
                    f"string; got {arg!r}."
                )
            cmd.append(arg)

        first = cmd[0]
        # Absolute path OR workspace-relative script path. We accept both
        # because the trigger scripts historically use either style; a
        # bare `python3` would fail under launchd's minimal PATH but a
        # `scripts/export-journals.py` is a well-known workspace-relative
        # convention. Naked-word commands ARE rejected here because they
        # are the exact class of bug launchd surfaces at fire time.
        if not (first.startswith("/") or "/" in first):
            raise CronConfigError(
                f"{where}: `pre_steps[{i}].cmd[0]` must be an absolute path "
                f"or a path containing `/` (workspace-relative script); "
                f"got {first!r}. Naked-word commands break under launchd's "
                "minimal PATH."
            )

        allow_fail = _parse_bool(
            entry.get("allow_fail", False),
            field=f"pre_steps[{i}].allow_fail",
            where=where,
        )
        comment_raw = entry.get("comment", "")
        if not isinstance(comment_raw, str):
            raise CronConfigError(
                f"{where}: `pre_steps[{i}].comment` must be a string; "
                f"got {type(comment_raw).__name__}."
            )

        known = {"cmd", "allow_fail", "comment"}
        unknown = [k for k in entry if k not in known]
        if unknown:
            raise CronConfigError(
                f"{where}: `pre_steps[{i}]` has unknown keys {sorted(unknown)!r}. "
                f"Allowed: {sorted(known)!r}."
            )

        steps.append(
            PreStep(cmd=tuple(cmd), allow_fail=allow_fail, comment=comment_raw)
        )

    return tuple(steps)


def _parse_schedule(raw: Any, *, where: str) -> Tuple[str, ...]:
    """Normalize `schedule:` to `Tuple[str, ...]`, validating shape.

    Accepts:
      - a single string `"0 7 * * *"` (single-instance schedule),
      - a list of strings `["31 23 * * 1", "31 23 * * 3", ...]`
        (multi-instance schedule; e.g. a pet-summary and prompts-alignment).

    Rejects everything else (missing key, empty list, non-cron shape).
    Only shape is enforced here — deeper semantics (weekday range 0-7,
    hour range 0-23) belong to the plist materializer.
    """
    if raw is None:
        raise CronConfigError(f"{where}: `schedule` is required.")
    if isinstance(raw, str):
        candidates: List[str] = [raw]
    elif isinstance(raw, list):
        if not raw:
            raise CronConfigError(
                f"{where}: `schedule` list must contain at least one cron string."
            )
        candidates = []
        for i, s in enumerate(raw):
            if not isinstance(s, str):
                raise CronConfigError(
                    f"{where}: `schedule[{i}]` must be a string; "
                    f"got {type(s).__name__}."
                )
            candidates.append(s)
    else:
        raise CronConfigError(
            f"{where}: `schedule` must be a string or a list of strings; "
            f"got {type(raw).__name__}."
        )

    for i, s in enumerate(candidates):
        s_stripped = s.strip()
        if not _CRON_FIELD_RE.match(s_stripped):
            hint = "" if len(candidates) == 1 else f"[{i}]"
            raise CronConfigError(
                f"{where}: `schedule{hint}` {s!r} is not a 5-field cron string "
                "(expected `MM HH DOM MON DOW`)."
            )
    return tuple(s.strip() for s in candidates)


def _parse_bool(raw: Any, *, field: str, where: str) -> bool:
    """Fail loud on a non-bool `bool`-typed field.

    YAML happily coerces `"true"` / `"yes"` at times; we want the strict
    Python bool so a typo (`enabled: no` -> Python False, but `enabled: "no"`
    -> Python str) surfaces at load time.
    """
    if not isinstance(raw, bool):
        raise CronConfigError(
            f"{where}: `{field}` must be a boolean (`true` or `false`); "
            f"got {raw!r} ({type(raw).__name__})."
        )
    return raw


def _validate_marker_placeholders(text: str, *, field: str, where: str) -> None:
    """Reject unknown `{placeholder}` tokens in a marker/glob string."""
    for match in _PLACEHOLDER_RE.finditer(text):
        placeholder = match.group(1)
        if placeholder not in _KNOWN_MARKER_PLACEHOLDERS:
            raise CronConfigError(
                f"{where}: `{field}` uses unknown template placeholder "
                f"{{{placeholder}}}; supported: "
                f"{sorted(_KNOWN_MARKER_PLACEHOLDERS)}."
            )


# Allowed keys per kind, so a typo raises loudly instead of silently
# defaulting the real field. The lists include the shared keys as well as
# the kind-specific ones so the check is a single set-diff at the end of
# `_parse_one_job`.
_SHARED_ALLOWED_KEYS = frozenset(
    {"name", "kind", "schedule", "enabled", "timeout_seconds",
     "working_directory", "env"}
)
_LLM_ALLOWED_KEYS = _SHARED_ALLOWED_KEYS | frozenset(
    {"model", "instruction", "expected_output_glob", "idempotency_marker",
     "custom_prompt", "custom_prompt_suffix", "pre_steps"}
)
_SCRIPT_ALLOWED_KEYS = _SHARED_ALLOWED_KEYS | frozenset({"program_args"})


def _reject_unknown_keys(entry: Dict[str, Any], *, kind: str, where: str) -> None:
    """Reject any key on `entry` that's not in the allowed set for `kind`.

    Keeps a typo like `instrucion:` from silently defaulting the real
    field to None. The individual-field validators above will already
    have raised for a WRONG-shape allowed key; this catches only the
    unknown-key case.
    """
    allowed = _LLM_ALLOWED_KEYS if kind == CRON_JOB_KIND_LLM else _SCRIPT_ALLOWED_KEYS
    unknown = sorted(k for k in entry if k not in allowed)
    if unknown:
        raise CronConfigError(
            f"{where}: unknown keys {unknown!r} for `kind: {kind}` job. "
            f"Allowed: {sorted(allowed)!r}."
        )
