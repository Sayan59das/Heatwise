import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmt, fmtBytesMb } from "@/lib/format";
import type { ProcessInfo } from "@/lib/types";

interface ProcessTableProps {
  processes: ProcessInfo[];
  /** "cpu" sorts and highlights CPU use; "gpu" lists only processes with a GPU share */
  focus?: "cpu" | "gpu";
  title?: string;
  limit?: number;
}

/** Top consumers. Same-named processes are folded into one row (e.g. 18 chrome.exe -> "chrome.exe x18"). */
export function ProcessTable({ processes, focus = "cpu", title, limit = 8 }: ProcessTableProps) {
  const rows =
    focus === "gpu"
      ? processes.filter((p) => (p.gpu_pct ?? 0) > 0).sort((a, b) => (b.gpu_pct ?? 0) - (a.gpu_pct ?? 0))
      : [...processes].sort((a, b) => b.cpu_pct - a.cpu_pct);
  const shown = rows.slice(0, limit);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title ?? (focus === "gpu" ? "Top GPU processes" : "Top processes")}</CardTitle>
        <CardDescription>
          {focus === "gpu"
            ? "Share of GPU engine time per process, as reported by the NVIDIA driver."
            : "CPU is a share of the whole processor (100% = every logical core busy). Sampled every few seconds."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {shown.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted">
            {focus === "gpu" ? "No process is using the GPU right now." : "Waiting for the first process sample."}
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Process</TableHead>
                <TableHead className="text-right">CPU</TableHead>
                <TableHead className="text-right">GPU</TableHead>
                <TableHead className="text-right">Memory</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {shown.map((p) => (
                <TableRow key={p.name}>
                  <TableCell className="max-w-[16rem] truncate text-foreground" title={p.name}>
                    {p.name}
                    {p.count > 1 ? <span className="ml-1.5 text-xs text-muted">×{p.count}</span> : null}
                  </TableCell>
                  <TableCell className="num-tabular text-right">{fmt(p.cpu_pct, 1)}%</TableCell>
                  <TableCell className="num-tabular text-right">{p.gpu_pct == null ? "–" : `${fmt(p.gpu_pct, 0)}%`}</TableCell>
                  <TableCell className="num-tabular text-right">{fmtBytesMb(p.mem_mb)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
