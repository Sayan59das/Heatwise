/**
 * Where the backend lives. Resolution order:
 *   1. window.thermalsense.apiBase  (injected by the Electron preload script)
 *   2. ?api=...  (loopback hosts only - handy for development)
 *   3. NEXT_PUBLIC_API_BASE
 *   4. http://127.0.0.1:8765
 */

const DEFAULT_BASE = "http://127.0.0.1:8765";
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

declare global {
  interface Window {
    thermalsense?: { apiBase?: string };
  }
}

function sanitize(candidate: string | null | undefined): string | null {
  if (!candidate) return null;
  try {
    const u = new URL(candidate);
    // The sensor API must never be read from (or sent to) a non-local host.
    if ((u.protocol === "http:" || u.protocol === "https:") && LOOPBACK.has(u.hostname)) {
      return u.origin;
    }
  } catch {
    /* fall through */
  }
  return null;
}

export function apiBase(): string {
  if (typeof window !== "undefined") {
    const injected = sanitize(window.thermalsense?.apiBase);
    if (injected) return injected;
    const fromQuery = sanitize(new URLSearchParams(window.location.search).get("api"));
    if (fromQuery) return fromQuery;
  }
  return sanitize(process.env.NEXT_PUBLIC_API_BASE) ?? DEFAULT_BASE;
}

export function wsBase(): string {
  return apiBase().replace(/^http/, "ws");
}
