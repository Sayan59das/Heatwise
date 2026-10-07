import type { CpuSnapshot, Snapshot } from "./types";

/** One point on a live chart. Every field is nullable: a gap in the line is honest, a zero is not. */
export interface ChartPoint {
  t: number; // unix seconds
  cpuTemp: number | null;
  cpuMaxCore: number | null;
  gpuTemp: number | null;
  gpuHotspot: number | null;
  gpuMemTemp: number | null;
  cpuClock: number | null; // mean clock of the busy cores
  cpuMaxClock: number | null;
  gpuClock: number | null;
  cpuPower: number | null;
  gpuPower: number | null;
  gpuPowerLimit: number | null;
  cpuLoad: number | null;
  gpuUtil: number | null;
  cpuFan: number | null;
  gpuFan: number | null;
}

export type ChartKey = Exclude<keyof ChartPoint, "t">;

/** Mean clock of the cores doing real work (P-cores on hybrid CPUs). Mirrors the backend's active_core_clock. */
export function activeCoreClock(cpu: CpuSnapshot, busyLoad = 40): number | null {
  const pool = cpu.cores.filter((c) => c.kind === "P");
  const cores = pool.length > 0 ? pool : cpu.cores;
  const busy = cores.filter((c) => c.clock_mhz != null && (c.load_pct ?? 0) >= busyLoad);
  if (busy.length === 0) return null;
  return busy.reduce((s, c) => s + (c.clock_mhz ?? 0), 0) / busy.length;
}

export function snapshotToPoint(s: Snapshot): ChartPoint {
  const cpuFan = s.fans.find((f) => f.name === "CPU Fan") ?? s.fans.find((f) => !/gpu/i.test(f.name));
  const gpuFan = s.gpu?.fan_rpm ?? s.fans.find((f) => f.name === "GPU Fan")?.rpm ?? null;
  return {
    t: s.timestamp,
    cpuTemp: s.cpu.package_temp_c ?? s.cpu.max_core_temp_c,
    cpuMaxCore: s.cpu.max_core_temp_c,
    gpuTemp: s.gpu?.temp_c ?? null,
    gpuHotspot: s.gpu?.temp_hotspot_c ?? null,
    gpuMemTemp: s.gpu?.temp_memory_c ?? null,
    cpuClock: activeCoreClock(s.cpu),
    cpuMaxClock: s.cpu.max_clock_mhz,
    gpuClock: s.gpu?.core_clock_mhz ?? null,
    cpuPower: s.cpu.package_power_w,
    gpuPower: s.gpu?.power_draw_w ?? null,
    gpuPowerLimit: s.gpu?.power_limit_w ?? null,
    cpuLoad: s.cpu.total_load_pct,
    gpuUtil: s.gpu?.util_gpu_pct ?? null,
    cpuFan: cpuFan?.rpm ?? null,
    gpuFan,
  };
}

/** Which backend history metric feeds which live-chart field (used to seed charts after a page load). */
export const HISTORY_TO_CHART: Record<string, ChartKey> = {
  cpu_pkg_temp: "cpuTemp",
  cpu_max_core_temp: "cpuMaxCore",
  gpu_temp: "gpuTemp",
  gpu_hotspot: "gpuHotspot",
  gpu_mem_temp: "gpuMemTemp",
  cpu_active_clock: "cpuClock",
  cpu_max_clock: "cpuMaxClock",
  gpu_core_clock: "gpuClock",
  cpu_pkg_power: "cpuPower",
  gpu_power: "gpuPower",
  gpu_power_limit: "gpuPowerLimit",
  cpu_load: "cpuLoad",
  gpu_util: "gpuUtil",
  cpu_fan_rpm: "cpuFan",
  gpu_fan_rpm: "gpuFan",
};

/**
 * Fixed entity -> colour mapping. Colour follows the entity, never its rank, so toggling or filtering a
 * series can't repaint the others. CPU is always blue and GPU always orange, on every chart.
 */
export const SERIES_COLOR: Record<ChartKey, string> = {
  cpuTemp: "rgb(var(--series-1))",
  gpuTemp: "rgb(var(--series-2))",
  cpuMaxCore: "rgb(var(--series-3))",
  gpuHotspot: "rgb(var(--series-4))",
  gpuMemTemp: "rgb(var(--series-5))",
  cpuClock: "rgb(var(--series-1))",
  gpuClock: "rgb(var(--series-2))",
  cpuMaxClock: "rgb(var(--series-3))",
  cpuPower: "rgb(var(--series-1))",
  gpuPower: "rgb(var(--series-2))",
  gpuPowerLimit: "rgb(var(--ink-3))", // a limit is context, not a competing series: muted ink
  cpuLoad: "rgb(var(--series-1))",
  gpuUtil: "rgb(var(--series-2))",
  cpuFan: "rgb(var(--series-1))",
  gpuFan: "rgb(var(--series-2))",
};

export const SERIES_LABEL: Record<ChartKey, string> = {
  cpuTemp: "CPU package",
  gpuTemp: "GPU core",
  cpuMaxCore: "Hottest CPU core",
  gpuHotspot: "GPU hot spot",
  gpuMemTemp: "GPU memory",
  cpuClock: "CPU busy cores",
  gpuClock: "GPU core",
  cpuMaxClock: "CPU fastest core",
  cpuPower: "CPU package",
  gpuPower: "GPU",
  gpuPowerLimit: "GPU limit",
  cpuLoad: "CPU",
  gpuUtil: "GPU",
  cpuFan: "CPU fan",
  gpuFan: "GPU fan",
};
