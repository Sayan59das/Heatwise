"""Root-cause engine tests, driven by simulated situations (see tests/scenarios.py)."""

from __future__ import annotations

import unittest

from app.analyzer.engine import DiagnosisEngine
from app.analyzer.fans import FanKnowledge
from app.analyzer.models import Diagnosis, DiagnosisReport
from app.analyzer.rules import RULES, Rule
from app.analyzer.window import Sample, process_shares, slope_per_s
from app.models import Snapshot

from tests.scenarios import T0, comp, feed, proc, ramp, snap


def find(report: DiagnosisReport, rule_id: str, component: str | None = None) -> Diagnosis | None:
    return next(
        (d for d in report.diagnoses if d.rule_id == rule_id and (component is None or d.component == component)), None
    )


def skipped(report: DiagnosisReport, rule_id: str) -> str | None:
    return next((s.reason for s in report.not_evaluated if s.rule_id == rule_id), None)


def learned_fans() -> FanKnowledge:
    """A machine whose fans have been seen idling at 2000 RPM and flat out at 6500 RPM."""
    return FanKnowledge({"CPU Fan": (2000.0, 6500.0), "GPU Fan": (2000.0, 6500.0)})


class BaselineTests(unittest.TestCase):
    def test_warming_up_until_enough_samples(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 8)
        self.assertEqual(r.status, "warming_up")
        self.assertEqual(r.diagnoses, [])
        self.assertIn("8 of", r.summary)

    def test_healthy_idle_laptop_is_all_clear(self) -> None:
        e = DiagnosisEngine(fans=learned_fans())
        r = feed(e, 0, 90, cpu_temp=48.0, cpu_load=3.0, cpu_power=7.0, cpu_fan=2100.0, gpu=True, gpu_temp=42.0)
        self.assertEqual((r.status, r.diagnoses), ("ok", []))
        self.assertIn("No thermal problems", r.summary)

    def test_a_normal_hot_game_session_without_symptoms_is_not_a_problem(self) -> None:
        # 82 C under heavy load, clocks steady, no flags set: warm, but working as designed.
        e = DiagnosisEngine(fans=learned_fans())
        r = feed(e, 0, 120, cpu_temp=82.0, cpu_load=90.0, cpu_power=60.0, clock=4200.0, cpu_fan=5000.0,
                 cpu_throttle=comp(False), processes=[proc("a.exe", 20), proc("b.exe", 20), proc("c.exe", 20)])
        self.assertEqual([d.rule_id for d in r.diagnoses], [])


class IdleHotTests(unittest.TestCase):
    def test_background_process_is_named_with_numbers(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=76.0, cpu_load=9.0, cpu_power=18.0,
                 processes=[proc("chrome.exe", 7.0, count=14), proc("Code.exe", 1.0)])
        d = find(r, "idle_hot")
        assert d is not None
        self.assertIn("chrome.exe", d.diagnosis)
        self.assertEqual((d.certainty, d.component), ("inferred", "system"))
        self.assertLess(d.confidence, 1.0)
        joined = " ".join(d.evidence)
        self.assertIn("76 °C", joined)
        self.assertIn("9%", joined)
        self.assertIn("7.0%", joined)
        self.assertIn("14 instances", joined)

    def test_known_background_task_gets_specific_advice(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=74.0, cpu_load=10.0, processes=[proc("MsMpEng.exe", 8.0)])
        d = find(r, "idle_hot")
        assert d is not None
        self.assertIn("Defender", d.suggested_fix)

    def test_no_process_and_low_power_points_to_cooling(self) -> None:
        e = DiagnosisEngine(fans=learned_fans())
        r = feed(e, 0, 60, cpu_temp=78.0, cpu_load=2.0, cpu_power=9.0, cpu_fan=5200.0, processes=[proc("idle.exe", 0.5)])
        d = find(r, "idle_hot")
        assert d is not None
        self.assertIn("airflow", d.diagnosis)
        self.assertIn("not coming from load", " ".join(d.evidence))
        self.assertGreaterEqual(d.confidence, 0.6)
        self.assertEqual(d.severity, "info" if 78.0 < 80.0 else "warning")

    def test_fan_barely_spinning_while_hot_suggests_fan_profile(self) -> None:
        e = DiagnosisEngine(fans=learned_fans())
        r = feed(e, 0, 60, cpu_temp=77.0, cpu_load=2.0, cpu_power=9.0, cpu_fan=2100.0)
        d = find(r, "idle_hot")
        assert d is not None
        self.assertIn("fan profile", d.suggested_fix.lower())

    def test_high_performance_plan_is_blamed_when_nothing_else_is(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=73.0, cpu_load=2.0, cpu_power=25.0, plan="high_performance")
        d = find(r, "idle_hot")
        assert d is not None
        self.assertIn("power plan", d.diagnosis.lower())

    def test_busy_machine_is_not_an_idle_hot_case(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=88.0, cpu_load=70.0)
        self.assertIsNone(find(r, "idle_hot"))

    def test_without_temperature_data_the_rule_says_so(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 40, cpu_temp=None, cpu_load=3.0)
        self.assertIn("temperature", skipped(r, "idle_hot") or "")


