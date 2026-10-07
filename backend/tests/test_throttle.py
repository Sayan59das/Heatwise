"""Throttle decoding / inference tests (pure logic, no hardware)."""

from __future__ import annotations

import unittest

from app.collectors.throttle import (
    CpuThrottleInference,
    combine,
    decode_intel_throttle,
    decode_nvml_throttle,
    decode_power_limits,
    nvml_reason_names,
)
from app.models import CoreReading, CpuSnapshot, ThrottleComponent


class NvmlDecodeTests(unittest.TestCase):
    def test_unavailable(self) -> None:
        r = decode_nvml_throttle(None)
        self.assertIsNone(r.active)
        self.assertEqual(r.confidence, "unavailable")

    def test_no_reasons(self) -> None:
        r = decode_nvml_throttle(0)
        self.assertIs(r.active, False)
        self.assertEqual(r.confidence, "detected")

    def test_idle_and_other_benign_bits_are_not_throttling(self) -> None:
        r = decode_nvml_throttle(0x1 | 0x2 | 0x10 | 0x100)
        self.assertIs(r.active, False)
        self.assertIsNone(r.type)
        # ...but they are still named for display
        self.assertEqual(nvml_reason_names(0x1 | 0x100), ["GPU idle", "Display clock setting"])

    def test_sw_power_cap(self) -> None:
        r = decode_nvml_throttle(0x4)
        self.assertTrue(r.active)
        self.assertEqual((r.type, r.confidence, r.source), ("power", "detected", "nvml"))
        self.assertEqual(r.reasons, ["Software power cap"])

    def test_thermal_outranks_power_and_all_reasons_listed(self) -> None:
        r = decode_nvml_throttle(0x4 | 0x40)
        self.assertEqual(r.type, "thermal")
        self.assertCountEqual(r.reasons, ["Software power cap", "Hardware thermal slowdown"])
        self.assertEqual(r.detail["raw_mask"], "0x44")

    def test_hw_slowdown_alone_is_other(self) -> None:
        self.assertEqual(decode_nvml_throttle(0x8).type, "other")

    def test_hw_slowdown_with_power_brake_is_power(self) -> None:
        self.assertEqual(decode_nvml_throttle(0x8 | 0x80).type, "power")

    def test_board_limit_and_idle_together(self) -> None:
        r = decode_nvml_throttle(0x200 | 0x1)
        self.assertTrue(r.active)
        self.assertEqual(r.reasons, ["Board power limit"])  # idle is not reported as a throttle reason


class IntelDecodeTests(unittest.TestCase):
    def test_nothing_readable(self) -> None:
        r = decode_intel_throttle(None, None)
        self.assertIsNone(r.active)
        self.assertEqual(r.confidence, "unavailable")

    def test_clean(self) -> None:
        r = decode_intel_throttle(0, 0)
        self.assertIs(r.active, False)
        self.assertEqual((r.confidence, r.source), ("detected", "msr:perf_limit_reasons"))

    def test_thermal(self) -> None:
        r = decode_intel_throttle(1 << 1, None)
        self.assertEqual((r.active, r.type), (True, "thermal"))

    def test_prochot(self) -> None:
        r = decode_intel_throttle(1 << 0, None)
        self.assertEqual((r.active, r.type), (True, "prochot"))

    def test_pl1_and_pl2(self) -> None:
        r = decode_intel_throttle((1 << 11) | (1 << 12), None)
        self.assertEqual(r.type, "power")
        self.assertEqual(r.reasons, ["Package power limit PL1", "Package power limit PL2"])

    def test_thermal_outranks_power(self) -> None:
        self.assertEqual(decode_intel_throttle((1 << 11) | (1 << 1), None).type, "thermal")

    def test_sticky_log_bits_alone_do_not_mean_throttling_now(self) -> None:
        # bits 16-31 mean "happened since last clear"
        r = decode_intel_throttle((1 << 17) | (1 << 27), None)
        self.assertIs(r.active, False)

    def test_max_turbo_is_not_a_throttle(self) -> None:
        self.assertIs(decode_intel_throttle(1 << 13, None).active, False)

    def test_falls_back_to_package_therm_status(self) -> None:
        r = decode_intel_throttle(None, 1 << 0)
        self.assertEqual((r.active, r.type, r.source), (True, "thermal", "msr:package_therm_status"))
        self.assertEqual(decode_intel_throttle(None, 1 << 2).type, "prochot")


class PowerLimitTests(unittest.TestCase):
    def test_pl1_pl2_decode(self) -> None:
        unit = 0x3  # 1/8 W resolution
        value = (440 | (1 << 15) | (1 << 16)) | ((1256 | (1 << 15)) << 32)  # 55 W and 157 W, both enabled
        self.assertEqual(decode_power_limits(value, unit), (55.0, 157.0))

    def test_disabled_limit_is_none(self) -> None:
        value = 440 | (1 << 15)  # PL2 enable bit not set
        self.assertEqual(decode_power_limits(value | (1256 << 32), 0x3), (55.0, None))

    def test_unreadable(self) -> None:
        self.assertEqual(decode_power_limits(None, 3), (None, None))
        self.assertEqual(decode_power_limits(123, None), (None, None))


def snap(load: float | None, temp: float | None, clock: float | None, power: float | None = 40.0,
         tjmax: float | None = 100.0) -> CpuSnapshot:
    cores = [CoreReading(index=i, kind="P", clock_mhz=clock, load_pct=load) for i in range(1, 9)]
    return CpuSnapshot(
        vendor="intel", package_temp_c=temp, max_core_temp_c=temp, tjmax_c=tjmax, total_load_pct=load,
        package_power_w=power, cores=cores,
    )


