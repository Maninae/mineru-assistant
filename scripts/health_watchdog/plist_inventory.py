"""Inventory of the workspace's launchd plists: which labels are services vs jobs.

Python 3.9 compatible (see config.py).

A KeepAlive plist (bool True or a conditions dict) is a long-running service and
its whole process tree is off-limits to the hung-job killer. Everything else with
StartInterval / StartCalendarInterval / RunAtLoad is a scheduled job.
"""

import plistlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Set

from health_watchdog.config import LAUNCHD_PREFIX, LAUNCH_AGENTS_DIR, SERVICE_LABEL_ALLOWLIST


@dataclass
class LaunchdJob:
    """One plist, reduced to what the selector needs."""

    label: str
    keepalive: bool
    scheduled: bool
    program_arguments: List[str] = field(default_factory=list)

    @property
    def suffix(self) -> str:
        """Label with the shared prefix removed, e.g. "morning-brief"."""
        return self.label.split(".", 2)[-1] if "." in self.label else self.label


def classify_plist(plist: Dict[str, Any]) -> LaunchdJob:
    """Pure classification of one parsed plist dict."""
    keepalive_value = plist.get("KeepAlive", False)
    keepalive = bool(keepalive_value) if not isinstance(keepalive_value, dict) else True
    scheduled = any(key in plist for key in ("StartInterval", "StartCalendarInterval", "RunAtLoad"))
    return LaunchdJob(
        label=str(plist.get("Label", "")),
        keepalive=keepalive,
        scheduled=scheduled,
        program_arguments=[str(arg) for arg in plist.get("ProgramArguments", [])],
    )


def load_launchd_jobs(
    launch_agents_dir: Path = LAUNCH_AGENTS_DIR, prefix: str = LAUNCHD_PREFIX
) -> List[LaunchdJob]:
    """Parse every `<prefix>.*.plist`; unreadable plists are skipped, not fatal."""
    jobs = []  # type: List[LaunchdJob]
    for plist_path in sorted(launch_agents_dir.glob(prefix + ".*.plist")):
        try:
            with open(plist_path, "rb") as handle:
                jobs.append(classify_plist(plistlib.load(handle)))
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
    return jobs


def service_labels(jobs: List[LaunchdJob], prefix: str = LAUNCHD_PREFIX) -> Set[str]:
    """Labels protected from the killer: KeepAlive plists plus the explicit allowlist."""
    protected = {job.label for job in jobs if job.keepalive}
    protected.update("%s.%s" % (prefix, suffix) for suffix in SERVICE_LABEL_ALLOWLIST)
    return protected
