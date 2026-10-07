r"""Laptop fan RPM from Acer Predator/Nitro firmware (root\wmi : AcerGamingFunction).

Why this exists: NVML and LibreHardwareMonitor cannot see a laptop's fans - the embedded controller
owns them, so NVML answers "0 fans / not supported". Acer's own PredatorSense reads them through this
WMI class, and the Linux kernel's ``acer-wmi`` driver documents the read-only encoding used here::

    GetGamingSysInfo(gmInput: u32) -> gmOutput: u64
      gmInput = 0x0000                    -> supported-sensor bitmask in bits 39:24 (sensor id N <-> BIT(N-1))
      gmInput = 0x0001 | (sensor_id << 8) -> reading in bits 23:8
      result bits 7:0 must be zero for the call to have succeeded
      sensor ids: 0x01 CPU temp, 0x02 CPU fan (RPM), 0x03 external temp 2, 0x06 GPU fan (RPM), 0x0A GPU temp

Safety, enforced in code rather than by convention:
* Only the method ``GetGamingSysInfo`` is ever invoked - no ``Set*`` method is reachable from here.
* Only the exact commands in ``_ALLOWED_COMMANDS`` may be sent; anything else raises before reaching firmware.
* The collector only runs on machines whose BIOS manufacturer is Acer, and only elevated (WMI denies others).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, NamedTuple

from app.collectors.lhm import LhmUnavailable, ensure_runtime
from app.models import FanReading
from app.winutil import is_admin

log = logging.getLogger(__name__)

_METHOD = "GetGamingSysInfo"
_CMD_SUPPORTED = 0x0000
_CMD_READING = 0x0001

SENSOR_CPU_TEMP = 0x01
SENSOR_CPU_FAN = 0x02
SENSOR_EXTERNAL_TEMP_2 = 0x03
SENSOR_GPU_FAN = 0x06
SENSOR_GPU_TEMP = 0x0A

_FAN_SENSORS: dict[int, str] = {SENSOR_CPU_FAN: "CPU Fan", SENSOR_GPU_FAN: "GPU Fan"}
_TEMP_SENSORS: dict[int, str] = {
    SENSOR_CPU_TEMP: "EC CPU temp",
    SENSOR_GPU_TEMP: "EC GPU temp",
    SENSOR_EXTERNAL_TEMP_2: "EC external temp 2",
}


def reading_command(sensor_id: int) -> int:
    return _CMD_READING | (sensor_id << 8)


_ALLOWED_COMMANDS: frozenset[int] = frozenset(
    {_CMD_SUPPORTED, *(reading_command(s) for s in (*_FAN_SENSORS, *_TEMP_SENSORS))}
)
_MAX_PLAUSIBLE_RPM = 20_000


class AcerWmiUnavailable(RuntimeError):
    """The Acer gaming WMI interface is absent, denied, or returned data that failed validation."""


class WmiResult(NamedTuple):
    ok: bool
    reading: int  # bits 23:8
    supported_mask: int  # bits 39:24


def decode_result(result: int) -> WmiResult:
    """Decode a ``gmOutput`` value. ``ok`` is False when the firmware flagged an error in the low byte."""
    return WmiResult(
        ok=(result & 0xFF) == 0,
        reading=(result >> 8) & 0xFFFF,
        supported_mask=(result >> 24) & 0xFFFF,
    )


def sensor_supported(mask: int, sensor_id: int) -> bool:
    return bool(mask & (1 << (sensor_id - 1)))


class AcerGamingWmi:
    name = "acer_wmi"

    def __init__(self, libs_dir: Path) -> None:
        self._libs_dir = libs_dir
        self._obj: Any = None
        self._uint32: Any = None
        self._fan_ids: list[int] = []
        self.supported_mask = 0  # raw bitmask from the firmware, kept for diagnostics

    # ---- low level -------------------------------------------------------------------------------

    def _call(self, command: int) -> WmiResult:
        if command not in _ALLOWED_COMMANDS:
            raise ValueError(f"command 0x{command:x} is not on the read-only allowlist")
        params = self._obj.GetMethodParameters(_METHOD)
        params["gmInput"] = self._uint32(command)
        out = self._obj.InvokeMethod(_METHOD, params, None)
        return decode_result(int(out["gmOutput"]))

    # ---- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        if not is_admin():
            raise AcerWmiUnavailable("requires an elevated process (WMI denies root\\wmi access otherwise)")
        try:
            ensure_runtime(self._libs_dir)
            import clr  # type: ignore[import-not-found]

            clr.AddReference("System.Management")  # in-box on .NET Framework; no extra Python package
            from System import UInt32  # type: ignore[import-not-found]
            from System.Management import (  # type: ignore[import-not-found]
                ManagementObjectSearcher,
                ManagementScope,
                ObjectQuery,
            )
        except (LhmUnavailable, ImportError) as exc:
            raise AcerWmiUnavailable(f".NET WMI client unavailable: {exc}") from exc

        try:
            scope = ManagementScope(r"\\.\root\wmi")
            scope.Connect()
            searcher = ManagementObjectSearcher(scope, ObjectQuery("SELECT * FROM AcerGamingFunction"))
            instances = list(searcher.Get())
        except Exception as exc:  # noqa: BLE001 - .NET ManagementException / UnauthorizedAccess
            raise AcerWmiUnavailable(f"AcerGamingFunction not reachable: {exc}") from exc
        if not instances:
            raise AcerWmiUnavailable("no AcerGamingFunction instance (not an Acer gaming laptop?)")
        self._obj, self._uint32 = instances[0], UInt32

        try:
            supported = self._call(_CMD_SUPPORTED)
        except Exception as exc:  # noqa: BLE001
            raise AcerWmiUnavailable(f"supported-sensors query failed: {exc}") from exc
        if not supported.ok or supported.supported_mask == 0:
            raise AcerWmiUnavailable("firmware reports no supported sensors")

        self.supported_mask = supported.supported_mask
        self._fan_ids = [s for s in _FAN_SENSORS if sensor_supported(supported.supported_mask, s)]
        if not self._fan_ids:
            raise AcerWmiUnavailable(f"firmware exposes no fan sensors (mask 0x{supported.supported_mask:x})")
        log.info("Acer WMI fan sensors: %s", [_FAN_SENSORS[s] for s in self._fan_ids])

    def read(self) -> list[FanReading]:
        if self._obj is None:
            raise AcerWmiUnavailable("not open")
        fans: list[FanReading] = []
        for sensor_id in self._fan_ids:
            result = self._call(reading_command(sensor_id))
            if not result.ok or result.reading > _MAX_PLAUSIBLE_RPM:
                continue  # a failed or implausible read is a gap, never a made-up number
            fans.append(FanReading(name=_FAN_SENSORS[sensor_id], rpm=float(result.reading), source="Acer EC (WMI)"))
        if not fans:
            raise AcerWmiUnavailable("all fan reads failed validation")
        return fans

    def read_temps(self) -> dict[str, int]:
        """EC temperatures, for the probe script's sanity check against LHM/NVML. Not used by the hub."""
        if self._obj is None:
            raise AcerWmiUnavailable("not open")
        temps: dict[str, int] = {}
        for sensor_id, label in _TEMP_SENSORS.items():
            result = self._call(reading_command(sensor_id))
            if result.ok and 0 < result.reading < 150:
                temps[label] = result.reading
        return temps

    def close(self) -> None:
        self._obj = None
