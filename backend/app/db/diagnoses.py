"""Diagnosis history persistence."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.analyzer.models import Diagnosis as DiagnosisModel
from app.db.database import Database
from app.db.schema import Diagnosis as DiagnosisRow


class DiagnosisStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def save(self, d: DiagnosisModel, window_s: float | None = None) -> int:
        """Insert a newly confirmed diagnosis; returns its row id."""
        row = DiagnosisRow(
            ts_ms=int(d.first_seen * 1000),
            ended_ms=None,
            rule_id=d.rule_id,
            component=d.component,
            diagnosis=d.diagnosis,
            severity=d.severity,
            confidence=d.confidence,
            certainty=d.certainty,
            evidence=json.dumps(d.evidence),
            suggested_fix=d.suggested_fix,
            metrics=json.dumps(d.metrics),
            window_s=window_s,
        )
        with Session(self._db.engine) as s:
            s.add(row)
            s.commit()
            return int(row.id)

    def end(self, row_id: int, ended_at_s: float) -> None:
        with Session(self._db.engine) as s:
            s.execute(update(DiagnosisRow).where(DiagnosisRow.id == row_id).values(ended_ms=int(ended_at_s * 1000)))
            s.commit()

    def close_stale(self) -> int:
        """Rows left open by a crash/kill: end them at the last reading we have (never in the future)."""
        with self._db.engine.begin() as conn:
            last = conn.execute(text("SELECT MAX(ts_ms) FROM readings")).scalar()
            res = conn.execute(
                update(DiagnosisRow)
                .where(DiagnosisRow.ended_ms.is_(None))
                .values(ended_ms=func.max(DiagnosisRow.ts_ms, int(last or 0)))  # SQLite scalar max(a, b)
            )
            return int(res.rowcount or 0)

    def list(self, start_ms: int, end_ms: int, limit: int = 200) -> list[dict[str, Any]]:
        """Diagnoses that overlap [start, end), newest first."""
        with Session(self._db.engine) as s:
            rows = s.scalars(
                select(DiagnosisRow)
                .where(DiagnosisRow.ts_ms < end_ms)
                .where((DiagnosisRow.ended_ms.is_(None)) | (DiagnosisRow.ended_ms >= start_ms))
                .order_by(DiagnosisRow.ts_ms.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "id": r.id,
                    "rule_id": r.rule_id,
                    "component": r.component,
                    "diagnosis": r.diagnosis,
                    "severity": r.severity,
                    "confidence": r.confidence,
                    "certainty": r.certainty,
                    "evidence": json.loads(r.evidence),
                    "suggested_fix": r.suggested_fix,
                    "metrics": json.loads(r.metrics) if r.metrics else {},
                    "started": r.ts_ms / 1000.0,
                    "ended": r.ended_ms / 1000.0 if r.ended_ms is not None else None,
                    "active": r.ended_ms is None,
                }
                for r in rows
            ]
