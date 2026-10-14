"""Persistent state and the alert bookkeeping that runs on top of it.

Python 3.9 compatible (see config.py).

State file (JSON under cache/) holds:
  - streaks:     check_id -> consecutive runs the raw condition held (sustained checks)
  - active:      check_id -> severity currently alerting (drives "recovered" lines)
  - last_alert:  check_id -> {"ts", "severity"} of the last message sent (cooldowns)
  - tcc_probe_pid: pid of a TCC probe that survived SIGKILL, so the next run does
                 not start another one while it lives
  - runs:        counter, for the log
"""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from health_watchdog.config import CRITICAL_COOLDOWN_SECONDS, WARN_COOLDOWN_SECONDS
from health_watchdog.findings import Finding, Severity

STATE_VERSION = 1


def default_state() -> Dict[str, Any]:
    """Fresh state for a first run or an unreadable file."""
    return {
        "version": STATE_VERSION,
        "streaks": {},
        "flags": {},
        "active": {},
        "last_alert": {},
        "survivors": {},
        "tcc_probe_pid": None,
        "runs": 0,
    }


def load_state(path: Path) -> Dict[str, Any]:
    """Read the state file; corruption or absence yields default_state()."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return default_state()
    if not isinstance(loaded, dict) or loaded.get("version") != STATE_VERSION:
        return default_state()
    state = default_state()
    state.update(loaded)
    return state


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """Atomic write (temp file + os.replace) so a crash mid-write cannot corrupt it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(temp_path, path)


def bump_streak(state: Dict[str, Any], check_id: str, condition_holds: bool) -> int:
    """Increment or reset the consecutive-run counter; return the new streak."""
    streaks = state.setdefault("streaks", {})
    streaks[check_id] = streaks.get(check_id, 0) + 1 if condition_holds else 0
    return streaks[check_id]


def hysteresis_flag(state: Dict[str, Any], key: str, is_bad: bool, is_clear: bool) -> bool:
    """Latch: set when `is_bad`, released only when `is_clear`, otherwise unchanged.

    Metrics such as swap percentage flap across any single line, so a check turns on
    at the WARN bar but turns off only below a lower CLEAR bar.
    """
    flags = state.setdefault("flags", {})
    if is_bad:
        flags[key] = True
    elif is_clear:
        flags[key] = False
    return bool(flags.get(key, False))


def cooldown_for(severity: Severity) -> int:
    """Seconds to stay quiet after alerting at this severity."""
    return CRITICAL_COOLDOWN_SECONDS if severity == Severity.CRITICAL else WARN_COOLDOWN_SECONDS


@dataclass
class AlertDecision:
    """Which findings go out this run, which are on cooldown, and which checks cleared."""

    to_send: List[Finding] = field(default_factory=list)
    suppressed: List[Finding] = field(default_factory=list)
    recovered: List[str] = field(default_factory=list)


def should_send(finding: Finding, last: Optional[Dict[str, Any]], now_ts: float) -> bool:
    """Cooldown rule: first alert, escalation, or cooldown expired -> send."""
    if finding.action_taken or last is None:
        return True
    last_severity = Severity(last.get("severity", Severity.WARN.value))
    if finding.severity.rank > last_severity.rank:
        return True
    return now_ts - float(last.get("ts", 0)) >= cooldown_for(finding.severity)


def decide_alerts(findings: List[Finding], state: Dict[str, Any], now_ts: Optional[float] = None) -> AlertDecision:
    """Apply cooldown + recovery bookkeeping; mutates `state` for the caller to persist."""
    now_ts = time.time() if now_ts is None else now_ts
    decision = AlertDecision()
    last_alert = state.setdefault("last_alert", {})
    previously_active = dict(state.get("active", {}))
    currently_active = {}  # type: Dict[str, str]

    for finding in findings:
        if not finding.action_taken:
            currently_active[finding.check_id] = finding.severity.value
        if should_send(finding, last_alert.get(finding.check_id), now_ts):
            decision.to_send.append(finding)
            for check_id in [finding.check_id] + list(finding.also_cools):
                last_alert[check_id] = {"ts": now_ts, "severity": finding.severity.value}
        else:
            decision.suppressed.append(finding)

    # A check that clears gets a "recovered" line only if it had reached CRITICAL;
    # WARN-level flapping stays silent. The cooldown record is kept across recovery
    # so a re-occurrence inside the window does not re-alert.
    for check_id in previously_active:
        record = last_alert.get(check_id)
        if check_id in currently_active or record is None:
            continue
        if record.get("severity") == Severity.CRITICAL.value and not record.get("recovered"):
            decision.recovered.append(check_id)
            # One recovered line per sent alert; the record (and its cooldown) stays.
            record["recovered"] = True

    state["active"] = currently_active
    return decision
