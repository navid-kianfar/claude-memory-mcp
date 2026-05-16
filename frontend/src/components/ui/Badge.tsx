import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export type BadgeVariant =
  | "default"
  | "secondary"
  | "outline"
  | "destructive"
  | "success";

const variants: Record<BadgeVariant, string> = {
  default: "bg-primary/15 text-primary border-primary/30",
  secondary: "bg-secondary text-secondary-foreground border-transparent",
  outline: "text-foreground border-border",
  destructive: "bg-destructive/15 text-destructive border-destructive/30",
  success:
    "bg-emerald-500/15 text-emerald-500 border-emerald-500/30",
};

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
}

export function Badge({ className, variant = "default", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium transition-colors",
        variants[variant],
        className
      )}
      {...props}
    />
  );
}

const CATEGORY_STYLES: Record<string, string> = {
  decision: "bg-blue-500/15 text-blue-400 border-blue-500/30",
  session: "bg-slate-500/15 text-slate-400 border-slate-500/30",
  sprint: "bg-cyan-500/15 text-cyan-400 border-cyan-500/30",
  project_plan: "bg-violet-500/15 text-violet-400 border-violet-500/30",
  architecture: "bg-indigo-500/15 text-indigo-400 border-indigo-500/30",
  devops: "bg-orange-500/15 text-orange-400 border-orange-500/30",
  mandatory_rules: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  forbidden_rules: "bg-red-500/15 text-red-400 border-red-500/30",
  developer_docs: "bg-teal-500/15 text-teal-400 border-teal-500/30",
  feedback: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  reference: "bg-fuchsia-500/15 text-fuchsia-400 border-fuchsia-500/30",
};

export interface CategoryBadgeProps extends HTMLAttributes<HTMLSpanElement> {
  category: string;
}

export function CategoryBadge({
  category,
  className,
  ...props
}: CategoryBadgeProps) {
  const style =
    CATEGORY_STYLES[category] ??
    "bg-secondary text-secondary-foreground border-transparent";
  const label = category.replace(/[_-]+/g, " ");
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium capitalize",
        style,
        className
      )}
      {...props}
    >
      {label}
    </span>
  );
}