class DominantProcessTests(unittest.TestCase):
    def test_single_heavy_process_is_named(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=90.0, cpu_load=70.0, cpu_power=70.0,
                 processes=[proc("blender.exe", 55.0), proc("chrome.exe", 4.0), proc("Teams.exe", 2.0)])
        d = find(r, "dominant_process", "cpu")
        assert d is not None
        self.assertIn("blender.exe", d.diagnosis)
        self.assertEqual(d.severity, "warning")
        self.assertIn("90 °C", d.diagnosis)
        self.assertEqual(d.metrics["process"], "blender.exe")
        self.assertGreater(d.metrics["share_of_attributed_cpu"], 0.8)  # type: ignore[operator]

    def test_near_the_limit_is_critical(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=98.0, cpu_load=80.0, processes=[proc("render.exe", 60.0)])
        self.assertEqual(find(r, "dominant_process", "cpu").severity, "critical")  # type: ignore[union-attr]

    def test_shared_load_has_no_dominant_process(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=90.0, cpu_load=70.0, processes=[proc("a.exe", 20.0), proc("b.exe", 19.0), proc("c.exe", 18.0)])
        self.assertIsNone(find(r, "dominant_process"))

    def test_gpu_process_is_named(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=60.0, cpu_load=20.0, gpu=True, gpu_temp=86.0, gpu_util=95.0, gpu_power=85.0,
                 processes=[proc("game.exe", 10.0, gpu=88.0), proc("dwm.exe", 1.0, gpu=4.0)])
        d = find(r, "dominant_process", "gpu")
        assert d is not None
        self.assertIn("game.exe", d.diagnosis)
        self.assertIn("86 °C", d.diagnosis)

    def test_hot_gpu_without_per_process_data_is_reported_as_not_evaluable(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=60.0, cpu_load=20.0, gpu=True, gpu_temp=86.0, gpu_util=95.0,
                 processes=[proc("game.exe", 10.0)])
        self.assertIsNone(find(r, "dominant_process", "gpu"))
        self.assertIn("per-process GPU", skipped(r, "dominant_process") or "")

    def test_own_process_is_never_blamed(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=90.0, cpu_load=70.0, processes=[proc("ThermalSense.exe", 40.0), proc("x.exe", 3.0)])
        d = find(r, "dominant_process", "cpu")
        self.assertTrue(d is None or "ThermalSense" not in d.diagnosis)

    def test_missing_process_data_is_reported_not_silently_ignored(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=92.0, cpu_load=80.0, processes=[])
        self.assertIn("per-process", skipped(r, "dominant_process") or "")


