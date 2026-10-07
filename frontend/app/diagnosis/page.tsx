"use client";

import { CheckCircle2, Loader2, MinusCircle } from "lucide-react";
import { DiagnosisCard } from "@/components/diagnosis-card";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { fmtAgo } from "@/lib/format";
import { useFetched } from "@/lib/hooks";

export default function DiagnosisPage() {
  const report = useFetched((s) => api.diagnosis(s), "diagnosis", 5000);
  const r = report.data;
  const nowS = r?.generated_at;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-foreground">Diagnosis</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          A rules-based engine reads the last few minutes of data and explains <em>why</em> the machine is hot or slow. Every finding shows
          the numbers it is based on and says whether it was <strong className="font-medium text-secondary">detected</strong> by a hardware
          flag or only <strong className="font-medium text-secondary">inferred</strong> from the data, which is a hypothesis, not a fact.
        </p>
      </div>

      {report.error && !r ? (
        <Card className="p-5 text-sm text-secondary">The diagnosis engine is unavailable: {report.error}</Card>
      ) : !r ? (
        <Card className="flex items-center gap-3 p-5 text-sm text-secondary">
          <Loader2 className="size-4 animate-spin" aria-hidden /> Loading…
        </Card>
      ) : (
        <>
          <Card className="flex flex-wrap items-center gap-x-6 gap-y-2 p-5">
            {r.status === "ok" ? <CheckCircle2 className="size-5 text-status-good" aria-hidden /> : r.status === "warming_up" ? <Loader2 className="size-5 animate-spin text-muted" aria-hidden /> : null}
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-foreground">{r.summary}</div>
              <div className="mt-0.5 text-xs text-muted">
                Looked at {Math.round(r.window_s)} s of data ({r.window_samples} samples) · updated {fmtAgo(r.generated_at)}
              </div>
            </div>
          </Card>

          {r.diagnoses.length > 0 ? (
            <section className="space-y-3" aria-label="Current findings" aria-live="polite">
              {r.diagnoses.map((d) => (
                <DiagnosisCard key={d.key} d={d} nowS={nowS} />
              ))}
            </section>
          ) : null}

          {r.recently_resolved.length > 0 ? (
            <section className="space-y-3" aria-label="Recently resolved">
              <h2 className="text-sm font-semibold text-foreground">Recently resolved</h2>
              {r.recently_resolved.map((d) => (
                <DiagnosisCard key={`${d.key}-${d.last_seen}`} d={d} compact nowS={nowS} />
              ))}
            </section>
          ) : null}

          <Card>
            <CardHeader>
              <CardTitle>Checks that could not run</CardTitle>
              <CardDescription>
                If a check is missing the data it needs, it is listed here instead of silently passing, so “nothing found” never hides “could not look”.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {r.not_evaluated.length === 0 ? (
                <p className="text-sm text-secondary">All checks had the data they need.</p>
              ) : (
                <ul className="space-y-3">
                  {r.not_evaluated.map((s) => (
                    <li key={s.rule_id} className="flex gap-3 text-sm">
                      <MinusCircle className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />
                      <div>
                        <div className="text-foreground">{s.title}</div>
                        <div className="text-xs text-muted">{s.reason}</div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
