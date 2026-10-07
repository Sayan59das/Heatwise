"use client";

import { CheckCircle2, Loader2 } from "lucide-react";
import Link from "next/link";
import { MetricChart } from "@/components/charts/metric-chart";
import { DiagnosisCard } from "@/components/diagnosis-card";
import { ProcessTable } from "@/components/process-table";
import { StatTile, type MeterLevel } from "@/components/stat-tile";
import { ThrottleBadge } from "@/components/status-badges";
import { Card } from "@/components/ui/card";
import { api } from "@/lib/api";
import { fmt, fmtClock, fmtPct, fmtRpm, fmtTemp, fmtWatts } from "@/lib/format";
import { HEAT_BAND_LABEL, heatBand, heatColor, type HeatBand } from "@/lib/heat";
import { useFetched } from "@/lib/hooks";
import { useLive } from "@/lib/live";
import type { Snapshot } from "@/lib/types";

const LEVEL: Record<HeatBand, MeterLevel> = { cool: "ok", normal: "ok", warm: "warn", hot: "hot", critical: "critical" };

interface Hottest {
  value: number;
  label: string;
  limit: number | null;
}

/** The single number the dashboard leads with: the hottest sensor we can read, and which one it is. */
function hottest(s: Snapshot): Hottest | null {
  const cpuLimit = s.cpu.tjmax_c;
  const gpuLimit = s.gpu?.temp_slowdown_c ?? null;
  const candidates: (Hottest | null)[] = [
    s.cpu.package_temp_c == null ? null : { value: s.cpu.package_temp_c, label: "CPU package", limit: cpuLimit },
    s.cpu.max_core_temp_c == null ? null : { value: s.cpu.max_core_temp_c, label: "Hottest CPU core", limit: cpuLimit },
    s.gpu?.temp_c == null ? null : { value: s.gpu.temp_c, label: "GPU core", limit: gpuLimit },
    s.gpu?.temp_hotspot_c == null ? null : { value: s.gpu.temp_hotspot_c, label: "GPU hot spot", limit: gpuLimit },
  ];
  const real = candidates.filter((c): c is Hottest => c !== null);
  if (real.length === 0) return null;
  return real.reduce((a, b) => (b.value > a.value ? b : a));
}

function fanText(s: Snapshot): { value: string; sub: string } {
  const cpu = s.fans.find((f) => f.name === "CPU Fan") ?? s.fans.find((f) => !/gpu/i.test(f.name));
  const gpu = s.gpu?.fan_rpm ?? s.fans.find((f) => f.name === "GPU Fan")?.rpm ?? null;
  if (cpu?.rpm == null && gpu == null) {
    const acer = s.health.collectors.find((c) => c.name === "acer_wmi");
    const why = acer && !acer.available ? "Laptop fans need an elevated run." : "This device does not expose fan speed.";
    return { value: "n/a", sub: why };
  }
  return { value: cpu?.rpm == null ? "n/a" : fmt(cpu.rpm), sub: `CPU fan RPM · GPU fan ${gpu == null ? "n/a" : fmtRpm(gpu)}` };
}

