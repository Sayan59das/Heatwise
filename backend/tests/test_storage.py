"""Storage tests: schema, mapping, recorder, prune, history queries, recovery and the HTTP API."""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

import uvicorn
from sqlalchemy import insert, select, text

from app.collectors.hub import SensorHub
from app.collectors.parsing import GpuExtras, LhmReading
from app.config import Settings
from app.db import recorder as recorder_mod
from app.db.database import Database
from app.db.history import UnknownMetric, choose_bucket_s, query_history, throttle_episodes
from app.db.mapping import snapshot_to_row
from app.db.recorder import Recorder
from app.db.schema import METRIC_NAMES, Diagnosis, Reading
from app.main import create_app
from app.models import (
    BatteryInfo,
    CoreReading,
    CpuSnapshot,
    FanReading,
    GpuSnapshot,
    PowerPlan,
    SensorHealth,
    Snapshot,
    SystemSnapshot,
    ThrottleComponent,
    ThrottleStatus,
)
from tests.test_hub import FakeCpu, FakeSystem

NOW = time.time()


def snap(ts: float, *, cpu: CpuSnapshot | None = None, gpu: GpuSnapshot | None = None,
         throttle: ThrottleStatus | None = None, fans: list[FanReading] | None = None,
         system: SystemSnapshot | None = None) -> Snapshot:
    return Snapshot(
        timestamp=ts, seq=1, interval_s=1.0, cpu=cpu or CpuSnapshot(), gpu=gpu,
        throttle=throttle or ThrottleStatus(), fans=fans or [], system=system or SystemSnapshot(),
        health=SensorHealth(is_admin=True),
    )


def tmp_db() -> tuple[tempfile.TemporaryDirectory[str], Database]:
    d = tempfile.TemporaryDirectory()
    return d, Database.open(Path(d.name) / "t.db")


def rows(db: Database) -> list[dict[str, Any]]:
    with db.engine.connect() as c:
        return [dict(r) for r in c.execute(text("SELECT * FROM readings ORDER BY ts_ms")).mappings()]


def put(db: Database, ts_s: float, **cols: Any) -> None:
    base = {"ts_ms": int(ts_s * 1000)}
    with db.engine.begin() as c:
        c.execute(insert(Reading), [{**base, **cols}])


class SchemaTests(unittest.TestCase):
    def test_tables_wal_and_idempotent_open(self) -> None:
        d, db = tmp_db()
        try:
            with db.engine.connect() as c:
                self.assertEqual(c.execute(text("PRAGMA journal_mode")).scalar(), "wal")
                names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
            self.assertTrue({"readings", "diagnoses", "settings"} <= names)
            db.close()
            db2 = Database.open(Path(d.name) / "t.db")  # reopening an existing file must be a no-op
            self.assertEqual(db2.get_setting("schema_version"), "1")
            db2.close()
        finally:
            d.cleanup()

    def test_every_metric_is_a_real_numeric_column(self) -> None:
        cols = set(Reading.__table__.c.keys())
        self.assertTrue(METRIC_NAMES <= cols, METRIC_NAMES - cols)

    def test_settings_roundtrip(self) -> None:
        d, db = tmp_db()
        try:
            self.assertIsNone(db.get_setting("x"))
            db.set_setting("x", "1")
            db.set_setting("x", "2")
            self.assertEqual(db.get_setting("x"), "2")
            db.close()
        finally:
            d.cleanup()


