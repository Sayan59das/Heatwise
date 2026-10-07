"""Read-only access to the Intel MSRs that expose throttle reasons and power limits.

Uses LibreHardwareMonitor's own public ``PawnIo.IntelMsr`` class, so it goes through the signed
PawnIO driver LHM already depends on - ThermalSense ships no kernel code of its own. This module only
ever calls ``ReadMsr``; there is no write path.

Requirements: elevated process + PawnIO installed + an Intel CPU. Anything else raises
``MsrUnavailable`` from ``start()`` and the hub falls back to inference.

Trap this guards against: without elevation LHM's ``ReadMsr`` still returns ``(True, 0)``. A zeroed
register would decode as "not throttling", i.e. a confident but fabricated "detected" verdict. Real
Intel CPUs never report 0 in MSR_RAPL_POWER_UNIT, so a zero there proves the reads are not genuine.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, NamedTuple

from app.collectors.lhm import LhmUnavailable, load_library
from app.collectors.throttle import (
    MSR_CORE_PERF_LIMIT_REASONS,
    MSR_PKG_POWER_LIMIT,
    MSR_PKG_THERM_STATUS,
    MSR_RAPL_POWER_UNIT,
)
from app.winutil import is_admin

log = logging.getLogger(__name__)


class MsrUnavailable(RuntimeError):
    """MSR access is not possible (not elevated, driver missing, non-Intel CPU, ...)."""


class IntelMsrSample(NamedTuple):
    perf_limit_reasons: int | None  # 0x64F
    package_therm_status: int | None  # 0x1B1
    pkg_power_limit: int | None  # 0x610
    power_unit: int | None  # 0x606


class IntelMsrReader:
    name = "intel_msr"

    def __init__(self, libs_dir: Path) -> None:
        self._libs_dir = libs_dir
        self._msr: Any = None
        self._uint64: Any = None

    def start(self) -> None:
        if not is_admin():
            raise MsrUnavailable("process is not elevated (PawnIO only serves administrators)")
        try:
            load_library(self._libs_dir)
            import LibreHardwareMonitor.PawnIo as pawnio  # type: ignore[import-not-found]
            from System import UInt64  # type: ignore[import-not-found]
        except (LhmUnavailable, ImportError) as exc:
            raise MsrUnavailable(str(exc)) from exc

        if not pawnio.PawnIo.IsInstalled:
            raise MsrUnavailable("PawnIO driver is not installed")
        try:
            msr = pawnio.IntelMsr()
        except Exception as exc:  # noqa: BLE001 - driver/module load failure surfaces as .NET exceptions
            raise MsrUnavailable(f"could not open IntelMsr: {exc}") from exc
        self._msr, self._uint64 = msr, UInt64
        # IntelMsr exposes no "is loaded" flag, so a real read is the only honest test. Doing it here makes
        # a broken setup fail at startup instead of silently reporting fabricated values.
        if not self._read(MSR_RAPL_POWER_UNIT):
            self.close()
            raise MsrUnavailable(
                "MSR reads return nothing or zeros (PawnIO module failed to load, or unsupported CPU)"
            )

    def _read(self, index: int) -> int | None:
        try:
            ok, value = self._msr.ReadMsr(index, self._uint64(0))
        except Exception as exc:  # noqa: BLE001
            log.debug("ReadMsr(0x%x) raised: %s", index, exc)
            return None
        return int(value) if ok else None

    def read(self) -> IntelMsrSample:
        if self._msr is None:
            raise MsrUnavailable("MSR reader is not open")
        sample = IntelMsrSample(
            perf_limit_reasons=self._read(MSR_CORE_PERF_LIMIT_REASONS),
            package_therm_status=self._read(MSR_PKG_THERM_STATUS),
            pkg_power_limit=self._read(MSR_PKG_POWER_LIMIT),
            power_unit=self._read(MSR_RAPL_POWER_UNIT),
        )
        if not sample.power_unit:  # the canary register is zero/unreadable: nothing in this sample can be trusted
            raise MsrUnavailable("MSR reads returned zeros (driver lost or access revoked)")
        return sample

    def close(self) -> None:
        if self._msr is not None:
            try:
                self._msr.Close()
            except Exception as exc:  # noqa: BLE001
                log.debug("IntelMsr.Close() failed: %s", exc)
            self._msr = None
