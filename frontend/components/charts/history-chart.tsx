"use client";

import { Flame, LineChart as ChartIcon, Table2 } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fixedAxis, niceAxis } from "@/lib/axis";
import { SERIES_COLOR, SERIES_LABEL, type ChartKey } from "@/lib/chart";
import { fmt, fmtClockTimeShort, fmtDateTime, fmtDuration } from "@/lib/format";
import type { HistoryResponse, ThrottleEpisode } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface HistorySeries {
  metric: string; // backend metric name
  key: ChartKey; // colour/label identity
}

interface HistoryChartProps {
  title: string;
  unit: string;
  digits?: number;
  domain?: [number, number];
  series: HistorySeries[];
  data: HistoryResponse | null;
  episodes: ThrottleEpisode[];
  refreshing: boolean;
}

type Row = { t: number } & Record<string, number | null | [number, number]>;

const AXIS = "rgb(var(--axis))";
const GRID = "rgb(var(--grid))";
const SURFACE = "rgb(var(--card))";
const MUTED = "rgb(var(--ink-3))";
const WARN = "rgb(var(--warning))";

interface TipProps {
  active?: boolean;
  payload?: ReadonlyArray<{ dataKey?: unknown; value?: unknown; payload?: Row }>;
  label?: unknown;
  series: HistorySeries[];
  unit: string;
  digits: number;
}

