/** Formatting helpers. A missing value is always "n/a" - never 0 - so a dead sensor can't pass for a cold one. */

export const NA = "n/a";

const nf = (digits: number) => new Intl.NumberFormat("en-US", { maximumFractionDigits: digits, minimumFractionDigits: digits });
const nf0 = nf(0);
const nf1 = nf(1);

export function fmt(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return NA;
  return digits === 0 ? nf0.format(v) : digits === 1 ? nf1.format(v) : nf(digits).format(v);
}

export const fmtTemp = (v: number | null | undefined) => (v == null ? NA : `${fmt(v)} °C`);
export const fmtPct = (v: number | null | undefined, digits = 0) => (v == null ? NA : `${fmt(v, digits)}%`);
export const fmtWatts = (v: number | null | undefined, digits = 0) => (v == null ? NA : `${fmt(v, digits)} W`);
export const fmtRpm = (v: number | null | undefined) => (v == null ? NA : `${fmt(v)} RPM`);

export function fmtClock(mhz: number | null | undefined): string {
  if (mhz == null || !Number.isFinite(mhz)) return NA;
  return mhz >= 1000 ? `${fmt(mhz / 1000, 2)} GHz` : `${fmt(mhz)} MHz`;
}

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${s % 60 ? `${s % 60} s` : ""}`.trim();
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60 ? `${m % 60} min` : ""}`.trim();
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const timeShortFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });

export const fmtClockTime = (unixS: number) => timeFmt.format(new Date(unixS * 1000));
export const fmtClockTimeShort = (unixS: number) => timeShortFmt.format(new Date(unixS * 1000));
export const fmtDateTime = (unixS: number) => dateTimeFmt.format(new Date(unixS * 1000));

/** "12 s ago", "3 min ago". */
export function fmtAgo(unixS: number, nowS = Date.now() / 1000): string {
  const d = nowS - unixS;
  if (d < 5) return "just now";
  return `${fmtDuration(d)} ago`;
}

export function fmtBytesMb(mb: number | null | undefined): string {
  if (mb == null) return NA;
  return mb >= 1024 ? `${fmt(mb / 1024, 1)} GB` : `${fmt(mb)} MB`;
}
