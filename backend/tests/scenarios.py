"""Snapshot-stream builders for analyzer tests: describe a situation, get realistic Snapshots."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.analyzer.engine import DiagnosisEngine
from app.analyzer.models import DiagnosisReport
from app.models import (
    BatteryInfo,
    CoreReading,
    CpuSnapshot,
    FanReading,
    GpuSnapshot,
    PowerPlan,
    ProcessInfo,
    SensorHealth,
    Snapshot,
    SystemSnapshot,
    ThrottleComponent,
    ThrottleStatus,
)

T0 = 1_800_000_000.0

_UNSET: Any = object()


def comp(active: bool | None, type_: str | None = None, reasons: list[str] | None = None, *,
         confidence: str = "detected", source: str = "msr:perf_limit_reasons", mask: str | None = None) -> ThrottleComponent:
    return ThrottleComponent(
        active=active, type=type_, source=source, confidence=confidence,  # type: ignore[arg-type]
        reasons=reasons or [], detail={"raw_mask": mask} if mask else {},
    )


def snap(
    t: float,
    *,
    cpu_temp: float | None = 50.0,
    cpu_load: float | None = 5.0,
    cpu_power: float | None = 10.0,
    clock: float | None = 3000.0,  # clock of the busy cores
    pl1: float | None = None,
    pl2: float | None = None,
    tjmax: float | None = 100.0,
    gpu: bool = False,
    gpu_temp: float | None = 45.0,
    gpu_util: float | None = 2.0,
    gpu_power: float | None = 10.0,
    gpu_limit: float | None = 90.0,
    gpu_clock: float | None = 1800.0,
    cpu_fan: float | None = None,
    gpu_fan: float | None = None,
    battery: bool = True,
    on_ac: bool = True,
    plan: str = "balanced",
    cpu_throttle: ThrottleComponent | None = None,
    gpu_throttle: ThrottleComponent | None = None,
    processes: list[ProcessInfo] | None = None,
) -> Snapshot:
    busy = (cpu_load or 0) >= 40
    cores = [
        CoreReading(index=i, kind="P", clock_mhz=clock, load_pct=cpu_load if busy else 2.0, temp_c=cpu_temp)
        for i in range(1, 9)
    ]
    fans: list[FanReading] = []
    if cpu_fan is not None:
        fans.append(FanReading(name="CPU Fan", rpm=cpu_fan))
    if gpu_fan is not None:
        fans.append(FanReading(name="GPU Fan", rpm=gpu_fan))
    cpu_t = cpu_throttle or comp(None, confidence="unavailable", source="none")
    gpu_t = gpu_throttle if gpu_throttle is not None else (comp(False, source="nvml") if gpu else None)
    return Snapshot(
        timestamp=t, seq=int(t - T0), interval_s=1.0,
        cpu=CpuSnapshot(
            name="Test CPU", vendor="intel", package_temp_c=cpu_temp, max_core_temp_c=cpu_temp,
            tjmax_c=tjmax, total_load_pct=cpu_load, package_power_w=cpu_power, pl1_w=pl1, pl2_w=pl2,
            avg_clock_mhz=clock, max_clock_mhz=clock, cores=cores,
        ),
        gpu=GpuSnapshot(
            name="Test GPU", temp_c=gpu_temp, temp_slowdown_c=91.0, util_gpu_pct=gpu_util, power_draw_w=gpu_power,
            power_limit_w=gpu_limit, core_clock_mhz=gpu_clock, fan_rpm=gpu_fan,
        ) if gpu else None,
        throttle=ThrottleStatus(cpu=cpu_t, gpu=gpu_t),
        fans=fans,
        system=SystemSnapshot(
            cpu_percent=cpu_load,
            battery=BatteryInfo(percent=80.0, plugged_in=on_ac) if battery else None,
            power_plan=PowerPlan(name=plan, kind=plan),  # type: ignore[arg-type]
        ),
        processes=processes or [],
        health=SensorHealth(is_admin=True),
    )


def proc(name: str, cpu: float, *, gpu: float | None = None, count: int = 1) -> ProcessInfo:
    return ProcessInfo(name=name, cpu_pct=cpu, gpu_pct=gpu, count=count, mem_mb=100.0)


def feed(
    engine: DiagnosisEngine, start: int, seconds: int, mutate: Callable[[Snapshot], None] | None = None, **kw: Any
) -> DiagnosisReport:
    """Ingest ``seconds`` samples starting at offset ``start``. Any kwarg may be a function of the offset.

    The engine is evaluated every 5 s, like the real runner, so confirmation/linger timing behaves realistically.
    """
    for i in range(start, start + seconds):
        args = {k: (v(i) if callable(v) else v) for k, v in kw.items()}
        s = snap(T0 + i, **args)
        if mutate is not None:
            mutate(s)
        engine.ingest(s)
        if i % 5 == 0:
            engine.evaluate()
    return engine.evaluate()


def ramp(i0: int, i1: int, v0: float, v1: float) -> Callable[[int], float]:
    """Linear ramp from v0 at i0 to v1 at i1, flat outside."""
    def f(i: int) -> float:
        if i <= i0:
            return v0
        if i >= i1:
            return v1
        return v0 + (v1 - v0) * (i - i0) / (i1 - i0)
    return f
