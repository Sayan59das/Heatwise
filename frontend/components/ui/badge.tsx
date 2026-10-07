import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * Status variants are reserved for meaning (good / warning / serious / critical). Callers pair them with
 * an icon and a text label, so colour is never the only signal.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap [&_svg]:size-3.5 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        neutral: "border-border bg-hover text-secondary",
        accent: "border-accent/40 bg-accent/10 text-foreground",
        good: "border-status-good/40 bg-status-good/10 text-foreground",
        warning: "border-status-warning/40 bg-status-warning/10 text-foreground",
        serious: "border-status-serious/40 bg-status-serious/10 text-foreground",
        critical: "border-status-critical/50 bg-status-critical/15 text-foreground",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
);

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
