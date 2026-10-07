"""psutil-based system collector: overall load, memory, battery and the active Windows power plan."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time

import psutil

from app.models import BatteryInfo, PowerPlan, PowerPlanKind, SystemSnapshot

log = logging.getLogger(__name__)

_PLAN_GUIDS: dict[str, PowerPlanKind] = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "balanced",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "high_performance",
    "a1841308-3541-4fab-bc81-f71556f20b4a": "power_saver",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "ultimate",
}
_PLAN_RE = re.compile(r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\s*\(([^)]*)\)")
_PLAN_REFRESH_S = 30.0
_GIB = 1024**3


def parse_powercfg(output: str) -> PowerPlan | None:
    """Parse ``powercfg /getactivescheme`` output. Locale-independent: keys on the GUID, not the label."""
    m = _PLAN_RE.search(output)
    if not m:
        return None
    guid = m.group(1).lower()
    return PowerPlan(name=m.group(2).strip(), guid=guid, kind=_PLAN_GUIDS.get(guid, "other"))


class SystemCollector:
    name = "system"

    def __init__(self) -> None:
        self._plan: PowerPlan | None = None
        self._plan_checked_at = 0.0

    def start(self) -> None:
        # psutil's first non-blocking cpu_percent() call always returns 0.0; prime the counters.
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)

    def read(self) -> SystemSnapshot:
        mem = psutil.virtual_memory()
        return SystemSnapshot(
            cpu_percent=round(psutil.cpu_percent(interval=None), 1),
            cpu_percent_per_logical=[round(v, 1) for v in psutil.cpu_percent(interval=None, percpu=True)],
            memory_percent=round(mem.percent, 1),
            memory_used_gb=round(mem.used / _GIB, 2),
            memory_total_gb=round(mem.total / _GIB, 2),
            battery=self._battery(),
            power_plan=self._power_plan(),
        )

    def close(self) -> None:
        return None

    @staticmethod
    def _battery() -> BatteryInfo | None:
        try:
            b = psutil.sensors_battery()
        except (AttributeError, OSError):
            return None
        if b is None:
            return None  # desktop without a battery
        # secsleft is a negative sentinel when the OS cannot estimate it or the battery is charging
        minutes = int(b.secsleft // 60) if isinstance(b.secsleft, (int, float)) and b.secsleft >= 0 else None
        return BatteryInfo(percent=round(b.percent, 1), plugged_in=b.power_plugged, minutes_left=minutes)

    def _power_plan(self) -> PowerPlan | None:
        now = time.monotonic()
        if now - self._plan_checked_at < _PLAN_REFRESH_S:
            return self._plan
        self._plan_checked_at = now
        if sys.platform != "win32":
            return None
        try:
            proc = subprocess.run(
                ["powercfg", "/getactivescheme"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
            )
            self._plan = parse_powercfg(proc.stdout) or self._plan
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("powercfg failed: %s", exc)
        return self._plan
