"""Hub resilience tests: a broken collector must degrade its section, never stop the loop."""

from __future__ import annotations

import asyncio
import threading
import unittest

from app.collectors.hub import SensorHub
from app.collectors.intel_msr import IntelMsrSample
from app.collectors.parsing import GpuExtras, LhmReading
from app.config import Settings
from app.models import CoreReading, CpuSnapshot, FanReading, GpuSnapshot, SystemSnapshot


class FakeCpu:
    def __init__(self, *, fail_start: bool = False, fail_reads: set[int] | None = None) -> None:
        self.fail_start = fail_start
        self.fail_reads = fail_reads or set()
        self.reads = 0
        self.threads: dict[str, int] = {}

    def start(self) -> None:
        self.threads["start"] = threading.get_ident()
        if self.fail_start:
            raise RuntimeError("driver exploded")

    def read(self) -> LhmReading:
        self.reads += 1
        self.threads["read"] = threading.get_ident()
        if self.reads in self.fail_reads:
            raise OSError("transient sensor failure")
        return LhmReading(CpuSnapshot(name="Fake CPU", package_temp_c=50.0 + self.reads), [], GpuExtras())

    def close(self) -> None:
        self.threads["close"] = threading.get_ident()


class FakeSystem:
    def start(self) -> None: ...
    def read(self) -> SystemSnapshot:
        return SystemSnapshot(cpu_percent=1.0)
    def close(self) -> None: ...


class FakeGpu:
    def __init__(self, mask: int = 0x1, *, fail_start: bool = False) -> None:
        self.mask, self.fail_start = mask, fail_start

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("NVML init failed: driver not loaded")

    def read(self) -> GpuSnapshot:
        return GpuSnapshot(name="Fake RTX", temp_c=70.0, throttle_mask=self.mask)

    def close(self) -> None: ...


class FakeMsr:
    def __init__(self, sample: IntelMsrSample) -> None:
        self.sample = sample

    def start(self) -> None: ...
    def read(self) -> IntelMsrSample:
        return self.sample
    def close(self) -> None: ...


# 55 W PL1 / 157 W PL2 at 1/8 W resolution
PL_REG = (440 | (1 << 15) | (1 << 16)) | ((1256 | (1 << 15)) << 32)


