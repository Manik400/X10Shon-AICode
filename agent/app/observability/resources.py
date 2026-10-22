"""Resource monitoring: RAM/CPU of this process and the machine, with pressure signalling."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

import psutil


@dataclass(slots=True)
class ResourceSnapshot:
    process_rss_mb: float
    process_cpu_percent: float
    system_available_mb: float
    system_memory_percent: float
    system_cpu_percent: float
    threads: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


class ResourceMonitor:
    def __init__(self, max_ram_mb: int, *, memory_pressure_percent: int = 92, min_available_mb: int = 1536):
        self._proc = psutil.Process(os.getpid())
        self._proc.cpu_percent(None)
        self.max_ram_mb = max_ram_mb
        # The machine counts as under pressure above this percentage, unless plenty of memory is
        # still available in absolute terms (a 32 GB box at 94% still has ~2 GB free).
        self.memory_pressure_percent = memory_pressure_percent
        self.min_available_mb = min_available_mb

    def snapshot(self) -> ResourceSnapshot:
        vm = psutil.virtual_memory()
        with self._proc.oneshot():
            rss = self._proc.memory_info().rss / (1024 * 1024)
            cpu = self._proc.cpu_percent(None)
            threads = self._proc.num_threads()
        return ResourceSnapshot(
            process_rss_mb=round(rss, 1),
            process_cpu_percent=cpu,
            system_available_mb=round(vm.available / (1024 * 1024), 1),
            system_memory_percent=vm.percent,
            system_cpu_percent=psutil.cpu_percent(None),
            threads=threads,
        )

    def under_pressure(self) -> tuple[bool, str]:
        s = self.snapshot()
        if s.process_rss_mb > self.max_ram_mb:
            return True, f"process RSS {s.process_rss_mb:.0f} MB exceeds budget {self.max_ram_mb} MB"
        if s.system_memory_percent > self.memory_pressure_percent and s.system_available_mb < self.min_available_mb:
            return True, f"system memory at {s.system_memory_percent:.0f}% ({s.system_available_mb:.0f} MB free)"
        return False, ""
