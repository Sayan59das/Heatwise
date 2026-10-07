"""History endpoints: downsampled series, metric catalogue, throttle episodes."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.db.database import Database
from app.db.history import UnknownMetric, query_history, throttle_episodes
from app.db.schema import METRICS

router = APIRouter(prefix="/api/history", tags=["history"])

_DEFAULT_METRICS = "cpu_pkg_temp,gpu_temp"
_DEFAULT_RANGE_S = 3600.0


def parse_time(value: str, name: str) -> float:
    """Unix seconds, unix milliseconds (JS ``Date.now()``), or ISO-8601 (naive = local time) -> unix seconds."""
    try:
        n = float(value)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"'{name}' must be unix seconds or ISO-8601, got {value!r}"
            ) from None
        return dt.timestamp()  # naive datetimes are interpreted in the machine's local time zone
    return n / 1000.0 if n > 1e11 else n


def _db(request: Request) -> tuple[Database, int]:
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="History storage is unavailable (see server log).")
    retention = int(getattr(request.app.state, "retention_days", 7))
    return db, retention


def _range(request: Request, from_: str | None, to: str | None) -> tuple[int, int, bool]:
    _, retention = _db(request)
    now = time.time()
    end_s = parse_time(to, "to") if to else now
    start_s = parse_time(from_, "from") if from_ else end_s - _DEFAULT_RANGE_S
    if end_s <= start_s:
        raise HTTPException(status_code=400, detail="'to' must be after 'from'")
    earliest = now - retention * 86400
    clamped = start_s < earliest
    start_s = max(start_s, earliest)  # nothing older than the retention window exists
    if end_s <= start_s:
        return int(start_s * 1000), int(start_s * 1000) + 1000, True
    return int(start_s * 1000), int(end_s * 1000), clamped


@router.get("/metrics")
def list_metrics() -> list[dict[str, str]]:
    return [m._asdict() for m in METRICS]


@router.get("")
def get_history(
    request: Request,
    from_: str | None = Query(None, alias="from", description="unix seconds/ms or ISO-8601; default: 1 h before 'to'"),
    to: str | None = Query(None, description="unix seconds/ms or ISO-8601; default: now"),
    metric: list[str] = Query(default=[_DEFAULT_METRICS], description="comma-separated and/or repeated"),
    max_points: int = Query(600, ge=10, le=5000),
) -> dict[str, Any]:
    db, _ = _db(request)
    metrics = [m.strip() for item in metric for m in item.split(",") if m.strip()]
    if not metrics:
        raise HTTPException(status_code=400, detail="at least one metric is required")
    start_ms, end_ms, clamped = _range(request, from_, to)
    try:
        result = query_history(db, list(dict.fromkeys(metrics)), start_ms, end_ms, max_points)
    except UnknownMetric as exc:
        raise HTTPException(
            status_code=422, detail={"error": str(exc), "available": [m.name for m in METRICS]}
        ) from exc
    result["clamped_to_retention"] = clamped
    return result


@router.get("/throttle")
def get_throttle_episodes(
    request: Request,
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
) -> dict[str, Any]:
    db, _ = _db(request)
    start_ms, end_ms, clamped = _range(request, from_, to)
    return {
        "from": start_ms / 1000.0,
        "to": end_ms / 1000.0,
        "clamped_to_retention": clamped,
        "episodes": throttle_episodes(db, start_ms, end_ms),
    }
