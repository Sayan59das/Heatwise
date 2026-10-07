"""Per-process CPU / memory / GPU attribution, sampled on its own thread.

Why not psutil: ``Process.cpu_percent`` costs ~5 ms per process on Windows (it re-reads the whole
process table for each one), i.e. ~2 s for a normal desktop. Task Manager instead asks the kernel for the
complete table once; this module does the same with ``NtQuerySystemInformation`` through ``ctypes``
(standard library, no extra dependency) and computes CPU% from the difference between two samples.

The sampler runs on a background thread at a slower cadence than the main poll (default 3 s) so it can
never delay the 1 Hz sensor loop. GPU shares come from NVML's per-process utilisation when available.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from app.models import ProcessInfo

log = logging.getLogger(__name__)

_IGNORED_NAMES: Final = frozenset({"idle", "system idle process", "[system process]", "memory compression"})


@dataclass(frozen=True)
class RawProcess:
    pid: int
    name: str
    cpu_100ns: int  # cumulative user + kernel time
    working_set: int  # bytes


# ---------------------------------------------------------------------------------------------------
# Pure computation (unit-tested without touching the OS)
# ---------------------------------------------------------------------------------------------------


def compute_processes(
    prev: Mapping[int, RawProcess],
    cur: Mapping[int, RawProcess],
    elapsed_s: float,
    ncpu: int,
    gpu_by_pid: Mapping[int, float] | None = None,
    top_cpu: int = 8,
    top_gpu: int = 4,
) -> list[ProcessInfo]:
    """Aggregate by executable name; ``cpu_pct`` is a share of the whole CPU (all logical cores)."""
    if elapsed_s <= 0 or ncpu <= 0:
        return []
    gpu_by_pid = gpu_by_pid or {}
    budget_100ns = elapsed_s * 1e7 * ncpu

    groups: dict[str, list[tuple[int, float, int, float | None]]] = defaultdict(list)
    for pid, now in cur.items():
        if now.name.lower() in _IGNORED_NAMES or pid == 0:
            continue
        before = prev.get(pid)
        # A pid reused by a new process (or a restart) must not produce a negative / huge delta.
        delta = now.cpu_100ns - before.cpu_100ns if before is not None and before.name == now.name else 0
        cpu = max(0.0, delta / budget_100ns * 100.0)
        groups[now.name].append((pid, cpu, now.working_set, gpu_by_pid.get(pid)))

    rows: list[ProcessInfo] = []
    for name, members in groups.items():
        gpus = [g for *_, g in members if g is not None]
        busiest = max(members, key=lambda m: (m[1], m[3] or 0.0))
        rows.append(
            ProcessInfo(
                name=name,
                count=len(members),
                pid=busiest[0],
                cpu_pct=round(sum(m[1] for m in members), 2),
                mem_mb=round(sum(m[2] for m in members) / 2**20, 1),
                gpu_pct=round(sum(gpus), 1) if gpus else None,
            )
        )

    by_cpu = sorted(rows, key=lambda r: r.cpu_pct, reverse=True)[:top_cpu]
    by_gpu = sorted((r for r in rows if r.gpu_pct), key=lambda r: r.gpu_pct or 0.0, reverse=True)[:top_gpu]
    chosen = {r.name: r for r in (*by_cpu, *by_gpu)}
    return sorted(chosen.values(), key=lambda r: (r.cpu_pct, r.gpu_pct or 0.0), reverse=True)


# ---------------------------------------------------------------------------------------------------
# Windows process table via NtQuerySystemInformation
# ---------------------------------------------------------------------------------------------------


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]


class _SystemProcessInformation(ctypes.Structure):
    """Leading part of SYSTEM_PROCESS_INFORMATION (x64); ctypes applies the natural alignment."""

    _fields_ = [
        ("NextEntryOffset", ctypes.c_ulong),
        ("NumberOfThreads", ctypes.c_ulong),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", ctypes.c_ulong),
        ("NumberOfThreadsHighWatermark", ctypes.c_ulong),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", _UnicodeString),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ctypes.c_ulong),
        ("SessionId", ctypes.c_ulong),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
    ]


_SYSTEM_PROCESS_INFORMATION_CLASS = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


class ProcessTableUnavailable(RuntimeError):
    pass


def read_process_table() -> dict[int, RawProcess]:
    """One kernel call -> every process. Works without elevation."""
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ProcessTableUnavailable("process table access is implemented for 64-bit Windows only")

    nt_query = ctypes.windll.ntdll.NtQuerySystemInformation  # type: ignore[attr-defined]
    nt_query.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    nt_query.restype = ctypes.c_ulong

    size = 1 << 20
    for _ in range(6):  # the table grows between calls; retry with the size the kernel asks for
        buf = ctypes.create_string_buffer(size)
        needed = ctypes.c_ulong(0)
        status = nt_query(_SYSTEM_PROCESS_INFORMATION_CLASS, buf, size, ctypes.byref(needed))
        if status == 0:
            break
        if status != _STATUS_INFO_LENGTH_MISMATCH:
            raise ProcessTableUnavailable(f"NtQuerySystemInformation failed: 0x{status:08x}")
        size = max(size * 2, needed.value + (1 << 16))
    else:
        raise ProcessTableUnavailable("process table kept growing")

    out: dict[int, RawProcess] = {}
    base = ctypes.addressof(buf)
    offset = 0
    while True:
        info = _SystemProcessInformation.from_address(base + offset)
        pid = int(info.UniqueProcessId or 0)
        name_len = info.ImageName.Length // 2
        name = ctypes.wstring_at(info.ImageName.Buffer, name_len) if info.ImageName.Buffer and name_len else ""
        if pid == 0:
            name = "Idle"
        elif pid == 4 and not name:
            name = "System"
        out[pid] = RawProcess(
            pid=pid, name=name or f"pid {pid}", cpu_100ns=info.UserTime + info.KernelTime, working_set=int(info.WorkingSetSize)
        )
        if info.NextEntryOffset == 0:
            break
        offset += info.NextEntryOffset
    return out


# ---------------------------------------------------------------------------------------------------
# Sampler thread
# ---------------------------------------------------------------------------------------------------


class GpuProcessSource(Protocol):
    def start(self) -> None: ...
    def read(self) -> dict[int, float]: ...
    def close(self) -> None: ...


class ProcessSampler:
    """Background sampler: ``latest()`` is a cheap, thread-safe read of the most recent result."""

    def __init__(
        self,
        interval_s: float = 3.0,
        *,
        gpu_source: GpuProcessSource | None = None,
        table_reader: Callable[[], dict[int, RawProcess]] = read_process_table,
        ncpu: int | None = None,
    ) -> None:
        self._interval_s = interval_s
        self._gpu = gpu_source
        self._read_table = table_reader
        self._ncpu = ncpu or os.cpu_count() or 1
        self._lock = threading.Lock()
        self._latest: list[ProcessInfo] = []
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="process-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def latest(self) -> list[ProcessInfo]:
        with self._lock:
            return list(self._latest)

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def _run(self) -> None:
        gpu = self._gpu
        if gpu is not None:
            try:
                gpu.start()
            except Exception as exc:  # noqa: BLE001 - GPU attribution is optional; CPU attribution still works
                log.info("per-process GPU utilisation disabled: %s", exc)
                gpu = None
        try:
            self._loop(gpu)
        finally:
            if gpu is not None:
                try:
                    gpu.close()
                except Exception as exc:  # noqa: BLE001
                    log.debug("GPU process source close failed: %s", exc)

    def _loop(self, gpu: GpuProcessSource | None) -> None:
        prev: dict[int, RawProcess] = {}
        prev_t = time.monotonic()
        try:
            prev = self._read_table()
        except Exception as exc:  # noqa: BLE001
            self._set_error(f"{type(exc).__name__}: {exc}")
        while not self._stop.wait(self._interval_s):
            try:
                cur = self._read_table()
                now = time.monotonic()
                gpu_by_pid: dict[int, float] = {}
                if gpu is not None:
                    try:
                        gpu_by_pid = gpu.read()
                    except Exception as exc:  # noqa: BLE001
                        log.debug("GPU process read failed: %s", exc)
                result = compute_processes(prev, cur, now - prev_t, self._ncpu, gpu_by_pid)
                prev, prev_t = cur, now
                with self._lock:
                    self._latest, self._error = result, None
            except Exception as exc:  # noqa: BLE001 - never let the thread die
                self._set_error(f"{type(exc).__name__}: {exc}")

    def _set_error(self, message: str) -> None:
        with self._lock:
            if message != self._error:
                log.warning("process sampler: %s", message)
            self._error = message
