"use client";

import { Check } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { HistoryChart, type HistorySeries } from "@/components/charts/history-chart";
import { CertaintyBadge, SeverityBadge } from "@/components/status-badges";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";
import { SERIES_COLOR, SERIES_LABEL, type ChartKey } from "@/lib/chart";
import { fmtDateTime, fmtDuration } from "@/lib/format";
import { useFetched } from "@/lib/hooks";
import { cn } from "@/lib/utils";

const RANGES = [
  { id: "15m", label: "15 min", seconds: 900 },
  { id: "1h", label: "1 hour", seconds: 3600 },
  { id: "6h", label: "6 hours", seconds: 6 * 3600 },
  { id: "24h", label: "24 hours", seconds: 86400 },
  { id: "7d", label: "7 days", seconds: 7 * 86400 },
] as const;
type RangeId = (typeof RANGES)[number]["id"];

interface Group {
  id: string;
  label: string;
  unit: string;
  digits: number;
  domain?: [number, number];
  series: HistorySeries[];
  defaults: string[];
}

const GROUPS: Group[] = [
  {
    id: "temp", label: "Temperature", unit: "°C", digits: 0,
    series: [
      { metric: "cpu_pkg_temp", key: "cpuTemp" },
      { metric: "cpu_max_core_temp", key: "cpuMaxCore" },
      { metric: "gpu_temp", key: "gpuTemp" },
      { metric: "gpu_hotspot", key: "gpuHotspot" },
      { metric: "gpu_mem_temp", key: "gpuMemTemp" },
    ],
    defaults: ["cpu_pkg_temp", "gpu_temp"],
  },
  {
    id: "clock", label: "Clock speed", unit: "MHz", digits: 0,
    series: [
      { metric: "cpu_active_clock", key: "cpuClock" },
      { metric: "cpu_max_clock", key: "cpuMaxClock" },
      { metric: "gpu_core_clock", key: "gpuClock" },
    ],
    defaults: ["cpu_active_clock", "gpu_core_clock"],
  },
  {
    id: "power", label: "Power", unit: "W", digits: 1,
    series: [
      { metric: "cpu_pkg_power", key: "cpuPower" },
      { metric: "gpu_power", key: "gpuPower" },
      { metric: "gpu_power_limit", key: "gpuPowerLimit" },
    ],
    defaults: ["cpu_pkg_power", "gpu_power"],
  },
  {
    id: "load", label: "Load", unit: "%", digits: 0, domain: [0, 100],
    series: [
      { metric: "cpu_load", key: "cpuLoad" },
      { metric: "gpu_util", key: "gpuUtil" },
    ],
    defaults: ["cpu_load", "gpu_util"],
  },
  {
    id: "fan", label: "Fans", unit: "RPM", digits: 0,
    series: [
      { metric: "cpu_fan_rpm", key: "cpuFan" },
      { metric: "gpu_fan_rpm", key: "gpuFan" },
    ],
    defaults: ["cpu_fan_rpm", "gpu_fan_rpm"],
  },
];

