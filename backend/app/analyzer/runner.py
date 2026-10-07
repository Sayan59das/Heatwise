"""DiagnosisRunner: connects the engine to the hub's snapshot stream and to storage.

Runs on the event loop (rule evaluation is a few milliseconds over <=180 samples); every database call
goes through a dedicated single-thread executor so a slow disk can never stall the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.analyzer.engine import DiagnosisEngine, EngineEvent
from app.analyzer.fans import FanKnowledge
from app.collectors.hub import Published, SensorHub
from app.db.database import Database
from app.db.diagnoses import DiagnosisStore

log = logging.getLogger(__name__)

_FAN_KNOWLEDGE_KEY = "fan_knowledge"
_FAN_SAVE_EVERY_S = 60.0


class DiagnosisRunner:
    def __init__(self, hub: SensorHub, engine: DiagnosisEngine | None = None, db: Database | None = None) -> None:
        self._hub = hub
        self.engine = engine or DiagnosisEngine()
        self._db = db
        self._store = DiagnosisStore(db) if db is not None else None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diagnosis-db")
        self._queue: asyncio.Queue[Published] | None = None
        self._task: asyncio.Task[None] | None = None
        self._open_rows: dict[str, int] = {}  # diagnosis key -> database row id
        self._last_ts = 0.0

    # ---- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        self._queue = self._hub.subscribe(maxsize=64)
        if self._db is not None:
            await self._db_call(self._load_state)
        self._task = asyncio.create_task(self._run(), name="diagnosis-runner")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._queue is not None:
            self._hub.unsubscribe(self._queue)
        if self._db is not None:
            await self._db_call(self._save_state)
        self._executor.shutdown(wait=True)

    # ---- main loop -------------------------------------------------------------------------------

    async def _run(self) -> None:
        assert self._queue is not None
        interval = self.engine.th.eval_interval_s
        last_eval = time.monotonic()
        last_fan_save = time.monotonic()
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=interval)
                self.engine.ingest(item.snapshot)
                self._last_ts = item.snapshot.timestamp
            except asyncio.TimeoutError:
                pass  # no data (collectors stalled): still evaluate so staleness is visible
            now = time.monotonic()
            if now - last_eval >= interval:
                last_eval = now
                try:
                    self.engine.evaluate(self._last_ts or None)
                except Exception:  # noqa: BLE001 - never let diagnosis kill the loop
                    log.exception("diagnosis evaluation failed")
                events = self.engine.drain_events()
                if events and self._store is not None:
                    await self._db_call(self._persist, events)
            if self._db is not None and now - last_fan_save >= _FAN_SAVE_EVERY_S:
                last_fan_save = now
                if self.engine.fans.dirty:
                    await self._db_call(self._save_fans)

    async def _db_call(self, fn: Any, *args: Any) -> Any:
        try:
            return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)
        except Exception as exc:  # noqa: BLE001 - history is best-effort; the live report must keep working
            log.error("diagnosis storage call %s failed: %s", getattr(fn, "__name__", fn), exc)
            return None

    # ---- executor-thread side --------------------------------------------------------------------

    def _load_state(self) -> None:
        assert self._db is not None and self._store is not None
        self.engine.fans = FanKnowledge.from_json(self._db.get_setting(_FAN_KNOWLEDGE_KEY))
        closed = self._store.close_stale()
        if closed:
            log.info("closed %d diagnosis rows left open by the previous run", closed)

    def _save_fans(self) -> None:
        assert self._db is not None
        self._db.set_setting(_FAN_KNOWLEDGE_KEY, self.engine.fans.to_json())
        self.engine.fans.dirty = False

    def _save_state(self) -> None:
        """Shutdown: end every open diagnosis row and persist what we learned about the fans."""
        assert self._store is not None
        ended_at = self._last_ts or time.time()
        for row_id in self._open_rows.values():
            self._store.end(row_id, ended_at)
        self._open_rows.clear()
        if self.engine.fans.dirty:
            self._save_fans()

    def _persist(self, events: list[EngineEvent]) -> None:
        assert self._store is not None
        window_s = self.engine.window.duration_s
        for ev in events:
            key = ev.diagnosis.key
            if ev.kind == "confirmed":
                self._open_rows[key] = self._store.save(ev.diagnosis, window_s)
            elif ev.kind == "changed":
                old = self._open_rows.pop(key, None)
                if old is not None:
                    self._store.end(old, ev.at)
                self._open_rows[key] = self._store.save(ev.diagnosis, window_s)
            elif ev.kind == "resolved":
                row = self._open_rows.pop(key, None)
                if row is not None:
                    self._store.end(row, ev.diagnosis.last_seen)
