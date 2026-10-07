"""Throttle detection: decode hardware flags (NVML, Intel MSRs) and infer where no flag exists.

Everything in here is pure logic over plain numbers, so it is unit-testable without hardware.

Honesty rule: a verdict read from a hardware flag is ``confidence="detected"``. A verdict derived
from clocks/temperature/power is ``confidence="inferred"`` and must never be presented as detected.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import mean
from typing import Literal

from app.models import CpuSnapshot, ThrottleComponent, ThrottleStatus, ThrottleType

_Category = Literal["thermal", "power", "prochot", "current", "other", "benign"]

# Dominant-cause ordering: the most actionable explanation wins the headline ``type``.
_TYPE_RANK: dict[ThrottleType, int] = {"thermal": 0, "prochot": 1, "current": 2, "power": 3, "other": 4}


def _as_type(category: _Category) -> ThrottleType | None:
    """Narrow a table category to a reportable throttle type ("benign" is not a throttle)."""
    return None if category == "benign" else category


def _dominant(types: Iterable[ThrottleType]) -> ThrottleType | None:
    ranked = sorted(set(types), key=_TYPE_RANK.__getitem__)
    return ranked[0] if ranked else None


# --------------------------------------------------------------------------------------------------
# NVIDIA (NVML clocks-throttle-reasons bitmask)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _NvmlBit:
    mask: int
    label: str
    category: _Category


# Values are the stable nvml.h ABI constants (nvmlClocksThrottleReason*), so we do not depend on
# which names a given pynvml build exports.
_NVML_BITS: tuple[_NvmlBit, ...] = (
    _NvmlBit(0x001, "GPU idle", "benign"),
    _NvmlBit(0x002, "Applications clocks setting", "benign"),
    _NvmlBit(0x004, "Software power cap", "power"),
    _NvmlBit(0x008, "Hardware slowdown", "other"),
    _NvmlBit(0x010, "Sync boost", "benign"),
    _NvmlBit(0x020, "Software thermal slowdown", "thermal"),
    _NvmlBit(0x040, "Hardware thermal slowdown", "thermal"),
    _NvmlBit(0x080, "Hardware power-brake slowdown", "power"),
    _NvmlBit(0x100, "Display clock setting", "benign"),
    _NvmlBit(0x200, "Board power limit", "power"),
    _NvmlBit(0x400, "Reliability voltage limit", "other"),
)


def nvml_reason_names(mask: int) -> list[str]:
    """Every set bit by name, including benign ones (idle, app clocks) - for display."""
    return [b.label for b in _NVML_BITS if mask & b.mask]


def decode_nvml_throttle(mask: int | None) -> ThrottleComponent:
    """GPU throttle verdict from ``nvmlDeviceGetCurrentClocksThrottleReasons``."""
    if mask is None:
        return ThrottleComponent(source="nvml")
    active_bits = [b for b in _NVML_BITS if mask & b.mask and b.category != "benign"]
    kinds = [t for t in (_as_type(b.category) for b in active_bits) if t is not None]
    return ThrottleComponent(
        active=bool(active_bits),
        type=_dominant(kinds),
        source="nvml",
        confidence="detected",
        reasons=[b.label for b in active_bits],
        detail={"raw_mask": f"0x{mask:x}", "all_bits": ", ".join(nvml_reason_names(mask)) or "none"},
    )


# --------------------------------------------------------------------------------------------------
# Intel (MSRs, read-only)
# --------------------------------------------------------------------------------------------------

MSR_PKG_THERM_STATUS = 0x1B1  # IA32_PACKAGE_THERM_STATUS
MSR_RAPL_POWER_UNIT = 0x606
MSR_PKG_POWER_LIMIT = 0x610
MSR_CORE_PERF_LIMIT_REASONS = 0x64F


@dataclass(frozen=True)
class _MsrBit:
    bit: int
    label: str
    category: _Category


# MSR_CORE_PERF_LIMIT_REASONS (0x64F), *status* bits 0-15 (the sticky log bits are the same
# positions + 16 and are deliberately ignored: they say "happened since cleared", not "happening").
# Layout per Intel SDM vol. 4; verified against live readings by scripts/probe_msr.py.
_INTEL_LIMIT_BITS: tuple[_MsrBit, ...] = (
    _MsrBit(0, "PROCHOT# asserted", "prochot"),
    _MsrBit(1, "Thermal throttling (TCC active)", "thermal"),
    _MsrBit(4, "Residency state regulation", "benign"),
    _MsrBit(5, "Running-average thermal limit", "thermal"),
    _MsrBit(6, "VR thermal alert", "thermal"),
    _MsrBit(7, "VR thermal design current", "current"),
    _MsrBit(8, "Other limit", "other"),
    _MsrBit(10, "Electrical design point (ICCmax)", "current"),
    _MsrBit(11, "Package power limit PL1", "power"),
    _MsrBit(12, "Package power limit PL2", "power"),
    _MsrBit(13, "Max turbo limit", "benign"),
    _MsrBit(14, "Turbo transition attenuation", "benign"),
    _MsrBit(15, "Maximum efficiency frequency", "benign"),
)

# IA32_PACKAGE_THERM_STATUS (0x1B1) status bits, used when 0x64F is not readable.
_INTEL_PKG_THERM_BITS: tuple[_MsrBit, ...] = (
    _MsrBit(0, "Package thermal throttling", "thermal"),
    _MsrBit(2, "PROCHOT# / FORCEPR# asserted", "prochot"),
    _MsrBit(4, "Critical temperature", "thermal"),
    _MsrBit(10, "Package power limitation", "power"),
)


def decode_intel_throttle(perf_limit_reasons: int | None, package_therm_status: int | None) -> ThrottleComponent:
    """CPU throttle verdict from Intel MSRs. ``confidence`` is "detected" when either register read."""
    if perf_limit_reasons is not None:
        table, raw, source = _INTEL_LIMIT_BITS, perf_limit_reasons, "msr:perf_limit_reasons"
    elif package_therm_status is not None:
        table, raw, source = _INTEL_PKG_THERM_BITS, package_therm_status, "msr:package_therm_status"
    else:
        return ThrottleComponent(source="msr")

    hits = [b for b in table if raw & (1 << b.bit) and b.category != "benign"]
    kinds = [t for t in (_as_type(b.category) for b in hits) if t is not None]
    detail: dict[str, float | int | str | None] = {f"msr_0x{MSR_CORE_PERF_LIMIT_REASONS:x}": None}
    if perf_limit_reasons is not None:
        detail[f"msr_0x{MSR_CORE_PERF_LIMIT_REASONS:x}"] = f"0x{perf_limit_reasons:x}"
    if package_therm_status is not None:
        detail[f"msr_0x{MSR_PKG_THERM_STATUS:x}"] = f"0x{package_therm_status:x}"
    return ThrottleComponent(
        active=bool(hits),
        type=_dominant(kinds),
        source=source,
        confidence="detected",
        reasons=[b.label for b in hits],
        detail=detail,
    )


def decode_power_limits(pkg_power_limit: int | None, power_unit: int | None) -> tuple[float | None, float | None]:
    """(PL1, PL2) in watts from MSR_PKG_POWER_LIMIT / MSR_RAPL_POWER_UNIT; None for a disabled or unreadable limit."""
    if pkg_power_limit is None or power_unit is None:
        return None, None
    unit_w = 1.0 / (1 << (power_unit & 0xF))
    pl1_raw = pkg_power_limit & 0x7FFF
    pl2_raw = (pkg_power_limit >> 32) & 0x7FFF
    pl1_enabled = bool(pkg_power_limit & (1 << 15))
    pl2_enabled = bool(pkg_power_limit & (1 << 47))
    return (
        round(pl1_raw * unit_w, 1) if pl1_enabled and pl1_raw else None,
        round(pl2_raw * unit_w, 1) if pl2_enabled and pl2_raw else None,
    )


# --------------------------------------------------------------------------------------------------
# Inference (AMD, and Intel when the MSRs cannot be read)
# --------------------------------------------------------------------------------------------------

_DEFAULT_TJMAX = {"intel": 100.0, "amd": 95.0}
_WINDOW_S = 120.0
_MIN_LOAD_PCT = 60.0  # below this, low clocks are just idling, not throttling
_BUSY_CORE_LOAD_PCT = 40.0
_CLOCK_DROP_FRACTION = 0.10  # active clock must be >=10% under its recent peak
_NEAR_LIMIT_C = 4.0  # "at the limit" = within this many degrees of TjMax
_SAFE_MARGIN_C = 12.0  # "comfortably below the limit" for the power-limit hypothesis
_MIN_HISTORY_S = 8.0  # need some history before comparing against a recent peak


@dataclass(frozen=True)
class _Sample:
    t: float
    active_clock: float
    temp: float | None
    power: float | None


def active_core_clock(cpu: CpuSnapshot) -> float | None:
    """Mean clock of the busy cores (P-cores only on hybrid CPUs, so idle E-cores don't skew it)."""
    pool = [c for c in cpu.cores if c.kind == "P"] or cpu.cores
    busy = [c.clock_mhz for c in pool if c.clock_mhz is not None and (c.load_pct or 0.0) >= _BUSY_CORE_LOAD_PCT]
    return mean(busy) if busy else None


class CpuThrottleInference:
    """Stateful clock-vs-temperature-vs-power heuristic over a short rolling window.

    Deliberately conservative: it needs sustained load *and* a clock drop *and* a temperature or
    power signature, and it always reports ``confidence="inferred"``.
    """

    def __init__(self) -> None:
        self._history: deque[_Sample] = deque()

    def update(self, now: float, cpu: CpuSnapshot) -> ThrottleComponent:
        clock = active_core_clock(cpu)
        temp = cpu.package_temp_c if cpu.package_temp_c is not None else cpu.max_core_temp_c
        load = cpu.total_load_pct

        if clock is not None:
            self._history.append(_Sample(now, clock, temp, cpu.package_power_w))
        while self._history and now - self._history[0].t > _WINDOW_S:
            self._history.popleft()

        unavailable = ThrottleComponent(source="inference:clock_temp_power")
        if load is None:
            return unavailable
        if load < _MIN_LOAD_PCT:  # idle clocks are low by design; there is nothing to judge
            return ThrottleComponent(
                active=False, source="inference:clock_temp_power", confidence="inferred",
                detail={"load_pct": load, "reason": "no sustained load to judge"},
            )
        if clock is None or temp is None:
            return unavailable  # under load but clocks/temperature are unreadable: genuinely unknown
        if len(self._history) < 2 or now - self._history[0].t < _MIN_HISTORY_S:
            return ThrottleComponent(
                active=False, source="inference:clock_temp_power", confidence="inferred",
                detail={"load_pct": load, "reason": "not enough history yet"},
            )

        peak = max(s.active_clock for s in self._history)
        limit = cpu.tjmax_c or _DEFAULT_TJMAX.get(cpu.vendor or "", 100.0)
        dropped = clock <= peak * (1.0 - _CLOCK_DROP_FRACTION)
        near_limit = temp >= limit - _NEAR_LIMIT_C
        comfortable = temp <= limit - _SAFE_MARGIN_C

        detail: dict[str, float | int | str | None] = {
            "active_clock_mhz": round(clock),
            "recent_peak_clock_mhz": round(peak),
            "temp_c": temp,
            "temp_limit_c": limit,
            "load_pct": load,
            "package_power_w": cpu.package_power_w,
        }
        if dropped and near_limit:
            return ThrottleComponent(
                active=True, type="thermal", source="inference:clock_temp_power", confidence="inferred",
                reasons=[f"Clocks fell {round((1 - clock / peak) * 100)}% while at {temp:.0f}°C (limit ~{limit:.0f}°C)"],
                detail=detail,
            )
        if dropped and comfortable:
            return ThrottleComponent(
                active=True, type="power", source="inference:clock_temp_power", confidence="inferred",
                reasons=[f"Clocks fell {round((1 - clock / peak) * 100)}% at only {temp:.0f}°C - likely a power limit"],
                detail=detail,
            )
        return ThrottleComponent(
            active=False, source="inference:clock_temp_power", confidence="inferred", detail=detail
        )


# --------------------------------------------------------------------------------------------------
# Combining CPU + GPU into the snapshot's single ``throttle`` object
# --------------------------------------------------------------------------------------------------

_CONFIDENCE_RANK = {"detected": 0, "inferred": 1, "unavailable": 2}


def combine(cpu: ThrottleComponent, gpu: ThrottleComponent | None) -> ThrottleStatus:
    parts: list[tuple[Literal["cpu", "gpu"], ThrottleComponent]] = [("cpu", cpu)]
    if gpu is not None:
        parts.append(("gpu", gpu))

    active = [p for p in parts if p[1].active]
    if active:
        # A detected throttle outranks an inferred one; within that, thermal outranks power, etc.
        name, top = min(
            active,
            key=lambda p: (_CONFIDENCE_RANK[p[1].confidence], _TYPE_RANK.get(p[1].type or "other", 9)),
        )
        verdict = top.model_copy()
        reasons = list(top.reasons)
        for other_name, other in active:
            if other is not top:
                reasons.extend(f"{other_name.upper()}: {r}" for r in other.reasons)
        return ThrottleStatus(
            **verdict.model_dump(exclude={"reasons"}), reasons=reasons, component=name, cpu=cpu, gpu=gpu
        )

    known = [p for p in parts if p[1].active is False]
    if known:
        best = min(known, key=lambda p: _CONFIDENCE_RANK[p[1].confidence])[1]
        return ThrottleStatus(
            active=False, source=best.source, confidence=best.confidence, cpu=cpu, gpu=gpu,
        )
    return ThrottleStatus(cpu=cpu, gpu=gpu)  # nothing could be determined
