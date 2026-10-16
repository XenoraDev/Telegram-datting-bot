"""Measurement helpers: percentiles and host / process / database samplers.

Host numbers come from ``/proc`` (inside a container these are the numbers of the whole server),
so no extra dependency is needed. Every sampler returns None for what it cannot read instead of
guessing.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


def percentile(sorted_values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile of an already sorted sequence; None when empty."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return float(sorted_values[min(rank, len(sorted_values)) - 1])


def latency_stats(values: Sequence[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    return {
        "p50": percentile(ordered, 50),
        "p95": percentile(ordered, 95),
        "p99": percentile(ordered, 99),
        "max": float(ordered[-1]) if ordered else None,
    }


def parse_cpu_times(proc_stat: str) -> tuple[float, float] | None:
    """(busy, total) jiffies from the aggregate ``cpu`` line of /proc/stat."""
    for line in proc_stat.splitlines():
        if line.startswith("cpu "):
            parts = [float(x) for x in line.split()[1:]]
            if len(parts) < 4:
                return None
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)  # idle + iowait
            total = sum(parts[:8])  # user nice system idle iowait irq softirq steal
            return total - idle, total
    return None


def parse_meminfo(text: str) -> dict[str, float]:
    """RAM / swap use in percent and sizes in MB from /proc/meminfo."""
    kb: dict[str, float] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            kb[name] = float(fields[0])
    out: dict[str, float] = {}
    total, avail = kb.get("MemTotal"), kb.get("MemAvailable")
    if total and avail is not None:
        out["memory_total_mb"] = round(total / 1024)
        out["ram_pct"] = round((total - avail) / total * 100, 2)
    swap_total, swap_free = kb.get("SwapTotal"), kb.get("SwapFree")
    if swap_total and swap_free is not None:
        out["swap_total_mb"] = round(swap_total / 1024)
        out["swap_pct"] = round((swap_total - swap_free) / swap_total * 100, 2)
    return out


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


@dataclass(frozen=True, slots=True)
class HostSample:
    cpu_pct: float | None
    ram_pct: float | None
    swap_pct: float | None  # None when the host has no swap
    worker_cpu_pct: float  # CPU of this worker process, as % of ONE core


class HostSampler:
    """CPU needs two readings, so the first sample after construction reports cpu None."""

    def __init__(self, proc_stat: str = "/proc/stat", meminfo: str = "/proc/meminfo") -> None:
        self._stat, self._mem = proc_stat, meminfo
        self._last_cpu = parse_cpu_times(_read(proc_stat) or "")
        self._last_proc = (time.monotonic(), time.process_time())

    def sample(self) -> HostSample:
        cpu = None
        now = parse_cpu_times(_read(self._stat) or "")
        if now and self._last_cpu and now[1] > self._last_cpu[1]:
            cpu = round((now[0] - self._last_cpu[0]) / (now[1] - self._last_cpu[1]) * 100, 2)
        if now:
            self._last_cpu = now
        mem = parse_meminfo(_read(self._mem) or "")
        wall, cpu_time = time.monotonic(), time.process_time()
        dt = wall - self._last_proc[0]
        worker = round((cpu_time - self._last_proc[1]) / dt * 100, 2) if dt > 0 else 0.0
        self._last_proc = (wall, cpu_time)
        return HostSample(cpu, mem.get("ram_pct"), mem.get("swap_pct"), worker)


def server_info(commit: str | None, extra: dict[str, object] | None = None) -> dict[str, object]:
    """Non-secret facts about the machine, kept with every report so upgrades can be compared."""
    import os

    mem = parse_meminfo(_read("/proc/meminfo") or "")
    info: dict[str, object] = {
        "cpu_count": os.cpu_count(),
        "memory_total_mb": mem.get("memory_total_mb"),
        "swap_total_mb": mem.get("swap_total_mb"),
        "commit": commit,
    }
    info.update(extra or {})
    return info


def app_commit(env_var: str = "APP_COMMIT") -> str | None:
    """The deployed commit: APP_COMMIT, or the checkout's HEAD when a .git directory exists."""
    import os

    value = os.environ.get(env_var, "").strip()
    if value:
        return value[:64]
    head = Path(__file__).resolve().parents[2] / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            ref = (head.parent / ref[5:]).read_text().strip()
        return ref[:40] if ref else None
    except OSError:
        return None


class HealthTracker:
    """Counts failed probes and service restarts (>= 2 failed probes, then healthy again)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures = 0
        self.consecutive = 0
        self.restarts = 0
        self.observed = False

    def update(self, ok: bool | None) -> None:
        if ok is None:
            return
        self.observed = True
        if ok:
            if self.consecutive >= 2:
                self.restarts += 1
            self.consecutive = 0
        else:
            self.failures += 1
            self.consecutive += 1


def restarts_from_windows(windows: Sequence[dict[str, object]]) -> dict[str, int]:
    """Restarts of monitored services seen in stored windows (same rule as during the run)."""
    trackers = {"bot": HealthTracker("bot"), "web": HealthTracker("web")}
    for w in windows:
        for name, tracker in trackers.items():
            value = w.get(f"{name}_ok")
            tracker.update(value if isinstance(value, bool) else None)
    return {name: t.restarts for name, t in trackers.items()}
