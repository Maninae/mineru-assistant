"""`mineru cron` subpackage — data model + cron.yaml loader.

Public API:

  - `CronConfig`, `CronDefaults`, `CronJob`, `PreStep`  — dataclass model.
  - `CRON_JOB_KIND_LLM`, `CRON_JOB_KIND_SCRIPT`, `CRON_JOB_KINDS`
                                                     — legal `kind` values.
  - `CronConfigError`                                — loader failure type.
  - `load_cron_config(profile)`                      — parse cron.yaml for a
                                                        loaded profile.
  - `get_job(config, name)`, `all_job_names(config)` — lookup helpers.
  - `default_cron_yaml_path(profile)`                — path used by the loader
                                                        (exposed for tests +
                                                        `mineru cron edit`).

Everything under this subpackage is READ-ONLY and PURELY DECLARATIVE. The
plist materializer, `mineru cron install`, and any launchd-touching code
lives outside this subpackage — this one just describes the schedule.
"""

from __future__ import annotations

from mineru_cli.cron.config import (
    CRON_YAML_FILENAME,
    CronConfigError,
    default_cron_yaml_path,
    get_job,
    load_cron_config,
)
from mineru_cli.cron.model import (
    CRON_JOB_KIND_LLM,
    CRON_JOB_KIND_SCRIPT,
    CRON_JOB_KINDS,
    CronConfig,
    CronDefaults,
    CronJob,
    PreStep,
    all_job_names,
)
from mineru_cli.cron.plist import PlistRenderError, render_plist

__all__ = [
    "CRON_JOB_KIND_LLM",
    "CRON_JOB_KIND_SCRIPT",
    "CRON_JOB_KINDS",
    "CRON_YAML_FILENAME",
    "CronConfig",
    "CronConfigError",
    "CronDefaults",
    "CronJob",
    "PlistRenderError",
    "PreStep",
    "all_job_names",
    "default_cron_yaml_path",
    "get_job",
    "load_cron_config",
    "render_plist",
]
