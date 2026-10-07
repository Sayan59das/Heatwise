"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface Fetched<T> {
  data: T | null;
  /** message of the most recent failure; `data` keeps the last good value */
  error: string | null;
  /** true while a (re)fetch is in flight. The previous render stays on screen, dimmed: no skeleton flash. */
  refreshing: boolean;
  reload: () => void;
}

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));
const isAbort = (e: unknown): boolean => e instanceof DOMException && e.name === "AbortError";

/**
 * Fetch on mount and whenever `key` changes, optionally repeating every `intervalMs`.
 * `fetcher` must be stable per `key` (it is read through a ref, so identity changes don't refetch).
 */
export function useFetched<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  key: string,
  intervalMs: number | null = null,
): Fetched<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [tick, setTick] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    const ctl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const run = async () => {
      setRefreshing(true);
      try {
        const result = await fetcherRef.current(ctl.signal);
        if (cancelled) return;
        setData(result);
        setError(null);
      } catch (e) {
        if (cancelled || isAbort(e)) return;
        setError(message(e));
      } finally {
        if (!cancelled) {
          setRefreshing(false);
          if (intervalMs) timer = setTimeout(run, intervalMs);
        }
      }
    };
    void run();

    return () => {
      cancelled = true;
      ctl.abort();
      if (timer) clearTimeout(timer);
    };
  }, [key, intervalMs, tick]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { data, error, refreshing, reload };
}