/** A one-of-many control whose selection is marked with a check (not by colour alone). */
function Segmented<T extends string>({
  label, value, options, onChange,
}: {
  label: string;
  value: T;
  options: readonly { id: T; label: string }[];
  onChange: (id: T) => void;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-border bg-card p-1">
      {options.map((o) => {
        const selected = o.id === value;
        return (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(o.id)}
            className={cn(
              "inline-flex items-center gap-1 rounded-sm px-3 py-1 text-xs font-medium transition-colors",
              selected ? "bg-hover text-foreground" : "text-secondary hover:bg-hover/60 hover:text-foreground",
            )}
          >
            {selected ? <Check className="size-4 stroke-[3]" aria-hidden /> : null}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export default function HistoryPage() {
  const [range, setRange] = useState<RangeId>("1h");
  const [groupId, setGroupId] = useState<string>("temp");
  const [chosen, setChosen] = useState<Record<string, string[]>>(() => Object.fromEntries(GROUPS.map((g) => [g.id, g.defaults])));

  const group = GROUPS.find((g) => g.id === groupId) ?? GROUPS[0]!;
  const selectedMetrics = chosen[group.id] ?? group.defaults;
  const series = useMemo(() => group.series.filter((s) => selectedMetrics.includes(s.metric)), [group, selectedMetrics]);
  const seconds = RANGES.find((r) => r.id === range)?.seconds ?? 3600;

  const toggle = useCallback(
    (metric: string) =>
      setChosen((prev) => {
        const cur = prev[group.id] ?? group.defaults;
        if (cur.includes(metric)) return cur.length <= 1 ? prev : { ...prev, [group.id]: cur.filter((m) => m !== metric) }; // keep one
        return { ...prev, [group.id]: [...cur, metric] };
      }),
    [group],
  );

  // `to`/`from` are computed at fetch time, so the 30 s auto-refresh always asks for "the last <range>".
  const metricsKey = series.map((s) => s.metric).join(",");
  const history = useFetched(
    (signal) => {
      const to = Date.now() / 1000;
      return api.history({ from: to - seconds, to: to + 1, metrics: series.map((s) => s.metric), maxPoints: 600 }, signal);
    },
    `${range}|${metricsKey}`,
    30_000,
  );
  const episodes = useFetched(
    (signal) => {
      const to = Date.now() / 1000;
      return api.throttleEpisodes(to - seconds, to + 1, signal);
    },
    `episodes|${range}`,
    30_000,
  );
  const diagnoses = useFetched(
    (signal) => {
      const to = Date.now() / 1000;
      return api.diagnosisHistory(to - seconds, to + 1, signal);
    },
    `diag|${range}`,
    30_000,
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-foreground">History</h1>
        <p className="mt-1 text-sm text-muted">Everything below is scoped by the row of filters. Data is kept for the retention period, then deleted.</p>
      </div>

      {/* One filter row above everything it scopes */}
      <div className="flex flex-wrap items-center gap-3" aria-label="Filters">
        <Segmented label="Time range" value={range} options={RANGES} onChange={setRange} />
        <Segmented label="Metric group" value={group.id} options={GROUPS.map((g) => ({ id: g.id, label: g.label }))} onChange={setGroupId} />
        <div className="flex flex-wrap gap-2" role="group" aria-label="Series">
          {group.series.map((s) => {
            const on = selectedMetrics.includes(s.metric);
            return (
              <button
                key={s.metric}
                type="button"
                aria-pressed={on}
                onClick={() => toggle(s.metric)}
                className={cn(
                  "inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs transition-colors",
                  on ? "border-border bg-hover text-foreground" : "border-border text-muted hover:text-foreground",
                )}
              >
                <span className="h-0.5 w-3.5 rounded-full" style={{ background: on ? SERIES_COLOR[s.key as ChartKey] : "rgb(var(--axis))" }} aria-hidden />
                {SERIES_LABEL[s.key as ChartKey]}
              </button>
            );
          })}
        </div>
      </div>

      {history.error && !history.data ? (
        <Card className="p-5 text-sm text-secondary">History is unavailable: {history.error}</Card>
      ) : (
        <HistoryChart
          title={group.label}
          unit={group.unit}
          digits={group.digits}
          domain={group.domain}
          series={series}
          data={history.data}
          episodes={episodes.data?.episodes ?? []}
          refreshing={history.refreshing && history.data !== null}
        />
      )}

      <Card>
        <CardHeader>
          <CardTitle>Diagnoses in this range</CardTitle>
          <CardDescription>Problems the root-cause engine reported while ThermalSense was running.</CardDescription>
        </CardHeader>
        <CardContent>
          {diagnoses.error && !diagnoses.data ? (
            <p className="text-sm text-secondary">Diagnosis history is unavailable: {diagnoses.error}</p>
          ) : !diagnoses.data || diagnoses.data.diagnoses.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted">No problems were reported in this range.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Started</TableHead>
                  <TableHead>Lasted</TableHead>
                  <TableHead>Severity</TableHead>
                  <TableHead>Finding</TableHead>
                  <TableHead>Basis</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {diagnoses.data.diagnoses.map((d) => (
                  <TableRow key={d.id}>
                    <TableCell className="num-tabular whitespace-nowrap">{fmtDateTime(d.started)}</TableCell>
                    <TableCell className="num-tabular whitespace-nowrap">{d.ended == null ? "ongoing" : fmtDuration(d.ended - d.started)}</TableCell>
                    <TableCell><SeverityBadge severity={d.severity} /></TableCell>
                    <TableCell className="min-w-[16rem] text-foreground">{d.diagnosis}</TableCell>
                    <TableCell>{d.certainty ? <CertaintyBadge certainty={d.certainty} /> : null}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
