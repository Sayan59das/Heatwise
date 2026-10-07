"""Typed snapshot schema shared by the REST API, the WebSocket stream and (later) storage.

Every sensor-derived field is ``Optional``: a missing sensor is ``None``, never an exception.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

CoreKind = Literal["P", "E"]
ThrottleType = Literal["thermal", "power", "prochot", "current", "other"]
ThrottleConfidence = Literal["detected", "inferred", "unavailable"]
PowerPlanKind = Literal["balanced", "high_performance", "power_saver", "ultimate", "other"]


class CoreReading(BaseModel):
    index: int  # unique 1-based position, P-cores first on hybrid CPUs
    label: str = ""  # human name as the vendor numbers it, e.g. "P-Core 3" or "Core 3"
    kind: CoreKind | None = None  # hybrid CPUs only (performance / efficiency core)
    temp_c: float | None = None
    clock_mhz: float | None = None
    load_pct: float | None = None


class CpuSnapshot(BaseModel):
    name: str | None = None
    vendor: Literal["intel", "amd", "other"] | None = None
    package_temp_c: float | None = None
    max_core_temp_c: float | None = None
    avg_core_temp_c: float | None = None
    tjmax_c: float | None = None  # thermal junction limit, when the sensor stack exposes it
    total_load_pct: float | None = None
    package_power_w: float | None = None
    cores_power_w: float | None = None
    pl1_w: float | None = None  # sustained package power limit (Intel MSR_PKG_POWER_LIMIT), when readable
    pl2_w: float | None = None  # short-term boost power limit
    avg_clock_mhz: float | None = None
    max_clock_mhz: float | None = None
    cores: list[CoreReading] = []


class FanReading(BaseModel):
    name: str
    rpm: float | None = None
    percent: float | None = None  # commanded duty, when a matching control sensor exists
    source: str | None = None  # hardware that reported it


class BatteryInfo(BaseModel):
    percent: float | None = None
    plugged_in: bool | None = None
    minutes_left: int | None = None


class PowerPlan(BaseModel):
    name: str | None = None
    guid: str | None = None
    kind: PowerPlanKind | None = None


class SystemSnapshot(BaseModel):
    cpu_percent: float | None = None
    cpu_percent_per_logical: list[float] = []
    memory_percent: float | None = None
    memory_used_gb: float | None = None
    memory_total_gb: float | None = None
    battery: BatteryInfo | None = None
    power_plan: PowerPlan | None = None


class GpuSnapshot(BaseModel):
    name: str | None = None
    driver_version: str | None = None
    temp_c: float | None = None
    temp_slowdown_c: float | None = None  # temperature at which the GPU starts slowing down
    temp_shutdown_c: float | None = None
    temp_hotspot_c: float | None = None  # from LibreHardwareMonitor/NVAPI - NVML cannot report it
    temp_memory_c: float | None = None  # memory junction, same source
    core_clock_mhz: float | None = None
    max_core_clock_mhz: float | None = None
    mem_clock_mhz: float | None = None
    max_mem_clock_mhz: float | None = None
    power_draw_w: float | None = None
    power_limit_w: float | None = None  # currently enforced limit
    power_limit_default_w: float | None = None
    fan_percent: float | None = None  # None on most laptops: the EC owns the fan, NVML cannot see it
    fan_rpm: float | None = None  # filled from the laptop's embedded controller when a vendor source exists
    util_gpu_pct: float | None = None
    util_mem_pct: float | None = None
    mem_used_mb: float | None = None
    mem_total_mb: float | None = None
    pstate: int | None = None
    throttle_mask: int | None = None  # raw nvmlDeviceGetCurrentClocksThrottleReasons bitmask
    throttle_reasons: list[str] = []  # every set bit, by name (idle / app clocks included)
    # Fields this GPU/driver answers "not supported" for (e.g. fan_percent on laptops). Lets the UI say
    # "not exposed by this device" instead of showing an unexplained blank.
    unsupported: list[str] = []


class ThrottleComponent(BaseModel):
    """Throttle state for one component (CPU or GPU)."""

    active: bool | None = None  # None = cannot tell (confidence is then "unavailable")
    type: ThrottleType | None = None  # dominant active reason
    source: str = "none"  # where the verdict came from, e.g. "nvml", "msr:perf_limit_reasons"
    confidence: ThrottleConfidence = "unavailable"  # "inferred" is never reported as "detected"
    reasons: list[str] = []  # every active reason, human readable
    detail: dict[str, float | int | str | None] = {}  # raw numbers behind the verdict


class ThrottleStatus(ThrottleComponent):
    """Overall verdict (the most severe active component), plus the per-component breakdown."""

    component: Literal["cpu", "gpu"] | None = None
    cpu: ThrottleComponent = ThrottleComponent()
    gpu: ThrottleComponent | None = None


class ProcessInfo(BaseModel):
    """Processes sharing an executable name, aggregated (e.g. 20 chrome.exe -> one row)."""

    name: str
    count: int = 1  # how many processes were folded into this row
    pid: int | None = None  # the busiest instance
    cpu_pct: float = 0.0  # share of the WHOLE CPU (100 = every logical core fully busy)
    mem_mb: float | None = None  # working set
    gpu_pct: float | None = None  # NVML per-process GPU utilisation, None when not reported


class CollectorStatus(BaseModel):
    name: str
    available: bool
    error: str | None = None
    last_ok: float | None = None  # unix time of the last successful read


class SensorHealth(BaseModel):
    is_admin: bool
    pawnio_installed: bool | None = None  # None = not checkable on this platform
    collectors: list[CollectorStatus] = []
    warnings: list[str] = []


class Snapshot(BaseModel):
    timestamp: float
    seq: int
    interval_s: float
    collect_ms: float | None = None  # wall time spent reading sensors this tick
    cpu: CpuSnapshot
    gpu: GpuSnapshot | None = None
    throttle: ThrottleStatus = ThrottleStatus()
    fans: list[FanReading] = []
    system: SystemSnapshot
    processes: list[ProcessInfo] = []  # top consumers by CPU, plus top GPU users
    health: SensorHealth
