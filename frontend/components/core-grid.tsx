import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { fmt, fmtClock, fmtPct } from "@/lib/format";
import { HEAT_BAND_LABEL, HEAT_STOPS, heatBand, heatColor } from "@/lib/heat";
import { cn } from "@/lib/utils";
import type { CoreKind, CoreReading } from "@/lib/types";

const GROUP_TITLE: Record<CoreKind | "all", string> = {
  P: "Performance cores",
  E: "Efficiency cores",
  all: "Cores",
};

function CoreCell({ core, limit }: { core: CoreReading; limit: number | null }) {
  const t = core.temp_c;
  const band = t == null ? null : heatBand(t, limit);
  const accent = t == null ? "rgb(var(--axis))" : heatColor(t, limit);
  const tint = t == null ? "transparent" : heatColor(t, limit, 0.13);
  const desc = [
    core.label,
    t == null ? "temperature not available" : `${fmt(t)} degrees Celsius, ${HEAT_BAND_LABEL[band!]}`,
    core.clock_mhz != null ? fmtClock(core.clock_mhz) : null,
    core.load_pct != null ? `${fmt(core.load_pct)} percent load` : null,
  ]
    .filter(Boolean)
    .join(", ");
  return (
    <li
      aria-label={desc}
      className="rounded-md border border-border border-l-4 px-3 py-2.5"
      style={{ borderLeftColor: accent, backgroundColor: tint }}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-secondary">{core.label}</span>
        <span className="text-[11px] text-muted">{band ? HEAT_BAND_LABEL[band] : "unavailable"}</span>
      </div>
      <div className="mt-0.5 flex items-baseline gap-1">
        <span className="text-xl font-semibold leading-tight text-foreground">{t == null ? "n/a" : fmt(t)}</span>
        {t != null ? <span className="text-xs text-secondary">°C</span> : null}
      </div>
      <div className="num-tabular mt-0.5 flex justify-between text-[11px] text-muted">
        <span>{fmtClock(core.clock_mhz)}</span>
        <span>{fmtPct(core.load_pct)}</span>
      </div>
    </li>
  );
}

/** The scale the cells are coloured by. Ticks are rescaled to this CPU's own limit. */
export function HeatLegend({ limit }: { limit: number | null }) {
  const l = limit && limit > 40 ? limit : 100;
  const lo = 30;
  const hi = 100;
  const pos = (t: number) => `${((t - lo) / (hi - lo)) * 100}%`;
  const gradient = `linear-gradient(to right, ${HEAT_STOPS.map(([t, [r, g, b]]) => `rgb(${r} ${g} ${b}) ${pos(t)}`).join(", ")})`;
  const ticks: [number, string][] = [
    [40, "Cool"],
    [60, "Normal"],
    [76, "Warm"],
    [87, "Hot"],
    [97, "At limit"],
  ];
  return (
    <div className="max-w-xl" aria-label="Temperature colour scale">
      <div className="h-2 rounded-full" style={{ background: gradient }} />
      <div className="relative mt-1 h-8 text-[11px] text-muted">
        {ticks.map(([t, label], i) => (
          <div
            key={label}
            className={cn(
              "absolute whitespace-nowrap",
              i === ticks.length - 1 ? "-translate-x-full text-right" : "-translate-x-1/2 text-center",
              i % 2 === 1 && "hidden sm:block", // Normal / Hot: dropped on phones where neighbours would touch
            )}
            style={{ left: pos(t) }}
          >
            <div className="num-tabular text-secondary">{Math.round((t * l) / 100)} °C</div>
            <div>{label}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function CoreGrid({ cores, limit }: { cores: CoreReading[]; limit: number | null }) {
  const kinds: (CoreKind | "all")[] = cores.some((c) => c.kind) ? ["P", "E"] : ["all"];
  return (
    <div className="space-y-4">
      {kinds.map((kind) => {
        const group = cores.filter((c) => (kind === "all" ? true : c.kind === kind));
        if (group.length === 0) return null;
        const temps = group.map((c) => c.temp_c).filter((v): v is number => v != null);
        // Intel efficiency cores share one thermal sensor per cluster, so they read identically.
        const shared = temps.length > 2 && temps.every((v) => v === temps[0]);
        return (
          <Card key={kind}>
            <CardHeader>
              <CardTitle>
                {GROUP_TITLE[kind]} <span className="font-normal text-muted">({group.length})</span>
              </CardTitle>
              {shared ? (
                <CardDescription>These cores report one shared sensor reading, so they all show the same temperature.</CardDescription>
              ) : null}
            </CardHeader>
            <CardContent>
              <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
                {group.map((c) => (
                  <CoreCell key={c.index} core={c} limit={limit} />
                ))}
              </ul>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
