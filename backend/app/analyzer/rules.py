"""The seven root-cause rules.

Each rule is a pure function ``Context -> list[Finding]``:
* ``[]``                        evaluated, nothing wrong found;
* ``[Finding, ...]``            one or more problems, each carrying its raw numbers as evidence;
* ``raise NotEvaluable(why)``   the inputs this rule needs are missing (shown to the user as "not evaluated",
                                because silence would be indistinguishable from "all clear").

Wording rule: only hardware flags ("detected") are stated as fact. Everything derived from clocks,
temperatures, power or process shares is "inferred" and phrased as likely/suspected, with confidence < 1.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.analyzer.config import Thresholds
from app.analyzer.fans import FanKnowledge
from app.analyzer.models import Certainty, Component, Severity
from app.models import ThrottleComponent
from app.analyzer.window import (
    Sample,
    Window,
    fraction,
    mean,
    peak,
    process_shares,
    slope_per_s,
    values,
)


class NotEvaluable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Finding:
    rule_id: str
    component: Component
    diagnosis: str
    severity: Severity
    confidence: float
    certainty: Certainty
    evidence: list[str]
    suggested_fix: str
    metrics: dict[str, float | str | None] = field(default_factory=dict)
    immediate: bool = False  # event-style findings skip the confirmation delay

    @property
    def key(self) -> str:
        return f"{self.rule_id}:{self.component}"


@dataclass
class Context:
    now: float
    window: Window
    th: Thresholds
    fans: FanKnowledge

    @property
    def latest(self) -> Sample:
        return self.window.all()[-1]

    def recent(self, seconds: float | None = None) -> list[Sample]:
        return self.window.last(seconds if seconds is not None else self.th.recent_s)


# ---- tiny accessors (module-level so they can be passed as getters) ---------------------------------

def _cpu_temp(s: Sample) -> float | None: return s.cpu_temp
def _cpu_load(s: Sample) -> float | None: return s.cpu_load
def _cpu_power(s: Sample) -> float | None: return s.cpu_power
def _cpu_clock(s: Sample) -> float | None: return s.cpu_clock
def _cpu_avg_clock(s: Sample) -> float | None: return s.cpu_avg_clock
def _gpu_temp(s: Sample) -> float | None: return s.gpu_temp
def _gpu_util(s: Sample) -> float | None: return s.gpu_util
def _gpu_power(s: Sample) -> float | None: return s.gpu_power
def _gpu_clock(s: Sample) -> float | None: return s.gpu_clock
def _cpu_fan(s: Sample) -> float | None: return s.cpu_fan_rpm
def _gpu_fan(s: Sample) -> float | None: return s.gpu_fan_rpm


def _f0(v: float | None, unit: str = "") -> str:
    return "n/a" if v is None else f"{v:.0f}{unit}"


def _f1(v: float | None, unit: str = "") -> str:
    return "n/a" if v is None else f"{v:.1f}{unit}"


def _tjmax(ctx: Context) -> float:
    for s in reversed(ctx.window.all()):
        if s.tjmax:
            return s.tjmax
    return ctx.th.default_tjmax_c


def _gpu_limit_temp(ctx: Context) -> float:
    for s in reversed(ctx.window.all()):
        if s.gpu_slowdown:
            return s.gpu_slowdown
    return ctx.th.default_gpu_slowdown_c


def _is_power_reason(reason: str) -> bool:
    r = reason.lower()
    return any(k in r for k in ("power limit", "power cap", "power-brake", "power brake", "iccmax", "design point"))


def _is_thermal_reason(reason: str) -> bool:
    r = reason.lower()
    return any(k in r for k in ("thermal", "tcc", "prochot", "critical temperature"))


# Windows/background processes whose heat is "maintenance", with what to do about it.
_KNOWN_BACKGROUND: dict[str, str] = {
    "msmpeng.exe": "Windows Defender is scanning. It normally finishes on its own; schedule scans for off-hours.",
    "searchindexer.exe": "Windows Search is indexing. It usually settles within minutes.",
    "tiworker.exe": "Windows Update is installing. Let it finish.",
    "trustedinstaller.exe": "Windows Update is installing. Let it finish.",
    "wmiprvse.exe": "A monitoring/management tool is polling WMI heavily. Close the tool that is polling.",
    "onedrive.exe": "OneDrive is syncing. It will quiet down after the sync completes.",
    "compattelrunner.exe": "Windows telemetry/compatibility scan. It stops by itself.",
}


# ====================================================================================================
# Rule 1 - high temperature while the machine is (nearly) idle
# ====================================================================================================


def rule_idle_hot(ctx: Context) -> list[Finding]:
    th, r = ctx.th, ctx.recent()
    cpu_t, cpu_load = mean(r, _cpu_temp), mean(r, _cpu_load)
    gpu_t, gpu_util = mean(r, _gpu_temp), mean(r, _gpu_util)
    if cpu_t is None and gpu_t is None:
        raise NotEvaluable("no CPU or GPU temperature available (CPU sensors need an elevated run with the PawnIO driver)")
    if cpu_load is None:
        raise NotEvaluable("CPU load is unavailable")

    cpu_hot = cpu_t is not None and cpu_t >= th.cpu_idle_hot_c
    gpu_hot = gpu_t is not None and gpu_t >= th.gpu_idle_hot_c
    quiet = cpu_load < th.low_load_cpu_pct and (gpu_util is None or gpu_util < th.low_load_gpu_pct)
    if not quiet or not (cpu_hot or gpu_hot):
        return []  # busy machines are the business of rules 2, 5 and 6

    power = mean(r, _cpu_power)
    shares = process_shares(r, th.ignore_process_names)
    top = next((p for p in shares if p.cpu_pct >= th.bg_process_min_pct), None)
    gpu_user = max((p for p in shares if (p.gpu_pct or 0) >= 5), key=lambda p: p.gpu_pct or 0, default=None)
    plan = ctx.latest.power_plan
    fan_rpm, fan_name = mean(r, _cpu_fan), "CPU Fan"
    fan_ratio = ctx.fans.ratio(fan_name, fan_rpm) if fan_rpm is not None else None
    fan_known = ctx.fans.trusted(fan_name, th.fan_learn_min_span)

    evidence = [
        f"CPU package {_f0(cpu_t, ' °C')} with CPU load only {_f0(cpu_load, '%')} (last {int(th.recent_s)} s mean)",
    ]
    if gpu_t is not None:
        evidence.append(f"GPU {_f0(gpu_t, ' °C')} at {_f0(gpu_util, '% utilisation')}")
    if power is not None:
        evidence.append(f"CPU package power {_f1(power, ' W')} - low draw for this temperature")
    avg_clock = mean(r, _cpu_avg_clock)
    if avg_clock is not None:
        evidence.append(f"Average core clock {_f0(avg_clock, ' MHz')}")

    causes: list[str] = []
    fixes: list[str] = []
    confidence = 0.4

    if top is not None:
        causes.append("process")
        evidence.append(
            f"Top background process: {top.name} averaging {top.cpu_pct:.1f}% of the whole CPU"
            + (f" across {top.count} instances" if top.count > 1 else "")
        )
        confidence = 0.7 if top.cpu_pct >= 6 else 0.55
        hint = _KNOWN_BACKGROUND.get(top.name.lower())
        fixes.append(hint or f"Check what {top.name} is doing in Task Manager; close or restart it if it is not needed.")
    if gpu_user is not None and gpu_user.gpu_pct:
        causes.append("gpu_process")
        evidence.append(f"{gpu_user.name} is using {gpu_user.gpu_pct:.0f}% of the GPU")
        fixes.append(f"Close {gpu_user.name}, or stop it from using the discrete GPU (Windows Graphics settings).")
        confidence = max(confidence, 0.55)
    if plan in ("high_performance", "ultimate"):
        causes.append("power_plan")
        evidence.append(f"Active Windows power plan is '{plan}', which keeps clocks and voltage high even when idle")
        fixes.append("Switch to the Balanced power plan (or the vendor's quiet profile) while idle.")
        confidence = max(confidence, 0.5)

    if not causes:
        # Nothing explains the heat: it is not coming from software load.
        causes.append("cooling")
        low_power = power is not None and power <= 20.0
        if low_power:
            evidence.append("No process explains the heat and the CPU draws little power, so the heat is not coming from load")
            confidence = 0.6
        else:
            evidence.append("No process accounts for the heat")
            confidence = 0.4
        if fan_rpm is not None:
            position = ctx.fans.position(fan_name, fan_rpm) if fan_known else None
            if position is not None and position >= 0.6:
                evidence.append(
                    f"{fan_name} is already at {_f0(fan_rpm, ' RPM')} ({(fan_ratio or 0) * 100:.0f}% of the highest speed seen) yet the system stays hot"
                )
                confidence += 0.05
            elif position is not None and position <= 0.15:
                evidence.append(
                    f"{fan_name} is near its slowest speed ({_f0(fan_rpm, ' RPM')}; slowest seen {_f0(ctx.fans.min_rpm(fan_name), ' RPM')}) "
                    "although the system is hot"
                )
                fixes.append("The fan profile may be set to Silent/Quiet; try a Balanced or Performance fan profile.")
            else:
                evidence.append(f"{fan_name} at {_f0(fan_rpm, ' RPM')}")
        fixes.append("Check for blocked vents, dust, or a hot environment; use the laptop on a hard, flat surface.")
        fixes.append("If it stays hot at idle in a cool room, the heatsink contact/thermal paste may be degraded.")

    hotter_cpu = cpu_hot and (gpu_t is None or not gpu_hot or (cpu_t or 0) >= (gpu_t or 0))
    what = f"CPU is {_f0(cpu_t, ' °C')}" if hotter_cpu else f"GPU is {_f0(gpu_t, ' °C')}"
    if "process" in causes:
        assert top is not None
        headline = f"{what} at only {_f0(cpu_load, '%')} load - likely {top.name} running in the background"
    elif "gpu_process" in causes and gpu_user is not None:
        headline = f"{what} while mostly idle - likely {gpu_user.name} using the GPU"
    elif "power_plan" in causes:
        headline = f"{what} while idle - the high-performance power plan is a likely contributor"
    else:
        headline = f"{what} while nearly idle - the heat is not explained by load, which points to airflow/cooling"

    worst = max(x for x in (cpu_t, gpu_t) if x is not None)
    severity: Severity = "warning" if worst >= th.cpu_idle_hot_c + 10 else "info"
    return [
        Finding(
            rule_id="idle_hot",
            component="system",
            diagnosis=headline,
            severity=severity,
            confidence=round(min(confidence, 0.9), 2),
            certainty="inferred",
            evidence=evidence,
            suggested_fix=" ".join(dict.fromkeys(fixes)),
            metrics={
                "cpu_temp_c": round(cpu_t, 1) if cpu_t is not None else None,
                "cpu_load_pct": round(cpu_load, 1),
                "cpu_power_w": round(power, 1) if power is not None else None,
                "gpu_temp_c": round(gpu_t, 1) if gpu_t is not None else None,
                "top_process": top.name if top else None,
                "power_plan": plan,
            },
        )
    ]


# ====================================================================================================
# Rule 2 - hot, and one process dominates the CPU / GPU
# ====================================================================================================


def rule_dominant_process(ctx: Context) -> list[Finding]:
    th, r = ctx.th, ctx.recent()
    cpu_t, load = mean(r, _cpu_temp), mean(r, _cpu_load)
    gpu_t, gpu_u = mean(r, _gpu_temp), mean(r, _gpu_util)
    if cpu_t is None and gpu_t is None:
        raise NotEvaluable("no CPU or GPU temperature available")
    shares = process_shares(r, th.ignore_process_names)

    cpu_case = cpu_t is not None and load is not None and cpu_t >= th.cpu_hot_c and load >= th.low_load_cpu_pct
    gpu_case = gpu_t is not None and gpu_u is not None and gpu_t >= th.gpu_hot_c and gpu_u >= 50.0
    if (cpu_case or gpu_case) and not shares:
        raise NotEvaluable("per-process usage data is not available yet")

    findings: list[Finding] = []
    tjmax = _tjmax(ctx)

    if cpu_case and shares:
        assert cpu_t is not None and load is not None
        total = sum(p.cpu_pct for p in shares)
        top = shares[0]
        if total > 0 and top.cpu_pct >= th.dominant_min_pct and top.cpu_pct / total >= th.dominant_share:
            others = ", ".join(f"{p.name} {p.cpu_pct:.1f}%" for p in shares[1:4])
            hint = _KNOWN_BACKGROUND.get(top.name.lower())
            findings.append(
                Finding(
                    rule_id="dominant_process",
                    component="cpu",
                    diagnosis=f"{top.name} is the main source of CPU heat ({top.cpu_pct:.0f}% of the CPU) at {cpu_t:.0f} °C",
                    severity="critical" if cpu_t >= tjmax - th.near_limit_margin_c else "warning",
                    confidence=0.75,
                    certainty="inferred",
                    evidence=[
                        f"CPU package {cpu_t:.0f} °C, load {load:.0f}% (last {int(th.recent_s)} s mean)",
                        f"{top.name}: {top.cpu_pct:.1f}% of the whole CPU"
                        + (f" over {top.count} instances" if top.count > 1 else "")
                        + f" = {top.cpu_pct / total * 100:.0f}% of all attributed CPU use",
                        f"Next: {others}" if others else "No other process is significant",
                        f"Package power {_f1(mean(r, _cpu_power), ' W')}, active clock {_f0(mean(r, _cpu_clock), ' MHz')}",
                    ],
                    suggested_fix=hint
                    or (
                        f"If {top.name} is doing work you did not ask for, close or restart it. If it is intentional "
                        "(game, render, build), cap its frame rate/thread count or switch to a more aggressive fan profile."
                    ),
                    metrics={
                        "process": top.name,
                        "process_cpu_pct": round(top.cpu_pct, 1),
                        "share_of_attributed_cpu": round(top.cpu_pct / total, 2),
                        "cpu_temp_c": round(cpu_t, 1),
                    },
                )
            )

    if gpu_case and gpu_t is not None:
        gpu_users = [p for p in shares if p.gpu_pct is not None]
        if not gpu_users:
            if not findings:
                raise NotEvaluable("the GPU is hot but per-process GPU usage is not reported by the driver")
        else:
            total_g = sum(p.gpu_pct or 0 for p in gpu_users)
            top_g = max(gpu_users, key=lambda p: p.gpu_pct or 0)
            share = (top_g.gpu_pct or 0) / total_g if total_g else 0.0
            if (top_g.gpu_pct or 0) >= th.gpu_dominant_min_pct and share >= th.gpu_dominant_share:
                findings.append(
                    Finding(
                        rule_id="dominant_process",
                        component="gpu",
                        diagnosis=f"{top_g.name} is driving the GPU to {gpu_t:.0f} °C ({top_g.gpu_pct:.0f}% GPU use)",
                        severity="critical" if gpu_t >= _gpu_limit_temp(ctx) - th.near_limit_margin_c else "warning",
                        confidence=0.75,
                        certainty="inferred",
                        evidence=[
                            f"GPU {gpu_t:.0f} °C at {gpu_u:.0f}% utilisation, {_f0(mean(r, _gpu_power), ' W')} draw",
                            f"{top_g.name}: {top_g.gpu_pct:.0f}% of GPU engine time = {share * 100:.0f}% of attributed GPU use",
                        ],
                        suggested_fix=(
                            f"If {top_g.name} is a game or render, cap its frame rate (or enable V-Sync) and lower the "
                            "graphics quality; otherwise close it."
                        ),
                        metrics={"process": top_g.name, "process_gpu_pct": round(top_g.gpu_pct or 0, 1), "gpu_temp_c": round(gpu_t, 1)},
                    )
                )
    return findings


# ====================================================================================================
# Rule 3 - fast temperature spike when load starts, reaching 90 C+
# ====================================================================================================


@dataclass(frozen=True)
class _LoadStart:
    t: float  # when the load began rising
    base_temp: float | None


def _latest_load_start(samples: list[Sample], th: Thresholds) -> _LoadStart | None:
    """Last point where the CPU went from idle (<30% for a few seconds) to busy (>=60%) within ~8 s."""
    idle_run = 0
    idle_end: Sample | None = None
    found: _LoadStart | None = None
    for i, s in enumerate(samples):
        if s.cpu_load is None:
            idle_run, idle_end = 0, None
            continue
        if s.cpu_load < th.idle_load_pct:
            idle_run += 1
            if idle_run >= th.idle_run_samples:
                idle_end = s
            continue
        idle_run = 0
        if s.cpu_load >= th.high_load_pct and idle_end is not None and s.t - idle_end.t <= 8.0:
            base = [x.cpu_temp for x in samples[max(0, i - 12) : i] if x.cpu_temp is not None and x.t <= idle_end.t]
            found = _LoadStart(idle_end.t, sum(base[-5:]) / len(base[-5:]) if base else None)
            idle_end = None
    return found


def rule_spike_at_load_start(ctx: Context) -> list[Finding]:
    th = ctx.th
    samples = ctx.window.all()
    if not any(s.cpu_temp is not None for s in samples):
        raise NotEvaluable("no CPU temperature available")
    if not any(s.cpu_load is not None for s in samples):
        raise NotEvaluable("CPU load is unavailable")
    start = _latest_load_start(samples, th)
    if start is None or start.base_temp is None:
        return []

    after = [s for s in samples if start.t < s.t <= start.t + th.spike_within_s]
    first10 = [s for s in after if s.t <= start.t + 10.0]
    if len(first10) < 5 or (mean(first10, _cpu_load) or 0) < 50.0:
        return []  # not enough data yet, or the "load" was only a blip
    temps = [(s.t, s.cpu_temp) for s in after if s.cpu_temp is not None]
    if not temps:
        return []
    max_temp = max(t for _, t in temps)
    rise = max_temp - start.base_temp
    t_threshold = next((t - start.t for t, v in temps if v >= th.spike_temp_c), None)
    temp10 = [v for t, v in temps if t <= start.t + 10.0]
    rate = (temp10[-1] - start.base_temp) / max(1.0, first10[-1].t - start.t) if temp10 else 0.0
    if t_threshold is None or rise < th.spike_min_rise_c or rate < th.spike_min_rate_c_per_s:
        return []

    power_first10 = mean(first10, _cpu_power)
    peak_power = peak(after, _cpu_power)
    confidence = 0.55
    notes: list[str] = []
    if power_first10 is None:
        confidence -= 0.1
        notes.append("Package power is unavailable, so a normal boost-power surge cannot be ruled out")
    elif power_first10 >= th.boost_power_w:
        confidence = 0.4
        notes.append(
            f"The CPU drew {power_first10:.0f} W at the start, so a fast rise is partly expected from boost behaviour alone"
        )
    elif power_first10 < 60.0:
        confidence = 0.7
        notes.append(f"Only {power_first10:.0f} W was drawn, which should not heat the CPU this quickly with good cooling")
    fan_after = mean([s for s in after if s.t >= start.t + 8], _cpu_fan)

    evidence = [
        f"Load rose from idle to {mean(first10, _cpu_load):.0f}% at the start of the burst",
        f"CPU package went {start.base_temp:.0f} °C -> {max_temp:.0f} °C (+{rise:.0f} °C), reaching {th.spike_temp_c:.0f} °C after {t_threshold:.0f} s",
        f"Average rise over the first 10 s: {rate:.1f} °C/s",
        f"Package power in the first 10 s: {_f1(power_first10, ' W')} (peak {_f1(peak_power, ' W')})",
        f"CPU fan {_f0(fan_after, ' RPM')} shortly after the load started",
        *notes,
    ]
    return [
        Finding(
            rule_id="load_spike",
            component="cpu",
            diagnosis=(
                f"CPU shot from {start.base_temp:.0f} °C to {max_temp:.0f} °C within {t_threshold:.0f} s of load starting - "
                "possible dried thermal paste or poor cooler contact"
            ),
            severity="critical" if max_temp >= _tjmax(ctx) - 2 else "warning",
            confidence=round(max(0.2, confidence), 2),
            certainty="inferred",
            evidence=evidence,
            suggested_fix=(
                "Repeat a steady all-core load and compare: a healthy cooler settles after the first ~30 s, a poorly "
                "seated one keeps climbing. If it keeps happening, re-apply thermal paste and check the heatsink screws. "
                "Undervolting or lowering the boost power limit can also reduce the spike."
            ),
            metrics={
                "base_temp_c": round(start.base_temp, 1),
                "max_temp_c": round(max_temp, 1),
                "seconds_to_threshold": round(t_threshold, 1),
                "rate_c_per_s": round(rate, 2),
                "power_first_10s_w": round(power_first10, 1) if power_first10 is not None else None,
            },
            immediate=True,
        )
    ]


# ====================================================================================================
# Rule 4 - fan at ~100 % and the machine is still hot
# ====================================================================================================


def rule_fan_maxed_still_hot(ctx: Context) -> list[Finding]:
    th, r = ctx.th, ctx.recent()
    pairs: tuple[tuple[str, Callable[[Sample], float | None], Callable[[Sample], float | None], float, Component], ...] = (
        ("CPU Fan", _cpu_fan, _cpu_temp, th.cpu_hot_c, "cpu"),
        ("GPU Fan", _gpu_fan, _gpu_temp, th.gpu_hot_c, "gpu"),
    )
    have_data = False
    untrusted: list[str] = []
    findings: list[Finding] = []

    for name, fan_get, temp_get, hot_c, comp in pairs:
        rpms = values(r, fan_get)
        if not rpms:
            continue
        have_data = True
        mx = ctx.fans.max_rpm(name)
        if mx is None or not ctx.fans.trusted(name, th.fan_learn_min_span):
            untrusted.append(name)
            continue
        full = fraction(r, lambda s, g=fan_get, m=mx: g(s) is not None and (g(s) or 0) / m >= th.fan_full_ratio)
        temp = mean(r, temp_get)
        slope = slope_per_s(r, temp_get)
        recovering = slope is not None and slope <= th.cooling_slope_c_per_s
        if full >= th.fan_full_fraction and temp is not None and temp >= hot_c and not recovering:
            rpm_mean = sum(rpms) / len(rpms)
            near = temp >= (_tjmax(ctx) if comp == "cpu" else _gpu_limit_temp(ctx)) - th.near_limit_margin_c
            findings.append(
                Finding(
                    rule_id="fan_maxed",
                    component=comp,
                    diagnosis=f"{name} is at (about) full speed and the {comp.upper()} is still {temp:.0f} °C - cooling capacity looks insufficient",
                    severity="critical" if near else "warning",
                    confidence=0.65,
                    certainty="inferred",
                    evidence=[
                        f"{name} averaged {rpm_mean:.0f} RPM = {rpm_mean / mx * 100:.0f}% of the highest speed ever seen on this machine ({mx:.0f} RPM)",
                        f"At or above {th.fan_full_ratio * 100:.0f}% of that for {full * 100:.0f}% of the last {int(th.recent_s)} s",
                        f"{comp.upper()} temperature {temp:.0f} °C and "
                        + ("not falling" if slope is None else f"changing {slope * 60:+.1f} °C/min"),
                        "Fan duty is estimated from RPM (the laptop does not report it), so this is an inference",
                    ],
                    suggested_fix=(
                        "The fans cannot remove the heat being produced. Clean the intake and exhaust vents (compressed air), "
                        "make sure nothing blocks them, and try a cooling pad. If a fresh clean does not help, the thermal paste may "
                        "have degraded or a fan may be failing (noisy, or RPM lower than it used to reach)."
                    ),
                    metrics={"fan": name, "rpm_mean": round(rpm_mean), "rpm_max_seen": round(mx), "temp_c": round(temp, 1)},
                )
            )

    # Direct duty cycle (e.g. a desktop NVIDIA card): no estimation needed.
    pct = mean(r, lambda s: s.gpu_fan_pct)
    gpu_t = mean(r, _gpu_temp)
    if pct is not None:
        have_data = True
        if pct >= 95.0 and gpu_t is not None and gpu_t >= th.gpu_hot_c:
            findings.append(
                Finding(
                    rule_id="fan_maxed",
                    component="gpu",
                    diagnosis=f"GPU fan is at {pct:.0f}% and the GPU is still {gpu_t:.0f} °C - cooling capacity looks insufficient",
                    severity="warning",
                    confidence=0.85,
                    certainty="detected",
                    evidence=[f"GPU fan duty {pct:.0f}% (reported by the driver)", f"GPU temperature {gpu_t:.0f} °C"],
                    suggested_fix="Clean the GPU heatsink and fans, improve case airflow, and re-paste the GPU if it is old.",
                    metrics={"fan_percent": round(pct, 1), "gpu_temp_c": round(gpu_t, 1)},
                )
            )

    if not have_data:
        raise NotEvaluable("no fan speed is readable on this device (laptop fans need the vendor interface, elevated)")
    if untrusted and not findings and len(untrusted) == sum(1 for p in pairs if values(r, p[1])):
        raise NotEvaluable(
            f"{', '.join(untrusted)} range not learned yet - needs to be seen at both low and high speed before 'full speed' means anything"
        )
    return findings


# ====================================================================================================
# Rule 5 - clocks dropped while near the temperature limit => thermal throttling
# ====================================================================================================


def _detected_active(
    samples: list[Sample], get: Callable[[Sample], ThrottleComponent | None], types: tuple[str, ...]
) -> list[Sample]:
    """Samples where `get(sample)` is an *active, hardware-detected* throttle of one of `types`."""
    out: list[Sample] = []
    for s in samples:
        c = get(s)
        if c is not None and c.active and c.confidence == "detected" and c.type in types:
            out.append(s)
    return out


def rule_thermal_throttling(ctx: Context) -> list[Finding]:
    th = ctx.th
    r10 = ctx.window.last(10)
    findings: list[Finding] = []
    evaluated = False

    # ---- CPU
    cpu_det = _detected_active(r10, lambda s: s.cpu_throttle, ("thermal", "prochot"))
    cpu_state = ctx.latest.cpu_throttle
    cpu_t = mean(ctx.recent(), _cpu_temp)
    tjmax = _tjmax(ctx)
    if cpu_state.confidence == "detected":
        evaluated = True
    if cpu_det and len(cpu_det) >= max(1, len(r10) // 2):
        reasons = sorted({x for s in cpu_det for x in s.cpu_throttle.reasons})
        prochot = all(s.cpu_throttle.type == "prochot" for s in cpu_det)
        clock_now, clock_peak = mean(r10, _cpu_clock), peak(ctx.window.all(), _cpu_clock)
        evidence = [
            f"Hardware flag(s) set: {', '.join(reasons)} (source: {cpu_det[-1].cpu_throttle.source})",
            f"CPU package {_f0(cpu_t, ' °C')} against a limit of about {tjmax:.0f} °C",
            f"Busy-core clock {_f0(clock_now, ' MHz')} versus {_f0(clock_peak, ' MHz')} at its best in this window",
        ]
        if prochot and cpu_t is not None and cpu_t < tjmax - 15:
            evidence.append(
                f"PROCHOT# is asserted but the CPU is only {cpu_t:.0f} °C, so the trigger is probably not CPU heat "
                "(other causes: charger/battery signal, VRM temperature, or the embedded controller)"
            )
        findings.append(
            Finding(
                rule_id="thermal_throttle",
                component="cpu",
                diagnosis=(
                    "CPU is being throttled by PROCHOT#" if prochot else "CPU is thermally throttling"
                ),
                severity="critical",
                confidence=0.95,
                certainty="detected",
                evidence=evidence,
                suggested_fix=(
                    "Reduce heat: close heavy applications, clean the vents/fans, use a cooling pad and a more aggressive fan "
                    "profile. If it persists at modest temperatures, check the charger and battery, then the thermal paste."
                ),
                metrics={"cpu_temp_c": round(cpu_t, 1) if cpu_t is not None else None, "flags": "; ".join(reasons)},
            )
        )
    elif cpu_state.confidence != "detected":
        # No hardware flag available: infer from clock drop + temperature at the limit.
        samples = ctx.window.all()
        peak_clock, now_clock = peak(samples, _cpu_clock), mean(r10, _cpu_clock)
        load = mean(ctx.recent(), _cpu_load)
        if peak_clock is None or now_clock is None or cpu_t is None or load is None:
            pass
        else:
            evaluated = True
            drop = 1 - now_clock / peak_clock if peak_clock else 0.0
            if load >= th.high_load_pct and drop >= th.clock_drop_fraction and cpu_t >= tjmax - th.near_limit_margin_c:
                findings.append(
                    Finding(
                        rule_id="thermal_throttle",
                        component="cpu",
                        diagnosis=f"CPU is likely thermally throttling (clocks down {drop * 100:.0f}% at {cpu_t:.0f} °C)",
                        severity="warning",
                        confidence=0.6,
                        certainty="inferred",
                        evidence=[
                            f"Busy-core clock {now_clock:.0f} MHz versus {peak_clock:.0f} MHz earlier in the window (-{drop * 100:.0f}%)",
                            f"CPU package {cpu_t:.0f} °C, within {th.near_limit_margin_c:.0f} °C of the ~{tjmax:.0f} °C limit, at {load:.0f}% load",
                            "No hardware throttle flag is readable (needs elevation + PawnIO), so this is inferred from the numbers",
                        ],
                        suggested_fix="Reduce heat: close heavy applications, clean the vents/fans, use a cooling pad and a stronger fan profile.",
                        metrics={"clock_now_mhz": round(now_clock), "clock_peak_mhz": round(peak_clock), "cpu_temp_c": round(cpu_t, 1)},
                    )
                )

    # ---- GPU (NVML always provides flags, so only the detected path is needed)
    if ctx.latest.has_gpu:
        gpu_det = _detected_active(r10, lambda s: s.gpu_throttle, ("thermal",))
        if ctx.latest.gpu_throttle is not None and ctx.latest.gpu_throttle.confidence == "detected":
            evaluated = True
        if gpu_det and len(gpu_det) >= max(1, len(r10) // 2):
            g = gpu_det[-1].gpu_throttle
            assert g is not None
            gt = mean(ctx.recent(), _gpu_temp)
            findings.append(
                Finding(
                    rule_id="thermal_throttle",
                    component="gpu",
                    diagnosis="GPU is thermally throttling",
                    severity="critical",
                    confidence=0.95,
                    certainty="detected",
                    evidence=[
                        f"Driver reports: {', '.join(g.reasons)} (mask {g.detail.get('raw_mask')})",
                        f"GPU {_f0(gt, ' °C')} (slowdown starts around {_gpu_limit_temp(ctx):.0f} °C), hot spot {_f0(mean(ctx.recent(), lambda s: s.gpu_hotspot), ' °C')}",
                        f"GPU clock {_f0(mean(r10, _gpu_clock), ' MHz')}",
                    ],
                    suggested_fix="Lower graphics load (frame cap, quality), clean vents/fans and use a cooling pad / stronger fan profile.",
                    metrics={"gpu_temp_c": round(gt, 1) if gt is not None else None, "mask": str(g.detail.get("raw_mask"))},
                )
            )
    if not evaluated:
        raise NotEvaluable("no throttle flag, clock or temperature data available to judge throttling")
    return findings


# ====================================================================================================
# Rule 6 - clocks dropped while temperature is normal => power-limit throttling
# ====================================================================================================


def _power_context(ctx: Context, evidence: list[str], fixes: list[str]) -> tuple[bool, bool]:
    """Append battery / power-plan evidence. Returns (on_battery, power_saver)."""
    s = ctx.latest
    on_battery = s.has_battery and s.on_ac is False
    saver = s.power_plan == "power_saver"
    if on_battery:
        evidence.append("The laptop is running on battery, which caps CPU/GPU power")
        fixes.append("Plug in the charger for full performance.")
    if saver:
        evidence.append("The Windows power plan is 'Power saver'")
        fixes.append("Switch to the Balanced or Performance power plan.")
    return on_battery, saver


def _cpu_power_headline(reasons: list[str], pl1: float | None, pl2: float | None) -> str:
    joined = " ".join(reasons)
    if "PL1" in joined:
        return "CPU is held back by its sustained power limit" + (f" (PL1 = {pl1:.0f} W)" if pl1 else " (PL1)")
    if "PL2" in joined:
        return "CPU is held back by its short-term power limit" + (f" (PL2 = {pl2:.0f} W)" if pl2 else " (PL2)")
    return "CPU is held back by an electrical/power limit (" + ", ".join(reasons) + ")"


def rule_power_limit_throttling(ctx: Context) -> list[Finding]:
    th = ctx.th
    r10 = ctx.window.last(10)
    findings: list[Finding] = []
    evaluated = False

    # ---- CPU
    cpu_state = ctx.latest.cpu_throttle
    if cpu_state.confidence == "detected":
        evaluated = True
        power_hits = [
            s for s in r10 if s.cpu_throttle.active and s.cpu_throttle.confidence == "detected"
            and any(_is_power_reason(x) for x in s.cpu_throttle.reasons)
        ]
        if power_hits and len(power_hits) >= max(1, len(r10) // 2):
            reasons = sorted({x for s in power_hits for x in s.cpu_throttle.reasons if _is_power_reason(x)})
            power, pl1, pl2 = mean(r10, _cpu_power), power_hits[-1].cpu_pl1, power_hits[-1].cpu_pl2
            cpu_t = mean(ctx.recent(), _cpu_temp)
            evidence = [f"Hardware flag(s): {', '.join(reasons)}"]
            evidence.append(
                f"Package power {_f1(power, ' W')} against PL1 {_f1(pl1, ' W')} / PL2 {_f1(pl2, ' W')}"
            )
            evidence.append(
                f"CPU package {_f0(cpu_t, ' °C')} (limit ~{_tjmax(ctx):.0f} °C) - temperature is not the constraint"
                if cpu_t is not None and cpu_t < _tjmax(ctx) - th.near_limit_margin_c
                else f"CPU package {_f0(cpu_t, ' °C')}"
            )
            fixes: list[str] = []
            on_battery, saver = _power_context(ctx, evidence, fixes)
            if not on_battery and not saver:
                fixes.append(
                    "This is the laptop's sustained power budget (PL1) doing its job once boost ends; raising it is a BIOS / "
                    "vendor-tool setting and will increase heat and fan noise."
                )
            findings.append(
                Finding(
                    rule_id="power_limit_throttle",
                    component="cpu",
                    diagnosis=_cpu_power_headline(reasons, pl1, pl2),
                    severity="warning" if (on_battery or saver) else "info",
                    confidence=0.9,
                    certainty="detected",
                    evidence=evidence,
                    suggested_fix=" ".join(fixes),
                    metrics={"pkg_power_w": round(power, 1) if power is not None else None, "pl1_w": pl1, "pl2_w": pl2},
                )
            )
    else:
        samples = ctx.window.all()
        peak_clock, now_clock = peak(samples, _cpu_clock), mean(r10, _cpu_clock)
        load, cpu_t = mean(ctx.recent(), _cpu_load), mean(ctx.recent(), _cpu_temp)
        if None not in (peak_clock, now_clock, load, cpu_t):
            assert peak_clock is not None and now_clock is not None and load is not None and cpu_t is not None
            evaluated = True
            drop = 1 - now_clock / peak_clock
            if load >= th.high_load_pct and drop >= th.clock_drop_fraction and cpu_t <= _tjmax(ctx) - th.safe_margin_c:
                evidence = [
                    f"Busy-core clock {now_clock:.0f} MHz versus {peak_clock:.0f} MHz earlier in the window (-{drop * 100:.0f}%)",
                    f"CPU package only {cpu_t:.0f} °C (limit ~{_tjmax(ctx):.0f} °C) at {load:.0f}% load, so heat is not the constraint",
                    f"Package power {_f1(mean(r10, _cpu_power), ' W')}",
                    "No hardware throttle flag is readable (needs elevation + PawnIO), so this is inferred from the numbers",
                ]
                fixes = []
                _power_context(ctx, evidence, fixes)
                fixes.append("Check the Windows power plan and the vendor's performance profile (e.g. PredatorSense Turbo).")
                findings.append(
                    Finding(
                        rule_id="power_limit_throttle",
                        component="cpu",
                        diagnosis=f"CPU clocks are down {drop * 100:.0f}% while cool ({cpu_t:.0f} °C) - likely a power limit",
                        severity="info",
                        confidence=0.5,
                        certainty="inferred",
                        evidence=evidence,
                        suggested_fix=" ".join(fixes),
                        metrics={"clock_now_mhz": round(now_clock), "clock_peak_mhz": round(peak_clock), "cpu_temp_c": round(cpu_t, 1)},
                    )
                )

    # ---- GPU
    if ctx.latest.has_gpu and ctx.latest.gpu_throttle is not None and ctx.latest.gpu_throttle.confidence == "detected":
        evaluated = True
        hits = _detected_active(r10, lambda s: s.gpu_throttle, ("power",))
        util = mean(ctx.recent(), _gpu_util)
        if hits and len(hits) >= max(1, len(r10) // 2) and (util or 0) >= 30.0:
            g = hits[-1].gpu_throttle
            assert g is not None
            draw, limit = mean(r10, _gpu_power), hits[-1].gpu_power_limit
            evidence = [
                f"Driver reports: {', '.join(g.reasons)} (mask {g.detail.get('raw_mask')})",
                f"GPU drawing {_f0(draw, ' W')} against a limit of {_f0(limit, ' W')} at {util:.0f}% utilisation",
                f"GPU {_f0(mean(ctx.recent(), _gpu_temp), ' °C')} - not temperature-limited",
                f"CPU is drawing {_f0(mean(r10, _cpu_power), ' W')} from the same power budget",
            ]
            fixes = []
            on_battery, _ = _power_context(ctx, evidence, fixes)
            fixes.append(
                "Hitting the GPU power cap under load is normal boost behaviour. On laptops the limit moves with "
                "Dynamic Boost, so a heavy CPU load lowers the GPU's share."
            )
            findings.append(
                Finding(
                    rule_id="power_limit_throttle",
                    component="gpu",
                    diagnosis=f"GPU is at its power cap ({_f0(draw, ' W')} of {_f0(limit, ' W')})",
                    severity="warning" if on_battery else "info",
                    confidence=0.9,
                    certainty="detected",
                    evidence=evidence,
                    suggested_fix=" ".join(fixes),
                    metrics={"gpu_power_w": round(draw, 1) if draw is not None else None, "gpu_limit_w": limit},
                )
            )

    if not evaluated:
        raise NotEvaluable("no throttle flag, clock or power data available to judge power limiting")
    return findings


# ====================================================================================================
# Rule 7 - CPU and GPU hot together (laptop) => shared heatsink saturation
# ====================================================================================================


def rule_shared_heatsink(ctx: Context) -> list[Finding]:
    th, r = ctx.th, ctx.recent()
    latest = ctx.latest
    if not latest.has_gpu:
        raise NotEvaluable("no GPU telemetry")
    if not latest.has_battery:
        raise NotEvaluable("not a laptop (no battery detected), so a shared heatsink is unlikely")
    cpu_t, gpu_t = mean(r, _cpu_temp), mean(r, _gpu_temp)
    if cpu_t is None or gpu_t is None:
        raise NotEvaluable("CPU or GPU temperature unavailable")
    cpu_p, gpu_p = mean(r, _cpu_power), mean(r, _gpu_power)
    if cpu_t < th.shared_cpu_hot_c or gpu_t < th.shared_gpu_hot_c:
        return []
    # Both must actually be working; otherwise one is merely heat-soaked by the other, which is the same
    # conclusion but needs the powers to say so.
    if (cpu_p is not None and cpu_p < 25.0) or (gpu_p is not None and gpu_p < 25.0):
        return []

    fan_note: list[str] = []
    fans_high = False
    for name, get in (("CPU Fan", _cpu_fan), ("GPU Fan", _gpu_fan)):
        rpm = mean(r, get)
        if rpm is not None:
            ratio = ctx.fans.ratio(name, rpm) if ctx.fans.trusted(name, th.fan_learn_min_span) else None
            fan_note.append(f"{name} {rpm:.0f} RPM" + (f" ({ratio * 100:.0f}% of max seen)" if ratio is not None else ""))
            fans_high = fans_high or (ratio is not None and ratio >= 0.9)
    confidence = 0.6 + (0.1 if fans_high else 0.0)  # fans already flat out strengthens the saturation hypothesis
    near = cpu_t >= _tjmax(ctx) - th.near_limit_margin_c or gpu_t >= _gpu_limit_temp(ctx) - th.near_limit_margin_c
    return [
        Finding(
            rule_id="shared_heatsink",
            component="system",
            diagnosis=f"CPU ({cpu_t:.0f} °C) and GPU ({gpu_t:.0f} °C) are hot together - the shared cooling system looks saturated",
            severity="critical" if near else "warning",
            confidence=round(min(confidence, 0.8), 2),
            certainty="inferred",
            evidence=[
                f"CPU package {cpu_t:.0f} °C drawing {_f0(cpu_p, ' W')}",
                f"GPU {gpu_t:.0f} °C drawing {_f0(gpu_p, ' W')}",
                *(fan_note or ["No fan readings available"]),
                "Both processors are working hard at the same time, and laptop CPU and GPU usually share heat pipes and fans",
            ],
            suggested_fix=(
                "Reduce the combined load: cap the game's frame rate, lower GPU/CPU power limits, or use an undervolt. "
                "Switch to the strongest fan profile, raise the rear of the laptop and use a cooling pad, and clean the vents."
            ),
            metrics={
                "cpu_temp_c": round(cpu_t, 1),
                "gpu_temp_c": round(gpu_t, 1),
                "cpu_power_w": round(cpu_p, 1) if cpu_p is not None else None,
                "gpu_power_w": round(gpu_p, 1) if gpu_p is not None else None,
            },
        )
    ]


# ====================================================================================================


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    fn: Callable[[Context], list[Finding]]


RULES: tuple[Rule, ...] = (
    Rule("idle_hot", "High temperature at low load", rule_idle_hot),
    Rule("dominant_process", "High temperature with one dominant process", rule_dominant_process),
    Rule("load_spike", "Fast temperature spike at load start", rule_spike_at_load_start),
    Rule("fan_maxed", "Fan at ~100% and still hot", rule_fan_maxed_still_hot),
    Rule("thermal_throttle", "Clocks dropped near the temperature limit", rule_thermal_throttling),
    Rule("power_limit_throttle", "Clocks dropped at normal temperature", rule_power_limit_throttling),
    Rule("shared_heatsink", "CPU and GPU hot together", rule_shared_heatsink),
)
