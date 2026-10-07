"""SensorHub: owns every collector, polls them on a fixed cadence and fans snapshots out to subscribers.

Design notes
* All collectors run on ONE dedicated worker thread. LibreHardwareMonitor's ``Computer`` and the
  PawnIO handle are not thread-safe, and this keeps open/read/close on one thread for everything.
* A failing collector never stops the loop: its section falls back to an empty model and the
  failure is recorded in ``snapshot.health``.
* Throttle detection prefers hardware flags (NVML, Intel MSRs -> "detected") and only falls back to
  clock/temperature/power inference ("inferred") when no flag is readable.
* Subscribers get a small bounded queue; a slow consumer drops its oldest item instead of
  back-pressuring the poll loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple, Protocol, TypeVar

from app.collectors.acer_wmi import AcerGamingWmi
from app.collectors.intel_msr import IntelMsrReader, IntelMsrSample
from app.collectors.lhm import LhmCollector
from app.collectors.nvidia import NvmlCollector, NvmlProcessReader
from app.collectors.parsing import GpuExtras, LhmReading
from app.collectors.processes import ProcessSampler
from app.collectors.system import SystemCollector
from app.collectors.throttle import (
    CpuThrottleInference,
    combine,
    decode_intel_throttle,
    decode_nvml_throttle,
    decode_power_limits,
)
from app.config import Settings
from app.models import (
    CollectorStatus,
    CpuSnapshot,
    FanReading,
    GpuSnapshot,
    ProcessInfo,
    SensorHealth,
    Snapshot,
    SystemSnapshot,
    ThrottleComponent,
)
from app.winutil import is_admin, pawnio_installed, system_manufacturer

log = logging.getLogger(__name__)
T = TypeVar("T")


class CpuSource(Protocol):
    def start(self) -> None: ...
    def read(self) -> LhmReading: ...
    def close(self) -> None: ...


class FanSource(Protocol):
    def start(self) -> None: ...
    def read(self) -> list[FanReading]: ...
    def close(self) -> None: ...


class SystemSource(Protocol):
    def start(self) -> None: ...
    def read(self) -> SystemSnapshot: ...
    def close(self) -> None: ...


class GpuSource(Protocol):
    def start(self) -> None: ...
    def read(self) -> GpuSnapshot: ...
    def close(self) -> None: ...


class MsrSource(Protocol):
    def start(self) -> None: ...
    def read(self) -> IntelMsrSample: ...
    def close(self) -> None: ...


class ProcessSource(Protocol):
    """Runs on its own thread; ``latest`` is a cheap read of the newest result."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def latest(self) -> list[ProcessInfo]: ...
    @property
    def error(self) -> str | None: ...


class Published(NamedTuple):
    snapshot: Snapshot
    json: str  # serialised once per tick, shared by every WebSocket client


