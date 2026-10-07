"""SQLite engine setup: pragmas, schema creation, version tracking and corrupt-file recovery."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from app.db.schema import SCHEMA_VERSION, Base, Setting

log = logging.getLogger(__name__)


def _make_engine(path: Path) -> Engine:
    engine = create_engine(URL.create("sqlite", database=str(path)), future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:  # pragma: no cover - exercised via every connection
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")  # readers never block the 1 Hz writer
        cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; far fewer fsyncs than FULL
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.close()

    return engine


class Database:
    """Owns the engine. ``open`` never raises for a damaged file: it quarantines it and starts clean."""

    def __init__(self, engine: Engine, path: Path) -> None:
        self.engine = engine
        self.path = path

    @classmethod
    def open(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return cls._open_checked(path)
        except DatabaseError as exc:
            quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            log.error("database %s is unreadable (%s); moving it to %s and starting fresh", path, exc, quarantine.name)
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(path) + suffix)
                if src.exists():
                    src.replace(Path(str(quarantine) + suffix))
            return cls._open_checked(path)

    @classmethod
    def _open_checked(cls, path: Path) -> Database:
        engine = _make_engine(path)
        try:
            Base.metadata.create_all(engine)  # idempotent
            with engine.connect() as conn:
                conn.execute(text("PRAGMA quick_check")).fetchall()  # surfaces corruption early
            db = cls(engine, path)
            db._stamp_version()
            return db
        except Exception:
            engine.dispose()
            raise

    def _stamp_version(self) -> None:
        with Session(self.engine) as s:
            row = s.get(Setting, "schema_version")
            if row is None:
                s.add(Setting(key="schema_version", value=str(SCHEMA_VERSION)))
                s.commit()
            elif int(row.value) > SCHEMA_VERSION:
                log.warning("database schema v%s is newer than this build (v%s)", row.value, SCHEMA_VERSION)

    def checkpoint(self) -> None:
        """Fold the WAL back into the main file (keeps the -wal file small after pruning)."""
        with self.engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    def close(self) -> None:
        try:
            self.checkpoint()
        except Exception as exc:  # noqa: BLE001
            log.debug("checkpoint on close failed: %s", exc)
        self.engine.dispose()

    # --- tiny key/value helper (used by the settings page later) -------------------------------

    def get_setting(self, key: str) -> str | None:
        with Session(self.engine) as s:
            row = s.scalar(select(Setting).where(Setting.key == key))
            return row.value if row else None

    def set_setting(self, key: str, value: str) -> None:
        with Session(self.engine) as s:
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value))
            else:
                row.value = value
            s.commit()
