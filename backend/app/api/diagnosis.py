"""Diagnosis endpoints."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.analyzer.models import DiagnosisReport
from app.analyzer.runner import DiagnosisRunner
from app.api.history import parse_time
from app.db.diagnoses import DiagnosisStore
from app.db.database import Database

router = APIRouter(prefix="/api/diagnosis", tags=["diagnosis"])


@router.get("", response_model=DiagnosisReport)
async def get_diagnosis(request: Request) -> DiagnosisReport:
    """Current root-cause report over the rolling window, each finding with its raw-number evidence."""
    runner: DiagnosisRunner | None = getattr(request.app.state, "diagnosis", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Diagnosis engine is not running.")
    return runner.engine.report


@router.get("/history")
def get_diagnosis_history(
    request: Request,
    from_: str | None = Query(None, alias="from", description="unix seconds/ms or ISO-8601; default: 24 h before 'to'"),
    to: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="History storage is unavailable (see server log).")
    now = time.time()
    end_s = parse_time(to, "to") if to else now
    start_s = parse_time(from_, "from") if from_ else end_s - 86400.0
    if end_s <= start_s:
        raise HTTPException(status_code=400, detail="'to' must be after 'from'")
    items = DiagnosisStore(db).list(int(start_s * 1000), int(end_s * 1000), limit)
    return {"from": start_s, "to": end_s, "count": len(items), "diagnoses": items}