class SpikeTests(unittest.TestCase):
    def burst(self, power: float | None, *, rise_s: int = 12, peak_c: float = 96.0) -> DiagnosisReport:
        e = DiagnosisEngine()
        return feed(
            e, 0, 80,
            cpu_load=lambda i: 4.0 if i < 30 else 100.0,
            cpu_temp=lambda i: 45.0 if i < 30 else ramp(30, 30 + rise_s, 45.0, peak_c)(i),
            cpu_power=lambda i: power if i >= 30 else 8.0,
            cpu_fan=lambda i: 2000.0,
        )

    def test_mid_power_is_the_baseline_confidence(self) -> None:
        d = find(self.burst(power=90.0), "load_spike")
        assert d is not None
        self.assertEqual(d.confidence, 0.55)  # neither suspiciously low power nor boost-level power

    def test_fast_rise_at_low_power_is_flagged_with_numbers(self) -> None:
        r = self.burst(power=50.0)
        d = find(r, "load_spike")
        assert d is not None
        self.assertEqual((d.severity, d.certainty), ("warning", "inferred"))
        self.assertEqual(d.confidence, 0.7)  # little power for so much heat -> stronger suspicion
        joined = " ".join(d.evidence)
        self.assertIn("45 °C", joined)
        self.assertIn("96 °C", joined)
        self.assertIn("thermal paste", d.diagnosis)
        self.assertEqual(d.metrics["base_temp_c"], 45.0)

    def test_boost_level_power_lowers_confidence_and_says_why(self) -> None:
        d = find(self.burst(power=150.0), "load_spike")
        assert d is not None
        self.assertEqual(d.confidence, 0.4)
        self.assertIn("boost", " ".join(d.evidence))

    def test_missing_power_data_lowers_confidence_and_says_why(self) -> None:
        d = find(self.burst(power=None), "load_spike")
        assert d is not None
        self.assertLess(d.confidence, 0.5)
        self.assertIn("unavailable", " ".join(d.evidence))

    def test_reaching_tjmax_is_critical(self) -> None:
        d = find(self.burst(power=70.0, peak_c=99.0), "load_spike")
        assert d is not None
        self.assertEqual(d.severity, "critical")

    def test_slow_rise_is_not_a_spike(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 120, cpu_load=lambda i: 4.0 if i < 30 else 100.0,
                 cpu_temp=lambda i: ramp(30, 90, 45.0, 92.0)(i), cpu_power=70.0)
        self.assertIsNone(find(r, "load_spike"))

    def test_never_reaching_90_is_not_flagged(self) -> None:
        r = self.burst(power=70.0, peak_c=88.0)
        self.assertIsNone(find(r, "load_spike"))

    def test_a_two_second_load_blip_is_ignored(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 80, cpu_load=lambda i: 100.0 if 40 <= i < 42 else 4.0,
                 cpu_temp=lambda i: 95.0 if 40 <= i < 46 else 45.0, cpu_power=70.0)
        self.assertIsNone(find(r, "load_spike"))

    def test_steady_load_without_an_idle_start_is_not_a_load_start(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 80, cpu_load=100.0, cpu_temp=lambda i: ramp(0, 10, 60.0, 95.0)(i), cpu_power=70.0)
        self.assertIsNone(find(r, "load_spike"))


