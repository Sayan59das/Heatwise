/** "Nice" y-axis: round bounds and ticks to clean numbers (0 / 500 / 1,000 ...), never 203 / 803 / 1,403. */

export interface NiceAxis {
  domain: [number, number];
  ticks: number[];
}

function niceStep(raw: number): number {
  const exp = Math.floor(Math.log10(raw));
  const f = raw / 10 ** exp;
  const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nf * 10 ** exp;
}

const clean = (v: number): number => Number(v.toFixed(10)); // strip floating-point residue

/**
 * @param min   smallest data value
 * @param max   largest data value
 * @param pad   fraction of the range added on each side before rounding, so a line never touches the frame
 * @param count roughly how many intervals to aim for
 */
export function niceAxis(min: number, max: number, pad = 0.05, count = 4): NiceAxis {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { domain: [0, 1], ticks: [0, 1] };
  let lo = min;
  let hi = max;
  if (lo === hi) {
    const p = Math.max(1, Math.abs(lo) * 0.1);
    lo -= p;
    hi += p;
  } else {
    const p = (hi - lo) * pad;
    lo -= p;
    hi += p;
  }
  // Power, clocks, load and RPM can never be negative: padding must not invent an axis below zero.
  if (min >= 0) lo = Math.max(0, lo);
  const step = niceStep((hi - lo) / count);
  const start = clean(Math.floor(lo / step) * step);
  const end = clean(Math.ceil(hi / step) * step);
  const ticks: number[] = [];
  for (let v = start; v <= end + step / 2; v += step) ticks.push(clean(v));
  return { domain: [start, end], ticks };
}

/** Axis for a fixed domain (e.g. 0-100 %): quarter ticks. */
export function fixedAxis(lo: number, hi: number): NiceAxis {
  const step = (hi - lo) / 4;
  return { domain: [lo, hi], ticks: [0, 1, 2, 3, 4].map((i) => clean(lo + step * i)) };
}
