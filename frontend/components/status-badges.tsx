import { AlertOctagon, AlertTriangle, CheckCircle2, CircleHelp, Flame, Gauge, Info, Plug, ShieldAlert, Zap } from "lucide-react";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import type { Certainty, Severity, ThrottleComponent, ThrottleStatus, ThrottleType } from "@/lib/types";

/** Severity, always icon + word. */
export function SeverityBadge({ severity }: { severity: Severity }) {
  if (severity === "critical")
    return (
      <Badge variant="critical">
        <AlertOctagon aria-hidden /> Critical
      </Badge>
    );
  if (severity === "warning")
    return (
      <Badge variant="warning">
        <AlertTriangle aria-hidden /> Warning
      </Badge>
    );
  return (
    <Badge variant="accent">
      <Info aria-hidden /> Info
    </Badge>
  );
}

/** "Detected" = a hardware flag or direct measurement. "Inferred" = derived from numbers: a hypothesis. */
export function CertaintyBadge({ certainty }: { certainty: Certainty }) {
  return certainty === "detected" ? (
    <Badge variant="neutral" title="Reported by a hardware flag or measured directly">
      <ShieldAlert aria-hidden /> Detected
    </Badge>
  ) : (
    <Badge variant="neutral" title="Derived from clocks, temperatures, power or process usage. A hypothesis, not a measurement.">
      <CircleHelp aria-hidden /> Inferred
    </Badge>
  );
}

const TYPE_LABEL: Record<ThrottleType, string> = {
  thermal: "thermal throttling",
  power: "power limiting",
  prochot: "PROCHOT throttling",
  current: "current limiting",
  other: "throttling",
};

function typeIcon(t: ThrottleType | null): ReactNode {
  if (t === "thermal" || t === "prochot") return <Flame aria-hidden />;
  if (t === "power") return <Zap aria-hidden />;
  if (t === "current") return <Plug aria-hidden />;
  return <Gauge aria-hidden />;
}

function typeVariant(t: ThrottleType | null): "critical" | "serious" | "warning" {
  if (t === "thermal" || t === "prochot") return "critical";
  if (t === "power" || t === "current") return "warning";
  return "serious";
}

/**
 * One throttle verdict as icon + words. An inferred verdict is worded "likely ..." and says so; an
 * unknown one says "unknown" rather than defaulting to "fine".
 */
export function ThrottleBadge({ throttle, scope }: { throttle: ThrottleComponent | ThrottleStatus | null; scope?: string }) {
  const prefix = scope ? `${scope}: ` : "";
  if (!throttle || throttle.active === null || throttle.confidence === "unavailable") {
    return (
      <Badge variant="neutral" title="No throttle flag or clock/temperature data is available to judge this.">
        <CircleHelp aria-hidden /> {prefix}Throttle status unknown
      </Badge>
    );
  }
  const how = throttle.confidence === "detected" ? "detected" : "inferred";
  if (!throttle.active) {
    return (
      <Badge variant="good" title={`Source: ${throttle.source} (${how})`}>
        <CheckCircle2 aria-hidden /> {prefix}Not throttling{throttle.confidence === "inferred" ? " (inferred)" : ""}
      </Badge>
    );
  }
  const where = "component" in throttle && throttle.component ? `${throttle.component.toUpperCase()} ` : "";
  const what = TYPE_LABEL[throttle.type ?? "other"];
  const words = throttle.confidence === "inferred" ? `Likely ${where}${what}` : `${where}${what}`.replace(/^./, (c) => c.toUpperCase());
  return (
    <Badge variant={typeVariant(throttle.type)} title={`${throttle.reasons.join("; ") || "No detail"}. Source: ${throttle.source} (${how})`}>
      {typeIcon(throttle.type)} {prefix}
      {words}
      {throttle.confidence === "inferred" ? " (inferred)" : ""}
    </Badge>
  );
}