class FanTests(unittest.TestCase):
    def test_fan_flat_out_and_still_hot(self) -> None:
        e = DiagnosisEngine(fans=learned_fans())
        r = feed(e, 0, 90, cpu_temp=92.0, cpu_load=90.0, cpu_fan=6450.0, cpu_throttle=comp(False))
        d = find(r, "fan_maxed", "cpu")
        assert d is not None
        self.assertEqual(d.certainty, "inferred")
        self.assertLess(d.confidence, 0.8)
        joined = " ".join(d.evidence)
        self.assertIn("6450 RPM", joined)
        self.assertIn("6500 RPM", joined)
        self.assertIn("estimated", joined)

    def test_temperature_dropping_means_the_cooling_is_working(self) -> None:
        e = DiagnosisEngine(fans=learned_fans())
        r = feed(e, 0, 90, cpu_temp=ramp(0, 60, 98.0, 86.0), cpu_load=60.0, cpu_fan=6450.0)
        self.assertIsNone(find(r, "fan_maxed"))

    def test_fan_not_at_max_is_fine(self) -> None:
        e = DiagnosisEngine(fans=learned_fans())
        r = feed(e, 0, 90, cpu_temp=92.0, cpu_load=90.0, cpu_fan=4000.0)
        self.assertIsNone(find(r, "fan_maxed"))

    def test_unlearned_fan_range_is_not_treated_as_full_speed(self) -> None:
        # Fresh install: the only RPM ever seen is 6500, so "6500 / max seen" is trivially 100%.
        e = DiagnosisEngine(fans=FanKnowledge())
        r = feed(e, 0, 90, cpu_temp=92.0, cpu_load=90.0, cpu_fan=6500.0)
        self.assertIsNone(find(r, "fan_maxed"))
        self.assertIn("not learned", skipped(r, "fan_maxed") or "")

    def test_range_is_learned_while_running(self) -> None:
        e = DiagnosisEngine()
        feed(e, 0, 40, cpu_temp=50.0, cpu_load=5.0, cpu_fan=2000.0)  # idle: fan slow
        r = feed(e, 40, 120, cpu_temp=92.0, cpu_load=90.0, cpu_fan=6400.0)  # then flat out and hot
        self.assertIsNotNone(find(r, "fan_maxed", "cpu"))

    def test_no_fan_data_is_reported_as_not_evaluable(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=92.0, cpu_load=90.0)
        self.assertIn("no fan speed", skipped(r, "fan_maxed") or "")

    def test_direct_gpu_fan_duty_is_detected_not_inferred(self) -> None:
        def duty(s: Snapshot) -> None:  # a desktop card reporting duty directly
            assert s.gpu is not None
            s.gpu.fan_percent = 100.0

        r = feed(DiagnosisEngine(), 0, 90, mutate=duty, gpu=True, gpu_temp=84.0, gpu_util=90.0, cpu_temp=60.0, cpu_load=30.0)
        d = find(r, "fan_maxed", "gpu")
        assert d is not None
        self.assertEqual(d.certainty, "detected")


class ThermalThrottleTests(unittest.TestCase):
    def test_hardware_flag_is_reported_as_detected(self) -> None:
        e = DiagnosisEngine()
        flag = comp(True, "thermal", ["Thermal throttling (TCC active)"])
        r = feed(e, 0, 60, cpu_temp=99.0, cpu_load=95.0, clock=lambda i: 4500.0 if i < 20 else 3000.0, cpu_throttle=flag)
        d = find(r, "thermal_throttle", "cpu")
        assert d is not None
        self.assertEqual((d.certainty, d.severity, d.confidence), ("detected", "critical", 0.95))
        joined = " ".join(d.evidence)
        self.assertIn("Thermal throttling (TCC active)", joined)
        self.assertIn("99 °C", joined)
        self.assertIn("msr:perf_limit_reasons", joined)

    def test_prochot_at_a_cool_temperature_is_not_blamed_on_cpu_heat(self) -> None:
        e = DiagnosisEngine()
        flag = comp(True, "prochot", ["PROCHOT# asserted"])
        r = feed(e, 0, 60, cpu_temp=58.0, cpu_load=60.0, cpu_throttle=flag)
        d = find(r, "thermal_throttle", "cpu")
        assert d is not None
        self.assertIn("PROCHOT", d.diagnosis)
        self.assertIn("not CPU heat", " ".join(d.evidence))

    def test_without_flags_it_is_inferred_and_labelled_so(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 90, cpu_temp=98.0, cpu_load=95.0, clock=lambda i: 4500.0 if i < 30 else 3400.0)  # no throttle data
        d = find(r, "thermal_throttle", "cpu")
        assert d is not None
        self.assertEqual((d.certainty, d.severity), ("inferred", "warning"))
        self.assertLess(d.confidence, 0.8)
        self.assertIn("likely", d.diagnosis)
        self.assertIn("inferred", " ".join(d.evidence))

    def test_a_working_hardware_flag_overrides_inference(self) -> None:
        # The flag channel works and says "not throttling": don't contradict it with a guess.
        e = DiagnosisEngine()
        r = feed(e, 0, 90, cpu_temp=98.0, cpu_load=95.0, clock=lambda i: 4500.0 if i < 30 else 3400.0,
                 cpu_throttle=comp(False))
        self.assertIsNone(find(r, "thermal_throttle", "cpu"))

    def test_clock_drop_when_cool_is_not_thermal(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 90, cpu_temp=70.0, cpu_load=95.0, clock=lambda i: 4500.0 if i < 30 else 3400.0)
        self.assertIsNone(find(r, "thermal_throttle", "cpu"))

    def test_gpu_thermal_slowdown_from_the_driver(self) -> None:
        e = DiagnosisEngine()
        flag = comp(True, "thermal", ["Hardware thermal slowdown"], source="nvml", mask="0x40")
        r = feed(e, 0, 60, cpu_temp=60.0, cpu_load=20.0, gpu=True, gpu_temp=91.0, gpu_util=99.0, gpu_throttle=flag)
        d = find(r, "thermal_throttle", "gpu")
        assert d is not None
        self.assertEqual(d.certainty, "detected")
        self.assertIn("0x40", " ".join(d.evidence))

    def test_no_data_at_all_is_not_evaluable(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 40, cpu_temp=None, cpu_load=None, clock=None)
        self.assertIsNotNone(skipped(r, "thermal_throttle"))


