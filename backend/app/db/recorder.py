"""Recorder: persists the hub's snapshot stream to SQLite in batches and prunes old data.

* Subscribes to the hub with a large queue, so a slow disk never costs samples.
* Inserts are batched (default every 5 s) and run on a dedicated thread, never on the event loop.
* A failed flush keeps the rows and retries with the next batch; the buffer is capped so a dead disk
  cannot grow memory without bound (oldest rows are dropped first).
* On shutdown the remaining buffer is flushed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import delete, insert

from app.collectors.hub import SensorHub
from app.db.database import Database
from app.db.mapping import snapshot_to_row
from app.db.schema import Diagnosis, Reading

log = logging.getLogger(__name__)

_MAX_BUFFER_ROWS = 3600  # ~1 hour at 1 Hz
_PRUNE_EVERY_S = 3600.0


class Recorder:
    def __init__(
        self,
        db: Database,
        hub: SensorHub,
        *,
        flush_interval_s: float = 5.0,
        retention_days: int = 7,
    ) -> None:
        self._db = db
        self._hub = hub
        self._flush_interval_s = flush_interval_s
        self.retention_days = retention_days
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
        self._buffer: deque[dict[str, Any]] = deque(maxlen=_MAX_BUFFER_ROWS)
        self._queue: asyncio.Queue[Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self.rows_written = 0
        self.rows_dropped = 0

    # ---- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        self._queue = self._hub.subscribe(maxsize=256)
        await self._run_in_writer(self.prune)  # clear anything that aged out while the app was closed
        self._task = asyncio.create_task(self._run(), name="db-recorder")

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
            while not self._queue.empty():  # samples that arrived after the last loop iteration
                self._buffer.append(snapshot_to_row(self._queue.get_nowait().snapshot))
        await self._flush()
        self._executor.shutdown(wait=True)

    # ---- internals -------------------------------------------------------------------------------

    async def _run_in_writer(self, fn: Any, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    async def _run(self) -> None:
        assert self._queue is not None
        last_flush = time.monotonic()
        last_prune = time.monotonic()
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval_s)
                if len(self._buffer) == self._buffer.maxlen:
                    self.rows_dropped += 1  # deque drops the oldest on append; count it
                self._buffer.append(snapshot_to_row(item.snapshot))
            except asyncio.TimeoutError:
                pass  # no sample arrived (collector stalled); still honour the flush schedule below
            now = time.monotonic()
            if self._buffer and now - last_flush >= self._flush_interval_s:
                await self._flush()
                last_flush = now
            if now - last_prune >= _PRUNE_EVERY_S:
                await self._run_in_writer(self.prune)
                last_prune = now

    async def _flush(self) -> None:
        if not self._buffer:
            return
        rows = list(self._buffer)
        self._buffer.clear()
        try:
            await self._run_in_writer(self._write_rows, rows)
        except Exception as exc:  # noqa: BLE001 - disk full / locked / removed: keep running, retry next batch
            log.error("could not write %d readings (%s); will retry", len(rows), exc)
            for row in reversed(rows):  # put them back in front of anything newer
                self._buffer.appendleft(row)

    # ---- writer-thread side ----------------------------------------------------------------------

    def _write_rows(self, rows: list[dict[str, Any]]) -> None:
        with self._db.engine.begin() as conn:
            # OR IGNORE: a duplicate millisecond (restart overlap, retry after a partial commit) is skipped
            conn.execute(insert(Reading).prefix_with("OR IGNORE"), rows)
        self.rows_written += len(rows)

    def prune(self) -> int:
        """Delete readings and diagnoses older than the retention window. Returns rows removed."""
        cutoff_ms = int((time.time() - self.retention_days * 86400) * 1000)
        try:
            with self._db.engine.begin() as conn:
                n = conn.execute(delete(Reading).where(Reading.ts_ms < cutoff_ms)).rowcount or 0
                n += conn.execute(delete(Diagnosis).where(Diagnosis.ts_ms < cutoff_ms)).rowcount or 0
            if n:
                log.info("pruned %d rows older than %d days", n, self.retention_days)
                self._db.checkpoint()
            return n
        except Exception as exc:  # noqa: BLE001
            log.error("prune failed: %s", exc)
            return 0
