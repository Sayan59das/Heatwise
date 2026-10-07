"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import control as control_api
from app.api import diagnosis as diagnosis_api
from app.api import history as history_api
from app.api.routes import router
from app.analyzer.engine import DiagnosisEngine
from app.analyzer.runner import DiagnosisRunner
from app.collectors.hub import SensorHub
from app.config import Settings, get_settings
from app.db.database import Database
from app.db.recorder import Recorder
from app.logging_setup import configure_logging
from app.winutil import is_admin

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, hub: SensorHub | None = None) -> FastAPI:
    """Build the app. ``hub`` can be injected (tests); by default the real collectors are used."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not is_admin():
            log.warning("Not elevated: CPU temperature/power/fan sensors will report null. Run as administrator.")
        active_hub = hub or SensorHub(settings)
        app.state.hub = active_hub
        app.state.retention_days = settings.retention_days
        app.state.control_token = settings.control_token

        # Storage is optional: if the database cannot be opened we still serve live data.
        db: Database | None = None
        recorder: Recorder | None = None
        try:
            db = Database.open(settings.db_path)
            recorder = Recorder(
                db, active_hub, flush_interval_s=settings.flush_interval_s, retention_days=settings.retention_days
            )
            await recorder.start()  # subscribes before the hub starts, so the first sample is kept
        except Exception:  # noqa: BLE001
            log.exception("history storage disabled: could not open %s", settings.db_path)
            if db is not None:
                db.close()
            db, recorder = None, None
        app.state.db, app.state.recorder = db, recorder

        # The diagnosis engine works with or without storage (history is just skipped without it).
        diagnosis = DiagnosisRunner(active_hub, DiagnosisEngine(), db)
        await diagnosis.start()
        app.state.diagnosis = diagnosis

        await active_hub.start()
        log.info("ThermalSense backend %s ready on http://%s:%d", __version__, settings.host, settings.port)
        try:
            yield
        finally:
            await active_hub.stop()
            await diagnosis.stop()  # ends open diagnosis rows, saves learned fan ranges
            if recorder is not None:
                await recorder.stop()  # flushes the last buffered samples
            if db is not None:
                db.close()

    app = FastAPI(title="ThermalSense", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(history_api.router)
    app.include_router(diagnosis_api.router)
    app.include_router(control_api.router)
    return app


app = create_app()
