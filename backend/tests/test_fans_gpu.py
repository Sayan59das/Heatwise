"""Acer fan source, GPU extras and NVML 'unsupported' bookkeeping. No hardware or drivers needed."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from typing import Any

from app.collectors import acer_wmi
from app.collectors.acer_wmi import (
    AcerGamingWmi,
    AcerWmiUnavailable,
    decode_result,
    reading_command,
    sensor_supported,
)
from app.collectors.hub import SensorHub
from app.collectors.nvidia import NvmlCollector
from app.collectors.parsing import RawSensor, build_gpu_extras
from app.config import Settings
from app.models import FanReading
from tests.test_hub import FakeCpu, FakeGpu, FakeSystem


def gpu_sensor(name: str, value: float | None, hw: str = "GpuNvidia", kind: str = "Temperature") -> RawSensor:
    return RawSensor("RTX", hw, kind, name, value, f"/gpu-nvidia/0/{name}")


class GpuExtrasTests(unittest.TestCase):
    def test_hotspot_and_memory_junction(self) -> None:
        extras = build_gpu_extras(
            [gpu_sensor("GPU Core", 49.0), gpu_sensor("GPU Hot Spot", 56.8125), gpu_sensor("GPU Memory Junction", 60.0)]
        )
        self.assertEqual((extras.hotspot_c, extras.memory_c), (56.8, 60.0))

    def test_absent_or_wrong_hardware_gives_none(self) -> None:
        self.assertEqual(tuple(build_gpu_extras([])), (None, None))
        cpu_like = [gpu_sensor("GPU Hot Spot", 70.0, hw="Cpu")]
        self.assertEqual(tuple(build_gpu_extras(cpu_like)), (None, None))
        load_not_temp = [gpu_sensor("GPU Hot Spot", 70.0, kind="Load")]
        self.assertEqual(tuple(build_gpu_extras(load_not_temp)), (None, None))


class AcerDecodeTests(unittest.TestCase):
    def test_reading_command_encoding(self) -> None:
        self.assertEqual(reading_command(0x02), 0x0201)  # CPU fan
        self.assertEqual(reading_command(0x06), 0x0601)  # GPU fan
        self.assertEqual(reading_command(0x0A), 0x0A01)  # GPU temp

    def test_decode_reading(self) -> None:
        r = decode_result(2400 << 8)
        self.assertTrue(r.ok)
        self.assertEqual(r.reading, 2400)

    def test_nonzero_low_byte_means_firmware_error(self) -> None:
        self.assertFalse(decode_result((2400 << 8) | 0x01).ok)

    def test_supported_mask_uses_bit_id_minus_one(self) -> None:
        mask = decode_result(0b100011 << 24).supported_mask  # sensor ids 1, 2 and 6
        self.assertTrue(sensor_supported(mask, 0x01))
        self.assertTrue(sensor_supported(mask, 0x02))
        self.assertFalse(sensor_supported(mask, 0x03))
        self.assertTrue(sensor_supported(mask, 0x06))
        self.assertFalse(sensor_supported(mask, 0x0A))


class FakeWmiObject:
    """Stands in for System.Management.ManagementObject; records every call made to the 'firmware'."""

    def __init__(self, results: dict[int, int]) -> None:
        self.results = results
        self.invoked: list[tuple[str, int]] = []
        self.params_requested: list[str] = []

    def GetMethodParameters(self, method: str) -> dict[str, Any]:  # noqa: N802
        self.params_requested.append(method)
        return {}

    def InvokeMethod(self, method: str, params: dict[str, Any], _options: object) -> dict[str, int]:  # noqa: N802
        command = int(params["gmInput"])
        self.invoked.append((method, command))
        return {"gmOutput": self.results.get(command, 0x01)}  # unknown command -> firmware error code


def opened(results: dict[int, int], fan_ids: tuple[int, ...] = (0x02, 0x06)) -> tuple[AcerGamingWmi, FakeWmiObject]:
    obj = FakeWmiObject(results)
    w = AcerGamingWmi(Path("."))
    w._obj, w._uint32, w._fan_ids = obj, int, list(fan_ids)
    return w, obj


class AcerSafetyTests(unittest.TestCase):
    def test_only_read_commands_are_allowed(self) -> None:
        allowed = acer_wmi._ALLOWED_COMMANDS
        self.assertIn(0x0000, allowed)
        self.assertIn(0x0201, allowed)
        self.assertIn(0x0601, allowed)
        # Anything else - battery calibration, writes, unknown sensors - must be rejected locally.
        for cmd in (0x0002, 0x0101 + 0x0100 * 6, 0x0701, 0xFFFF, 0x0001, 0x1234_5678):
            self.assertNotIn(cmd, allowed, hex(cmd))

    def test_disallowed_command_never_reaches_firmware(self) -> None:
        w, obj = opened({})
        with self.assertRaises(ValueError):
            w._call(0x0002)
        with self.assertRaises(ValueError):
            w._call(0xDEAD)
        self.assertEqual(obj.invoked, [])
        self.assertEqual(obj.params_requested, [])

    def test_only_the_getter_method_is_ever_invoked(self) -> None:
        w, obj = opened({0x0201: 2400 << 8, 0x0601: 3100 << 8})
        w.read()
        w.read_temps()
        self.assertTrue(obj.invoked)
        self.assertEqual({m for m, _ in obj.invoked}, {"GetGamingSysInfo"})
        self.assertFalse(any("Set" in m for m, _ in obj.invoked))
        self.assertEqual(acer_wmi._METHOD, "GetGamingSysInfo")


class AcerReadTests(unittest.TestCase):
    def test_reads_both_fans(self) -> None:
        w, _ = opened({0x0201: 2400 << 8, 0x0601: 3100 << 8})
        fans = w.read()
        self.assertEqual([(f.name, f.rpm) for f in fans], [("CPU Fan", 2400.0), ("GPU Fan", 3100.0)])
        self.assertEqual(fans[0].source, "Acer EC (WMI)")

    def test_stopped_fan_is_zero_not_missing(self) -> None:
        w, _ = opened({0x0201: 0, 0x0601: 3100 << 8})
        self.assertEqual(w.read()[0].rpm, 0.0)

    def test_failed_and_implausible_reads_are_dropped_not_invented(self) -> None:
        w, _ = opened({0x0201: (2400 << 8) | 0x01, 0x0601: 65535 << 8})  # firmware error / absurd RPM
        with self.assertRaises(AcerWmiUnavailable):
            w.read()

    def test_one_bad_fan_does_not_hide_the_other(self) -> None:
        w, _ = opened({0x0201: (2400 << 8) | 0x01, 0x0601: 3100 << 8})
        self.assertEqual([f.name for f in w.read()], ["GPU Fan"])

    def test_not_open(self) -> None:
        with self.assertRaises(AcerWmiUnavailable):
            AcerGamingWmi(Path(".")).read()


# ---- NVML 'unsupported' bookkeeping, against a fake driver ---------------------------------------


class FakeNvmlError(Exception):
    def __init__(self, value: int) -> None:
        super().__init__(f"NVML error {value}")
        self.value = value


class _U:
    gpu, memory = 24, 1


class _M:
    used, total = 1323 * 2**20, 6141 * 2**20


class FakeNvml:
    NVMLError = FakeNvmlError
    NVML_ERROR_UNINITIALIZED, NVML_ERROR_INVALID_ARGUMENT, NVML_ERROR_NOT_SUPPORTED = 1, 2, 3
    NVML_ERROR_DRIVER_NOT_LOADED, NVML_ERROR_GPU_IS_LOST, NVML_ERROR_UNKNOWN = 9, 15, 999
    NVML_TEMPERATURE_GPU, NVML_TEMPERATURE_THRESHOLD_SLOWDOWN, NVML_TEMPERATURE_THRESHOLD_SHUTDOWN = 0, 1, 2
    NVML_CLOCK_GRAPHICS, NVML_CLOCK_MEM = 0, 1

    def __init__(self, fan: object = FakeNvmlError(3), enforced: object = 92667, mgmt: object = FakeNvmlError(3)) -> None:
        self._vals = {"fan": fan, "enforced": enforced, "mgmt": mgmt}

    def _get(self, key: str) -> Any:
        v = self._vals[key]
        if isinstance(v, Exception):
            raise v
        return v

    def nvmlDeviceGetFanSpeed(self, _h: object) -> Any: return self._get("fan")  # noqa: N802
    def nvmlDeviceGetEnforcedPowerLimit(self, _h: object) -> Any: return self._get("enforced")  # noqa: N802
    def nvmlDeviceGetPowerManagementLimit(self, _h: object) -> Any: return self._get("mgmt")  # noqa: N802
    def nvmlDeviceGetUtilizationRates(self, _h: object) -> Any: return _U()  # noqa: N802
    def nvmlDeviceGetMemoryInfo(self, _h: object) -> Any: return _M()  # noqa: N802
    def nvmlDeviceGetCurrentClocksThrottleReasons(self, _h: object) -> int: return 0x1  # noqa: N802
    def nvmlDeviceGetTemperature(self, _h: object, _s: int) -> int: return 49  # noqa: N802
    def nvmlDeviceGetClockInfo(self, _h: object, _c: int) -> int: return 210  # noqa: N802
    def nvmlDeviceGetPowerUsage(self, _h: object) -> int: return 2029  # noqa: N802
    def nvmlDeviceGetPerformanceState(self, _h: object) -> int: return 8  # noqa: N802


def nvml_collector(fake: FakeNvml) -> NvmlCollector:
    c = NvmlCollector()
    c._nvml, c._handle = fake, object()
    c._static = {"name": "RTX 4050", "driver": "617.14", "temp_slowdown": 91, "temp_shutdown": 101,
                 "max_core_clock": 3105, "max_mem_clock": 8001, "power_default_mw": 80000}
    return c


class NvmlUnsupportedTests(unittest.TestCase):
    def test_not_supported_fan_is_none_and_listed(self) -> None:
        snap = nvml_collector(FakeNvml()).read()
        self.assertIsNone(snap.fan_percent)
        self.assertIn("fan_percent", snap.unsupported)

    def test_supported_fan_is_reported_and_not_listed(self) -> None:
        snap = nvml_collector(FakeNvml(fan=63)).read()
        self.assertEqual(snap.fan_percent, 63.0)
        self.assertNotIn("fan_percent", snap.unsupported)

    def test_transient_error_is_a_gap_not_an_unsupported_field(self) -> None:
        snap = nvml_collector(FakeNvml(fan=FakeNvmlError(FakeNvml.NVML_ERROR_UNKNOWN))).read()
        self.assertIsNone(snap.fan_percent)
        self.assertNotIn("fan_percent", snap.unsupported)

    def test_enforced_limit_used_when_management_limit_unsupported(self) -> None:
        snap = nvml_collector(FakeNvml(enforced=92667, mgmt=FakeNvmlError(3))).read()
        self.assertEqual(snap.power_limit_w, 92.7)
        self.assertNotIn("power_limit_w", snap.unsupported)

    def test_management_limit_fallback_clears_the_unsupported_flag(self) -> None:
        snap = nvml_collector(FakeNvml(enforced=FakeNvmlError(3), mgmt=100000)).read()
        self.assertEqual(snap.power_limit_w, 100.0)
        self.assertNotIn("power_limit_w", snap.unsupported)

    def test_both_unsupported_marks_the_field(self) -> None:
        snap = nvml_collector(FakeNvml(enforced=FakeNvmlError(3), mgmt=FakeNvmlError(3))).read()
        self.assertIsNone(snap.power_limit_w)
        self.assertIn("power_limit_w", snap.unsupported)

    def test_gpu_lost_is_raised_so_the_hub_can_retry(self) -> None:
        c = nvml_collector(FakeNvml(fan=FakeNvmlError(FakeNvml.NVML_ERROR_GPU_IS_LOST)))
        with self.assertRaises(FakeNvmlError):
            c.read()
        self.assertTrue(c._needs_reinit)


# ---- hub merging ---------------------------------------------------------------------------------


class FakeFans:
    def start(self) -> None: ...
    def read(self) -> list[FanReading]:
        return [FanReading(name="CPU Fan", rpm=2400.0), FanReading(name="GPU Fan", rpm=3100.0)]
    def close(self) -> None: ...


class HubMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fans_and_gpu_extras_are_merged_into_the_snapshot(self) -> None:
        from app.collectors.parsing import GpuExtras, LhmReading
        from app.models import CpuSnapshot

        class CpuWithGpuTemps(FakeCpu):
            def read(self) -> LhmReading:
                return LhmReading(CpuSnapshot(name="Fake CPU"), [], GpuExtras(hotspot_c=57.0, memory_c=60.0))

        hub = SensorHub(Settings(poll_interval_s=0.05), lhm=CpuWithGpuTemps(), system=FakeSystem(),
                        gpu=FakeGpu(), fans=FakeFans(), auto_detect=False)
        await hub.start()
        try:
            for _ in range(100):
                if hub.latest and hub.latest.snapshot.seq >= 2:
                    break
                await asyncio.sleep(0.02)
            snap = hub.latest.snapshot  # type: ignore[union-attr]
            self.assertEqual([(f.name, f.rpm) for f in snap.fans], [("CPU Fan", 2400.0), ("GPU Fan", 3100.0)])
            assert snap.gpu is not None
            self.assertEqual(snap.gpu.fan_rpm, 3100.0)
            self.assertEqual((snap.gpu.temp_hotspot_c, snap.gpu.temp_memory_c), (57.0, 60.0))
        finally:
            await hub.stop()

    async def test_cpu_load_falls_back_to_psutil_when_lhm_has_none(self) -> None:
        from app.models import SystemSnapshot

        class Sys(FakeSystem):
            def read(self) -> SystemSnapshot:
                return SystemSnapshot(cpu_percent=42.0)

        hub = SensorHub(Settings(poll_interval_s=0.05), lhm=FakeCpu(), system=Sys(), auto_detect=False)
        await hub.start()
        try:
            for _ in range(100):
                if hub.latest:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(hub.latest.snapshot.cpu.total_load_pct, 42.0)  # type: ignore[union-attr]
        finally:
            await hub.stop()


if __name__ == "__main__":
    unittest.main()