class _State:
    """Mutable per-collector bookkeeping behind ``CollectorStatus``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.available = False
        self.error: str | None = "not started"
        self.last_ok: float | None = None

    def ok(self) -> None:
        if not self.available or self.error is not None:
            log.info("collector %s is healthy", self.name)
        self.available, self.error, self.last_ok = True, None, time.time()

    def fail(self, error: str) -> None:
        if error != self.error:  # log transitions and changes only, never once per second
            log.warning("collector %s: %s", self.name, error)
        self.available, self.error = False, error

    def status(self) -> CollectorStatus:
        return CollectorStatus(name=self.name, available=self.available, error=self.error, last_ok=self.last_ok)


def _cpu_is_intel() -> bool:
    ident = os.environ.get("PROCESSOR_IDENTIFIER", "")
    return "GenuineIntel" in ident or "Intel" in ident


class SensorHub:
    def __init__(
        self,
        settings: Settings,
        *,
        lhm: CpuSource | None = None,  # every collector is injectable for tests
        system: SystemSource | None = None,
        gpu: GpuSource | None = None,
        msr: MsrSource | None = None,
        fans: FanSource | None = None,
        procs: ProcessSource | None = None,
        auto_detect: bool = True,  # False: never create real GPU/MSR collectors that were not injected
    ) -> None:
        self._settings = settings
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sensors")
        self._lhm: CpuSource = lhm or LhmCollector(settings.libs_dir)
        self._system: SystemSource = system or SystemCollector()
        self._gpu: GpuSource | None = gpu or (NvmlCollector() if auto_detect else None)
        # MSR flags only exist on Intel CPUs; on AMD we never even try.
        self._msr: MsrSource | None = msr or (
            IntelMsrReader(settings.libs_dir) if auto_detect and _cpu_is_intel() else None
        )

        # Laptop fans live behind the embedded controller; only Acer's firmware interface is implemented.
        manufacturer = (system_manufacturer() or "").lower()
        self._fans: FanSource | None = fans or (
            AcerGamingWmi(settings.libs_dir) if auto_detect and "acer" in manufacturer else None
        )

        self._procs: ProcessSource | None = procs or (
            ProcessSampler(gpu_source=NvmlProcessReader()) if auto_detect and sys.platform == "win32" else None
        )

        self._states: dict[str, _State] = {"lhm": _State("lhm"), "system": _State("system")}
        if self._gpu is not None:
            self._states["nvml"] = _State("nvml")
        if self._msr is not None:
            self._states["intel_msr"] = _State("intel_msr")
        if self._fans is not None:
            self._states["acer_wmi"] = _State("acer_wmi")
        if self._procs is not None:
            self._states["processes"] = _State("processes")

        self._inference = CpuThrottleInference()
        self._subscribers: set[asyncio.Queue[Published]] = set()
        self._task: asyncio.Task[None] | None = None
        self._seq = 0
        self._admin = is_admin()
        self._pawnio = pawnio_installed()
        self.latest: Published | None = None

    # ---- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._open_all)
        self._task = asyncio.create_task(self._run(), name="sensor-poll")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._close_all)
        self._executor.shutdown(wait=True)

    # ---- pub/sub ---------------------------------------------------------------------------------

    def subscribe(self, maxsize: int = 2) -> asyncio.Queue[Published]:
        q: asyncio.Queue[Published] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Published]) -> None:
        self._subscribers.discard(q)

    def _publish(self, item: Published) -> None:
        self.latest = item
        for q in tuple(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()  # drop the oldest so a slow client only ever sees fresh data
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(item)

    # ---- worker-thread side ----------------------------------------------------------------------

    def _collectors(self) -> list[tuple[str, CpuSource | SystemSource | GpuSource | MsrSource | FanSource]]:
        items: list[tuple[str, CpuSource | SystemSource | GpuSource | MsrSource | FanSource]] = [
            ("lhm", self._lhm),
            ("system", self._system),
        ]
        if self._gpu is not None:
            items.append(("nvml", self._gpu))
        if self._msr is not None:
            items.append(("intel_msr", self._msr))
        if self._fans is not None:
            items.append(("acer_wmi", self._fans))
        return items

    def _open_all(self) -> None:
        if self._procs is not None:
            self._procs.start()  # spawns its own thread; never blocks this one
        for name, collector in self._collectors():
            state = self._states[name]
            try:
                collector.start()
                state.ok()
            except Exception as exc:  # noqa: BLE001 - startup failure degrades, never aborts the app
                state.fail(f"{type(exc).__name__}: {exc}")

    def _close_all(self) -> None:
        if self._procs is not None:
            try:
                self._procs.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("process sampler stop failed: %s", exc)
        for _name, collector in self._collectors():
            try:
                collector.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("close failed: %s", exc)

    def _guard(self, name: str, fn: Callable[[], T], default: T) -> T:
        state = self._states[name]
        if not state.available and state.last_ok is None:
            return default  # never started successfully; error already recorded
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            state.fail(f"{type(exc).__name__}: {exc}")
            return default
        state.ok()
        return result

    def _cpu_throttle(self, now: float, cpu: CpuSnapshot, msr: IntelMsrSample | None) -> ThrottleComponent:
        """Detected from MSR flags when readable, otherwise inferred from clock/temp/power."""
        if msr is not None and (msr.perf_limit_reasons is not None or msr.package_therm_status is not None):
            return decode_intel_throttle(msr.perf_limit_reasons, msr.package_therm_status)
        return self._inference.update(now, cpu)

    def _poll_once(self) -> Snapshot:
        started = time.perf_counter()
        now = time.time()
        reading = self._guard("lhm", self._lhm.read, LhmReading(CpuSnapshot(), [], GpuExtras()))
        cpu, fans = reading.cpu, list(reading.fans)
        system = self._guard("system", self._system.read, SystemSnapshot())
        if cpu.total_load_pct is None:
            cpu.total_load_pct = system.cpu_percent  # psutil keeps load available when LHM is not

        gpu: GpuSnapshot | None = None
        if self._gpu is not None:
            gpu = self._guard("nvml", self._gpu.read, None)

        if self._fans is not None:
            fans.extend(self._guard("acer_wmi", self._fans.read, []))
        if gpu is not None:
            gpu.temp_hotspot_c, gpu.temp_memory_c = reading.gpu.hotspot_c, reading.gpu.memory_c
            gpu.fan_rpm = next((f.rpm for f in fans if f.name == "GPU Fan"), None)

        processes: list[ProcessInfo] = []
        if self._procs is not None:
            processes = self._procs.latest()
            err = self._procs.error
            (self._states["processes"].fail(err) if err else self._states["processes"].ok())

        msr: IntelMsrSample | None = None
        if self._msr is not None:
            msr = self._guard("intel_msr", self._msr.read, None)
            if msr is not None:
                cpu.pl1_w, cpu.pl2_w = decode_power_limits(msr.pkg_power_limit, msr.power_unit)

        throttle = combine(
            self._cpu_throttle(time.monotonic(), cpu, msr),  # monotonic: a clock change must not corrupt the window
            decode_nvml_throttle(gpu.throttle_mask) if gpu is not None else None,
        )

        self._seq += 1
        return Snapshot(
            timestamp=round(now, 3),
            seq=self._seq,
            interval_s=self._settings.poll_interval_s,
            collect_ms=round((time.perf_counter() - started) * 1000, 1),
            cpu=cpu,
            gpu=gpu,
            throttle=throttle,
            fans=fans,
            system=system,
            processes=processes,
            health=self._health(cpu),
        )

    def _health(self, cpu: CpuSnapshot) -> SensorHealth:
        warnings: list[str] = []
        lhm = self._states["lhm"]
        if not self._admin:
            warnings.append(
                "Not running as administrator: CPU temperature, power and fan sensors need elevation."
            )
        if not lhm.available:
            warnings.append(f"LibreHardwareMonitor unavailable: {lhm.error}")
        elif cpu.package_temp_c is None and cpu.max_core_temp_c is None:
            if self._pawnio is False:
                warnings.append(
                    "No CPU temperature sensors are reporting and the PawnIO driver is not installed. "
                    "Install PawnIO (https://pawnio.eu) and run elevated."
                )
            elif self._admin:
                warnings.append(
                    "No CPU temperature sensors are reporting even though the app is elevated. "
                    "The kernel driver may be blocked (Memory Integrity / vulnerable-driver blocklist)."
                )

        msr = self._states.get("intel_msr")
        if msr is not None and not msr.available and self._admin:
            warnings.append(
                f"Intel throttle flags unavailable ({msr.error}); CPU throttling is being inferred, not detected."
            )
        fan_state = self._states.get("acer_wmi")
        if fan_state is not None and not fan_state.available and self._admin:
            warnings.append(f"Laptop fan RPM unavailable: {fan_state.error}")
        nvml = self._states.get("nvml")
        if nvml is not None and not nvml.available:
            warnings.append(f"NVIDIA GPU telemetry unavailable: {nvml.error}")

        return SensorHealth(
            is_admin=self._admin,
            pawnio_installed=self._pawnio,
            collectors=[s.status() for s in self._states.values()],
            warnings=warnings,
        )

    # ---- event-loop side -------------------------------------------------------------------------

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        interval = self._settings.poll_interval_s
        deadline = time.monotonic()
        while True:
            try:
                snapshot = await loop.run_in_executor(self._executor, self._poll_once)
                self._publish(Published(snapshot, snapshot.model_dump_json()))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the poll loop must outlive any single bad tick
                log.exception("poll tick failed")
            # Fixed-rate schedule (no drift); if a tick overruns, skip ahead instead of bursting.
            deadline += interval
            now = time.monotonic()
            if deadline < now:
                deadline = now
            await asyncio.sleep(deadline - now)