def settings() -> Settings:
    return Settings(poll_interval_s=0.05)


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for(self, hub: SensorHub, seq: int) -> None:
        for _ in range(200):
            if hub.latest and hub.latest.snapshot.seq >= seq:
                return
            await asyncio.sleep(0.02)
        self.fail(f"hub never reached seq {seq}")

    async def test_startup_failure_degrades_but_keeps_streaming(self) -> None:
        hub = SensorHub(settings(), lhm=FakeCpu(fail_start=True), system=FakeSystem(), auto_detect=False)
        await hub.start()
        try:
            await self._wait_for(hub, 3)
            snap = hub.latest.snapshot  # type: ignore[union-attr]
            self.assertIsNone(snap.cpu.package_temp_c)  # empty section, not an exception
            self.assertEqual(snap.system.cpu_percent, 1.0)  # unrelated collector unaffected
            lhm = next(c for c in snap.health.collectors if c.name == "lhm")
            self.assertFalse(lhm.available)
            self.assertIn("driver exploded", lhm.error or "")
            self.assertTrue(any("LibreHardwareMonitor unavailable" in w for w in snap.health.warnings))
        finally:
            await hub.stop()

    async def test_transient_failure_recovers(self) -> None:
        cpu = FakeCpu(fail_reads={2, 3})
        hub = SensorHub(settings(), lhm=cpu, system=FakeSystem(), auto_detect=False)
        await hub.start()
        try:
            await self._wait_for(hub, 8)
            snap = hub.latest.snapshot  # type: ignore[union-attr]
            self.assertEqual(snap.cpu.name, "Fake CPU")  # back after the two failed reads
            lhm = next(c for c in snap.health.collectors if c.name == "lhm")
            self.assertTrue(lhm.available)
            self.assertIsNone(lhm.error)
        finally:
            await hub.stop()

    async def test_all_collector_calls_share_one_thread_and_close_runs_on_it(self) -> None:
        cpu = FakeCpu()
        hub = SensorHub(settings(), lhm=cpu, system=FakeSystem(), auto_detect=False)
        await hub.start()
        await self._wait_for(hub, 2)
        await hub.stop()
        self.assertEqual(len(set(cpu.threads.values())), 1, cpu.threads)
        self.assertNotEqual(cpu.threads["start"], threading.get_ident())  # not the event-loop thread
        self.assertIn("close", cpu.threads)

    async def test_slow_subscriber_only_sees_fresh_items(self) -> None:
        hub = SensorHub(settings(), lhm=FakeCpu(), system=FakeSystem(), auto_detect=False)
        q = hub.subscribe()
        await hub.start()
        try:
            await self._wait_for(hub, 10)  # consumer never read: queue is bounded, poll loop unaffected
            self.assertLessEqual(q.qsize(), 2)
            newest = None
            while not q.empty():
                newest = q.get_nowait()
            assert newest is not None
            self.assertGreaterEqual(newest.snapshot.seq, 9)
        finally:
            hub.unsubscribe(q)
            await hub.stop()

    async def test_snapshot_carries_timing_and_monotonic_seq(self) -> None:
        hub = SensorHub(settings(), lhm=FakeCpu(), system=FakeSystem(), auto_detect=False)
        q = hub.subscribe()
        await hub.start()
        try:
            first = await asyncio.wait_for(q.get(), 2)
            second = await asyncio.wait_for(q.get(), 2)
            self.assertGreater(second.snapshot.seq, first.snapshot.seq)
            self.assertIsNotNone(first.snapshot.collect_ms)
            self.assertIn('"seq"', first.json)
        finally:
            hub.unsubscribe(q)
            await hub.stop()

    async def test_msr_flags_give_detected_cpu_throttle_and_power_limits(self) -> None:
        msr = FakeMsr(IntelMsrSample(perf_limit_reasons=1 << 11, package_therm_status=0, pkg_power_limit=PL_REG, power_unit=3))
        hub = SensorHub(settings(), lhm=FakeCpu(), system=FakeSystem(), msr=msr, auto_detect=False)
        await hub.start()
        try:
            await self._wait_for(hub, 2)
            snap = hub.latest.snapshot  # type: ignore[union-attr]
            self.assertEqual((snap.throttle.active, snap.throttle.type), (True, "power"))
            self.assertEqual((snap.throttle.confidence, snap.throttle.component), ("detected", "cpu"))
            self.assertEqual(snap.throttle.source, "msr:perf_limit_reasons")
            self.assertEqual((snap.cpu.pl1_w, snap.cpu.pl2_w), (55.0, 157.0))
        finally:
            await hub.stop()

    async def test_gpu_throttle_is_detected_and_reported(self) -> None:
        hub = SensorHub(settings(), lhm=FakeCpu(), system=FakeSystem(), gpu=FakeGpu(mask=0x40), auto_detect=False)
        await hub.start()
        try:
            await self._wait_for(hub, 2)
            snap = hub.latest.snapshot  # type: ignore[union-attr]
            self.assertEqual(snap.gpu.name if snap.gpu else None, "Fake RTX")
            self.assertEqual((snap.throttle.active, snap.throttle.type, snap.throttle.component), (True, "thermal", "gpu"))
            self.assertEqual(snap.throttle.gpu.confidence if snap.throttle.gpu else None, "detected")
        finally:
            await hub.stop()

    async def test_nvml_startup_failure_leaves_gpu_empty_and_warns(self) -> None:
        hub = SensorHub(settings(), lhm=FakeCpu(), system=FakeSystem(), gpu=FakeGpu(fail_start=True), auto_detect=False)
        await hub.start()
        try:
            await self._wait_for(hub, 2)
            snap = hub.latest.snapshot  # type: ignore[union-attr]
            self.assertIsNone(snap.gpu)
            self.assertIsNone(snap.throttle.gpu)
            self.assertTrue(any("NVIDIA GPU telemetry unavailable" in w for w in snap.health.warnings))
            self.assertEqual(snap.cpu.name, "Fake CPU")  # CPU side unaffected
        finally:
            await hub.stop()

    async def test_without_msr_cpu_throttle_is_never_reported_as_detected(self) -> None:
        class BusyCpu(FakeCpu):
            def read(self) -> LhmReading:
                self.reads += 1
                cores = [CoreReading(index=i, kind="P", clock_mhz=3000.0, load_pct=95.0) for i in range(1, 5)]
                cpu = CpuSnapshot(name="Busy", vendor="intel", package_temp_c=99.0, total_load_pct=95.0, cores=cores)
                return LhmReading(cpu, [], GpuExtras())

        hub = SensorHub(settings(), lhm=BusyCpu(), system=FakeSystem(), auto_detect=False)
        await hub.start()
        try:
            await self._wait_for(hub, 3)
            snap = hub.latest.snapshot  # type: ignore[union-attr]
            self.assertNotEqual(snap.throttle.cpu.confidence, "detected")
            self.assertTrue(snap.throttle.cpu.source.startswith("inference:"))
        finally:
            await hub.stop()


if __name__ == "__main__":
    unittest.main()
