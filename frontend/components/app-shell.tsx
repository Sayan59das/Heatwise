"use client";

import { Activity, Cpu, History, LayoutDashboard, Loader2, MonitorCog, Stethoscope, Thermometer, WifiOff, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { HealthBanner } from "@/components/health-banner";
import { ThrottleBadge } from "@/components/status-badges";
import { Badge } from "@/components/ui/badge";
import { TooltipProvider } from "@/components/ui/tooltip";
import { fmtAgo } from "@/lib/format";
import { LiveProvider, useLive, type ConnectionState } from "@/lib/live";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

const NAV: NavItem[] = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/cores", label: "Per-core", icon: Cpu },
  { href: "/gpu", label: "GPU", icon: MonitorCog },
  { href: "/history", label: "History", icon: History },
  { href: "/diagnosis", label: "Diagnosis", icon: Stethoscope },
];

function isActive(pathname: string, href: string): boolean {
  const p = pathname.replace(/\/+$/, "") || "/";
  return href === "/" ? p === "/" : p === href || p.startsWith(`${href}/`);
}

function ConnectionPill({ state, lastMessageAt }: { state: ConnectionState; lastMessageAt: number | null }) {
  const common = "gap-1.5";
  if (state === "live")
    return (
      <Badge variant="good" className={common} title="Receiving a sample every second">
        <Activity aria-hidden /> Live
      </Badge>
    );
  if (state === "stale")
    return (
      <Badge variant="warning" className={common} title="The connection is open but no data has arrived recently">
        <Loader2 className="animate-spin" aria-hidden /> No data{lastMessageAt ? ` since ${fmtAgo(lastMessageAt)}` : ""}
      </Badge>
    );
  if (state === "reconnecting")
    return (
      <Badge variant="serious" className={common} title="Lost the backend; retrying with backoff">
        <WifiOff aria-hidden /> Reconnecting
      </Badge>
    );
  return (
    <Badge variant="neutral" className={common}>
      <Loader2 className="animate-spin" aria-hidden /> Connecting
    </Badge>
  );
}

function NavLinks({ pathname, orientation }: { pathname: string; orientation: "vertical" | "horizontal" }) {
  return (
    <>
      {NAV.map(({ href, label, icon: Icon }) => {
        const active = isActive(pathname, href);
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
              orientation === "horizontal" && "shrink-0",
              active ? "bg-hover font-medium text-foreground" : "text-secondary hover:bg-hover/60 hover:text-foreground",
            )}
          >
            <Icon className="size-4" aria-hidden />
            {label}
          </Link>
        );
      })}
    </>
  );
}

function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";
  const { snapshot, connection, lastMessageAt } = useLive();
  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col gap-1 border-r border-border bg-card/40 p-4 md:flex">
        <div className="mb-4 flex items-center gap-2 px-2 text-foreground">
          <Thermometer className="size-5 text-accent" aria-hidden />
          <span className="text-base font-semibold tracking-tight">ThermalSense</span>
        </div>
        <nav aria-label="Primary" className="flex flex-col gap-1">
          <NavLinks pathname={pathname} orientation="vertical" />
        </nav>
        <div className="mt-auto px-2 text-xs text-muted">
          {snapshot?.cpu.name ?? "Detecting hardware…"}
          {snapshot?.gpu?.name ? <div className="mt-0.5">{snapshot.gpu.name}</div> : null}
        </div>
      </aside>

      <div className="min-w-0 flex-1">
        <header className="sticky top-0 z-20 border-b border-border bg-background/85 backdrop-blur">
          <div className="flex min-h-14 flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2 md:px-6">
            <div className="flex items-center gap-2 md:hidden">
              <Thermometer className="size-5 text-accent" aria-hidden />
              <span className="font-semibold tracking-tight">ThermalSense</span>
            </div>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <ThrottleBadge throttle={snapshot?.throttle ?? null} />
              <ConnectionPill state={connection} lastMessageAt={lastMessageAt} />
            </div>
          </div>
          <nav aria-label="Primary" className="flex gap-1 overflow-x-auto px-3 pb-2 md:hidden">
            <NavLinks pathname={pathname} orientation="horizontal" />
          </nav>
        </header>

        <main className="mx-auto max-w-[1400px] space-y-6 p-4 md:p-6">
          <HealthBanner />
          {children}
        </main>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <LiveProvider>
      <TooltipProvider delayDuration={150}>
        <Shell>{children}</Shell>
      </TooltipProvider>
    </LiveProvider>
  );
}
