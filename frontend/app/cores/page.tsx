"use client";

import { Loader2 } from "lucide-react";
import { MetricChart } from "@/components/charts/metric-chart";
import { CoreGrid, HeatLegend } from "@/components/core-grid";
import { StatTile } from "@/components/stat-tile";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { fmt, fmtClock, fmtTemp } from "@/lib/format";
import { useLive } from "@/lib/live";

export default function CoresPage() {
  const { snapshot, points } = useLive();
  if (!snapshot) {
    return (
      <Card className="flex items-center gap-3 p-6 text-sm text-secondary">
        <Loader2 className="size-4 animate-spin" aria-hidden /> Waiting for the first reading from the backend…
      </Card>
    );
  }
  const { cpu } = snapshot;
  const withTemp = cpu.cores.filter((c) => c.temp_c != null);
  const hottest = withTemp.reduce<(typeof withTemp)[number] | null>((a, c) => (a === null || (c.temp_c ?? 0) > (a.temp_c ?? 0) ? c : a), null);
  const temps = withTemp.map((c) => c.temp_c as number);
  const spread = temps.length > 1 ? Math.max(...temps) - Math.min(...temps) : null;
  const clocks = cpu.cores.map((c) => c.clock_mhz).filter((v): v is number => v != null);
  const fastest = clocks.length ? Math.max(...clocks) : null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-foreground">Per-core detail</h1>
        <p className="mt-1 text-sm text-muted">{cpu.name ?? "CPU"} · {cpu.cores.length} cores</p>
      </div>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4" aria-label="Core summary">
        <StatTile label="Hottest core" value={fmt(hottest?.temp_c)} unit="°C" sub={hottest?.label ?? "No core sensors readable"} />
        <StatTile label="Average core" value={fmt(cpu.avg_core_temp_c)} unit="°C" sub={`package ${fmtTemp(cpu.package_temp_c)}`} />
        <StatTile label="Spread, hottest to coolest" value={fmt(spread)} unit={spread == null ? undefined : "°C"} sub="a large spread points to uneven cooling or load" />
        <StatTile label="Fastest core" value={fmtClock(fastest)} sub={`TjMax ${fmtTemp(cpu.tjmax_c)}`} />
      </section>

      <Card>
        <CardHeader>
          <CardTitle>Core temperatures</CardTitle>
          <CardDescription>Colour shows temperature against this CPU&apos;s own limit. Every cell also prints the number and a word.</CardDescription>
        </CardHeader>
        <CardContent>
          <HeatLegend limit={cpu.tjmax_c} />
        </CardContent>
      </Card>

      {cpu.cores.length === 0 ? (
        <Card className="p-6 text-sm text-secondary">No per-core data is available from the sensor layer yet.</Card>
      ) : (
        <CoreGrid cores={cpu.cores} limit={cpu.tjmax_c} />
      )}

      <MetricChart
        title="Package vs hottest core"
        subtitle="Last 5 minutes"
        unit="°C"
        series={["cpuTemp", "cpuMaxCore"]}
        data={points}
      />
    </div>
  );
}
