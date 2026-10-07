import { apiBase } from "./config";
import type {
  DiagnosisHistory,
  DiagnosisReport,
  HealthResponse,
  HistoryResponse,
  MetricInfo,
  ThrottleEpisodes,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Fetch JSON from the local backend. `signal` lets callers cancel on unmount / parameter change. */
export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${apiBase()}${path}`, { signal, headers: { Accept: "application/json" } });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError("Cannot reach the ThermalSense backend. Is it running?", null);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body: unknown = await res.json();
      if (body && typeof body === "object" && "detail" in body) {
        const d = (body as { detail: unknown }).detail;
        detail = typeof d === "string" ? d : JSON.stringify(d);
      }
    } catch {
      /* keep statusText */
    }
    throw new ApiError(detail || `HTTP ${res.status}`, res.status);
  }
  return (await res.json()) as T;
}

export interface HistoryQuery {
  from: number; // unix seconds
  to: number;
  metrics: string[];
  maxPoints?: number;
}

const qs = (o: Record<string, string | number>) =>
  Object.entries(o)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");

export const api = {
  health: (s?: AbortSignal) => getJson<HealthResponse>("/api/health", s),
  diagnosis: (s?: AbortSignal) => getJson<DiagnosisReport>("/api/diagnosis", s),
  diagnosisHistory: (from: number, to: number, s?: AbortSignal) =>
    getJson<DiagnosisHistory>(`/api/diagnosis/history?${qs({ from, to, limit: 100 })}`, s),
  metrics: (s?: AbortSignal) => getJson<MetricInfo[]>("/api/history/metrics", s),
  history: (q: HistoryQuery, s?: AbortSignal) =>
    getJson<HistoryResponse>(
      `/api/history?${qs({ from: q.from, to: q.to, metric: q.metrics.join(","), max_points: q.maxPoints ?? 600 })}`,
      s,
    ),
  throttleEpisodes: (from: number, to: number, s?: AbortSignal) =>
    getJson<ThrottleEpisodes>(`/api/history/throttle?${qs({ from, to })}`, s),
};
