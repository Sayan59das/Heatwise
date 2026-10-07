"use client";

import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import { HISTORY_TO_CHART, snapshotToPoint, type ChartKey, type ChartPoint } from "./chart";
import { wsBase } from "./config";
import type { Snapshot } from "./types";

/** connecting: first attempt; live: data flowing; stale: socket open but silent; reconnecting: socket lost. */
export type ConnectionState = "connecting" | "live" | "stale" | "reconnecting";

interface LiveValue {
  snapshot: Snapshot | null;
  /** The last ~5 minutes, oldest first, for the live charts. */
  points: ChartPoint[];
  connection: ConnectionState;
  /** unix seconds of the last message, null before the first one */
  lastMessageAt: number | null;
}

const LiveContext = createContext<LiveValue>({ snapshot: null, points: [], connection: "connecting", lastMessageAt: null });

export const useLive = (): LiveValue => useContext(LiveContext);

const MAX_POINTS = 300;
const STALE_AFTER_MS = 4000;
const MAX_BACKOFF_MS = 10_000;

function isSnapshot(x: unknown): x is Snapshot {
  if (typeof x !== "object" || x === null) return false;
  const o = x as Record<string, unknown>;
  return typeof o.timestamp === "number" && typeof o.cpu === "object" && o.cpu !== null && Array.isArray((o.cpu as { cores?: unknown }).cores);
}

function emptyPoint(t: number): ChartPoint {
  return {
    t,
    cpuTemp: null, cpuMaxCore: null, gpuTemp: null, gpuHotspot: null, gpuMemTemp: null,
    cpuClock: null, cpuMaxClock: null, gpuClock: null, cpuPower: null, gpuPower: null, gpuPowerLimit: null,
    cpuLoad: null, gpuUtil: null, cpuFan: null, gpuFan: null,
  };
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [points, setPoints] = useState<ChartPoint[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null);
  const lastRx = useRef<number>(0);
  const hasOpened = useRef(false);

  // ---- seed the charts from stored history so they are not empty right after a page load -------------
  useEffect(() => {
    const ctl = new AbortController();
    const to = Date.now() / 1000;
    const metrics = Object.keys(HISTORY_TO_CHART);
    api
      .history({ from: to - 300, to: to + 1, metrics, maxPoints: 300 }, ctl.signal)
      .then((h) => {
        const seeded: ChartPoint[] = h.t.map((t, i) => {
          const p = emptyPoint(t);
          for (const m of metrics) {
            const key = HISTORY_TO_CHART[m] as ChartKey;
            p[key] = h.series[m]?.avg[i] ?? null;
          }
          return p;
        });
        setPoints((prev) => {
          const firstLive = prev[0]?.t ?? Number.POSITIVE_INFINITY;
          return [...seeded.filter((p) => p.t < firstLive - 0.5), ...prev].slice(-MAX_POINTS);
        });
      })
      .catch(() => undefined); // no storage / backend not up yet: charts simply start empty
    return () => ctl.abort();
  }, []);

  // ---- the live socket -------------------------------------------------------------------------------
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let closedByUs = false;

    const connect = () => {
      setConnection(hasOpened.current ? "reconnecting" : "connecting");
      try {
        ws = new WebSocket(`${wsBase()}/ws/live`);
      } catch {
        schedule();
        return;
      }
      ws.onopen = () => {
        attempt = 0;
        hasOpened.current = true;
        setConnection("live");
      };
      ws.onmessage = (ev: MessageEvent) => {
        let data: unknown;
        try {
          data = JSON.parse(String(ev.data));
        } catch {
          return; // ignore a malformed frame, keep the stream
        }
        if (!isSnapshot(data)) return;
        lastRx.current = Date.now();
        setLastMessageAt(data.timestamp);
        setSnapshot(data);
        setConnection("live");
        const p = snapshotToPoint(data);
        setPoints((prev) => {
          const last = prev[prev.length - 1];
          if (last && p.t <= last.t) return prev; // duplicate / out-of-order frame
          const next = prev.length >= MAX_POINTS ? prev.slice(prev.length - MAX_POINTS + 1) : prev.slice();
          next.push(p);
          return next;
        });
      };
      ws.onclose = () => {
        if (!closedByUs) schedule();
      };
      ws.onerror = () => ws?.close();
    };

    const schedule = () => {
      setConnection("reconnecting");
      const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempt);
      attempt += 1;
      retryTimer = setTimeout(connect, delay);
    };

    connect();

    // An open socket that has gone quiet (backend hung) is "stale", not "live".
    const watchdog = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN && lastRx.current > 0 && Date.now() - lastRx.current > STALE_AFTER_MS) {
        setConnection("stale");
      }
    }, 1000);

    return () => {
      closedByUs = true;
      clearInterval(watchdog);
      if (retryTimer) clearTimeout(retryTimer);
      ws?.close();
    };
  }, []);

  const value = useMemo<LiveValue>(
    () => ({ snapshot, points, connection, lastMessageAt }),
    [snapshot, points, connection, lastMessageAt],
  );
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}