class PowerLimitTests(unittest.TestCase):
    PL1 = comp(True, "power", ["Package power limit PL1"])

    def test_pl1_is_detected_with_the_numbers(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=76.0, cpu_load=100.0, cpu_power=55.0, pl1=55.0, pl2=157.0, cpu_throttle=self.PL1)
        d = find(r, "power_limit_throttle", "cpu")
        assert d is not None
        self.assertEqual((d.certainty, d.severity), ("detected", "info"))
        self.assertIn("PL1 = 55 W", d.diagnosis)
        joined = " ".join(d.evidence)
        self.assertIn("55.0 W", joined)
        self.assertIn("157.0 W", joined)
        self.assertIn("temperature is not the constraint", joined)

    def test_on_battery_escalates_and_is_named(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=70.0, cpu_load=100.0, cpu_power=30.0, pl1=30.0, on_ac=False, cpu_throttle=self.PL1)
        d = find(r, "power_limit_throttle", "cpu")
        assert d is not None
        self.assertEqual(d.severity, "warning")
        self.assertIn("battery", " ".join(d.evidence))
        self.assertIn("charger", d.suggested_fix)

    def test_power_saver_plan_is_named(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=60.0, cpu_load=100.0, cpu_power=20.0, pl1=20.0, plan="power_saver", cpu_throttle=self.PL1)
        d = find(r, "power_limit_throttle", "cpu")
        assert d is not None
        self.assertIn("Power saver", " ".join(d.evidence))

    def test_inferred_when_no_flag_is_readable(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 90, cpu_temp=70.0, cpu_load=95.0, cpu_power=45.0, clock=lambda i: 4500.0 if i < 30 else 3300.0)
        d = find(r, "power_limit_throttle", "cpu")
        assert d is not None
        self.assertEqual(d.certainty, "inferred")
        self.assertLessEqual(d.confidence, 0.5)
        self.assertIn("likely", d.diagnosis)

    def test_thermal_and_power_flags_together_report_both(self) -> None:
        e = DiagnosisEngine()
        both = comp(True, "thermal", ["Thermal throttling (TCC active)", "Package power limit PL1"])
        r = feed(e, 0, 60, cpu_temp=99.0, cpu_load=100.0, cpu_power=55.0, pl1=55.0, cpu_throttle=both)
        self.assertIsNotNone(find(r, "thermal_throttle", "cpu"))
        self.assertIsNotNone(find(r, "power_limit_throttle", "cpu"))

    def test_gpu_power_cap_under_load(self) -> None:
        e = DiagnosisEngine()
        cap = comp(True, "power", ["Software power cap"], source="nvml", mask="0x4")
        r = feed(e, 0, 60, cpu_temp=70.0, cpu_load=60.0, cpu_power=45.0, gpu=True, gpu_temp=70.0, gpu_util=98.0,
                 gpu_power=88.0, gpu_limit=90.0, gpu_throttle=cap)
        d = find(r, "power_limit_throttle", "gpu")
        assert d is not None
        self.assertEqual((d.certainty, d.severity), ("detected", "info"))
        joined = " ".join(d.evidence)
        self.assertIn("88 W", joined)
        self.assertIn("Software power cap", joined)

    def test_gpu_power_cap_while_idle_is_not_reported(self) -> None:
        e = DiagnosisEngine()
        cap = comp(True, "power", ["Software power cap"], source="nvml")
        r = feed(e, 0, 60, gpu=True, gpu_temp=45.0, gpu_util=2.0, gpu_throttle=cap)
        self.assertIsNone(find(r, "power_limit_throttle", "gpu"))


