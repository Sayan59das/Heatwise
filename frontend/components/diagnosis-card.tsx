"use client";

import { Wrench } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { CertaintyBadge, SeverityBadge } from "@/components/status-badges";
import { Meter } from "@/components/stat-tile";
import { fmtAgo, fmtDuration } from "@/lib/format";
import type { Diagnosis } from "@/lib/types";
import { cn } from "@/lib/utils";

const EDGE: Record<Diagnosis["severity"], string> = {
  critical: "border-l-status-critical",
  warning: "border-l-status-warning",
  info: "border-l-accent",
};

const COMPONENT_LABEL: Record<Diagnosis["component"], string> = { cpu: "CPU", gpu: "GPU", system: "System" };

interface DiagnosisCardProps {
  d: Diagnosis;
  /** Compact cards (overview) keep the headline + top evidence; full cards show everything. */
  compact?: boolean;
  nowS?: number;
}

/**
 * One "Why is it hot?" finding. The raw numbers are always shown (evidence), the verdict says whether it
 * was detected or inferred, and confidence is a number as well as a meter.
 */
export function DiagnosisCard({ d, compact = false, nowS }: DiagnosisCardProps) {
  const evidence = compact ? d.evidence.slice(0, 3) : d.evidence;
  const pct = Math.round(d.confidence * 100);
  return (
    <Card className={cn("border-l-4 p-5", EDGE[d.severity], !d.active && "opacity-70")}>
      <div className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={d.severity} />
        <Badge variant="neutral">{COMPONENT_LABEL[d.component]}</Badge>
        <CertaintyBadge certainty={d.certainty} />
        {!d.active ? <Badge variant="neutral">Cleared, shown briefly</Badge> : null}
        <div className="ml-auto flex w-40 items-center gap-2 text-xs text-secondary" title="How strongly the evidence supports this explanation">
          <span className="whitespace-nowrap">Confidence</span>
          <Meter value={pct} max={100} label={`Confidence ${pct}%`} />
          <span className="num-tabular w-9 text-right font-semibold text-foreground">{pct}%</span>
        </div>
      </div>

      <h3 className="mt-3 text-base font-semibold leading-snug text-foreground">{d.diagnosis}</h3>

      <div className="mt-3">
        <div className="mb-1 text-xs font-medium text-muted">Evidence</div>
        <ul className="space-y-1 text-sm text-secondary">
          {evidence.map((line, i) => (
            <li key={i} className="flex gap-2">
              <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-muted" aria-hidden />
              <span>{line}</span>
            </li>
          ))}
        </ul>
        {compact && d.evidence.length > evidence.length ? (
          <div className="mt-1 text-xs text-muted">+{d.evidence.length - evidence.length} more on the Diagnosis page</div>
        ) : null}
      </div>

      {!compact ? (
        <div className="mt-4 flex gap-3 rounded-md border border-border bg-hover/50 p-3 text-sm">
          <Wrench className="mt-0.5 size-4 shrink-0 text-secondary" aria-hidden />
          <div>
            <div className="mb-0.5 text-xs font-medium text-muted">Suggested fix</div>
            <p className="text-secondary">{d.suggested_fix}</p>
          </div>
        </div>
      ) : null}

      <div className="mt-3 text-xs text-muted">
        Active for {fmtDuration(Math.max(0, d.last_seen - d.first_seen))}
        {nowS ? ` · updated ${fmtAgo(d.last_seen, nowS)}` : ""}
      </div>
    </Card>
  );
}
