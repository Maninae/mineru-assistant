"""Machine-level probes: swap, memory pressure, load, core count, disk, tccd CPU.

Python 3.9 compatible (see config.py).

Each reader runs its command under a deadline and returns None on failure so one
broken probe never blocks the others. Parsers are pure functions over the exact
macOS output formats, e.g.
  vm.swapusage: total = 4096.00M  used = 2817.31M  free = 1278.69M  (encrypted)
  System-wide memory free percentage: 65%
  { 2.60 5.90 9.43 }
"""

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from health_watchdog.config import QUICK_COMMAND_DEADLINE_SECONDS
from health_watchdog.process_table import ProcessInfo
from health_watchdog.subprocess_deadline import run_with_deadline

SYSCTL_BIN = "/usr/sbin/sysctl"
MEMORY_PRESSURE_BIN = "/usr/bin/memory_pressure"
BYTES_PER_GIGABYTE = 1024 ** 3
UNIT_TO_MEGABYTES = {"K": 1.0 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024}
TCCD_COMMAND_MARKER = "/TCC.framework/Support/tccd"
ROOT_USER = "root"

SWAP_FIELD_PATTERN = re.compile(r"(total|used|free)\s*=\s*([0-9.]+)([KMGT])")
MEMORY_FREE_PATTERN = re.compile(r"System-wide memory free percentage:\s*(\d+)%")
LOADAVG_PATTERN = re.compile(r"\{\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*\}")


@dataclass
class SystemSnapshot:
    """Everything the threshold checks need from one run; None = probe failed."""

    swap_total_mb: Optional[float] = None
    swap_used_mb: Optional[float] = None
    memory_free_percent: Optional[int] = None
    load_1m: Optional[float] = None
    load_5m: Optional[float] = None
    load_15m: Optional[float] = None
    ncpu: Optional[int] = None
    disk_free_gb: Optional[float] = None
    tccd_root_cpu: Optional[float] = None
    tccd_user_cpu: Optional[float] = None
    probe_errors: List[str] = field(default_factory=list)

    @property
    def swap_used_fraction(self) -> Optional[float]:
        """Used / total swap, or None when unknown or no swap is configured."""
        if self.swap_total_mb is None or self.swap_used_mb is None or self.swap_total_mb <= 0:
            return None
        return self.swap_used_mb / self.swap_total_mb

    @property
    def load_per_core(self) -> Optional[float]:
        """5-minute load average divided by core count."""
        if self.load_5m is None or not self.ncpu:
            return None
        return self.load_5m / self.ncpu


def parse_swapusage(text: str) -> Optional[Tuple[float, float]]:
    """(total_mb, used_mb) from `sysctl vm.swapusage` output."""
    values = {}
    for name, number, unit in SWAP_FIELD_PATTERN.findall(text):
        values[name] = float(number) * UNIT_TO_MEGABYTES[unit]
    if "total" not in values or "used" not in values:
        return None
    return values["total"], values["used"]


def parse_memory_pressure(text: str) -> Optional[int]:
    """Free-memory percentage from `memory_pressure` output."""
    match = MEMORY_FREE_PATTERN.search(text)
    return int(match.group(1)) if match else None


def parse_loadavg(text: str) -> Optional[Tuple[float, float, float]]:
    """(1m, 5m, 15m) from `sysctl -n vm.loadavg` output."""
    match = LOADAVG_PATTERN.search(text)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2)), float(match.group(3))


def tccd_cpu(table: List[ProcessInfo]) -> Tuple[Optional[float], Optional[float]]:
    """(root tccd %cpu, user tccd %cpu) from the process table; None when absent."""
    root_cpu = None
    user_cpu = None
    for row in table:
        if TCCD_COMMAND_MARKER not in row.command:
            continue
        if row.user == ROOT_USER:
            root_cpu = max(root_cpu or 0.0, row.cpu_percent)
        else:
            user_cpu = max(user_cpu or 0.0, row.cpu_percent)
    return root_cpu, user_cpu


def sysctl_value(name: str) -> Optional[str]:
    """`sysctl -n <name>` under a deadline; None on failure."""
    outcome = run_with_deadline([SYSCTL_BIN, "-n", name], QUICK_COMMAND_DEADLINE_SECONDS)
    return outcome.stdout.strip() if outcome.succeeded else None


def disk_free_gb(path: str = "/") -> Optional[float]:
    """Free space available to this user on the filesystem holding `path`."""
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    return stats.f_bavail * stats.f_frsize / BYTES_PER_GIGABYTE


def collect_system_snapshot(table: List[ProcessInfo]) -> SystemSnapshot:
    """Run every machine-level probe; failures land in probe_errors, never raise."""
    snapshot = SystemSnapshot()

    swap_text = sysctl_value("vm.swapusage")
    swap = parse_swapusage(swap_text) if swap_text else None
    if swap is None:
        snapshot.probe_errors.append("swap: sysctl vm.swapusage unavailable")
    else:
        snapshot.swap_total_mb, snapshot.swap_used_mb = swap

    pressure = run_with_deadline([MEMORY_PRESSURE_BIN], QUICK_COMMAND_DEADLINE_SECONDS)
    snapshot.memory_free_percent = parse_memory_pressure(pressure.stdout) if pressure.succeeded else None
    if snapshot.memory_free_percent is None:
        snapshot.probe_errors.append("memory: memory_pressure unavailable")

    load_text = sysctl_value("vm.loadavg")
    load = parse_loadavg(load_text) if load_text else None
    if load is None:
        snapshot.probe_errors.append("load: sysctl vm.loadavg unavailable")
    else:
        snapshot.load_1m, snapshot.load_5m, snapshot.load_15m = load

    ncpu_text = sysctl_value("hw.ncpu")
    snapshot.ncpu = int(ncpu_text) if ncpu_text and ncpu_text.isdigit() else None
    if snapshot.ncpu is None:
        snapshot.probe_errors.append("load: sysctl hw.ncpu unavailable")

    snapshot.disk_free_gb = disk_free_gb("/")
    if snapshot.disk_free_gb is None:
        snapshot.probe_errors.append("disk: statvfs / failed")

    snapshot.tccd_root_cpu, snapshot.tccd_user_cpu = tccd_cpu(table)
    return snapshot


def health_snapshot_line(snapshot: SystemSnapshot) -> str:
    """One compact line of the key numbers, reused inside several alerts."""
    parts = []
    if snapshot.load_5m is not None and snapshot.ncpu:
        parts.append("load5 %.1f on %d cores" % (snapshot.load_5m, snapshot.ncpu))
    if snapshot.swap_used_fraction is not None:
        parts.append("swap %.0f%% (%.1f/%.1f GB)" % (
            snapshot.swap_used_fraction * 100, snapshot.swap_used_mb / 1024, snapshot.swap_total_mb / 1024,
        ))
    if snapshot.memory_free_percent is not None:
        parts.append("mem free %d%%" % snapshot.memory_free_percent)
    if snapshot.tccd_root_cpu is not None:
        parts.append("tccd(root) %.0f%%" % snapshot.tccd_root_cpu)
    if snapshot.tccd_user_cpu is not None:
        parts.append("tccd(user) %.0f%%" % snapshot.tccd_user_cpu)
    if snapshot.disk_free_gb is not None:
        parts.append("disk free %.1f GB" % snapshot.disk_free_gb)
    return ", ".join(parts) if parts else "no system numbers available"
