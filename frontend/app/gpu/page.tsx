"use client";

import { Info, Loader2 } from "lucide-react";
import { MetricChart } from "@/components/charts/metric-chart";
import { ProcessTable } from "@/components/process-table";
import { StatTile, type MeterLevel } from "@/components/stat-tile";
import { ThrottleBadge } from "@/components/status-badges";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { fmt, fmtBytesMb, fmtClock, fmtTemp, fmtWatts } from "@/lib/format";
import { heatBand, type HeatBand } from "@/lib/heat";
import { useLive } from "@/lib/live";

const LEVEL: Record<HeatBand, MeterLevel> = { cool: "ok", normal: "ok", warm: "warn", hot: "hot", critical: "critical" };

/** Why a field is empty, in plain words. Keys are the backend's GpuSnapshot field names. */
const UNSUPPORTED: Record<string, string> = {
  fan_percent: "Fan speed: laptop fans are controlled by the embedded controller, so the NVIDIA driver cannot see them.",
  power_limit_w: "Power limit: the driver reports neither the enforced nor the management limit.",
  temp_slowdown_c: "Slowdown temperature: not reported by this driver.",
  temp_shutdown_c: "Shutdown temperature: not reported by this driver.",
  util_gpu_pct: "Utilisation: not reported by this driver.",
  pstate: "Performance state: not reported by this driver.",
};

export default function GpuPage() {
  const { snapshot, points } = useLive();
  if (!snapshot) {
    return (
      <Card className="flex items-center gap-3 p-6 text-sm text-secondary">
        <Loader2 className="size-4 animate-spin" aria-hidden /> Waiting for the first reading from the backend…
      </Card>
    );
  }
  const gpu = snapshot.gpu;
  if (!gpu) {
    const nvml = snapshot.health.collectors.find((c) => c.name === "nvml");
    return (
      <Card className="p-6">
        <h1 className="text-xl font-semibold text-foreground">GPU</h1>
        <p className="mt-2 text-sm text-secondary">
          No NVIDIA GPU telemetry is available{nvml?.error ? `: ${nvml.error}` : "."} Make sure the NVIDIA driver is installed.
        </p>
      </Card>
    );
  }

  const band = gpu.temp_c == null ? null : heatBand(gpu.temp_c, gpu.temp_slowdown_c);
  const fanRpm = gpu.fan_rpm;
  const unsupported = gpu.unsupported.map((f) => UNSUPPORTED[f] ?? `${f}: not reported by this driver.`);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-foreground">GPU</h1>
          <p className="mt-1 text-sm text-muted">
            {gpu.name ?? "NVIDIA GPU"}
            {gpu.driver_version ? ` · driver ${gpu.driver_version}` : ""}
          </p>
        </div>
        <ThrottleBadge throttle={snapshot.throttle.gpu} scope="GPU" />
      </div>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4" aria-label="GPU readings">
        <StatTile
          label="Core temperature"
          value={fmt(gpu.temp_c)}
          unit="°C"
          meter={{ value: gpu.temp_c, max: gpu.temp_slowdown_c ?? 91, level: band ? LEVEL[band] : "ok", label: "GPU temperature against its slowdown point" }}
          sub={`slows down at ${fmtTemp(gpu.temp_slowdown_c)} · shuts down at ${fmtTemp(gpu.temp_shutdown_c)}`}
        />
        <StatTile label="Hot spot" value={fmt(gpu.temp_hotspot_c)} unit="°C" sub="hottest point on the die" />
        <StatTile label="Memory junction" value={fmt(gpu.temp_memory_c)} unit="°C" sub="video memory temperature" />
        <StatTile
          label="Power draw"
          value={fmt(gpu.power_draw_w, 1)}
          unit="W"
          meter={gpu.power_limit_w ? { value: gpu.power_draw_w, max: gpu.power_limit_w, label: "GPU power against its limit" } : undefined}
          sub={`limit ${fmtWatts(gpu.power_limit_w)} · default ${fmtWatts(gpu.power_limit_default_w)}`}
        />
        <StatTile label="Core clock" value={fmtClock(gpu.core_clock_mhz)} sub={`max ${fmtClock(gpu.max_core_clock_mhz)}`} />
        <StatTile label="Memory clock" value={fmtClock(gpu.mem_clock_mhz)} sub={`max ${fmtClock(gpu.max_mem_clock_mhz)}`} />
        <StatTile
          label="Utilisation"
          value={fmt(gpu.util_gpu_pct)}
          unit="%"
          sub={`memory ${fmtBytesMb(gpu.mem_used_mb)} of ${fmtBytesMb(gpu.mem_total_mb)} · P-state ${gpu.pstate ?? "n/a"}`}
        />
        <StatTile
          label="Fan"
          value={fanRpm != null ? fmt(fanRpm) : gpu.fan_percent != null ? fmt(gpu.fan_percent) : "n/a"}
          unit={fanRpm != null ? "RPM" : gpu.fan_percent != null ? "%" : undefined}
          sub={fanRpm != null || gpu.fan_percent != null ? "reported by the system" : "not exposed on this device"}
        />
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Throttle reasons</CardTitle>
            <CardDescription>
              What the driver says is limiting the GPU right now (a hardware flag, so this is detected, not guessed).
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {gpu.throttle_reasons.length === 0 ? (
                <Badge variant="good">No limiting reason reported</Badge>
              ) : (
                gpu.throttle_reasons.map((r) => (
                  <Badge key={r} variant={/idle|display|application|sync/i.test(r) ? "neutral" : "warning"}>
                    {r}
                  </Badge>
                ))
              )}
            </div>
            <p className="num-tabular text-xs text-muted">
              Raw bitmask {gpu.throttle_mask == null ? "n/a" : `0x${gpu.throttle_mask.toString(16)}`}. “GPU idle” and display/application
              clock settings are normal and are not counted as throttling.
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Not reported by this GPU</CardTitle>
            <CardDescription>Fields the driver answered “not supported” for. These show as n/a rather than zero.</CardDescription>
          </CardHeader>
          <CardContent>
            {unsupported.length === 0 ? (
              <p className="text-sm text-secondary">Everything ThermalSense asks for is reported.</p>
            ) : (
              <ul className="space-y-2 text-sm text-secondary">
                {unsupported.map((u) => (
                  <li key={u} className="flex gap-2">
                    <Info className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />
                    <span>{u}</span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 xl:grid-cols-2" aria-label="GPU charts">
        <MetricChart title="GPU temperatures" subtitle="Last 5 minutes" unit="°C" series={["gpuTemp", "gpuHotspot", "gpuMemTemp"]} data={points} />
        <MetricChart title="GPU clock" subtitle="Last 5 minutes" unit="MHz" series={["gpuClock"]} data={points} />
        <MetricChart title="GPU power" subtitle="Draw against the enforced limit" unit="W" series={["gpuPower", "gpuPowerLimit"]} data={points} digits={1} />
        <MetricChart title="GPU utilisation" subtitle="Last 5 minutes" unit="%" series={["gpuUtil"]} data={points} domain={[0, 100]} />
      </section>

      <ProcessTable processes={snapshot.processes} focus="gpu" />
    </div>
  );
}
