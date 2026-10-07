"""Parsing tests. Run from the backend folder: ``python -m unittest discover -s tests -t .``"""

from __future__ import annotations

import math
import unittest

from app.collectors.parsing import RawSensor, build_cpu_snapshot, build_fans
from app.collectors.system import parse_powercfg


def _s(hw_type: str, hw_name: str, sensor_type: str, name: str, value: float | None, ident: str) -> RawSensor:
    return RawSensor(hw_name, hw_type, sensor_type, name, value, ident)


def hybrid_intel(*, driver: bool = True, p_cores: int = 8, e_cores: int = 12) -> list[RawSensor]:
    """Sensor names copied from a real i7-14700HX dump (8 P-cores with HT + 12 E-cores)."""
    name, root = "Intel Core i7-14700HX", "/intelcpu/0"
    out: list[RawSensor] = []

    def temp_or_none(v: float) -> float | None:
        return v if driver else None

    n = 0
    for kind, count in (("P", p_cores), ("E", e_cores)):
        for i in range(1, count + 1):
            base = 60.0 + i + (5 if kind == "P" else 0)
            out.append(_s("Cpu", name, "Temperature", f"{kind}-Core #{i}", temp_or_none(base), f"{root}/temperature/{n}"))
            out.append(_s("Cpu", name, "Temperature", f"{kind}-Core #{i} Distance to TjMax",
                          temp_or_none(100 - base), f"{root}/temperature/d{n}"))
            out.append(_s("Cpu", name, "Clock", f"{kind}-Core #{i}", temp_or_none(4000.0 if kind == "P" else 3000.0),
                          f"{root}/clock/{n}"))
            n += 1
    out.append(_s("Cpu", name, "Temperature", "Core Max", temp_or_none(75.0), f"{root}/temperature/0"))
    out.append(_s("Cpu", name, "Temperature", "Core Average", temp_or_none(70.0), f"{root}/temperature/1"))
    out.append(_s("Cpu", name, "Temperature", "CPU Package", temp_or_none(76.0), f"{root}/temperature/22"))

    # Loads: one global numbering, P-cores first (two threads each), then single-thread E-cores.
    out.append(_s("Cpu", name, "Load", "CPU Total", 19.0, f"{root}/load/0"))
    out.append(_s("Cpu", name, "Load", "CPU Core Max", 72.7, f"{root}/load/1"))
    core = 0
    for _ in range(p_cores):
        core += 1
        for t in (1, 2):
            out.append(_s("Cpu", name, "Load", f"CPU Core #{core} Thread #{t}", 10.0 if t == 1 else 30.0, f"{root}/load/{core}{t}"))
    for _ in range(e_cores):
        core += 1
        out.append(_s("Cpu", name, "Load", f"CPU Core #{core}", 50.0, f"{root}/load/{core}"))

    # Without the driver LHM reports exactly 0.00 W for every power sensor.
    pw = (lambda x: x) if driver else (lambda _x: 0.0)
    out.append(_s("Cpu", name, "Power", "CPU Package", pw(45.5), f"{root}/power/0"))
    out.append(_s("Cpu", name, "Power", "CPU Cores", pw(38.2), f"{root}/power/1"))
    out.append(_s("Cpu", name, "Power", "CPU Memory", pw(0.4), f"{root}/power/3"))
    return out


