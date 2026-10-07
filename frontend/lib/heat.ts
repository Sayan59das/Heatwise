/**
 * Semantic heat scale for the per-core grid. Multi-hue sequential is one of the data-viz method's allowed
 * exceptions *because* it is semantic (cold -> hot) and ships with a visible scale legend; every cell also
 * prints its number, so colour is never the only channel.
 */

type Stop = readonly [celsius: number, rgb: readonly [number, number, number]];

// Stops are for a 100 degC junction limit; other limits are rescaled onto it (see normalise()).
const STOPS: readonly Stop[] = [
  [35, [57, 135, 229]], //  blue    #3987e5  cool
  [55, [25, 158, 112]], //  aqua    #199e70  fine
  [70, [201, 133, 0]], //   yellow  #c98500  warm
  [82, [217, 89, 38]], //   orange  #d95926  hot
  [95, [230, 103, 103]], // red     #e66767  at the limit
];

export const HEAT_STOPS = STOPS;

/** Rescale so that `limit` maps to the red end of the scale (AMD TjMax 95 reads as hot as Intel 100). */
export function normalise(celsius: number, limit: number | null | undefined): number {
  const l = limit && limit > 40 ? limit : 100;
  return (celsius * 100) / l;
}

export function heatRgb(celsius: number, limit?: number | null): [number, number, number] {
  const t = normalise(celsius, limit);
  const first = STOPS[0]!;
  const last = STOPS[STOPS.length - 1]!;
  if (t <= first[0]) return [...first[1]];
  if (t >= last[0]) return [...last[1]];
  for (let i = 1; i < STOPS.length; i++) {
    const [t1, c1] = STOPS[i]!;
    const [t0, c0] = STOPS[i - 1]!;
    if (t <= t1) {
      const f = (t - t0) / (t1 - t0);
      return [
        Math.round(c0[0] + (c1[0] - c0[0]) * f),
        Math.round(c0[1] + (c1[1] - c0[1]) * f),
        Math.round(c0[2] + (c1[2] - c0[2]) * f),
      ];
    }
  }
  return [...last[1]];
}

export const heatColor = (celsius: number, limit?: number | null, alpha = 1): string => {
  const [r, g, b] = heatRgb(celsius, limit);
  return `rgb(${r} ${g} ${b} / ${alpha})`;
};

export type HeatBand = "cool" | "normal" | "warm" | "hot" | "critical";

/** Words for the same bands, so the grid legend and cell labels never depend on hue alone. */
export function heatBand(celsius: number, limit?: number | null): HeatBand {
  const t = normalise(celsius, limit);
  if (t < 50) return "cool";
  if (t < 70) return "normal";
  if (t < 82) return "warm";
  if (t < 92) return "hot";
  return "critical";
}

export const HEAT_BAND_LABEL: Record<HeatBand, string> = {
  cool: "Cool",
  normal: "Normal",
  warm: "Warm",
  hot: "Hot",
  critical: "At limit",
};
