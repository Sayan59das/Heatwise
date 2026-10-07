"use client";

import { useId, useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Table2, LineChart as ChartIcon } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { SERIES_COLOR, SERIES_LABEL, type ChartKey, type ChartPoint } from "@/lib/chart";
import { fixedAxis, niceAxis } from "@/lib/axis";
import { fmt, fmtClockTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface ReferenceMark {
  y: number;
  label: string;
}

interface MetricChartProps {
  title: string;
  subtitle?: string;
  unit: string; // axis/tooltip suffix, e.g. "°C"
  series: ChartKey[];
  data: ChartPoint[];
  digits?: number;
  /** y-axis domain; defaults to a padded auto range so a flat line isn't stretched to fill the plot */
  domain?: [number | "auto", number | "auto"];
  references?: ReferenceMark[];
  height?: number;
  className?: string;
}

const AXIS = "rgb(var(--axis))";
const GRID = "rgb(var(--grid))";
const SURFACE = "rgb(var(--card))";
const MUTED = "rgb(var(--ink-3))";

function latest(data: ChartPoint[], key: ChartKey): number | null {
  for (let i = data.length - 1; i >= 0; i--) {
    const v = data[i]?.[key];
    if (v != null) return v;
  }
  return null;
}

interface TooltipRow {
  dataKey?: unknown;
  value?: unknown;
}
interface ChartTooltipProps {
  active?: boolean;
  payload?: ReadonlyArray<TooltipRow>;
  label?: unknown;
  series: ChartKey[];
  unit: string;
  digits: number;
}

/** One tooltip, every series: the value leads, the name follows, rows are keyed with a short line, not a box. */
function ChartTooltip({ active, payload, label, series, unit, digits }: ChartTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const byKey = new Map(payload.map((p) => [String(p.dataKey), p.value]));
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2 text-xs shadow-lg shadow-black/40">
      <div className="mb-1.5 text-muted">{typeof label === "number" ? fmtClockTime(label) : String(label ?? "")}</div>
      <ul className="space-y-1">
        {series.map((k) => {
          const v = byKey.get(k);
          const n = typeof v === "number" ? v : null;
          return (
            <li key={k} className="flex items-center gap-2">
              <span className="h-0.5 w-3.5 shrink-0 rounded-full" style={{ background: SERIES_COLOR[k] }} aria-hidden />
              <span className="num-tabular font-semibold text-foreground">{n == null ? "n/a" : `${fmt(n, digits)} ${unit}`}</span>
              <span className="text-secondary">{SERIES_LABEL[k]}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function MetricChart({
  title,
  subtitle,
  unit,
  series,
  data,
  digits = 0,
  domain,
  references = [],
  height = 220,
  className,
}: MetricChartProps) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const tableId = useId();
  const hasData = useMemo(() => data.some((p) => series.some((k) => p[k] != null)), [data, series]);

  // Clean tick values (0 / 500 / 1,000 ...) and headroom so a line never touches the frame.
  const axis = useMemo(() => {
    if (domain && domain[0] !== "auto" && domain[1] !== "auto") return fixedAxis(domain[0], domain[1]);
    const vals = data.flatMap((p) => series.map((k) => p[k])).filter((v): v is number => v != null);
    return vals.length ? niceAxis(Math.min(...vals), Math.max(...vals)) : niceAxis(0, 1);
  }, [data, series, domain]);

  const rows = useMemo(() => data.slice(-40).reverse(), [data]);
  const summary = series.map((k) => `${SERIES_LABEL[k]} ${fmt(latest(data, k), digits)} ${unit}`).join(", ");

  return (
    <Card className={className}>
      <CardHeader className="flex-row items-start justify-between gap-3 space-y-0">
        <div className="min-w-0">
          <CardTitle>{title}</CardTitle>
          {subtitle ? <CardDescription className="mt-1.5">{subtitle}</CardDescription> : null}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setView((v) => (v === "chart" ? "table" : "chart"))}
          aria-pressed={view === "table"}
          aria-controls={tableId}
          title={view === "chart" ? "Show the values as a table" : "Show the chart"}
        >
          {view === "chart" ? <Table2 /> : <ChartIcon />}
          {view === "chart" ? "Table" : "Chart"}
        </Button>
      </CardHeader>

      <CardContent id={tableId}>
        {/* Legend: line key + name + latest value, always present for 2+ series, absent for one */}
        {series.length > 1 ? (
          <ul className="mb-3 flex flex-wrap gap-x-5 gap-y-1.5 text-xs">
            {series.map((k) => (
              <li key={k} className="flex items-center gap-2">
                <span className="h-0.5 w-3.5 rounded-full" style={{ background: SERIES_COLOR[k] }} aria-hidden />
                <span className="text-secondary">{SERIES_LABEL[k]}</span>
                <span className="num-tabular font-semibold text-foreground">
                  {fmt(latest(data, k), digits)} {unit}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mb-3 text-xs">
            <span className="text-secondary">{SERIES_LABEL[series[0]!]}: </span>
            <span className="num-tabular font-semibold text-foreground">
              {fmt(latest(data, series[0]!), digits)} {unit}
            </span>
          </p>
        )}

        {view === "table" ? (
          <div className="max-h-[260px] overflow-auto rounded-md border border-border">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Time</TableHead>
                  {series.map((k) => (
                    <TableHead key={k} className="text-right">
                      {SERIES_LABEL[k]} ({unit})
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((p) => (
                  <TableRow key={p.t}>
                    <TableCell className="num-tabular">{fmtClockTime(p.t)}</TableCell>
                    {series.map((k) => (
                      <TableCell key={k} className="num-tabular text-right text-foreground">
                        {fmt(p[k], digits)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : !hasData ? (
          <div
            className="flex items-center justify-center rounded-md border border-border px-4 text-center text-xs text-muted"
            style={{ height }}
          >
            No readings yet from this sensor. If it stays empty, the backend lists why under the connection status.
          </div>
        ) : (
          <div role="img" aria-label={`${title}. Latest: ${summary}`} className={cn("w-full")} style={{ height }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}>
                <CartesianGrid vertical={false} stroke={GRID} strokeDasharray="" />
                <XAxis
                  dataKey="t"
                  type="number"
                  scale="time"
                  domain={["dataMin", "dataMax"]}
                  tickFormatter={(t: number) => fmtClockTime(t)}
                  tick={{ fontSize: 11, fill: MUTED }}
                  axisLine={{ stroke: AXIS }}
                  tickLine={false}
                  minTickGap={48}
                />
                <YAxis
                  width={44}
                  domain={axis.domain}
                  ticks={axis.ticks}
                  interval={0}
                  tick={{ fontSize: 11, fill: MUTED }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v: number) => fmt(v, digits)}
                />
                {references.map((r) => (
                  <ReferenceLine
                    key={r.label}
                    y={r.y}
                    stroke={MUTED}
                    strokeWidth={1}
                    label={{ value: r.label, position: "insideTopRight", fill: MUTED, fontSize: 11 }}
                  />
                ))}
                <Tooltip
                  isAnimationActive={false}
                  cursor={{ stroke: MUTED, strokeWidth: 1 }}
                  content={<ChartTooltip series={series} unit={unit} digits={digits} />}
                />
                {series.map((k) => (
                  <Line
                    key={k}
                    type="monotone"
                    dataKey={k}
                    stroke={SERIES_COLOR[k]}
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    dot={false}
                    // 8px marker with a 2px surface ring so it stays legible where lines cross
                    activeDot={{ r: 4, fill: SERIES_COLOR[k], stroke: SURFACE, strokeWidth: 2 }}
                    isAnimationActive={false}
                    connectNulls={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