class MappingTests(unittest.TestCase):
    def test_full_snapshot(self) -> None:
        cores = [CoreReading(index=i, kind="P", clock_mhz=4000.0, load_pct=90.0) for i in range(1, 5)]
        s = snap(
            1_700_000_000.123,
            cpu=CpuSnapshot(package_temp_c=80.0, max_core_temp_c=78.0, total_load_pct=90.0, package_power_w=55.5,
                            pl1_w=55.0, pl2_w=157.0, avg_clock_mhz=3000.0, max_clock_mhz=5000.0, cores=cores),
            gpu=GpuSnapshot(temp_c=70.0, temp_hotspot_c=77.0, power_draw_w=60.0, fan_percent=None, fan_rpm=3100.0),
            fans=[FanReading(name="CPU Fan", rpm=2400.0), FanReading(name="GPU Fan", rpm=3100.0)],
            system=SystemSnapshot(memory_percent=60.0, battery=BatteryInfo(percent=80.0, plugged_in=True),
                                  power_plan=PowerPlan(kind="balanced")),
            throttle=ThrottleStatus(active=True, type="power", component="cpu", confidence="detected",
                                    reasons=["Package power limit PL1", "Package power limit PL2"]),
        )
        r = snapshot_to_row(s)
        self.assertEqual(r["ts_ms"], 1_700_000_000_123)
        self.assertEqual((r["cpu_pkg_temp"], r["cpu_pl1"], r["cpu_pl2"], r["cpu_active_clock"]), (80.0, 55.0, 157.0, 4000))
        self.assertEqual((r["gpu_hotspot"], r["gpu_fan_rpm"], r["cpu_fan_rpm"]), (77.0, 3100.0, 2400.0))
        self.assertEqual((r["on_ac"], r["battery_pct"], r["power_plan"]), (1, 80.0, "balanced"))
        self.assertEqual((r["throttle_active"], r["throttle_type"], r["throttle_component"],
                          r["throttle_confidence"]), (1, "power", "cpu", "detected"))
        self.assertEqual(r["throttle_reasons"], "Package power limit PL1; Package power limit PL2")

    def test_sparse_snapshot_is_all_none_not_error(self) -> None:
        r = snapshot_to_row(snap(1_700_000_000.0))
        self.assertEqual(r["ts_ms"], 1_700_000_000_000)
        self.assertTrue(all(v is None for k, v in r.items() if k != "ts_ms"), r)

    def test_stopped_fan_zero_rpm_is_preserved(self) -> None:
        r = snapshot_to_row(snap(1.0, gpu=GpuSnapshot(fan_rpm=0.0), fans=[FanReading(name="CPU Fan", rpm=0.0)]))
        self.assertEqual((r["gpu_fan_rpm"], r["cpu_fan_rpm"]), (0.0, 0.0))

    def test_unknown_throttle_is_null_not_zero(self) -> None:
        r = snapshot_to_row(snap(1.0, throttle=ThrottleStatus()))  # active=None, confidence="unavailable"
        self.assertIsNone(r["throttle_active"])
        self.assertIsNone(r["throttle_confidence"])

    def test_not_throttling_is_zero(self) -> None:
        t = ThrottleStatus(active=False, confidence="detected")
        self.assertEqual(snapshot_to_row(snap(1.0, throttle=t))["throttle_active"], 0)

    def test_first_non_gpu_fan_is_the_cpu_fan_fallback(self) -> None:
        fans = [FanReading(name="GPU Fan", rpm=1.0), FanReading(name="Fan #1", rpm=2000.0)]
        self.assertEqual(snapshot_to_row(snap(1.0, fans=fans))["cpu_fan_rpm"], 2000.0)


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = Database.open(Path(self.dir.name) / "r.db")

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def make_hub(self) -> SensorHub:
        return SensorHub(Settings(poll_interval_s=0.05), lhm=FakeCpu(), system=FakeSystem(), auto_detect=False)

    async def test_stream_is_recorded_without_duplicates_and_flushed_on_stop(self) -> None:
        hub = self.make_hub()
        rec = Recorder(self.db, hub, flush_interval_s=60.0)  # never auto-flushes: only stop() can persist
        await rec.start()
        await hub.start()
        await asyncio.sleep(1.0)
        seq = hub.latest.snapshot.seq  # type: ignore[union-attr]
        await hub.stop()
        await rec.stop()
        stored = rows(self.db)
        self.assertGreaterEqual(len(stored), 10)
        self.assertLessEqual(len(stored), seq)
        self.assertEqual(len({r["ts_ms"] for r in stored}), len(stored))
        self.assertEqual(rec.rows_written, len(stored))
        self.assertEqual(stored[0]["cpu_pkg_temp"], 51.0)  # FakeCpu: 50 + reads

    async def test_periodic_flush_while_running(self) -> None:
        hub = self.make_hub()
        rec = Recorder(self.db, hub, flush_interval_s=0.3)
        await rec.start()
        await hub.start()
        try:
            await asyncio.sleep(1.2)
            self.assertGreater(len(rows(self.db)), 0)  # written before stop()
        finally:
            await hub.stop()
            await rec.stop()

    async def test_failed_write_is_retried_and_nothing_is_lost(self) -> None:
        hub = self.make_hub()
        rec = Recorder(self.db, hub, flush_interval_s=0.2)
        real = rec._write_rows
        calls = {"n": 0}

        def flaky(batch: list[dict[str, Any]]) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk is busy")
            real(batch)

        rec._write_rows = flaky  # type: ignore[method-assign]
        await rec.start()
        await hub.start()
        await asyncio.sleep(1.0)
        seq = hub.latest.snapshot.seq  # type: ignore[union-attr]
        await hub.stop()
        await rec.stop()
        self.assertGreaterEqual(calls["n"], 2)
        stored = rows(self.db)
        # the first batch failed, was put back, and was written with the next one: contiguous, none missing
        self.assertEqual([r["cpu_pkg_temp"] for r in stored][:3], [51.0, 52.0, 53.0])
        self.assertGreaterEqual(len(stored), seq - 8)

    async def test_buffer_is_capped_and_oldest_dropped(self) -> None:
        hub = self.make_hub()
        with mock.patch.object(recorder_mod, "_MAX_BUFFER_ROWS", 5):
            rec = Recorder(self.db, hub, flush_interval_s=60.0)
        self.assertEqual(rec._buffer.maxlen, 5)
        for i in range(8):
            rec._buffer.append({"ts_ms": i})
        self.assertEqual([r["ts_ms"] for r in rec._buffer], [3, 4, 5, 6, 7])

    def test_prune_removes_only_expired_rows_and_diagnoses(self) -> None:
        rec = Recorder(self.db, self.make_hub(), retention_days=7)
        put(self.db, NOW - 8 * 86400, cpu_pkg_temp=1.0)  # expired
        put(self.db, NOW - 7 * 86400 - 5, cpu_pkg_temp=2.0)  # just expired
        put(self.db, NOW - 6.9 * 86400, cpu_pkg_temp=3.0)  # kept
        put(self.db, NOW - 10, cpu_pkg_temp=4.0)  # kept
        with self.db.engine.begin() as c:
            c.execute(insert(Diagnosis), [
                {"ts_ms": int((NOW - 9 * 86400) * 1000), "rule_id": "r", "diagnosis": "old", "severity": "info",
                 "confidence": 0.5, "evidence": "[]", "suggested_fix": ""},
                {"ts_ms": int((NOW - 60) * 1000), "rule_id": "r", "diagnosis": "new", "severity": "info",
                 "confidence": 0.5, "evidence": "[]", "suggested_fix": ""},
            ])
        self.assertEqual(rec.prune(), 3)
        self.assertEqual([r["cpu_pkg_temp"] for r in rows(self.db)], [3.0, 4.0])
        with self.db.engine.connect() as c:
            self.assertEqual([r.diagnosis for r in c.execute(select(Diagnosis))], ["new"])

    def test_prune_respects_a_shorter_retention(self) -> None:
        rec = Recorder(self.db, self.make_hub(), retention_days=1)
        put(self.db, NOW - 2 * 86400, cpu_pkg_temp=1.0)
        put(self.db, NOW - 3600, cpu_pkg_temp=2.0)
        rec.prune()
        self.assertEqual([r["cpu_pkg_temp"] for r in rows(self.db)], [2.0])

    def test_duplicate_timestamp_is_ignored_not_an_error(self) -> None:
        rec = Recorder(self.db, self.make_hub())
        row = snapshot_to_row(snap(1_700_000_000.0, cpu=CpuSnapshot(package_temp_c=50.0)))
        rec._write_rows([row])
        rec._write_rows([row])
        self.assertEqual(len(rows(self.db)), 1)


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir, self.db = tmp_db()
        self.t0 = 1_800_000_000  # whole-minute boundary: 1_800_000_000 % 60 == 0

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def fill(self, n: int = 3600, **per_second: Any) -> None:
        batch = [{"ts_ms": (self.t0 + i) * 1000, **{k: f(i) for k, f in per_second.items()}} for i in range(n)]
        with self.db.engine.begin() as c:
            c.execute(insert(Reading), batch)

    def test_bucket_ladder(self) -> None:
        self.assertEqual(choose_bucket_s(300, 600), 1)
        self.assertEqual(choose_bucket_s(3600, 600), 10)  # 6 s wanted -> next rung is 10
        self.assertEqual(choose_bucket_s(86400, 600), 300)
        self.assertEqual(choose_bucket_s(7 * 86400, 600), 1800)
        self.assertEqual(choose_bucket_s(10**9, 10), 86400)  # clamps at the top rung

    def test_downsampling_avg_min_max(self) -> None:
        self.fill(3600, cpu_pkg_temp=lambda i: 40.0 + i / 60.0)  # climbs 1 degree per minute
        r = query_history(self.db, ["cpu_pkg_temp"], self.t0 * 1000, (self.t0 + 3600) * 1000, max_points=60)
        self.assertEqual(r["bucket_s"], 60)
        self.assertEqual(len(r["t"]), 60)
        self.assertEqual(r["t"][0], self.t0)
        s = r["series"]["cpu_pkg_temp"]
        self.assertAlmostEqual(s["avg"][0], 40.0 + 29.5 / 60.0, places=2)
        self.assertEqual(s["min"][0], 40.0)
        self.assertAlmostEqual(s["max"][-1], 40.0 + 3599 / 60.0, places=2)
        self.assertEqual(r["samples"], 3600)

    def test_spikes_survive_downsampling_via_max(self) -> None:
        self.fill(600, cpu_pkg_temp=lambda i: 99.0 if i == 300 else 50.0)
        r = query_history(self.db, ["cpu_pkg_temp"], self.t0 * 1000, (self.t0 + 600) * 1000, max_points=10)
        s = r["series"]["cpu_pkg_temp"]
        self.assertLess(max(a for a in s["avg"] if a is not None), 60.0)  # the average hides the spike...
        self.assertEqual(max(m for m in s["max"] if m is not None), 99.0)  # ...the max keeps it

    def test_raw_resolution_for_short_ranges(self) -> None:
        self.fill(30, cpu_load=lambda i: float(i))
        r = query_history(self.db, ["cpu_load"], self.t0 * 1000, (self.t0 + 30) * 1000, max_points=600)
        self.assertEqual((r["bucket_s"], len(r["t"])), (1, 30))
        self.assertEqual(r["series"]["cpu_load"]["avg"][:3], [0.0, 1.0, 2.0])

    def test_nulls_are_ignored_by_aggregates_and_all_null_buckets_are_none(self) -> None:
        self.fill(120, gpu_temp=lambda i: None if i < 60 else 70.0)
        r = query_history(self.db, ["gpu_temp"], self.t0 * 1000, (self.t0 + 120) * 1000, max_points=2)
        self.assertEqual(r["series"]["gpu_temp"]["avg"], [None, 70.0])

    def test_multiple_metrics_and_empty_range(self) -> None:
        self.fill(10, cpu_pkg_temp=lambda i: 1.0, gpu_temp=lambda i: 2.0)
        r = query_history(self.db, ["cpu_pkg_temp", "gpu_temp"], self.t0 * 1000, (self.t0 + 10) * 1000)
        self.assertEqual(set(r["series"]), {"cpu_pkg_temp", "gpu_temp"})
        empty = query_history(self.db, ["cpu_pkg_temp"], (self.t0 + 9999) * 1000, (self.t0 + 10000) * 1000)
        self.assertEqual((empty["t"], empty["series"]["cpu_pkg_temp"]["avg"], empty["samples"]), ([], [], 0))

    def test_unknown_metric_and_injection_attempts_are_rejected(self) -> None:
        for bad in ("nope", "cpu_pkg_temp; DROP TABLE readings", "ts_ms", "readings.ts_ms", "1=1", ""):
            with self.assertRaises(UnknownMetric, msg=bad):
                query_history(self.db, [bad], 0, 1000)
        with self.db.engine.connect() as c:  # table still there
            self.assertEqual(c.execute(text("SELECT COUNT(*) FROM readings")).scalar(), 0)

    def test_inverted_range(self) -> None:
        with self.assertRaises(ValueError):
            query_history(self.db, ["cpu_load"], 2000, 1000)

    def test_throttle_episodes(self) -> None:
        def mark(i: int, typ: str, comp: str, conf: str = "detected", reason: str = "r") -> None:
            put(self.db, self.t0 + i, throttle_active=1, throttle_type=typ, throttle_component=comp,
                throttle_confidence=conf, throttle_reasons=reason)

        for i in range(0, 5):
            mark(i, "power", "cpu", reason="Package power limit PL1")
        mark(5, "power", "cpu", conf="inferred", reason="Package power limit PL2")  # same episode, taints it
        for i in range(100, 103):
            mark(i, "thermal", "gpu", reason="Hardware thermal slowdown")
        put(self.db, self.t0 + 50, throttle_active=0)  # not throttling: ignored
        eps = throttle_episodes(self.db, self.t0 * 1000, (self.t0 + 1000) * 1000)
        self.assertEqual(len(eps), 2)
        a, b = eps
        self.assertEqual((a["type"], a["component"], a["samples"]), ("power", "cpu", 6))
        self.assertEqual(a["confidence"], "inferred")  # one inferred sample => never reported as detected
        self.assertEqual(a["reasons"], ["Package power limit PL1", "Package power limit PL2"])
        self.assertEqual((b["type"], b["component"], b["confidence"]), ("thermal", "gpu", "detected"))
        self.assertEqual(b["start"], self.t0 + 100)

    def test_type_change_splits_an_episode_and_a_gap_does_too(self) -> None:
        put(self.db, self.t0 + 0, throttle_active=1, throttle_type="power", throttle_component="cpu", throttle_confidence="detected")
        put(self.db, self.t0 + 1, throttle_active=1, throttle_type="thermal", throttle_component="cpu", throttle_confidence="detected")
        put(self.db, self.t0 + 10, throttle_active=1, throttle_type="thermal", throttle_component="cpu", throttle_confidence="detected")
        eps = throttle_episodes(self.db, self.t0 * 1000, (self.t0 + 100) * 1000)
        self.assertEqual([e["type"] for e in eps], ["power", "thermal", "thermal"])


