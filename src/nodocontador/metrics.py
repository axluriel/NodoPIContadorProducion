from __future__ import annotations

import os
import time
from pathlib import Path


class SystemMetrics:
    def __init__(self) -> None:
        self._previous_total = 0
        self._previous_idle = 0

    def read(self) -> dict:
        if not Path("/proc/stat").exists():
            return {
                "cpuPercent": 0.0, "memoryTotalMb": 0, "memoryUsedMb": 0,
                "memoryPercent": 0.0, "temperatureC": None,
                "loadAverage": 0.0, "uptimeSeconds": time.monotonic(),
                "processorCount": os.cpu_count() or 1,
            }
        fields = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        total, idle = sum(fields), fields[3] + (fields[4] if len(fields) > 4 else 0)
        delta, idle_delta = total - self._previous_total, idle - self._previous_idle
        cpu = 0.0 if not self._previous_total or delta <= 0 else max(0.0, min(100.0, 100 * (delta - idle_delta) / delta))
        self._previous_total, self._previous_idle = total, idle
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0])
        total_kb = memory["MemTotal"]
        used_kb = total_kb - memory.get("MemAvailable", memory.get("MemFree", 0))
        temperature_path = Path("/sys/class/thermal/thermal_zone0/temp")
        temperature = round(int(temperature_path.read_text().strip()) / 1000, 1) if temperature_path.exists() else None
        return {
            "cpuPercent": round(cpu, 1), "memoryTotalMb": total_kb // 1024,
            "memoryUsedMb": used_kb // 1024, "memoryPercent": round(100 * used_kb / total_kb, 1),
            "temperatureC": temperature, "loadAverage": os.getloadavg()[0],
            "uptimeSeconds": float(Path("/proc/uptime").read_text().split()[0]),
            "processorCount": os.cpu_count() or 1,
        }
