"""Rolling window of lightweight samples plus the small statistics the rules need."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

from app.collectors.throttle import active_core_clock
from app.models import ProcessInfo, Snapshot, ThrottleComponent

Getter = Callable[["Sample"], float | None]

_MIN_SLOPE_POINTS: Final = 5


@dataclass(frozen=True)
class Sample:
    """The subset of a Snapshot that the rules look at, flattened for cheap scanning."""

    t: float
    cpu_temp: float | None
    cpu_load: float | None
    cpu_power: float | None
    cpu_clock: float | None  # mean clock of the busy cores
    cpu_avg_clock: float | None  # mean over all cores (meaningful at idle)
    cpu_max_clock: float | None
    cpu_pl1: float | None
    cpu_pl2: float | None
    tjmax: float | None
    gpu_temp: float | None
    gpu_hotspot: float | None
    gpu_util: float | None
    gpu_power: float | None
    gpu_power_limit: float | None
    gpu_clock: float | None
    gpu_slowdown: float | None
    cpu_fan_rpm: float | None
    gpu_fan_rpm: float | None
    gpu_fan_pct: float | None
    has_gpu: bool
    has_battery: bool
    on_ac: bool | None
    power_plan: str | None
    cpu_throttle: ThrottleComponent
    gpu_throttle: ThrottleComponent | None
    processes: tuple[ProcessInfo, ...]

    @classmethod
    def from_snapshot(cls, s: Snapshot) -> Sample:
        cpu, gpu, battery = s.cpu, s.gpu, s.system.battery
        cpu_fan = next((f.rpm for f in s.fans if f.name == "CPU Fan"), None)
        if cpu_fan is None:
            cpu_fan = next((f.rpm for f in s.fans if "gpu" not in f.name.lower()), None)
        gpu_fan = gpu.fan_rpm if gpu is not None and gpu.fan_rpm is not None else next(
            (f.rpm for f in s.fans if f.name == "GPU Fan"), None
        )
        return cls(
            t=s.timestamp,
            cpu_temp=cpu.package_temp_c if cpu.package_temp_c is not None else cpu.max_core_temp_c,
            cpu_load=cpu.total_load_pct,
            cpu_power=cpu.package_power_w,
            cpu_clock=active_core_clock(cpu),
            cpu_avg_clock=cpu.avg_clock_mhz,
            cpu_max_clock=cpu.max_clock_mhz,
            cpu_pl1=cpu.pl1_w,
            cpu_pl2=cpu.pl2_w,
            tjmax=cpu.tjmax_c,
            gpu_temp=gpu.temp_c if gpu else None,
            gpu_hotspot=gpu.temp_hotspot_c if gpu else None,
            gpu_util=gpu.util_gpu_pct if gpu else None,
            gpu_power=gpu.power_draw_w if gpu else None,
            gpu_power_limit=gpu.power_limit_w if gpu else None,
            gpu_clock=gpu.core_clock_mhz if gpu else None,
            gpu_slowdown=gpu.temp_slowdown_c if gpu else None,
            cpu_fan_rpm=cpu_fan,
            gpu_fan_rpm=gpu_fan,
            gpu_fan_pct=gpu.fan_percent if gpu else None,
            has_gpu=gpu is not None,
            has_battery=battery is not None,
            on_ac=battery.plugged_in if battery is not None else None,
            power_plan=s.system.power_plan.kind if s.system.power_plan else None,
            cpu_throttle=s.throttle.cpu,
            gpu_throttle=s.throttle.gpu,
            processes=tuple(s.processes),
        )


class Window:
    """Samples from the last ``span_s`` seconds, oldest first."""

    def __init__(self, span_s: float) -> None:
        self.span_s = span_s
        self._samples: deque[Sample] = deque()

    def add(self, sample: Sample) -> None:
        self._samples.append(sample)
        cutoff = sample.t - self.span_s
        while self._samples and self._samples[0].t < cutoff:
            self._samples.popleft()

    def __len__(self) -> int:
        return len(self._samples)

    def all(self) -> list[Sample]:
        return list(self._samples)

    def last(self, seconds: float) -> list[Sample]:
        if not self._samples:
            return []
        cutoff = self._samples[-1].t - seconds
        return [s for s in self._samples if s.t >= cutoff]

    @property
    def duration_s(self) -> float:
        return self._samples[-1].t - self._samples[0].t if len(self._samples) > 1 else 0.0


# ---- statistics -------------------------------------------------------------------------------


def values(samples: Iterable[Sample], get: Getter) -> list[float]:
    return [v for v in (get(s) for s in samples) if v is not None]


def mean(samples: Iterable[Sample], get: Getter) -> float | None:
    v = values(samples, get)
    return sum(v) / len(v) if v else None


def peak(samples: Iterable[Sample], get: Getter) -> float | None:
    v = values(samples, get)
    return max(v) if v else None


def slope_per_s(samples: list[Sample], get: Getter) -> float | None:
    """Least-squares slope in units/second; None with too few points or no time spread."""
    pts = [(s.t, v) for s in samples if (v := get(s)) is not None]
    if len(pts) < _MIN_SLOPE_POINTS:
        return None
    t0 = pts[0][0]
    xs = [t - t0 for t, _ in pts]
    ys = [v for _, v in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def fraction(samples: list[Sample], pred: Callable[[Sample], bool]) -> float:
    return sum(1 for s in samples if pred(s)) / len(samples) if samples else 0.0


# ---- process attribution ------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessShare:
    name: str
    cpu_pct: float  # mean share of the whole CPU over the samples that carried process data
    gpu_pct: float | None
    count: int  # max number of instances seen


def process_shares(samples: Iterable[Sample], ignore: frozenset[str] = frozenset()) -> list[ProcessShare]:
    """Mean per-process usage over the window. A process absent from a sample's top list counts as 0."""
    carrying = [s for s in samples if s.processes]
    if not carrying:
        return []
    cpu: dict[str, float] = defaultdict(float)
    gpu: dict[str, float] = defaultdict(float)
    has_gpu: set[str] = set()
    count: dict[str, int] = defaultdict(int)
    for s in carrying:
        for p in s.processes:
            if p.name.lower() in ignore:
                continue
            cpu[p.name] += p.cpu_pct
            if p.gpu_pct is not None:
                gpu[p.name] += p.gpu_pct
                has_gpu.add(p.name)
            count[p.name] = max(count[p.name], p.count)
    n = len(carrying)
    return sorted(
        (
            ProcessShare(name, cpu[name] / n, gpu[name] / n if name in has_gpu else None, count[name])
            for name in cpu
        ),
        key=lambda p: p.cpu_pct,
        reverse=True,
    )
