"""Public shapes of the root-cause engine's output."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Severity = Literal["info", "warning", "critical"]
# "detected": a hardware flag or a direct measurement says so.
# "inferred": derived from numbers (clocks, temperatures, power) - a hypothesis, never a fact.
Certainty = Literal["detected", "inferred"]
Component = Literal["cpu", "gpu", "system"]


class Diagnosis(BaseModel):
    key: str  # stable identity, "<rule_id>:<component>"
    rule_id: str
    component: Component
    diagnosis: str  # headline
    severity: Severity
    confidence: float  # 0..1
    certainty: Certainty
    evidence: list[str]  # human-readable lines, each carrying its raw numbers
    suggested_fix: str
    metrics: dict[str, float | str | None] = {}  # the same numbers, structured
    first_seen: float  # unix seconds
    last_seen: float
    active: bool = True  # False while lingering after the condition cleared


class RuleSkip(BaseModel):
    """A rule that could not be evaluated, and why. Silence would look like 'no problem found'."""

    rule_id: str
    title: str
    reason: str


class DiagnosisReport(BaseModel):
    generated_at: float
    window_s: float  # how much history the rules actually had
    window_samples: int
    status: Literal["warming_up", "ok", "issues"]
    summary: str
    diagnoses: list[Diagnosis] = []
    recently_resolved: list[Diagnosis] = []
    not_evaluated: list[RuleSkip] = []