class SharedHeatsinkTests(unittest.TestCase):
    def test_both_hot_and_busy_on_a_laptop(self) -> None:
        e = DiagnosisEngine(fans=learned_fans())
        r = feed(e, 0, 90, cpu_temp=88.0, cpu_power=60.0, cpu_load=90.0, gpu=True, gpu_temp=79.0, gpu_power=70.0,
                 gpu_util=95.0, cpu_fan=6400.0, gpu_fan=6400.0, cpu_throttle=comp(False))
        d = find(r, "shared_heatsink")
        assert d is not None
        self.assertEqual(d.certainty, "inferred")
        self.assertLessEqual(d.confidence, 0.8)
        self.assertEqual(d.confidence, 0.7)  # fans flat out strengthens the case
        joined = " ".join(d.evidence)
        self.assertIn("88 °C", joined)
        self.assertIn("79 °C", joined)
        self.assertIn("60 W", joined)

    def test_one_side_cool_is_not_saturation(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 90, cpu_temp=88.0, cpu_power=60.0, cpu_load=90.0, gpu=True, gpu_temp=60.0, gpu_power=70.0)
        self.assertIsNone(find(r, "shared_heatsink"))

    def test_hot_but_one_side_barely_working_is_ignored(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 90, cpu_temp=88.0, cpu_power=60.0, cpu_load=90.0, gpu=True, gpu_temp=75.0, gpu_power=8.0)
        self.assertIsNone(find(r, "shared_heatsink"))

    def test_desktop_is_not_evaluated(self) -> None:
        e = DiagnosisEngine()
        r = feed(e, 0, 60, cpu_temp=88.0, cpu_power=60.0, cpu_load=90.0, gpu=True, gpu_temp=79.0, gpu_power=70.0, battery=False)
        self.assertIn("not a laptop", skipped(r, "shared_heatsink") or "")

    def test_no_gpu_is_not_evaluated(self) -> None:
        r = feed(DiagnosisEngine(), 0, 60, cpu_temp=88.0, cpu_load=90.0)
        self.assertIn("no GPU", skipped(r, "shared_heatsink") or "")


