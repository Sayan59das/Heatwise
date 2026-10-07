"""Turn a flat list of raw LibreHardwareMonitor sensors into typed CPU / fan snapshots.

This module is deliberately free of any .NET or hardware access so the name-matching logic
(the fragile part, since LHM names vary by CPU vendor and generation) can be unit tested with
fixture data. Missing sensors always yield ``None``; nothing here raises on odd input.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from statistics import mean, median
from typing import Literal, NamedTuple

from app.models import CoreKind, CoreReading, CpuSnapshot, FanReading


@dataclass(frozen=True)
class RawSensor:
    hardware_name: str
    hardware_type: str  # LHM HardwareType name: "Cpu", "Motherboard", "SuperIO", ...
    sensor_type: str  # LHM SensorType name: "Temperature", "Clock", "Load", "Power", "Fan", ...
    name: str
    value: float | None
    identifier: str  # e.g. "/intelcpu/0/temperature/3"


_CORE_TEMP_RE = re.compile(r"^(?:(?P<kind>[PE])-)?Core #(?P<idx>\d+)$")
_CORE_CLOCK_RE = re.compile(r"^(?:(?P<kind>[PE])-)?Core #(?P<idx>\d+)(?P<eff> \(Effective\))?$")
_CORE_LOAD_RE = re.compile(r"^CPU (?:(?P<kind>[PE])-)?Core #(?P<idx>\d+)(?: Thread #(?P<thr>\d+))?$")
_TRAILING_INT_RE = re.compile(r"(\d+)\s*$")

_PACKAGE_TEMP_NAMES = ("CPU Package", "Core (Tctl/Tdie)", "Package", "CPU Tdie", "Core (Tdie)", "Tdie", "Tctl")
_PACKAGE_POWER_NAMES = ("CPU Package", "Package")
_CORES_POWER_NAMES = ("CPU Cores", "Cores")
_DIST_SUFFIX = " Distance to TjMax"


def _clean(value: float | None, digits: int = 1) -> float | None:
    """Round and drop NaN/inf, which some sensors report when a read fails."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, digits)


def _kind(letter: str | None) -> CoreKind | None:
    if letter == "P":
        return "P"
    if letter == "E":
        return "E"
    return None


def _vendor(identifier: str, name: str) -> Literal["intel", "amd", "other"]:
    ident = identifier.lower()
    if ident.startswith("/intelcpu"):
        return "intel"
    if ident.startswith("/amdcpu"):
        return "amd"
    lowered = name.lower()
    if "intel" in lowered:
        return "intel"
    if "amd" in lowered or "ryzen" in lowered:
        return "amd"
    return "other"


_CoreKey = tuple[CoreKind | None, int]  # (kind, number). On hybrid CPUs P-Core #1 and E-Core #1 coexist.
_KIND_RANK: dict[CoreKind | None, int] = {"P": 0, "E": 1, None: 2}


def _core_order(key: _CoreKey) -> tuple[int, int]:
    return _KIND_RANK[key[0]], key[1]


def _map_core_loads(
    core_keys: list[_CoreKey], load_keys: set[_CoreKey], load_values: dict[_CoreKey, list[float]]
) -> dict[_CoreKey, float]:
    """Attach per-core load to the cores named by the temperature/clock sensors.

    Temperatures and clocks of a hybrid Intel CPU are named per kind (``P-Core #1..8``, ``E-Core
    #1..12``) but its load sensors use one global ``CPU Core #1..20`` numbering with the P-cores
    first. When the counts line up we map positionally; if they do not, we report no per-core load
    rather than guess.
    """
    hybrid_temps = any(kind is not None for kind, _ in core_keys)
    loads_have_kind = any(kind is not None for kind, _ in load_keys)

    if not hybrid_temps or loads_have_kind or not core_keys:
        return {k: mean(v) for k, v in load_values.items() if v}

    ordered_loads = sorted(load_keys, key=_core_order)
    if len(ordered_loads) != len(core_keys):
        return {}
    return {
        core_key: mean(load_values[load_key])
        for core_key, load_key in zip(core_keys, ordered_loads)
        if load_values.get(load_key)
    }


def _first_present(values: dict[str, float | None], names: tuple[str, ...]) -> float | None:
    for n in names:
        v = values.get(n)
        if v is not None:
            return v
    return None


