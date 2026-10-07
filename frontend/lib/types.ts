/**
 * Wire types. They mirror the backend's Pydantic models (backend/app/models.py and
 * backend/app/analyzer/models.py) field for field. Every sensor-derived value is nullable: a missing
 * sensor is `null`, never zero, and the UI must render that as "not available".
 */

export type CoreKind = "P" | "E";
export type Severity = "info" | "warning" | "critical";
export type Certainty = "detected" | "inferred";
export type ThrottleType = "thermal" | "power" | "prochot" | "current" | "other";
export type ThrottleConfidence = "detected" | "inferred" | "unavailable";

export interface CoreReading {
  index: number;
  label: string;
  kind: CoreKind | null;
  temp_c: number | null;
  clock_mhz: number | null;
  load_pct: number | null;
}

export interface CpuSnapshot {
  name: string | null;
  vendor: "intel" | "amd" | "other" | null;
  package_temp_c: number | null;
  max_core_temp_c: number | null;
  avg_core_temp_c: number | null;
  tjmax_c: number | null;
  total_load_pct: number | null;
  package_power_w: number | null;
  cores_power_w: number | null;
  pl1_w: number | null;
  pl2_w: number | null;
  avg_clock_mhz: number | null;
  max_clock_mhz: number | null;
  cores: CoreReading[];
}

export interface FanReading {
  name: string;
  rpm: number | null;
  percent: number | null;
  source: string | null;
}

export interface BatteryInfo {
  percent: number | null;
  plugged_in: boolean | null;
  minutes_left: number | null;
}

export interface PowerPlan {
  name: string | null;
  guid: string | null;
  kind: "balanced" | "high_performance" | "power_saver" | "ultimate" | "other" | null;
}

export interface SystemSnapshot {
  cpu_percent: number | null;
  cpu_percent_per_logical: number[];
  memory_percent: number | null;
  memory_used_gb: number | null;
  memory_total_gb: number | null;
  battery: BatteryInfo | null;
  power_plan: PowerPlan | null;
}

export interface GpuSnapshot {
  name: string | null;
  driver_version: string | null;
  temp_c: number | null;
  temp_slowdown_c: number | null;
  temp_shutdown_c: number | null;
  temp_hotspot_c: number | null;
  temp_memory_c: number | null;
  core_clock_mhz: number | null;
  max_core_clock_mhz: number | null;
  mem_clock_mhz: number | null;
  max_mem_clock_mhz: number | null;
  power_draw_w: number | null;
  power_limit_w: number | null;
  power_limit_default_w: number | null;
  fan_percent: number | null;
  fan_rpm: number | null;
  util_gpu_pct: number | null;
  util_mem_pct: number | null;
  mem_used_mb: number | null;
  mem_total_mb: number | null;
  pstate: number | null;
  throttle_mask: number | null;
  throttle_reasons: string[];
  unsupported: string[];
}

export interface ThrottleComponent {
  active: boolean | null;
  type: ThrottleType | null;
  source: string;
  confidence: ThrottleConfidence;
  reasons: string[];
  detail: Record<string, number | string | null>;
}

export interface ThrottleStatus extends ThrottleComponent {
  component: "cpu" | "gpu" | null;
  cpu: ThrottleComponent;
  gpu: ThrottleComponent | null;
}

export interface ProcessInfo {
  name: string;
  count: number;
  pid: number | null;
  cpu_pct: number;
  mem_mb: number | null;
  gpu_pct: number | null;
}

export interface CollectorStatus {
  name: string;
  available: boolean;
  error: string | null;
  last_ok: number | null;
}

export interface SensorHealth {
  is_admin: boolean;
  pawnio_installed: boolean | null;
  collectors: CollectorStatus[];
  warnings: string[];
}

export interface Snapshot {
  timestamp: number;
  seq: number;
  interval_s: number;
  collect_ms: number | null;
  cpu: CpuSnapshot;
  gpu: GpuSnapshot | null;
  throttle: ThrottleStatus;
  fans: FanReading[];
  system: SystemSnapshot;
  processes: ProcessInfo[];
  health: SensorHealth;
}

// ---- diagnosis ---------------------------------------------------------------------------------

export interface Diagnosis {
  key: string;
  rule_id: string;
  component: "cpu" | "gpu" | "system";
  diagnosis: string;
  severity: Severity;
  confidence: number;
  certainty: Certainty;
  evidence: string[];
  suggested_fix: string;
  metrics: Record<string, number | string | null>;
  first_seen: number;
  last_seen: number;
  active: boolean;
}

export interface RuleSkip {
  rule_id: string;
  title: string;
  reason: string;
}

export interface DiagnosisReport {
  generated_at: number;
  window_s: number;
  window_samples: number;
  status: "warming_up" | "ok" | "issues";
  summary: string;
  diagnoses: Diagnosis[];
  recently_resolved: Diagnosis[];
  not_evaluated: RuleSkip[];
}

export interface DiagnosisHistoryItem {
  id: number;
  rule_id: string;
  component: string | null;
  diagnosis: string;
  severity: Severity;
  confidence: number;
  certainty: Certainty | null;
  evidence: string[];
  suggested_fix: string;
  metrics: Record<string, number | string | null>;
  started: number;
  ended: number | null;
  active: boolean;
}

export interface DiagnosisHistory {
  from: number;
  to: number;
  count: number;
  diagnoses: DiagnosisHistoryItem[];
}

// ---- history -----------------------------------------------------------------------------------

export interface MetricInfo {
  name: string;
  label: string;
  unit: string;
  group: "cpu" | "gpu" | "fan" | "system";
}

export interface SeriesAggregate {
  avg: (number | null)[];
  min: (number | null)[];
  max: (number | null)[];
}

export interface HistoryResponse {
  from: number;
  to: number;
  bucket_s: number;
  samples: number;
  metrics: string[];
  t: number[];
  series: Record<string, SeriesAggregate>;
  clamped_to_retention: boolean;
}

export interface ThrottleEpisode {
  start: number;
  end: number;
  duration_s: number;
  type: ThrottleType | null;
  component: "cpu" | "gpu" | null;
  confidence: "detected" | "inferred" | null;
  reasons: string[];
  samples: number;
}

export interface ThrottleEpisodes {
  from: number;
  to: number;
  clamped_to_retention: boolean;
  episodes: ThrottleEpisode[];
}

export interface HealthResponse {
  status: string;
  version: string;
  uptime_s: number;
  snapshot_seq: number | null;
  storage: {
    enabled: boolean;
    path: string | null;
    retention_days: number | null;
    rows_written: number;
    rows_dropped: number;
  };
}
