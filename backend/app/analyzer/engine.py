"""DiagnosisEngine: feeds snapshots into a rolling window, runs the rules, and keeps diagnoses stable.

Stability matters as much as correctness. Raw rule output flickers with every noisy sample, so:
* a condition must hold for ``confirm_s`` before it is reported (event-style findings are immediate);
* once reported it lingers ``linger_s`` after the condition clears, instead of vanishing;
* every change is emitted as an event so the caller can persist history without re-deriving it.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.analyzer.config import Thresholds
from app.analyzer.fans import FanKnowledge
from app.analyzer.models import Diagnosis, DiagnosisReport, RuleSkip, Severity
from app.analyzer.rules import RULES, Context, Finding, NotEvaluable, Rule
from app.analyzer.window import Sample, Window
from app.models import Snapshot

log = logging.getLogger(__name__)

EventKind = Literal["confirmed", "changed", "resolved"]
_SEVERITY_RANK: dict[Severity, int] = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class _Track:
    first_seen: float
    last_seen: float
    finding: Finding
    confirmed: bool
    reported_severity: Severity | None = None
    lingering: bool = False
    row_id: int | None = None  # filled in by the persistence layer


@dataclass
class EngineEvent:
    kind: EventKind
    diagnosis: Diagnosis
    at: float


@dataclass
class _State:
    tracks: dict[str, _Track] = field(default_factory=dict)
    resolved: deque[Diagnosis] = field(default_factory=lambda: deque(maxlen=20))
    resolved_at: dict[str, float] = field(default_factory=dict)


class DiagnosisEngine:
    def __init__(
        self,
        thresholds: Thresholds | None = None,
        fans: FanKnowledge | None = None,
        rules: Sequence[Rule] = RULES,
    ) -> None:
        self.th = thresholds or Thresholds()
        self.fans = fans or FanKnowledge()
        self._rules = tuple(rules)
        self.window = Window(self.th.window_s)
        self._state = _State()
        self._events: list[EngineEvent] = []
        self._crash_logged: set[str] = set()
        self.report: DiagnosisReport = DiagnosisReport(
            generated_at=0.0, window_s=0.0, window_samples=0, status="warming_up", summary="Waiting for the first samples."
        )

    # ---- input -----------------------------------------------------------------------------------

    def ingest(self, snapshot: Snapshot) -> None:
        sample = Sample.from_snapshot(snapshot)
        self.window.add(sample)
        self.fans.observe("CPU Fan", sample.cpu_fan_rpm)
        self.fans.observe("GPU Fan", sample.gpu_fan_rpm)

    def drain_events(self) -> list[EngineEvent]:
        events, self._events = self._events, []
        return events

    # ---- evaluation ------------------------------------------------------------------------------

    def evaluate(self, now: float | None = None) -> DiagnosisReport:
        samples = self.window.all()
        if now is None:
            now = samples[-1].t if samples else 0.0

        if len(samples) < self.th.min_samples:
            self.report = DiagnosisReport(
                generated_at=now,
                window_s=round(self.window.duration_s, 1),
                window_samples=len(samples),
                status="warming_up",
                summary=f"Collecting data ({len(samples)} of {self.th.min_samples} samples) before judging.",
            )
            return self.report

        ctx = Context(now=now, window=self.window, th=self.th, fans=self.fans)
        findings: dict[str, Finding] = {}
        skips: list[RuleSkip] = []
        for rule in self._rules:
            try:
                for f in rule.fn(ctx):
                    findings[f.key] = f
            except NotEvaluable as why:
                skips.append(RuleSkip(rule_id=rule.id, title=rule.title, reason=why.reason))
            except Exception as exc:  # noqa: BLE001 - one broken rule must never take the engine down
                if rule.id not in self._crash_logged:  # a permanently broken rule must not flood the log
                    self._crash_logged.add(rule.id)
                    log.exception("rule %s crashed (further crashes of this rule are not logged)", rule.id)
                skips.append(RuleSkip(rule_id=rule.id, title=rule.title, reason=f"internal error: {type(exc).__name__}"))

        self._update_tracks(findings, now)
        diagnoses = self._visible(now)
        active = [d for d in diagnoses if d.active]
        resolved = [d for d in self._state.resolved if now - self._state.resolved_at.get(d.key, 0) <= self.th.resolved_keep_s]

        self.report = DiagnosisReport(
            generated_at=now,
            window_s=round(self.window.duration_s, 1),
            window_samples=len(samples),
            status="issues" if active else "ok",
            summary=self._summary(active, skips, now),
            diagnoses=diagnoses,
            recently_resolved=resolved,
            not_evaluated=skips,
        )
        return self.report

    def _update_tracks(self, findings: dict[str, Finding], now: float) -> None:
        tracks = self._state.tracks
        for key, f in findings.items():
            tr = tracks.get(key)
            if tr is None:
                tr = tracks[key] = _Track(first_seen=now, last_seen=now, finding=f, confirmed=False)
            tr.last_seen, tr.finding, tr.lingering = now, f, False

            if not tr.confirmed and (f.immediate or now - tr.first_seen >= self.th.confirm_s):
                tr.confirmed, tr.reported_severity = True, f.severity
                self._events.append(EngineEvent("confirmed", self._to_diagnosis(key, tr), now))
            elif tr.confirmed and tr.reported_severity != f.severity:
                tr.reported_severity = f.severity
                self._events.append(EngineEvent("changed", self._to_diagnosis(key, tr), now))

        for key in list(tracks):
            if key in findings:
                continue
            tr = tracks[key]
            if not tr.confirmed:
                del tracks[key]  # flickered away before it was ever reported: forget it, restart the clock
            elif now - tr.last_seen >= self.th.linger_s:
                d = self._to_diagnosis(key, tr)
                d.active = False
                self._state.resolved.appendleft(d)
                self._state.resolved_at[key] = now
                self._events.append(EngineEvent("resolved", d, now))
                del tracks[key]
            else:
                tr.lingering = True

    def _to_diagnosis(self, key: str, tr: _Track) -> Diagnosis:
        f = tr.finding
        return Diagnosis(
            key=key,
            rule_id=f.rule_id,
            component=f.component,
            diagnosis=f.diagnosis,
            severity=f.severity,
            confidence=f.confidence,
            certainty=f.certainty,
            evidence=list(f.evidence),
            suggested_fix=f.suggested_fix,
            metrics=dict(f.metrics),
            first_seen=tr.first_seen,
            last_seen=tr.last_seen,
            active=not tr.lingering,
        )

    def _visible(self, now: float) -> list[Diagnosis]:
        out = [self._to_diagnosis(k, t) for k, t in self._state.tracks.items() if t.confirmed]
        return sorted(out, key=lambda d: (not d.active, _SEVERITY_RANK[d.severity], -d.confidence))

    def _summary(self, active: list[Diagnosis], skips: list[RuleSkip], now: float) -> str:
        span = int(self.window.duration_s)
        if active:
            lead = active[0]
            more = f" (+{len(active) - 1} more)" if len(active) > 1 else ""
            return f"{lead.diagnosis}{more}"
        text = f"No thermal problems detected over the last {span} s."
        if skips:
            text += f" {len(skips)} of {len(self._rules)} checks could not run - see details."
        return text