def build_cpu_snapshot(sensors: list[RawSensor]) -> CpuSnapshot:
    """Build a :class:`CpuSnapshot` from the sensors of the first CPU in ``sensors``."""
    cpu_sensors = [s for s in sensors if s.hardware_type == "Cpu"]
    if not cpu_sensors:
        return CpuSnapshot()

    # Only the first physical CPU; multi-socket desktops are out of scope.
    first = cpu_sensors[0]
    prefix = "/".join(first.identifier.split("/")[:3])  # "/intelcpu/0"
    cpu_sensors = [s for s in cpu_sensors if s.identifier.startswith(prefix + "/")]

    temps_by_name: dict[str, float | None] = {}
    dist_by_name: dict[str, float] = {}
    core_temp: dict[_CoreKey, float | None] = {}
    core_clock: dict[_CoreKey, float | None] = {}
    core_clock_eff: dict[_CoreKey, float | None] = {}
    load_values: dict[_CoreKey, list[float]] = {}
    load_keys: set[_CoreKey] = set()
    powers: dict[str, float | None] = {}
    total_load: float | None = None

    for s in cpu_sensors:
        v = _clean(s.value)
        name = s.name

        if s.sensor_type == "Temperature":
            if name.endswith(_DIST_SUFFIX):
                if v is not None:
                    dist_by_name[name[: -len(_DIST_SUFFIX)]] = v
                continue
            temps_by_name[name] = v
            m = _CORE_TEMP_RE.match(name)
            if m:
                core_temp[(_kind(m["kind"]), int(m["idx"]))] = v

        elif s.sensor_type == "Clock":
            m = _CORE_CLOCK_RE.match(name)
            if m:
                key = (_kind(m["kind"]), int(m["idx"]))
                (core_clock_eff if m["eff"] else core_clock)[key] = _clean(s.value, 0)

        elif s.sensor_type == "Load":
            if name == "CPU Total":
                total_load = v
                continue
            m = _CORE_LOAD_RE.match(name)
            if m:
                key = (_kind(m["kind"]), int(m["idx"]))
                load_keys.add(key)  # per-thread sensors collapse onto their core
                if v is not None:
                    load_values.setdefault(key, []).append(v)

        elif s.sensor_type == "Power":
            p = _clean(s.value, 2)
            # LHM reports exactly 0.00 W when it cannot read the energy counters (no driver / not
            # elevated). A running CPU never draws 0 W, so treat it as "no data" rather than a reading.
            powers[name] = p if p is not None and p > 0 else None

    core_keys = sorted(set(core_temp) | set(core_clock) | set(core_clock_eff), key=_core_order)
    load_by_key = _map_core_loads(core_keys, load_keys, load_values)
    if not core_keys:  # no temp/clock sensors at all: fall back to whatever load rows exist
        core_keys = sorted(load_keys, key=_core_order)

    cores = []
    for ordinal, key in enumerate(core_keys, start=1):
        kind, n = key
        clock = core_clock.get(key)
        load = load_by_key.get(key)
        cores.append(
            CoreReading(
                index=ordinal,
                kind=kind,
                label=f"{kind}-Core {n}" if kind else f"Core {n}",
                temp_c=core_temp.get(key),
                clock_mhz=clock if clock is not None else core_clock_eff.get(key),
                load_pct=round(load, 1) if load is not None else None,
            )
        )

    core_temps = [t for t in core_temp.values() if t is not None]
    clocks = [c.clock_mhz for c in cores if c.clock_mhz is not None]
    package_temp = _first_present(temps_by_name, _PACKAGE_TEMP_NAMES)

    max_core = max(core_temps) if core_temps else _clean(temps_by_name.get("Core Max"))
    avg_core = round(mean(core_temps), 1) if core_temps else _clean(temps_by_name.get("Core Average"))
    if max_core is None and package_temp is not None:
        max_core = package_temp

    # TjMax = temperature + distance-to-TjMax, taken as the median across sensors that report both.
    tjmax_candidates = [
        temps_by_name[base] + dist
        for base, dist in dist_by_name.items()
        if temps_by_name.get(base) is not None
    ]
    tjmax = round(median(tjmax_candidates)) if tjmax_candidates else None

    return CpuSnapshot(
        name=first.hardware_name,
        vendor=_vendor(first.identifier, first.hardware_name),
        package_temp_c=package_temp,
        max_core_temp_c=max_core,
        avg_core_temp_c=avg_core,
        tjmax_c=float(tjmax) if tjmax is not None else None,
        total_load_pct=total_load,
        package_power_w=_first_present(powers, _PACKAGE_POWER_NAMES),
        cores_power_w=_first_present(powers, _CORES_POWER_NAMES),
        avg_clock_mhz=round(mean(clocks)) if clocks else None,
        max_clock_mhz=max(clocks) if clocks else None,
        cores=cores,
    )


def build_fans(sensors: list[RawSensor]) -> list[FanReading]:
    """Collect fan sensors from non-GPU hardware, pairing each with its control (duty %) sensor."""
    gpu_types = {"GpuNvidia", "GpuAmd", "GpuIntel"}
    candidates = [s for s in sensors if s.hardware_type not in gpu_types]

    controls: dict[tuple[str, int], float] = {}
    for s in candidates:
        if s.sensor_type == "Control":
            m = _TRAILING_INT_RE.search(s.name)
            v = _clean(s.value)
            if m and v is not None:
                controls[(s.hardware_name, int(m.group(1)))] = v

    fans: list[FanReading] = []
    for s in candidates:
        if s.sensor_type != "Fan":
            continue
        m = _TRAILING_INT_RE.search(s.name)
        pct = controls.get((s.hardware_name, int(m.group(1)))) if m else None
        fans.append(
            FanReading(name=s.name, rpm=_clean(s.value, 0), percent=pct, source=s.hardware_name)
        )
    return fans


class GpuExtras(NamedTuple):
    """GPU readings NVML cannot provide, taken from LibreHardwareMonitor's NVAPI-backed GPU hardware."""

    hotspot_c: float | None = None
    memory_c: float | None = None


def build_gpu_extras(sensors: list[RawSensor]) -> GpuExtras:
    """GPU hot-spot and memory-junction temperatures of the first NVIDIA GPU (None when absent)."""
    hotspot: float | None = None
    memory: float | None = None
    for s in sensors:
        if s.hardware_type != "GpuNvidia" or s.sensor_type != "Temperature":
            continue
        value = _clean(s.value)
        if s.name == "GPU Hot Spot" and hotspot is None:
            hotspot = value
        elif s.name == "GPU Memory Junction" and memory is None:
            memory = value
    return GpuExtras(hotspot, memory)


class LhmReading(NamedTuple):
    cpu: CpuSnapshot
    fans: list[FanReading]
    gpu: GpuExtras