function HistoryTooltip({ active, payload, label, series, unit, digits }: TipProps) {
  if (!active || !payload?.length) return null;
  const row = payload[0]?.payload;
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2 text-xs shadow-lg shadow-black/40">
      <div className="mb-1.5 text-muted">{typeof label === "number" ? fmtDateTime(label) : ""}</div>
      <ul className="space-y-1.5">
        {series.map((s) => {
          const avg = row?.[s.metric];
          const band = row?.[`${s.metric}__band`];
          return (
            <li key={s.metric} className="flex items-center gap-2">
              <span className="h-0.5 w-3.5 shrink-0 rounded-full" style={{ background: SERIES_COLOR[s.key] }} aria-hidden />
              <span className="num-tabular font-semibold text-foreground">{typeof avg === "number" ? `${fmt(avg, digits)} ${unit}` : "n/a"}</span>
              <span className="text-secondary">{SERIES_LABEL[s.key]}</span>
              {Array.isArray(band) ? (
                <span className="num-tabular text-muted">
                  ({fmt(band[0], digits)}–{fmt(band[1], digits)})
                </span>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function HistoryChart({ title, unit, digits = 0, domain, series, data, episodes, refreshing }: HistoryChartProps) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const tableId = useId();

  const rows = useMemo<Row[]>(() => {
    if (!data) return [];
    // The backend only returns buckets that contain samples. If ThermalSense was not running for a while,
    // consecutive buckets are far apart, and drawing a line between them would invent measurements. Insert an
    // empty row after the last bucket before the hole so the line (and band) breaks.
    const gapS = Math.max(data.bucket_s * 2.5, 5);
    const out: Row[] = [];
    let prevT: number | null = null;
    data.t.forEach((t, i) => {
      if (prevT !== null && t - prevT > gapS) {
        const hole: Row = { t: prevT + data.bucket_s };
        for (const s of series) {
          hole[s.metric] = null;
          hole[`${s.metric}__band`] = null;
        }
        out.push(hole);
      }
      const row: Row = { t };
      for (const s of series) {
        const agg = data.series[s.metric];
        const avg = agg?.avg[i] ?? null;
        const lo = agg?.min[i] ?? null;
        const hi = agg?.max[i] ?? null;
        row[s.metric] = avg;
        row[`${s.metric}__band`] = lo != null && hi != null ? [lo, hi] : null;
      }
      out.push(row);
      prevT = t;
    });
    return out;
  }, [data, series]);

  const hasData = rows.some((r) => series.some((s) => typeof r[s.metric] === "number"));
  const axis = useMemo(() => {
    if (domain) return fixedAxis(domain[0], domain[1]);
    // the band's min/max can exceed every average, so size the axis from the band, not just the lines
    const vals: number[] = [];
    for (const r of rows) {
      for (const s of series) {
        const b = r[`${s.metric}__band`];
        if (Array.isArray(b)) vals.push(b[0], b[1]);
        else if (typeof r[s.metric] === "number") vals.push(r[s.metric] as number);
      }
    }
    return vals.length ? niceAxis(Math.min(...vals), Math.max(...vals)) : niceAxis(0, 1);
  }, [rows, series, domain]);
  const span = data ? data.to - data.from : 0;
  const hasGaps = useMemo(() => rows.some((r) => series.every((s) => r[s.metric] === null)), [rows, series]);
  const tickFmt = (t: number) => (span > 6 * 3600 ? fmtDateTime(t) : fmtClockTimeShort(t));
  const visibleEpisodes = useMemo(
    () => (data ? episodes.filter((e) => e.end >= data.from && e.start <= data.to) : []),
    [episodes, data],
  );

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-3 space-y-0">
        <div className="min-w-0">
          <CardTitle>{title}</CardTitle>
          <CardDescription className="mt-1.5">
            {data
              ? `Each point is the average over ${fmtDuration(data.bucket_s)}; the shaded band is the minimum to maximum in that interval, so short spikes stay visible.`
              : "Loading…"}
          </CardDescription>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setView((v) => (v === "chart" ? "table" : "chart"))}
          aria-pressed={view === "table"}
          aria-controls={tableId}
        >
          {view === "chart" ? <Table2 /> : <ChartIcon />}
          {view === "chart" ? "Table" : "Chart"}
        </Button>
      </CardHeader>
      <CardContent id={tableId}>
        <ul className="mb-3 flex flex-wrap gap-x-5 gap-y-1.5 text-xs">
          {series.map((s) => (
            <li key={s.metric} className="flex items-center gap-2 text-secondary">
              <span className="h-0.5 w-3.5 rounded-full" style={{ background: SERIES_COLOR[s.key] }} aria-hidden />
              {SERIES_LABEL[s.key]}
            </li>
          ))}
          {visibleEpisodes.length > 0 ? (
            <li className="flex items-center gap-2 text-secondary">
              <span className="inline-flex size-4 items-center justify-center rounded-sm" style={{ background: "rgb(var(--warning) / 0.18)" }}>
                <Flame className="size-3" style={{ color: WARN }} aria-hidden />
              </span>
              Throttling ({visibleEpisodes.length})
            </li>
          ) : null}
        </ul>

        <div className={cn(refreshing && "is-refreshing")}>
          {view === "table" ? (
            <div className="max-h-[320px] overflow-auto rounded-md border border-border">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead>Time</TableHead>
                    {series.map((s) => (
                      <TableHead key={s.metric} className="text-right">
                        {SERIES_LABEL[s.key]} avg / min / max ({unit})
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {[...rows].reverse().slice(0, 300).map((r) => (
                    <TableRow key={r.t}>
                      <TableCell className="num-tabular">{fmtDateTime(r.t)}</TableCell>
                      {series.map((s) => {
                        const b = r[`${s.metric}__band`];
                        const avg = r[s.metric];
                        return (
                          <TableCell key={s.metric} className="num-tabular text-right">
                            <span className="text-foreground">{typeof avg === "number" ? fmt(avg, digits) : "n/a"}</span>
                            {Array.isArray(b) ? ` / ${fmt(b[0], digits)} / ${fmt(b[1], digits)}` : ""}
                          </TableCell>
                        );
                      })}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : !data ? (
            <div className="flex h-[300px] items-center justify-center text-sm text-muted">Loading history…</div>
          ) : !hasData ? (
            <div className="flex h-[300px] items-center justify-center rounded-md border border-border px-6 text-center text-sm text-muted">
              No readings were recorded for this metric in the selected range.
              {data.samples === 0 ? " ThermalSense has not recorded anything in this period yet." : " The sensor did not report a value (see the notice at the top of the page)."}
            </div>
          ) : (
            <div role="img" aria-label={`${title}, ${series.map((s) => SERIES_LABEL[s.key]).join(", ")}, over the selected range`} style={{ height: 300 }}>
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={rows} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}>
                  <CartesianGrid vertical={false} stroke={GRID} strokeDasharray="" />
                  <XAxis
                    dataKey="t"
                    type="number"
                    scale="time"
                    domain={[data.from, data.to]}
                    tickFormatter={tickFmt}
                    tick={{ fontSize: 11, fill: MUTED }}
                    axisLine={{ stroke: AXIS }}
                    tickLine={false}
                    minTickGap={56}
                    allowDataOverflow
                  />
                  <YAxis
                    width={48}
                    domain={axis.domain}
                    ticks={axis.ticks}
                    interval={0}
                    tick={{ fontSize: 11, fill: MUTED }}
                    axisLine={false}
                    tickLine={false}
                    tickFormatter={(v: number) => fmt(v, digits)}
                  />
                  {visibleEpisodes.map((e) => (
                    <ReferenceArea
                      key={`${e.start}-${e.component}-${e.type}`}
                      x1={Math.max(e.start, data.from)}
                      x2={Math.min(e.end + 1, data.to)}
                      fill={WARN}
                      fillOpacity={0.14}
                      stroke="none"
                      ifOverflow="hidden"
                    />
                  ))}
                  <Tooltip
                    isAnimationActive={false}
                    cursor={{ stroke: MUTED, strokeWidth: 1 }}
                    content={<HistoryTooltip series={series} unit={unit} digits={digits} />}
                  />
                  {series.map((s) => (
                    <Area
                      key={`${s.metric}-band`}
                      type="monotone"
                      dataKey={`${s.metric}__band`}
                      stroke="none"
                      fill={SERIES_COLOR[s.key]}
                      fillOpacity={0.1}
                      isAnimationActive={false}
                      activeDot={false}
                      legendType="none"
                    />
                  ))}
                  {series.map((s) => (
                    <Line
                      key={s.metric}
                      type="monotone"
                      dataKey={s.metric}
                      stroke={SERIES_COLOR[s.key]}
                      strokeWidth={2}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      dot={false}
                      activeDot={{ r: 4, fill: SERIES_COLOR[s.key], stroke: SURFACE, strokeWidth: 2 }}
                      isAnimationActive={false}
                      connectNulls={false}
                    />
                  ))}
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>
        {hasGaps ? (
          <p className="mt-2 text-xs text-muted">Breaks in the line are periods when ThermalSense was not running; nothing was measured then.</p>
        ) : null}
        {data?.clamped_to_retention ? (
          <p className="mt-2 text-xs text-muted">The range was shortened to the stored history (data older than the retention period is deleted).</p>
        ) : null}
        {visibleEpisodes.length > 0 && view === "chart" ? (
          <ul className="mt-3 space-y-1 text-xs text-secondary">
            {visibleEpisodes.slice(0, 5).map((e) => (
              <li key={`${e.start}-${e.component}`} className="flex flex-wrap gap-x-2">
                <Flame className="size-3.5 text-status-warning" aria-hidden />
                <span className="text-foreground">{fmtDateTime(e.start)}</span>
                <span>
                  {e.component?.toUpperCase() ?? "System"} {e.type ?? "throttling"} for {fmtDuration(e.duration_s)} ({e.confidence ?? "unknown"})
                </span>
                {e.reasons.length ? <span className="text-muted">· {e.reasons.join("; ")}</span> : null}
              </li>
            ))}
            {visibleEpisodes.length > 5 ? <li className="text-muted">+{visibleEpisodes.length - 5} more in this range</li> : null}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  );
}
