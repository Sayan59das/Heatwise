"""Read side of storage: downsampled metric series and throttle episodes."""

from __future__ import annotations

import math
import re
from typing import Any, Final

from sqlalchemy import text

from app.db.database import Database
from app.db.schema import METRIC_NAMES

# Bucket sizes in seconds. Using a fixed ladder (instead of ceil(range / points)) keeps the chart's
# x-grid stable while the user pans, and makes buckets line up with clock boundaries.
_NICE_BUCKETS_S: Final = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400)
_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


class UnknownMetric(ValueError):
    def __init__(self, names: list[str]) -> None:
        super().__init__(f"unknown metric(s): {', '.join(sorted(names))}")
        self.names = names


def choose_bucket_s(range_s: float, max_points: int) -> int:
    """Smallest bucket from the ladder that keeps the result within ``max_points`` points."""
    wanted = range_s / max(1, max_points)
    for b in _NICE_BUCKETS_S:
        if b >= wanted:
            return b
    return _NICE_BUCKETS_S[-1]


def _round(v: float | None, digits: int = 2) -> float | None:
    return None if v is None else round(float(v), digits)


def query_history(
    db: Database, metrics: list[str], start_ms: int, end_ms: int, max_points: int = 600
) -> dict[str, Any]:
    """Columnar series: ``t`` (unix seconds of each bucket start) plus avg/min/max arrays per metric."""
    bad = [m for m in metrics if m not in METRIC_NAMES or not _IDENT.match(m)]
    if bad:
        raise UnknownMetric(bad)
    if end_ms <= start_ms:
        raise ValueError("'to' must be after 'from'")

    bucket_s = choose_bucket_s((end_ms - start_ms) / 1000.0, max_points)
    bucket_ms = bucket_s * 1000

    # Identifiers below come exclusively from METRIC_NAMES (validated above), never from user text.
    selects = ", ".join(f"AVG({m}) AS {m}__avg, MIN({m}) AS {m}__min, MAX({m}) AS {m}__max" for m in metrics)
    sql = text(
        f"SELECT (ts_ms / :b) * :b AS bucket_ms, COUNT(*) AS n, {selects} "  # noqa: S608 - allowlisted identifiers
        "FROM readings WHERE ts_ms >= :start AND ts_ms < :end GROUP BY bucket_ms ORDER BY bucket_ms"
    )
    with db.engine.connect() as conn:
        rows = conn.execute(sql, {"b": bucket_ms, "start": start_ms, "end": end_ms}).mappings().all()

    series: dict[str, dict[str, list[float | None]]] = {
        m: {"avg": [], "min": [], "max": []} for m in metrics
    }
    t: list[float] = []
    samples = 0
    for r in rows:
        t.append(r["bucket_ms"] / 1000.0)
        samples += int(r["n"])
        for m in metrics:
            series[m]["avg"].append(_round(r[f"{m}__avg"]))
            series[m]["min"].append(_round(r[f"{m}__min"]))
            series[m]["max"].append(_round(r[f"{m}__max"]))

    return {
        "from": start_ms / 1000.0,
        "to": end_ms / 1000.0,
        "bucket_s": bucket_s,
        "samples": samples,
        "metrics": metrics,
        "t": t,
        "series": series,
    }


def throttle_episodes(
    db: Database, start_ms: int, end_ms: int, gap_s: float = 3.0, max_rows: int = 200_000
) -> list[dict[str, Any]]:
    """Group consecutive throttling samples (same component and type) into episodes."""
    sql = text(
        "SELECT ts_ms, throttle_type, throttle_component, throttle_confidence, throttle_reasons "
        "FROM readings WHERE throttle_active = 1 AND ts_ms >= :start AND ts_ms < :end "
        "ORDER BY ts_ms LIMIT :lim"
    )
    with db.engine.connect() as conn:
        rows = conn.execute(sql, {"start": start_ms, "end": end_ms, "lim": max_rows}).mappings().all()

    episodes: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    gap_ms = gap_s * 1000
    for r in rows:
        key = (r["throttle_type"], r["throttle_component"])
        if cur is not None and key == cur["_key"] and r["ts_ms"] - cur["_last_ms"] <= gap_ms:
            cur["_last_ms"] = r["ts_ms"]
            cur["samples"] += 1
            if r["throttle_reasons"]:
                cur["reasons"].update(r["throttle_reasons"].split("; "))
            if r["throttle_confidence"] == "inferred":
                cur["confidence"] = "inferred"  # one inferred sample taints the episode: never overstate
            continue
        if cur is not None:
            episodes.append(cur)
        cur = {
            "_key": key,
            "_last_ms": r["ts_ms"],
            "start": r["ts_ms"] / 1000.0,
            "type": r["throttle_type"],
            "component": r["throttle_component"],
            "confidence": r["throttle_confidence"],
            "reasons": set(r["throttle_reasons"].split("; ")) if r["throttle_reasons"] else set(),
            "samples": 1,
        }
    if cur is not None:
        episodes.append(cur)

    out: list[dict[str, Any]] = []
    for e in episodes:
        end = e["_last_ms"] / 1000.0
        out.append(
            {
                "start": e["start"],
                "end": end,
                "duration_s": math.ceil(end - e["start"]) + 1,  # inclusive of the final sample's interval
                "type": e["type"],
                "component": e["component"],
                "confidence": e["confidence"],
                "reasons": sorted(e["reasons"]),
                "samples": e["samples"],
            }
        )
    return out
