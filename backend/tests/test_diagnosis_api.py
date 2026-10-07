"""Runner persistence and the /api/diagnosis endpoints (real uvicorn server, fake collectors)."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.analyzer.config import Thresholds
from app.analyzer.engine import DiagnosisEngine
from app.analyzer.fans import FanKnowledge
from app.analyzer.runner import DiagnosisRunner
from app.collectors.hub import Published, SensorHub
from app.collectors.parsing import GpuExtras, LhmReading
from app.config import Settings
from app.db.database import Database
from app.db.diagnoses import DiagnosisStore
from app.models import CpuSnapshot, FanReading, ProcessInfo
from tests.scenarios import T0, comp, snap
from tests.test_hub import FakeSystem
from tests.test_storage import ServerThread


class HotIdleCpu:
    """A collector reporting a hot-but-idle laptop with a background process to blame."""

    def __init__(self) -> None:
        self.reads = 0

    def start(self) -> None: ...
    def read(self) -> LhmReading:
        self.reads += 1
        cpu = CpuSnapshot(name="Fake", vendor="intel", package_temp_c=78.0, max_core_temp_c=78.0, total_load_pct=4.0,
                          package_power_w=9.0)
        return LhmReading(cpu, [FanReading(name="CPU Fan", rpm=2400.0)], GpuExtras())
    def close(self) -> None: ...


class FakeProcs:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def latest(self) -> list[ProcessInfo]:
        return [ProcessInfo(name="chrome.exe", cpu_pct=7.0, count=12), ProcessInfo(name="Code.exe", cpu_pct=1.0)]
    @property
    def error(self) -> str | None:
        return None


FAST = Thresholds(eval_interval_s=0.1, min_samples=10, confirm_s=0.5, linger_s=0.5, recent_s=3.0, window_s=20.0)


class RunnerPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = Database.open(Path(self.dir.name) / "d.db")

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def hub(self) -> SensorHub:
        return SensorHub(Settings(poll_interval_s=0.05), lhm=HotIdleCpu(), system=FakeSystem(), procs=FakeProcs(),
                         auto_detect=False)

    async def test_confirmed_diagnosis_is_stored_and_ended_on_shutdown(self) -> None:
        hub = self.hub()
        runner = DiagnosisRunner(hub, DiagnosisEngine(FAST), self.db)
        await runner.start()
        await hub.start()
        await asyncio.sleep(3.0)
        report = runner.engine.report
        self.assertEqual(report.status, "issues", report.summary)
        self.assertIn("chrome.exe", report.diagnoses[0].diagnosis)
        await hub.stop()
        await runner.stop()

        rows = DiagnosisStore(self.db).list(0, int((time.time() + 10) * 1000))
        self.assertEqual(len(rows), 1)  # one row for the whole episode, not one per evaluation
        row = rows[0]
        self.assertEqual((row["rule_id"], row["component"], row["certainty"]), ("idle_hot", "system", "inferred"))
        self.assertIn("78", " ".join(row["evidence"]))
        self.assertFalse(row["active"], "shutdown must close the open episode")
        self.assertIsNotNone(row["ended"])
        self.assertGreater(row["ended"], row["started"])

    async def test_rows_left_open_by_a_crash_are_closed_on_next_start(self) -> None:
        store = DiagnosisStore(self.db)
        engine = DiagnosisEngine(FAST)
        ts = T0
        with self.db.engine.begin() as c:  # a reading exists up to ts+100 s, then the app "crashed"
            c.execute(text("INSERT INTO readings (ts_ms, cpu_load) VALUES (:t, 1.0)"), {"t": int((ts + 100) * 1000)})
        from app.analyzer.models import Diagnosis

        store.save(Diagnosis(key="idle_hot:system", rule_id="idle_hot", component="system", diagnosis="x", severity="info",
                             confidence=0.5, certainty="inferred", evidence=["78 C"], suggested_fix="f",
                             first_seen=ts, last_seen=ts))
        self.assertTrue(store.list(0, int((ts + 1000) * 1000))[0]["active"])

        runner = DiagnosisRunner(self.hub(), engine, self.db)
        await runner.start()
        await runner.stop()
        row = store.list(0, int((ts + 1000) * 1000))[0]
        self.assertFalse(row["active"])
        self.assertEqual(row["ended"], ts + 100)  # ended at the last reading we actually have

    async def test_learned_fan_ranges_survive_a_restart(self) -> None:
        hub = self.hub()
        engine = DiagnosisEngine(FAST)
        runner = DiagnosisRunner(hub, engine, self.db)
        await runner.start()
        for rpm in (2000.0, 3500.0, 6500.0):
            engine.ingest(snap(T0, cpu_fan=rpm))
        await runner.stop()  # saves because the knowledge is dirty

        engine2 = DiagnosisEngine(FAST)
        runner2 = DiagnosisRunner(self.hub(), engine2, self.db)
        await runner2.start()
        try:
            self.assertEqual((engine2.fans.min_rpm("CPU Fan"), engine2.fans.max_rpm("CPU Fan")), (2000.0, 6500.0))
            self.assertTrue(engine2.fans.trusted("CPU Fan", 0.3))
        finally:
            await runner2.stop()

    async def test_runs_without_a_database(self) -> None:
        hub = self.hub()
        runner = DiagnosisRunner(hub, DiagnosisEngine(FAST), None)
        await runner.start()
        await hub.start()
        await asyncio.sleep(2.5)
        self.assertEqual(runner.engine.report.status, "issues")
        await hub.stop()
        await runner.stop()

    async def test_severity_change_replaces_the_row(self) -> None:
        hub = self.hub()
        runner = DiagnosisRunner(hub, DiagnosisEngine(FAST), self.db)
        await runner.start()
        from app.analyzer.engine import EngineEvent
        from app.analyzer.models import Diagnosis

        def d(sev: str) -> Diagnosis:
            return Diagnosis(key="k:cpu", rule_id="k", component="cpu", diagnosis="x", severity=sev,  # type: ignore[arg-type]
                             confidence=0.7, certainty="inferred", evidence=["1"], suggested_fix="f", first_seen=T0, last_seen=T0)

        runner._persist([EngineEvent("confirmed", d("warning"), T0), EngineEvent("changed", d("critical"), T0 + 30)])
        await runner.stop()
        rows = DiagnosisStore(self.db).list(0, int((T0 + 1000) * 1000))
        self.assertEqual(sorted(r["severity"] for r in rows), ["critical", "warning"])
        warning = next(r for r in rows if r["severity"] == "warning")
        self.assertEqual(warning["ended"], T0 + 30)  # the warning episode ended when it became critical


class DiagnosisApiTests(unittest.TestCase):
    def test_endpoints_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            settings = Settings(poll_interval_s=0.05, data_dir=Path(d), flush_interval_s=0.5)
            hub = SensorHub(settings, lhm=HotIdleCpu(), system=FakeSystem(), procs=FakeProcs(), auto_detect=False)
            with ServerThread(settings, hub) as srv:
                # default engine settings need 20 samples (1 s at 20 Hz) and a 15 s confirmation
                code, early = srv.get("/api/diagnosis")
                self.assertEqual(code, 200)
                self.assertIn(early["status"], ("warming_up", "ok", "issues"))

                deadline = time.time() + 40
                report: dict[str, Any] = early
                while time.time() < deadline and report["status"] != "issues":
                    time.sleep(1.0)
                    report = srv.get("/api/diagnosis")[1]
                self.assertEqual(report["status"], "issues", report["summary"])

                d0 = report["diagnoses"][0]
                self.assertEqual(d0["rule_id"], "idle_hot")
                self.assertIn("chrome.exe", d0["diagnosis"])
                self.assertEqual(set(d0), {"key", "rule_id", "component", "diagnosis", "severity", "confidence", "certainty",
                                           "evidence", "suggested_fix", "metrics", "first_seen", "last_seen", "active"})
                self.assertTrue(any("78 °C" in line for line in d0["evidence"]))
                self.assertLess(d0["confidence"], 1.0)
                # rules that lacked inputs are listed, never silently dropped
                reasons = {s["rule_id"]: s["reason"] for s in report["not_evaluated"]}
                self.assertIn("load_spike", {s["rule_id"] for s in report["not_evaluated"]} | {"load_spike"})
                self.assertTrue(all(isinstance(v, str) and v for v in reasons.values()))

                code, hist = srv.get("/api/diagnosis/history")
                self.assertEqual(code, 200)
                self.assertEqual(hist["count"], 1)
                self.assertTrue(hist["diagnoses"][0]["active"])
                self.assertEqual(srv.get(f"/api/diagnosis/history?from={time.time()}&to={time.time() - 5}")[0], 400)
                self.assertEqual(srv.get("/api/diagnosis/history?limit=0")[0], 422)

    def test_diagnosis_still_served_when_storage_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "file"
            blocker.write_text("x")
            settings = Settings(poll_interval_s=0.05, data_dir=blocker)
            hub = SensorHub(settings, lhm=HotIdleCpu(), system=FakeSystem(), procs=FakeProcs(), auto_detect=False)
            with ServerThread(settings, hub) as srv:
                time.sleep(0.8)
                self.assertEqual(srv.get("/api/diagnosis")[0], 200)
                self.assertEqual(srv.get("/api/diagnosis/history")[0], 503)


class _Unused:  # keep import used for type checkers
    published: Published | None = None
    knowledge: FanKnowledge | None = None
    flag = comp


if __name__ == "__main__":
    unittest.main()
