"""NVIDIA GPU collector built on NVML (``pynvml``).

Every optional query is wrapped individually: laptop GPUs routinely answer NOT_SUPPORTED for fan
speed or power-management limits, and that must yield ``None`` for that field only. Fields the device
*permanently* cannot provide are listed in ``GpuSnapshot.unsupported`` so the UI can explain the gap.
Errors that mean "the GPU/driver went away" are re-raised so the hub marks the collector unhealthy and
retries, and the next read re-initialises NVML.
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

from app.collectors.throttle import nvml_reason_names
from app.models import GpuSnapshot

log = logging.getLogger(__name__)
T = TypeVar("T")


class NvmlUnavailable(RuntimeError):
    """No NVIDIA driver / no NVIDIA GPU / NVML cannot be initialised."""


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _f(value: int | float | None) -> float | None:
    return None if value is None else float(value)


class NvmlCollector:
    name = "nvml"

    def __init__(self, index: int = 0) -> None:
        self._index = index
        self._nvml: Any = None
        self._handle: Any = None
        self._needs_reinit = False
        self._static: dict[str, Any] = {}
        self._unsupported: set[str] = set()

    # ---- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # pynvml announces its own deprecation on import
                import pynvml
        except ImportError as exc:
            raise NvmlUnavailable("pynvml is not installed") from exc
        self._nvml = pynvml
        self._init_device()

    def _init_device(self) -> None:
        n = self._nvml
        try:
            n.nvmlInit()
            if n.nvmlDeviceGetCount() <= self._index:
                raise NvmlUnavailable("no NVIDIA GPU found")
            self._handle = n.nvmlDeviceGetHandleByIndex(self._index)
        except n.NVMLError as exc:
            raise NvmlUnavailable(f"NVML init failed: {exc}") from exc
        self._unsupported.clear()
        self._static = self._read_static()
        self._needs_reinit = False
        log.info("NVML ready: %s (driver %s)", self._static.get("name"), self._static.get("driver"))

    def close(self) -> None:
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception as exc:  # noqa: BLE001
                log.debug("nvmlShutdown failed: %s", exc)
        self._handle = None

    # ---- helpers ---------------------------------------------------------------------------------

    def _try(self, field: str, fn: Callable[[], T]) -> T | None:
        """Run one NVML query. NOT_SUPPORTED / INVALID_ARGUMENT become None *and* mark ``field`` unsupported."""
        n = self._nvml
        try:
            return fn()
        except n.NVMLError as exc:
            code = getattr(exc, "value", None)
            if code in (n.NVML_ERROR_GPU_IS_LOST, n.NVML_ERROR_UNINITIALIZED, n.NVML_ERROR_DRIVER_NOT_LOADED):
                self._needs_reinit = True
                raise
            if code in (n.NVML_ERROR_NOT_SUPPORTED, n.NVML_ERROR_INVALID_ARGUMENT):
                self._unsupported.add(field)
            return None  # anything else (timeout, unknown) is treated as a transient gap

    def _read_static(self) -> dict[str, Any]:
        n, h = self._nvml, self._handle
        t = self._try
        return {
            "name": t("name", lambda: _text(n.nvmlDeviceGetName(h))),
            "driver": t("driver_version", lambda: _text(n.nvmlSystemGetDriverVersion())),
            "temp_slowdown": t(
                "temp_slowdown_c", lambda: n.nvmlDeviceGetTemperatureThreshold(h, n.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN)
            ),
            "temp_shutdown": t(
                "temp_shutdown_c", lambda: n.nvmlDeviceGetTemperatureThreshold(h, n.NVML_TEMPERATURE_THRESHOLD_SHUTDOWN)
            ),
            "max_core_clock": t("max_core_clock_mhz", lambda: n.nvmlDeviceGetMaxClockInfo(h, n.NVML_CLOCK_GRAPHICS)),
            "max_mem_clock": t("max_mem_clock_mhz", lambda: n.nvmlDeviceGetMaxClockInfo(h, n.NVML_CLOCK_MEM)),
            "power_default_mw": t("power_limit_default_w", lambda: n.nvmlDeviceGetPowerManagementDefaultLimit(h)),
        }

    @staticmethod
    def _mw_to_w(mw: int | None) -> float | None:
        return None if mw is None else round(mw / 1000.0, 1)

    def _power_limit_mw(self) -> int | None:
        """Currently enforced limit. Laptops often reject the management-limit query but answer this one."""
        n, h = self._nvml, self._handle
        enforced = self._try("power_limit_w", lambda: n.nvmlDeviceGetEnforcedPowerLimit(h))
        if enforced is not None:
            return int(enforced)
        limit = self._try("power_limit_w", lambda: n.nvmlDeviceGetPowerManagementLimit(h))
        if limit is not None:
            self._unsupported.discard("power_limit_w")  # the fallback answered, so the field is available
        return limit

    def _fan_percent(self) -> float | None:
        """Fan duty from NVML. Laptops report 0 fans (the embedded controller owns them) -> None."""
        n, h = self._nvml, self._handle
        speed = self._try("fan_percent", lambda: n.nvmlDeviceGetFanSpeed(h))
        return _f(speed)

    # ---- polling ---------------------------------------------------------------------------------

    def read(self) -> GpuSnapshot:
        if self._needs_reinit:
            self.close()
            self._init_device()  # raises NvmlUnavailable while the GPU is still gone; hub retries next tick

        n, h = self._nvml, self._handle
        t = self._try
        static = self._static

        util = t("util_gpu_pct", lambda: n.nvmlDeviceGetUtilizationRates(h))
        mem = t("mem_used_mb", lambda: n.nvmlDeviceGetMemoryInfo(h))
        mask = t("throttle_mask", lambda: int(n.nvmlDeviceGetCurrentClocksThrottleReasons(h)))
        temp = t("temp_c", lambda: n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU))
        if temp is None and mask is None:
            # Nothing at all answered: treat as a failed read rather than reporting an all-null GPU.
            raise NvmlUnavailable("GPU did not answer temperature or throttle queries")

        return GpuSnapshot(
            name=static["name"],
            driver_version=static["driver"],
            temp_c=None if temp is None else float(temp),
            temp_slowdown_c=_f(static["temp_slowdown"]),
            temp_shutdown_c=_f(static["temp_shutdown"]),
            core_clock_mhz=_f(t("core_clock_mhz", lambda: n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_GRAPHICS))),
            max_core_clock_mhz=_f(static["max_core_clock"]),
            mem_clock_mhz=_f(t("mem_clock_mhz", lambda: n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_MEM))),
            max_mem_clock_mhz=_f(static["max_mem_clock"]),
            power_draw_w=self._mw_to_w(t("power_draw_w", lambda: n.nvmlDeviceGetPowerUsage(h))),
            power_limit_w=self._mw_to_w(self._power_limit_mw()),
            power_limit_default_w=self._mw_to_w(static["power_default_mw"]),
            fan_percent=self._fan_percent(),
            util_gpu_pct=None if util is None else float(util.gpu),
            util_mem_pct=None if util is None else float(util.memory),
            mem_used_mb=None if mem is None else round(mem.used / 2**20, 0),
            mem_total_mb=None if mem is None else round(mem.total / 2**20, 0),
            pstate=t("pstate", lambda: int(n.nvmlDeviceGetPerformanceState(h))),
            throttle_mask=mask,
            throttle_reasons=nvml_reason_names(mask) if mask is not None else [],
            unsupported=sorted(self._unsupported),
        )


class NvmlProcessReader:
    """Per-process GPU utilisation (SM %) from NVML. Runs on the process-sampler thread.

    ``nvmlDeviceGetProcessUtilization`` returns buffered samples newer than a driver timestamp, so we
    remember the newest timestamp and only ask for what is new; several samples per pid are averaged.
    """

    name = "nvml_processes"

    def __init__(self, index: int = 0) -> None:
        self._index = index
        self._nvml: Any = None
        self._handle: Any = None
        self._last_ts = 0

    def start(self) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import pynvml
        except ImportError as exc:
            raise NvmlUnavailable("pynvml is not installed") from exc
        self._nvml = pynvml
        try:
            pynvml.nvmlInit()  # reference-counted: independent of NvmlCollector's own init
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(self._index)
            self._advance(pynvml.nvmlDeviceGetProcessUtilization(self._handle, 0))  # prime: skip stale samples
        except pynvml.NVMLError as exc:
            raise NvmlUnavailable(f"per-process GPU utilisation unavailable: {exc}") from exc

    def _advance(self, samples: list[Any]) -> None:
        if samples:
            self._last_ts = max(self._last_ts, max(int(s.timeStamp) for s in samples))

    def read(self) -> dict[int, float]:
        n = self._nvml
        try:
            samples = n.nvmlDeviceGetProcessUtilization(self._handle, self._last_ts)
        except n.NVMLError as exc:
            if getattr(exc, "value", None) == n.NVML_ERROR_NOT_FOUND:
                return {}  # no new samples in this interval: nothing is using the GPU
            raise
        self._advance(samples)
        per_pid: dict[int, list[float]] = {}
        for s in samples:
            per_pid.setdefault(int(s.pid), []).append(float(s.smUtil))
        return {pid: round(sum(v) / len(v), 1) for pid, v in per_pid.items()}

    def close(self) -> None:
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception as exc:  # noqa: BLE001
                log.debug("nvmlShutdown (process reader) failed: %s", exc)
            self._nvml = None