def run(inf: CpuThrottleInference, steps: list[tuple[float, CpuSnapshot]]) -> ThrottleComponent:
    last = ThrottleComponent()
    for t, s in steps:
        last = inf.update(t, s)
    return last


class InferenceTests(unittest.TestCase):
    def test_idle_is_not_throttling(self) -> None:
        r = run(CpuThrottleInference(), [(t, snap(5, 45, 800)) for t in range(0, 30)])
        self.assertIs(r.active, False)

    def test_clock_collapse_at_the_thermal_limit_is_inferred_thermal(self) -> None:
        steps = [(t, snap(95, 90, 4500)) for t in range(0, 15)] + [(t, snap(95, 99, 3500)) for t in range(15, 30)]
        r = run(CpuThrottleInference(), steps)
        self.assertEqual((r.active, r.type), (True, "thermal"))
        self.assertEqual(r.confidence, "inferred")  # never "detected"
        self.assertTrue(r.source.startswith("inference:"))
        self.assertEqual(r.detail["temp_c"], 99.0)

    def test_clock_collapse_with_headroom_is_inferred_power_limit(self) -> None:
        steps = [(t, snap(95, 75, 4500)) for t in range(0, 15)] + [(t, snap(95, 72, 3300)) for t in range(15, 30)]
        r = run(CpuThrottleInference(), steps)
        self.assertEqual((r.active, r.type, r.confidence), (True, "power", "inferred"))

    def test_ambiguous_middle_temperature_makes_no_claim(self) -> None:
        steps = [(t, snap(95, 80, 4500)) for t in range(0, 15)] + [(t, snap(95, 90, 3500)) for t in range(15, 30)]
        r = run(CpuThrottleInference(), steps)
        self.assertIs(r.active, False)

    def test_hot_but_clocks_holding_is_not_throttling(self) -> None:
        r = run(CpuThrottleInference(), [(t, snap(95, 99, 4500)) for t in range(0, 30)])
        self.assertIs(r.active, False)

    def test_needs_history_before_judging(self) -> None:
        r = run(CpuThrottleInference(), [(0, snap(95, 90, 4500)), (2, snap(95, 99, 3000))])
        self.assertIs(r.active, False)

    def test_missing_temperature_is_unavailable_not_false(self) -> None:
        r = run(CpuThrottleInference(), [(t, snap(95, None, 4500)) for t in range(0, 20)])
        self.assertIsNone(r.active)
        self.assertEqual(r.confidence, "unavailable")

    def test_old_peak_ages_out_of_the_window(self) -> None:
        inf = CpuThrottleInference()
        for t in range(0, 10):
            inf.update(t, snap(95, 75, 4500))
        # 5 minutes later the 4500 MHz peak is outside the 2-minute window; a steady 3300 is the new normal
        r = run(inf, [(t, snap(95, 72, 3300)) for t in range(300, 330)])
        self.assertIs(r.active, False)

    def test_amd_default_tjmax_is_lower(self) -> None:
        def amd(clock: float, temp: float) -> CpuSnapshot:
            s = snap(95, temp, clock, tjmax=None)
            s.vendor = "amd"
            return s

        steps = [(t, amd(4500, 85)) for t in range(0, 15)] + [(t, amd(3500, 92)) for t in range(15, 30)]
        r = run(CpuThrottleInference(), steps)  # 92 C is within 4 C of AMD's default 95 C limit
        self.assertEqual((r.active, r.type), (True, "thermal"))


class CombineTests(unittest.TestCase):
    thermal_cpu = ThrottleComponent(active=True, type="thermal", source="msr:perf_limit_reasons",
                                    confidence="detected", reasons=["Thermal throttling (TCC active)"])
    power_gpu = ThrottleComponent(active=True, type="power", source="nvml", confidence="detected",
                                  reasons=["Software power cap"])
    quiet = ThrottleComponent(active=False, source="nvml", confidence="detected")

    def test_most_severe_detected_component_leads_and_others_are_appended(self) -> None:
        t = combine(self.thermal_cpu, self.power_gpu)
        self.assertEqual((t.active, t.type, t.component, t.confidence), (True, "thermal", "cpu", "detected"))
        self.assertEqual(t.reasons, ["Thermal throttling (TCC active)", "GPU: Software power cap"])
        self.assertEqual(t.cpu, self.thermal_cpu)
        self.assertEqual(t.gpu, self.power_gpu)

    def test_detected_outranks_inferred_even_if_less_severe(self) -> None:
        inferred_thermal = ThrottleComponent(active=True, type="thermal", source="inference:clock_temp_power",
                                             confidence="inferred", reasons=["Clocks fell 20%"])
        t = combine(inferred_thermal, self.power_gpu)
        self.assertEqual((t.component, t.type, t.confidence), ("gpu", "power", "detected"))

    def test_inferred_stays_inferred(self) -> None:
        inferred = ThrottleComponent(active=True, type="power", source="inference:clock_temp_power",
                                     confidence="inferred", reasons=["x"])
        t = combine(inferred, None)
        self.assertEqual(t.confidence, "inferred")

    def test_all_quiet(self) -> None:
        t = combine(ThrottleComponent(active=False, source="msr", confidence="detected"), self.quiet)
        self.assertIs(t.active, False)
        self.assertEqual(t.confidence, "detected")

    def test_nothing_known(self) -> None:
        t = combine(ThrottleComponent(), None)
        self.assertIsNone(t.active)
        self.assertEqual(t.confidence, "unavailable")


if __name__ == "__main__":
    unittest.main()