export default function OverviewPage() {
  const { snapshot, points } = useLive();
  const diag = useFetched((s) => api.diagnosis(s), "diagnosis", 5000);

  if (!snapshot) {
    return (
      <Card className="flex items-center gap-3 p-6 text-sm text-secondary">
        <Loader2 className="size-4 animate-spin" aria-hidden /> Waiting for the first reading from the backend…
      </Card>
    );
  }

  const { cpu, gpu } = snapshot;
  const hot = hottest(snapshot);
  const band = hot ? heatBand(hot.value, hot.limit) : null;
  const cpuBand = cpu.package_temp_c == null ? null : heatBand(cpu.package_temp_c, cpu.tjmax_c);
  const gpuBand = gpu?.temp_c == null ? null : heatBand(gpu.temp_c, gpu.temp_slowdown_c);
  const fans = fanText(snapshot);
  const nowS = snapshot.timestamp;
  const active = (diag.data?.diagnoses ?? []).filter((d) => d.active);

  return (
    <div className="space-y-6">
      {/* ---- hero + why ------------------------------------------------------------------------------ */}
      <section className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_1fr]" aria-label="Current state">
        <Card className="flex flex-col justify-between gap-4 p-6">
          <div>
            <div className="text-xs text-muted">Hottest sensor right now</div>
            <div className="mt-2 flex items-baseline gap-1.5">
              <span className="text-6xl font-semibold leading-none text-foreground">{hot ? fmt(hot.value) : "n/a"}</span>
              {hot ? <span className="text-2xl text-secondary">°C</span> : null}
            </div>
            <div className="mt-2 flex items-center gap-2 text-sm text-secondary">
              {hot && band ? (
                <>
                  <span className="h-2.5 w-2.5 rounded-full" style={{ background: heatColor(hot.value, hot.limit) }} aria-hidden />
                  <span>
                    {hot.label} · <span className="text-foreground">{HEAT_BAND_LABEL[band]}</span>
                  </span>
                </>
              ) : (
                <span>No temperature sensor is readable yet (see the notice above).</span>
              )}
            </div>
          </div>
          <div className="flex flex-col items-start gap-2">
            <ThrottleBadge throttle={snapshot.throttle.cpu} scope="CPU" />
            {snapshot.throttle.gpu ? <ThrottleBadge throttle={snapshot.throttle.gpu} scope="GPU" /> : null}
          </div>
        </Card>

        <div className="space-y-3" aria-live="polite">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="text-base font-semibold text-foreground">Why is it hot?</h2>
            <Link href="/diagnosis" className="text-xs text-accent hover:underline">
              All diagnoses →
            </Link>
          </div>
          {diag.error && !diag.data ? (
            <Card className="p-5 text-sm text-secondary">Diagnosis is unavailable: {diag.error}</Card>
          ) : !diag.data || diag.data.status === "warming_up" ? (
            <Card className="flex items-center gap-3 p-5 text-sm text-secondary">
              <Loader2 className="size-4 animate-spin" aria-hidden /> {diag.data?.summary ?? "Starting the analysis…"}
            </Card>
          ) : active.length === 0 ? (
            <Card className="flex items-start gap-3 p-5">
              <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-status-good" aria-hidden />
              <div>
                <div className="text-sm font-medium text-foreground">{diag.data.summary}</div>
                <p className="mt-1 text-xs text-muted">
                  Based on the last {Math.round(diag.data.window_s)} s of readings.{" "}
                  {diag.data.not_evaluated.length > 0 ? (
                    <Link href="/diagnosis" className="text-accent hover:underline">
                      See which checks could not run.
                    </Link>
                  ) : null}
                </p>
              </div>
            </Card>
          ) : (
            active.slice(0, 2).map((d) => <DiagnosisCard key={d.key} d={d} compact nowS={nowS} />)
          )}
        </div>
      </section>

      {/* ---- KPI row ------------------------------------------------------------------------------------ */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" aria-label="Key readings">
        <StatTile
          label="CPU package"
          value={fmt(cpu.package_temp_c ?? cpu.max_core_temp_c)}
          unit="°C"
          meter={{ value: cpu.package_temp_c ?? cpu.max_core_temp_c, max: cpu.tjmax_c ?? 100, level: cpuBand ? LEVEL[cpuBand] : "ok", label: "CPU temperature against its limit" }}
          sub={cpu.tjmax_c ? `limit ${fmtTemp(cpu.tjmax_c)}` : "limit unknown"}
        />
        <StatTile
          label="GPU core"
          value={fmt(gpu?.temp_c)}
          unit="°C"
          meter={{ value: gpu?.temp_c ?? null, max: gpu?.temp_slowdown_c ?? 91, level: gpuBand ? LEVEL[gpuBand] : "ok", label: "GPU temperature against its slowdown point" }}
          sub={gpu ? `slows down at ${fmtTemp(gpu.temp_slowdown_c)}` : "No NVIDIA GPU"}
        />
        <StatTile
          label="CPU load"
          value={fmt(cpu.total_load_pct)}
          unit="%"
          sub={`busy cores ${fmtClock(points.at(-1)?.cpuClock)}`}
        />
        <StatTile
          label="CPU power"
          value={fmt(cpu.package_power_w, 1)}
          unit="W"
          sub={cpu.pl1_w ? `PL1 ${fmtWatts(cpu.pl1_w)} · PL2 ${fmtWatts(cpu.pl2_w)}` : "power limits not readable"}
        />
        <StatTile
          label="GPU power"
          value={fmt(gpu?.power_draw_w, 1)}
          unit="W"
          meter={gpu?.power_limit_w ? { value: gpu.power_draw_w, max: gpu.power_limit_w, label: "GPU power against its limit" } : undefined}
          sub={gpu ? `limit ${fmtWatts(gpu.power_limit_w)} · util ${fmtPct(gpu.util_gpu_pct)}` : "No NVIDIA GPU"}
        />
        <StatTile label="Fans" value={fans.value} unit={fans.value === "n/a" ? undefined : "RPM"} sub={fans.sub} />
      </section>

      {/* ---- live charts: one axis each, never dual-axis ------------------------------------------------- */}
      <section className="grid gap-4 xl:grid-cols-2" aria-label="Live charts, last five minutes">
        <MetricChart
          title="Temperature"
          subtitle="Last 5 minutes"
          unit="°C"
          series={["cpuTemp", "cpuMaxCore", "gpuTemp"]}
          data={points}
        />
        <MetricChart title="Clock speed" subtitle="Last 5 minutes" unit="MHz" series={["cpuClock", "gpuClock"]} data={points} />
        <MetricChart title="Power" subtitle="Last 5 minutes" unit="W" series={["cpuPower", "gpuPower", "gpuPowerLimit"]} data={points} digits={1} />
        <MetricChart title="Load" subtitle="Last 5 minutes" unit="%" series={["cpuLoad", "gpuUtil"]} data={points} domain={[0, 100]} />
      </section>

      <ProcessTable processes={snapshot.processes} />
    </div>
  );
}
