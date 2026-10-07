"use client";

import { AlertTriangle, ChevronDown } from "lucide-react";
import { useState } from "react";
import { Card } from "@/components/ui/card";
import { useLive } from "@/lib/live";
import { cn } from "@/lib/utils";

/**
 * Surfaces what the backend could not read and why ("not elevated", "driver missing", ...). Missing data is
 * never silently shown as zero elsewhere, so this banner is where the reason lives.
 */
export function HealthBanner() {
  const { snapshot } = useLive();
  const [open, setOpen] = useState(false);
  const health = snapshot?.health;
  if (!health || health.warnings.length === 0) return null;
  return (
    <Card className="border-status-warning/40 bg-status-warning/5 p-4" role="status">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-status-warning" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-foreground">
            {health.warnings.length === 1 ? "Some sensor data is unavailable" : `${health.warnings.length} things limit what can be measured`}
          </div>
          <ul className="mt-1 space-y-1 text-sm text-secondary">
            {health.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            className="mt-2 inline-flex items-center gap-1 text-xs text-muted hover:text-foreground"
          >
            <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} aria-hidden />
            Collector status
          </button>
          {open ? (
            <ul className="mt-2 grid gap-1 text-xs sm:grid-cols-2">
              {health.collectors.map((c) => (
                <li key={c.name} className="flex gap-2">
                  <span className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", c.available ? "bg-status-good" : "bg-status-serious")} aria-hidden />
                  <span className="text-secondary">
                    <span className="font-medium text-foreground">{c.name}</span> {c.available ? "ok" : `unavailable: ${c.error ?? "unknown"}`}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      </div>
    </Card>
  );
}