class HybridIntelTests(unittest.TestCase):
    def test_core_counts_and_kinds(self) -> None:
        cpu = build_cpu_snapshot(hybrid_intel())
        self.assertEqual(cpu.vendor, "intel")
        self.assertEqual(len(cpu.cores), 20)
        self.assertEqual([c.kind for c in cpu.cores], ["P"] * 8 + ["E"] * 12)
        self.assertEqual([c.index for c in cpu.cores], list(range(1, 21)))  # unique ordinals
        self.assertEqual(cpu.cores[0].label, "P-Core 1")
        self.assertEqual(cpu.cores[8].label, "E-Core 1")  # numbering restarts per kind

    def test_p_and_e_core_one_do_not_collide(self) -> None:
        cpu = build_cpu_snapshot(hybrid_intel())
        p1, e1 = cpu.cores[0], cpu.cores[8]
        self.assertEqual(p1.temp_c, 66.0)  # 60 + 1 + 5
        self.assertEqual(e1.temp_c, 61.0)  # 60 + 1
        self.assertEqual(p1.clock_mhz, 4000.0)
        self.assertEqual(e1.clock_mhz, 3000.0)

    def test_loads_mapped_positionally_and_threads_averaged(self) -> None:
        cpu = build_cpu_snapshot(hybrid_intel())
        self.assertEqual(cpu.cores[0].load_pct, 20.0)  # mean of the two threads (10, 30)
        self.assertEqual(cpu.cores[8].load_pct, 50.0)  # first E-core = global core #9
        self.assertEqual(cpu.total_load_pct, 19.0)

    def test_aggregates_and_tjmax(self) -> None:
        cpu = build_cpu_snapshot(hybrid_intel())
        self.assertEqual(cpu.package_temp_c, 76.0)
        self.assertEqual(cpu.max_core_temp_c, 73.0)  # P-Core #8 = 60 + 8 + 5
        self.assertEqual(cpu.tjmax_c, 100.0)
        self.assertEqual(cpu.package_power_w, 45.5)
        self.assertEqual(cpu.cores_power_w, 38.2)
        self.assertEqual(cpu.max_clock_mhz, 4000.0)

    def test_without_driver_everything_hardware_backed_is_none(self) -> None:
        cpu = build_cpu_snapshot(hybrid_intel(driver=False))
        self.assertIsNone(cpu.package_temp_c)
        self.assertIsNone(cpu.max_core_temp_c)
        self.assertIsNone(cpu.avg_clock_mhz)
        self.assertIsNone(cpu.tjmax_c)
        self.assertIsNone(cpu.package_power_w, "0.00 W from an unreadable counter must not be reported")
        self.assertTrue(all(c.temp_c is None for c in cpu.cores))
        # ...but load needs no driver and must still come through.
        self.assertEqual(cpu.total_load_pct, 19.0)
        self.assertEqual(cpu.cores[0].load_pct, 20.0)

    def test_mismatched_hybrid_counts_yield_no_per_core_load(self) -> None:
        sensors = [s for s in hybrid_intel() if not s.name.startswith("CPU Core #20")]
        cpu = build_cpu_snapshot(sensors)
        self.assertEqual(len(cpu.cores), 20)
        self.assertTrue(all(c.load_pct is None for c in cpu.cores))


class AmdTests(unittest.TestCase):
    def test_amd_package_fallback_and_effective_clocks(self) -> None:
        root = "/amdcpu/0"
        sensors = [
            _s("Cpu", "AMD Ryzen 7 7840HS", "Temperature", "Core (Tctl/Tdie)", 71.0, f"{root}/temperature/2"),
            _s("Cpu", "AMD Ryzen 7 7840HS", "Temperature", "CCD1 (Tdie)", 68.0, f"{root}/temperature/3"),
            _s("Cpu", "AMD Ryzen 7 7840HS", "Clock", "Core #1 (Effective)", 4400.4, f"{root}/clock/10"),
            _s("Cpu", "AMD Ryzen 7 7840HS", "Clock", "Core #1", 4500.0, f"{root}/clock/1"),
            _s("Cpu", "AMD Ryzen 7 7840HS", "Clock", "Core #2 (Effective)", 4300.0, f"{root}/clock/11"),
            _s("Cpu", "AMD Ryzen 7 7840HS", "Load", "CPU Total", 33.3, f"{root}/load/0"),
            _s("Cpu", "AMD Ryzen 7 7840HS", "Load", "CPU Core #1", 40.0, f"{root}/load/1"),
            _s("Cpu", "AMD Ryzen 7 7840HS", "Load", "CPU Core #2", 20.0, f"{root}/load/2"),
            _s("Cpu", "AMD Ryzen 7 7840HS", "Power", "Package", 28.0, f"{root}/power/0"),
        ]
        cpu = build_cpu_snapshot(sensors)
        self.assertEqual(cpu.vendor, "amd")
        self.assertEqual(cpu.package_temp_c, 71.0)
        self.assertEqual(cpu.max_core_temp_c, 71.0)  # no per-core temps -> falls back to the package value
        self.assertEqual(cpu.package_power_w, 28.0)
        self.assertEqual([c.kind for c in cpu.cores], [None, None])
        self.assertEqual(cpu.cores[0].clock_mhz, 4500.0)  # plain clock preferred
        self.assertEqual(cpu.cores[1].clock_mhz, 4300.0)  # effective used when plain is absent
        self.assertEqual([c.load_pct for c in cpu.cores], [40.0, 20.0])
        self.assertIsNone(cpu.tjmax_c)