class RecoveryTests(unittest.TestCase):
    def test_corrupt_database_is_quarantined_and_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "broken.db"
            path.write_bytes(b"this is definitely not a sqlite database" * 100)
            db = Database.open(path)
            try:
                put(db, NOW, cpu_pkg_temp=1.0)  # the replacement is fully usable
                self.assertEqual(len(rows(db)), 1)
            finally:
                db.close()
            self.assertTrue(any(".corrupt-" in p.name for p in Path(d).iterdir()))


# ---- the whole stack over real HTTP -------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ServerThread:
    def __init__(self, settings: Settings, hub: SensorHub) -> None:
        self.port = free_port()
        app = create_app(settings, hub=hub)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        app.state.server = self.server  # what run.py does, so the control endpoint can stop it
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> ServerThread:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(15)

    def get(self, path: str) -> tuple[int, Any]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class ApiTests(unittest.TestCase):
    def test_history_api_end_to_end_and_flush_on_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            settings = Settings(poll_interval_s=0.05, data_dir=Path(d), flush_interval_s=0.5, retention_days=7)
            hub = SensorHub(settings, lhm=FakeCpu(), system=FakeSystem(), auto_detect=False)
            with ServerThread(settings, hub) as srv:
                time.sleep(2.0)
                code, health = srv.get("/api/health")
                self.assertEqual(code, 200)
                self.assertTrue(health["storage"]["enabled"])
                self.assertGreater(health["storage"]["rows_written"], 0)

                code, cat = srv.get("/api/history/metrics")
                self.assertEqual(code, 200)
                self.assertIn("cpu_pkg_temp", {m["name"] for m in cat})

                now = time.time()
                code, h = srv.get(f"/api/history?from={now - 60}&to={now + 5}&metric=cpu_pkg_temp,cpu_load&metric=gpu_temp")
                self.assertEqual(code, 200, h)
                self.assertEqual(h["metrics"], ["cpu_pkg_temp", "cpu_load", "gpu_temp"])
                self.assertGreater(h["samples"], 5)
                self.assertTrue(all(v is not None for v in h["series"]["cpu_pkg_temp"]["avg"]))
                self.assertTrue(all(v is None for v in h["series"]["gpu_temp"]["avg"]))  # no GPU in this fake rig
                self.assertLessEqual(len(h["t"]), 600)

                # JS-style millisecond timestamps and ISO strings are accepted too
                code, h2 = srv.get(f"/api/history?from={int((now - 60) * 1000)}&metric=cpu_pkg_temp")
                self.assertEqual(code, 200, h2)
                iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 30))
                self.assertEqual(srv.get(f"/api/history?from={iso}&metric=cpu_pkg_temp")[0], 200)

                # defaults: last hour, two temperature metrics
                code, h3 = srv.get("/api/history")
                self.assertEqual((code, h3["metrics"]), (200, ["cpu_pkg_temp", "gpu_temp"]))

                # errors are explicit
                code, err = srv.get("/api/history?metric=bogus")
                self.assertEqual(code, 422)
                self.assertIn("cpu_pkg_temp", err["detail"]["available"])
                self.assertEqual(srv.get("/api/history?from=banana")[0], 400)
                self.assertEqual(srv.get(f"/api/history?from={now}&to={now - 10}")[0], 400)
                self.assertEqual(srv.get("/api/history?metric=cpu_load&max_points=3")[0], 422)  # below the minimum

                # asking for more than the retention window is clamped, and says so
                code, old = srv.get(f"/api/history?from={now - 30 * 86400}&metric=cpu_load&max_points=50")
                self.assertEqual(code, 200)
                self.assertTrue(old["clamped_to_retention"])
                self.assertEqual(srv.get("/api/history/throttle")[1]["episodes"], [])

                before_stop = hub.latest.snapshot.seq  # type: ignore[union-attr]

            # after shutdown every sample up to the last flush interval must have been persisted
            db = Database.open(Path(d) / "thermalsense.db")
            try:
                n = len(rows(db))
                self.assertGreaterEqual(n, before_stop - 12)  # at most the final in-flight ticks
            finally:
                db.close()

    def test_backend_keeps_serving_live_data_when_storage_cannot_open(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "not_a_dir"
            blocker.write_text("a file where the data directory should be")
            settings = Settings(poll_interval_s=0.05, data_dir=blocker, flush_interval_s=0.5)
            hub = SensorHub(settings, lhm=FakeCpu(), system=FakeSystem(), auto_detect=False)
            with ServerThread(settings, hub) as srv:
                time.sleep(0.8)
                self.assertEqual(srv.get("/api/snapshot")[0], 200)  # live data unaffected
                code, health = srv.get("/api/health")
                self.assertFalse(health["storage"]["enabled"])
                code, err = srv.get("/api/history")
                self.assertEqual(code, 503)
                self.assertIn("unavailable", err["detail"])


if __name__ == "__main__":
    unittest.main()