class StabilityTests(unittest.TestCase):
    """The engine must not flicker, and must keep a history of what it said."""

    def hot_idle(self, e: DiagnosisEngine, start: int, n: int) -> DiagnosisReport:
        return feed(e, start, n, cpu_temp=78.0, cpu_load=2.0, cpu_power=9.0, processes=[proc("idle.exe", 0.2)])

    def cool_idle(self, e: DiagnosisEngine, start: int, n: int) -> DiagnosisReport:
        return feed(e, start, n, cpu_temp=45.0, cpu_load=2.0, cpu_power=6.0, processes=[proc("idle.exe", 0.2)])

    def test_condition_must_hold_before_it_is_reported(self) -> None:
        e = DiagnosisEngine()
        self.cool_idle(e, 0, 80)
        r = self.hot_idle(e, 80, 70)  # recent_s window is 60: needs time to turn "hot" then 15 s to confirm
        self.assertIsNotNone(find(r, "idle_hot"))
        events = [ev.kind for ev in e.drain_events()]
        self.assertEqual(events, ["confirmed"])  # exactly one confirmation, no repeats

    def test_a_brief_blip_never_reaches_the_user(self) -> None:
        e = DiagnosisEngine()
        self.cool_idle(e, 0, 100)
        feed(e, 100, 6, cpu_temp=95.0, cpu_load=2.0, cpu_power=9.0)  # 6 s of heat then back to cool
        r = self.cool_idle(e, 106, 80)
        self.assertEqual(r.diagnoses, [])
        self.assertEqual(e.drain_events(), [])

    def test_resolved_diagnosis_lingers_then_moves_to_history(self) -> None:
        e = DiagnosisEngine()
        self.hot_idle(e, 0, 90)
        self.assertIsNotNone(find(e.evaluate(), "idle_hot"))
        e.drain_events()
        # cool down; the 60 s "recent" window means the condition clears ~mid-way, then it must linger
        r = self.cool_idle(e, 90, 75)
        d = find(r, "idle_hot")
        # still shown (lingering, inactive) or already resolved - never silently vanished without an event
        if d is not None:
            self.assertFalse(d.active)
        r = self.cool_idle(e, 165, 60)
        self.assertIsNone(find(r, "idle_hot"))
        self.assertEqual([x.rule_id for x in r.recently_resolved], ["idle_hot"])
        kinds = [ev.kind for ev in e.drain_events()]
        self.assertEqual(kinds, ["resolved"])

    def test_severity_change_is_an_event(self) -> None:
        e = DiagnosisEngine()
        feed(e, 0, 70, cpu_temp=90.0, cpu_load=70.0, processes=[proc("a.exe", 55.0)])
        self.assertEqual(find(e.evaluate(), "dominant_process", "cpu").severity, "warning")  # type: ignore[union-attr]
        e.drain_events()
        feed(e, 70, 70, cpu_temp=98.0, cpu_load=70.0, processes=[proc("a.exe", 55.0)])
        self.assertEqual(find(e.evaluate(), "dominant_process", "cpu").severity, "critical")  # type: ignore[union-attr]
        self.assertIn("changed", [ev.kind for ev in e.drain_events()])

    def test_ranking_puts_critical_first(self) -> None:
        e = DiagnosisEngine()
        flag = comp(True, "thermal", ["Thermal throttling (TCC active)"])
        r = feed(e, 0, 90, cpu_temp=99.0, cpu_load=100.0, cpu_power=55.0, pl1=55.0, cpu_throttle=flag,
                 processes=[proc("a.exe", 60.0)])
        sev = [d.severity for d in r.diagnoses]
        self.assertEqual(sev, sorted(sev, key=["critical", "warning", "info"].index))
        self.assertEqual(r.diagnoses[0].severity, "critical")
        self.assertEqual(r.status, "issues")

    def test_a_crashing_rule_is_contained(self) -> None:
        def boom(_ctx: object) -> list:  # type: ignore[type-arg]
            raise RuntimeError("bug")

        e = DiagnosisEngine(rules=(*RULES, Rule("boom", "Broken rule", boom)))  # type: ignore[arg-type]
        r = feed(e, 0, 40, cpu_temp=45.0, cpu_load=3.0)
        self.assertIn("internal error", skipped(r, "boom") or "")
        self.assertEqual(r.status, "ok")  # the other rules still ran

    def test_skips_are_surfaced_in_the_summary(self) -> None:
        r = feed(DiagnosisEngine(), 0, 40, cpu_temp=45.0, cpu_load=3.0)
        self.assertIn("could not run", r.summary)