class RobustnessTests(unittest.TestCase):
    def test_no_cpu_hardware(self) -> None:
        cpu = build_cpu_snapshot([])
        self.assertIsNone(cpu.name)
        self.assertEqual(cpu.cores, [])

    def test_nan_and_inf_become_none(self) -> None:
        root = "/intelcpu/0"
        sensors = [
            _s("Cpu", "Intel X", "Temperature", "CPU Package", math.nan, f"{root}/temperature/0"),
            _s("Cpu", "Intel X", "Temperature", "Core #1", math.inf, f"{root}/temperature/1"),
            _s("Cpu", "Intel X", "Load", "CPU Total", -math.inf, f"{root}/load/0"),
        ]
        cpu = build_cpu_snapshot(sensors)
        self.assertIsNone(cpu.package_temp_c)
        self.assertIsNone(cpu.cores[0].temp_c)
        self.assertIsNone(cpu.total_load_pct)

    def test_only_first_cpu_is_used(self) -> None:
        sensors = [
            _s("Cpu", "Intel A", "Temperature", "CPU Package", 50.0, "/intelcpu/0/temperature/0"),
            _s("Cpu", "Intel B", "Temperature", "CPU Package", 99.0, "/intelcpu/1/temperature/0"),
        ]
        self.assertEqual(build_cpu_snapshot(sensors).package_temp_c, 50.0)


class FanTests(unittest.TestCase):
    def test_fans_paired_with_controls_and_gpu_excluded(self) -> None:
        sensors = [
            _s("SuperIO", "ITE IT8688E", "Fan", "Fan #1", 2100.4, "/lpc/it8688e/0/fan/0"),
            _s("SuperIO", "ITE IT8688E", "Control", "Fan Control #1", 55.0, "/lpc/it8688e/0/control/0"),
            _s("SuperIO", "ITE IT8688E", "Fan", "Fan #2", 0.0, "/lpc/it8688e/0/fan/1"),
            _s("GpuNvidia", "RTX", "Fan", "GPU Fan", 1800.0, "/gpu-nvidia/0/fan/1"),
        ]
        fans = build_fans(sensors)
        self.assertEqual([f.name for f in fans], ["Fan #1", "Fan #2"])
        self.assertEqual(fans[0].rpm, 2100.0)
        self.assertEqual(fans[0].percent, 55.0)
        self.assertIsNone(fans[1].percent)
        self.assertEqual(fans[1].rpm, 0.0)  # a stopped fan is real information, not "missing"

    def test_no_fans(self) -> None:
        self.assertEqual(build_fans(hybrid_intel()), [])


class PowercfgTests(unittest.TestCase):
    def test_english(self) -> None:
        plan = parse_powercfg("Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)")
        assert plan is not None
        self.assertEqual((plan.name, plan.kind), ("Balanced", "balanced"))

    def test_localised_label_still_parses_by_guid(self) -> None:
        plan = parse_powercfg("GUID du modèle de gestion de l'alimentation : a1841308-3541-4fab-bc81-f71556f20b4a  (Économie d'énergie)")
        assert plan is not None
        self.assertEqual(plan.kind, "power_saver")

    def test_unknown_guid_is_other_and_garbage_is_none(self) -> None:
        plan = parse_powercfg("Power Scheme GUID: 11111111-2222-3333-4444-555555555555  (Vendor Boost)")
        assert plan is not None
        self.assertEqual(plan.kind, "other")
        self.assertIsNone(parse_powercfg("nothing here"))


if __name__ == "__main__":
    unittest.main()
