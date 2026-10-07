"""Snapshot -> database row. Pure function so it can be tested without a database."""

from __future__ import annotations

from typing import Any

from app.collectors.throttle import active_core_clock
from app.models import FanReading, Snapshot


def _fan_rpm(fans: list[FanReading], name: str) -> float | None:
    return next((f.rpm for f in fans if f.name == name), None)


def snapshot_to_row(s: Snapshot) -> dict[str, Any]:
    cpu, gpu, th, system = s.cpu, s.gpu, s.throttle, s.system
    active = active_core_clock(cpu)
    cpu_fan = _fan_rpm(s.fans, "CPU Fan")
    if cpu_fan is None:  # SuperIO boards name fans "Fan #1" etc.; take the first non-GPU fan
        cpu_fan = next((f.rpm for f in s.fans if "gpu" not in f.name.lower()), None)

    # `is not None`, not truthiness: a stopped fan (0 RPM) is a real reading, not a missing one
    gpu_fan = gpu.fan_rpm if gpu is not None and gpu.fan_rpm is not None else _fan_rpm(s.fans, "GPU Fan")

    return {
        "ts_ms": int(round(s.timestamp * 1000)),
        "cpu_pkg_temp": cpu.package_temp_c,
        "cpu_max_core_temp": cpu.max_core_temp_c,
        "cpu_avg_core_temp": cpu.avg_core_temp_c,
        "cpu_load": cpu.total_load_pct,
        "cpu_pkg_power": cpu.package_power_w,
        "cpu_pl1": cpu.pl1_w,
        "cpu_pl2": cpu.pl2_w,
        "cpu_avg_clock": cpu.avg_clock_mhz,
        "cpu_max_clock": cpu.max_clock_mhz,
        "cpu_active_clock": round(active) if active is not None else None,
        "gpu_temp": gpu.temp_c if gpu else None,
        "gpu_hotspot": gpu.temp_hotspot_c if gpu else None,
        "gpu_mem_temp": gpu.temp_memory_c if gpu else None,
        "gpu_core_clock": gpu.core_clock_mhz if gpu else None,
        "gpu_mem_clock": gpu.mem_clock_mhz if gpu else None,
        "gpu_power": gpu.power_draw_w if gpu else None,
        "gpu_power_limit": gpu.power_limit_w if gpu else None,
        "gpu_util": gpu.util_gpu_pct if gpu else None,
        "cpu_fan_rpm": cpu_fan,
        "gpu_fan_rpm": gpu_fan,
        "gpu_fan_pct": gpu.fan_percent if gpu else None,
        "mem_percent": system.memory_percent,
        "battery_pct": system.battery.percent if system.battery else None,
        "on_ac": None if system.battery is None or system.battery.plugged_in is None else int(system.battery.plugged_in),
        "power_plan": system.power_plan.kind if system.power_plan else None,
        "throttle_active": None if th.active is None else int(th.active),
        "throttle_type": th.type,
        "throttle_component": th.component,
        "throttle_confidence": th.confidence if th.active is not None else None,
        "throttle_reasons": "; ".join(th.reasons) or None,
    }