class HonestyTests(unittest.TestCase):
    """Never present inference as certainty."""

    def all_findings(self) -> list[Diagnosis]:
        out: list[Diagnosis] = []
        scenarios = [
            dict(cpu_temp=76.0, cpu_load=9.0, processes=[proc("chrome.exe", 7.0)]),
            dict(cpu_temp=90.0, cpu_load=70.0, processes=[proc("blender.exe", 55.0)]),
            dict(cpu_temp=98.0, cpu_load=95.0, clock=lambda i: 4500.0 if i < 30 else 3400.0),
            dict(cpu_temp=70.0, cpu_load=95.0, clock=lambda i: 4500.0 if i < 30 else 3300.0),
            dict(cpu_temp=88.0, cpu_power=60.0, cpu_load=90.0, gpu=True, gpu_temp=79.0, gpu_power=70.0),
        ]
        for kw in scenarios:
            out.extend(feed(DiagnosisEngine(fans=learned_fans()), 0, 120, **kw).diagnoses)
        return out

    def test_inferred_findings_never_claim_near_certainty(self) -> None:
        findings = self.all_findings()
        self.assertGreaterEqual(len(findings), 5)
        for d in findings:
            if d.certainty == "inferred":
                self.assertLessEqual(d.confidence, 0.9, d.diagnosis)
                self.assertLess(d.confidence, 1.0)

    def test_every_finding_carries_numeric_evidence(self) -> None:
        for d in self.all_findings():
            self.assertTrue(d.evidence, d.diagnosis)
            self.assertTrue(any(ch.isdigit() for line in d.evidence for ch in line), d.diagnosis)
            self.assertTrue(d.suggested_fix.strip(), d.diagnosis)


class HelperTests(unittest.TestCase):
    def test_slope(self) -> None:
        samples = [Sample.from_snapshot(snap(T0 + i, cpu_temp=50.0 + 2 * i)) for i in range(10)]
        self.assertAlmostEqual(slope_per_s(samples, lambda s: s.cpu_temp) or 0.0, 2.0, places=6)
        self.assertIsNone(slope_per_s(samples[:3], lambda s: s.cpu_temp))

    def test_process_shares_treat_absence_as_zero_and_skip_empty_samples(self) -> None:
        samples = [
            Sample.from_snapshot(snap(T0 + 0, processes=[proc("a.exe", 10.0)])),
            Sample.from_snapshot(snap(T0 + 1, processes=[proc("a.exe", 20.0), proc("b.exe", 6.0)])),
            Sample.from_snapshot(snap(T0 + 2, processes=[])),  # sampler had not reported yet: ignored
        ]
        shares = {p.name: p.cpu_pct for p in process_shares(samples)}
        self.assertEqual(shares, {"a.exe": 15.0, "b.exe": 3.0})

    def test_fan_knowledge_roundtrip_and_corruption(self) -> None:
        k = FanKnowledge()
        for rpm in (2000, 3000, 6500):
            k.observe("CPU Fan", rpm)
        k2 = FanKnowledge.from_json(k.to_json())
        self.assertEqual((k2.min_rpm("CPU Fan"), k2.max_rpm("CPU Fan")), (2000.0, 6500.0))
        self.assertTrue(k2.trusted("CPU Fan", 0.3))
        self.assertFalse(FanKnowledge.from_json("{not json").trusted("CPU Fan", 0.3))
        self.assertFalse(FanKnowledge.from_json('{"CPU Fan": "oops"}').trusted("CPU Fan", 0.3))
        self.assertFalse(FanKnowledge.from_json(None).trusted("CPU Fan", 0.3))

    def test_constant_fan_is_never_trusted(self) -> None:
        k = FanKnowledge()
        for _ in range(50):
            k.observe("CPU Fan", 6500.0)
        self.assertFalse(k.trusted("CPU Fan", 0.3))


if __name__ == "__main__":
    unittest.main()
