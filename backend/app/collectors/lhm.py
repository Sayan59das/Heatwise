"""LibreHardwareMonitor host: loads the .NET library through pythonnet and reads its sensor tree.

Threading: the LibreHardwareMonitor ``Computer`` object is not thread-safe. ``start``, ``read_raw``
and ``close`` must all be called from the same thread (the hub guarantees this with a dedicated
single-thread executor).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from app.collectors.parsing import LhmReading, RawSensor, build_cpu_snapshot, build_fans, build_gpu_extras

log = logging.getLogger(__name__)

_GPU_EVERY = 3  # refresh the (slow) GPU hardware every Nth poll
_resolver: Any = None  # keeps the .NET ResolveEventHandler delegate alive for the process lifetime


class LhmUnavailable(RuntimeError):
    """Raised when LibreHardwareMonitor cannot be loaded (missing DLLs, no .NET Framework, ...)."""


def ensure_runtime(libs_dir: Path) -> None:
    """Start pythonnet's .NET Framework host and install the assembly resolver (idempotent)."""
    global _resolver

    try:
        from pythonnet import load

        load("netfx")  # .NET Framework 4.x ships with Windows 10/11; no extra runtime to install
    except Exception as exc:  # noqa: BLE001 - already-loaded raises; real failures surface at import clr
        log.debug("pythonnet.load('netfx') said: %s", exc)

    try:
        import clr  # type: ignore[import-not-found]  # noqa: F401
        from System import AppDomain, ResolveEventHandler  # type: ignore[import-not-found]
        from System.Reflection import Assembly  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise LhmUnavailable(f"pythonnet could not start the .NET runtime: {exc}") from exc

    def _resolve(_sender: object, args: Any) -> Any:
        # The host process (python.exe) has no app.config, so there are no binding redirects. Load any
        # requested dependency by simple name from libs/, ignoring the version the caller asked for.
        simple = str(args.Name).split(",")[0].strip()
        candidate = libs_dir / f"{simple}.dll"
        if candidate.is_file():
            return Assembly.LoadFrom(str(candidate))
        return None

    if _resolver is None:
        _resolver = ResolveEventHandler(_resolve)
        AppDomain.CurrentDomain.add_AssemblyResolve(_resolver)

    if str(libs_dir) not in sys.path:
        sys.path.insert(0, str(libs_dir))


def load_library(libs_dir: Path) -> Any:
    """Load LibreHardwareMonitorLib.dll into the .NET Framework runtime and return its Hardware namespace."""
    dll = libs_dir / "LibreHardwareMonitorLib.dll"
    if not dll.is_file():
        raise LhmUnavailable(f"{dll} not found. Run: python scripts/fetch_lhm.py")
    ensure_runtime(libs_dir)
    try:
        import clr  # type: ignore[import-not-found]

        clr.AddReference("LibreHardwareMonitorLib")
        import LibreHardwareMonitor.Hardware as hardware_ns  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise LhmUnavailable(f"failed to load LibreHardwareMonitorLib: {exc}") from exc
    return hardware_ns


class LhmSource:
    """Opens a LibreHardwareMonitor ``Computer`` and flattens its sensors into :class:`RawSensor` rows."""

    def __init__(self, libs_dir: Path) -> None:
        self._libs_dir = libs_dir
        self._computer: Any = None
        self._tick = 0

    def start(self) -> None:
        ns = load_library(self._libs_dir)
        computer = ns.Computer()
        computer.IsCpuEnabled = True
        computer.IsMotherboardEnabled = True  # SuperIO / embedded-controller fans
        computer.IsControllerEnabled = True  # fan controllers
        # GPU hardware is enabled only for the temperatures NVML cannot report (hot spot, memory junction).
        # Its D3D counters make an update cost ~75 ms, so it is refreshed every _GPU_EVERY ticks.
        computer.IsGpuEnabled = True
        computer.IsMemoryEnabled = False
        computer.IsStorageEnabled = False
        computer.IsNetworkEnabled = False
        computer.IsBatteryEnabled = False
        computer.IsPsuEnabled = False
        try:
            computer.Open()
        except Exception as exc:  # noqa: BLE001
            raise LhmUnavailable(f"Computer.Open() failed: {exc}") from exc
        self._computer = computer
        log.info("LibreHardwareMonitor opened (%d top-level hardware items)", len(list(computer.Hardware)))

    def read_raw(self) -> list[RawSensor]:
        if self._computer is None:
            raise LhmUnavailable("LibreHardwareMonitor is not open")
        out: list[RawSensor] = []
        self._tick += 1
        refresh_gpu = (self._tick - 1) % _GPU_EVERY == 0

        def visit(hw: Any) -> None:
            hw_name, hw_type = str(hw.Name), str(hw.HardwareType)
            # Skipped GPU updates are fine: sensors keep their last value, so reads below return cached data.
            if refresh_gpu or not hw_type.startswith("Gpu"):
                try:
                    hw.Update()
                except Exception as exc:  # noqa: BLE001 - one flaky device must not blank the others
                    log.debug("Update() failed for %s: %s", hw_name, exc)
            for sensor in hw.Sensors:
                try:
                    value = sensor.Value  # Nullable<float> -> float | None
                    out.append(
                        RawSensor(
                            hardware_name=hw_name,
                            hardware_type=hw_type,
                            sensor_type=str(sensor.SensorType),
                            name=str(sensor.Name),
                            value=None if value is None else float(value),
                            identifier=str(sensor.Identifier),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug("sensor read failed on %s: %s", hw_name, exc)
            for sub in hw.SubHardware:
                visit(sub)

        for hw in self._computer.Hardware:
            visit(hw)
        return out

    def close(self) -> None:
        if self._computer is not None:
            try:
                self._computer.Close()
            except Exception as exc:  # noqa: BLE001
                log.debug("Computer.Close() failed: %s", exc)
            self._computer = None


class LhmCollector:
    """CPU + fan collector built on :class:`LhmSource` and the pure parsing functions."""

    name = "lhm"

    def __init__(self, libs_dir: Path) -> None:
        self._source = LhmSource(libs_dir)

    def start(self) -> None:
        self._source.start()

    def read(self) -> LhmReading:
        raw = self._source.read_raw()
        return LhmReading(build_cpu_snapshot(raw), build_fans(raw), build_gpu_extras(raw))

    def close(self) -> None:
        self._source.close()
