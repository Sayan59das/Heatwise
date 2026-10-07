import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export type MeterLevel = "ok" | "warn" | "hot" | "critical";

const FILL: Record<MeterLevel, string> = {
  ok: "bg-accent",
  warn: "bg-status-warning",
  hot: "bg-status-serious",
  critical: "bg-status-critical",
};
const TRACK: Record<MeterLevel, string> = {
  ok: "bg-accent/20",
  warn: "bg-status-warning/20",
  hot: "bg-status-serious/20",
  critical: "bg-status-critical/20",
};

/**
 * A single ratio against a limit. The fill carries severity; the unfilled track is a lighter step of the
 * *same* hue, so the state reads across the whole bar. `value`/`max` are shown by the caller as text too.
 */
export function Meter({ value, max, level = "ok", label }: { value: number | null; max: number; level?: MeterLevel; label: string }) {
  const pct = value == null || max <= 0 ? 0 : Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={value ?? undefined}
      className={cn("h-1.5 w-full overflow-hidden rounded-full", TRACK[level])}
    >
      <div className={cn("h-full rounded-full transition-[width] duration-300", FILL[level])} style={{ width: `${pct}%` }} />
    </div>
  );
}

interface StatTileProps {
  label: string;
  /** already formatted; pass "n/a" (see lib/format) when the sensor has no value */
  value: string;
  unit?: string;
  sub?: ReactNode;
  meter?: { value: number | null; max: number; level?: MeterLevel; label: string };
  className?: string;
}

/** Label (sentence case, no colon) / value / optional context. Values use proportional figures. */
export function StatTile({ label, value, unit, sub, meter, className }: StatTileProps) {
  const missing = value === "n/a";
  return (
    <Card className={cn("flex flex-col gap-1.5 p-4", className)}>
      <div className="text-xs text-muted">{label}</div>
      <div className="flex items-baseline gap-1">
        <span className={cn("text-2xl font-semibold leading-none", missing ? "text-muted" : "text-foreground")}>{value}</span>
        {unit && !missing ? <span className="text-sm text-secondary">{unit}</span> : null}
      </div>
      {meter ? <Meter {...meter} /> : null}
      {sub ? <div className="text-xs text-secondary">{sub}</div> : null}
    </Card>
  );
}
