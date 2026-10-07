"""SQLAlchemy schema (SQLite): one wide row per sample, diagnosis history, and a small key/value table.

Design notes
* One *wide* row per sample (not one row per metric): at 1 Hz that is ~600k rows for 7 days instead of
  ~20M, which keeps range scans and the database file small.
* ``ts_ms`` (integer milliseconds since the epoch) is the primary key, so rows are ordered by time for free.
* Per-core values are not stored historically (the Per-Core page is live); the aggregates are.
"""

from __future__ import annotations

from typing import Final, NamedTuple

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION: Final = 1


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"

    ts_ms: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    # CPU
    cpu_pkg_temp: Mapped[float | None] = mapped_column(Float)
    cpu_max_core_temp: Mapped[float | None] = mapped_column(Float)
    cpu_avg_core_temp: Mapped[float | None] = mapped_column(Float)
    cpu_load: Mapped[float | None] = mapped_column(Float)
    cpu_pkg_power: Mapped[float | None] = mapped_column(Float)
    cpu_pl1: Mapped[float | None] = mapped_column(Float)
    cpu_pl2: Mapped[float | None] = mapped_column(Float)
    cpu_avg_clock: Mapped[float | None] = mapped_column(Float)
    cpu_max_clock: Mapped[float | None] = mapped_column(Float)
    cpu_active_clock: Mapped[float | None] = mapped_column(Float)  # mean clock of the busy cores

    # GPU
    gpu_temp: Mapped[float | None] = mapped_column(Float)
    gpu_hotspot: Mapped[float | None] = mapped_column(Float)
    gpu_mem_temp: Mapped[float | None] = mapped_column(Float)
    gpu_core_clock: Mapped[float | None] = mapped_column(Float)
    gpu_mem_clock: Mapped[float | None] = mapped_column(Float)
    gpu_power: Mapped[float | None] = mapped_column(Float)
    gpu_power_limit: Mapped[float | None] = mapped_column(Float)
    gpu_util: Mapped[float | None] = mapped_column(Float)

    # Fans
    cpu_fan_rpm: Mapped[float | None] = mapped_column(Float)
    gpu_fan_rpm: Mapped[float | None] = mapped_column(Float)
    gpu_fan_pct: Mapped[float | None] = mapped_column(Float)

    # System
    mem_percent: Mapped[float | None] = mapped_column(Float)
    battery_pct: Mapped[float | None] = mapped_column(Float)
    on_ac: Mapped[int | None] = mapped_column(Integer)  # 1 plugged in, 0 on battery, NULL no battery
    power_plan: Mapped[str | None] = mapped_column(String(24))

    # Throttle summary (see ThrottleStatus)
    throttle_active: Mapped[int | None] = mapped_column(Integer)  # 1 / 0 / NULL = unknown
    throttle_type: Mapped[str | None] = mapped_column(String(16))
    throttle_component: Mapped[str | None] = mapped_column(String(8))
    throttle_confidence: Mapped[str | None] = mapped_column(String(12))
    throttle_reasons: Mapped[str | None] = mapped_column(Text)


class Diagnosis(Base):
    __tablename__ = "diagnoses"
    __table_args__ = (Index("ix_diagnoses_ts", "ts_ms"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    ended_ms: Mapped[int | None] = mapped_column(Integer)  # NULL while the condition is still active
    rule_id: Mapped[str] = mapped_column(String(48), nullable=False)
    component: Mapped[str | None] = mapped_column(String(8))
    diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(12), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    certainty: Mapped[str | None] = mapped_column(String(10))  # detected | inferred
    evidence: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list of strings
    suggested_fix: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[str | None] = mapped_column(Text)  # JSON object of the raw numbers
    window_s: Mapped[float | None] = mapped_column(Float)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-encoded


class Metric(NamedTuple):
    name: str  # column name == API metric name
    label: str
    unit: str
    group: str  # cpu | gpu | fan | system


# The only columns /api/history will ever put in SQL. Names come from here, never from the request.
METRICS: Final[tuple[Metric, ...]] = (
    Metric("cpu_pkg_temp", "CPU package temperature", "°C", "cpu"),
    Metric("cpu_max_core_temp", "CPU hottest core", "°C", "cpu"),
    Metric("cpu_avg_core_temp", "CPU average core temperature", "°C", "cpu"),
    Metric("cpu_load", "CPU load", "%", "cpu"),
    Metric("cpu_pkg_power", "CPU package power", "W", "cpu"),
    Metric("cpu_pl1", "CPU sustained power limit (PL1)", "W", "cpu"),
    Metric("cpu_pl2", "CPU boost power limit (PL2)", "W", "cpu"),
    Metric("cpu_avg_clock", "CPU average clock", "MHz", "cpu"),
    Metric("cpu_max_clock", "CPU highest core clock", "MHz", "cpu"),
    Metric("cpu_active_clock", "CPU clock of busy cores", "MHz", "cpu"),
    Metric("gpu_temp", "GPU temperature", "°C", "gpu"),
    Metric("gpu_hotspot", "GPU hot spot", "°C", "gpu"),
    Metric("gpu_mem_temp", "GPU memory junction", "°C", "gpu"),
    Metric("gpu_core_clock", "GPU core clock", "MHz", "gpu"),
    Metric("gpu_mem_clock", "GPU memory clock", "MHz", "gpu"),
    Metric("gpu_power", "GPU power draw", "W", "gpu"),
    Metric("gpu_power_limit", "GPU power limit", "W", "gpu"),
    Metric("gpu_util", "GPU utilisation", "%", "gpu"),
    Metric("cpu_fan_rpm", "CPU fan", "RPM", "fan"),
    Metric("gpu_fan_rpm", "GPU fan", "RPM", "fan"),
    Metric("gpu_fan_pct", "GPU fan duty", "%", "fan"),
    Metric("mem_percent", "Memory used", "%", "system"),
    Metric("battery_pct", "Battery", "%", "system"),
    Metric("throttle_active", "Throttling (1 = yes)", "", "system"),
)
METRIC_NAMES: Final[frozenset[str]] = frozenset(m.name for m in METRICS)
