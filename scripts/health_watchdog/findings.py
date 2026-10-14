"""Finding and Severity: the unit of output every check produces.

Python 3.9 compatible (see config.py).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Severity(str, Enum):
    """Alert levels. Ordering (WARN < CRITICAL) drives escalation and cooldowns."""

    WARN = "WARN"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        """Numeric rank so escalation can be compared."""
        return SEVERITY_RANK[self]


SEVERITY_RANK = {
    Severity.WARN: 1,
    Severity.CRITICAL: 2,
}

SEVERITY_ICON = {
    Severity.WARN: "🟠",
    Severity.CRITICAL: "🔴",
}


@dataclass
class Finding:
    """One check's verdict for this run.

    - check_id: stable key for cooldown/recovery bookkeeping ("tcc", "memory", ...).
    - title: the one-line human headline.
    - details: short supporting lines (top processes, fix text).
    - action_taken: True when the watchdog changed the system (killed a job,
      stopped the browser); such findings bypass cooldowns and never "recover".
    - also_cools: other check_ids whose cooldown this finding starts when sent
      (a kill that reports SIGKILL survivors already covers the standing
      survivor check, so that one must not fire again five minutes later).
    - cooldown_seconds: overrides the severity's default cooldown (a daily
      heads-up uses 24 h instead of the WARN default).
    """

    check_id: str
    severity: Severity
    title: str
    details: List[str] = field(default_factory=list)
    action_taken: bool = False
    also_cools: List[str] = field(default_factory=list)
    cooldown_seconds: Optional[int] = None
