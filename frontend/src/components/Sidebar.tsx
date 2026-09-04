import {
  Brain,
  Globe,
  LayoutTemplate,
  Plus,
  ShieldCheck,
  Users,
  Command as CommandIcon,
  Plug,
} from "lucide-react";
import type { Project } from "../types";
import { cn } from "../lib/utils";
import { Button } from "./ui/Button";
import { Badge } from "./ui/Badge";

export type SidebarView =
  | "projects"
  | "templates"
  | "moderation"
  | "users"
  | "org-rules"
  | "integrations";

const ADMIN_NAV: { view: SidebarView; label: string; icon: React.ReactNode }[] = [
  { view: "moderation", label: "Moderation", icon: <ShieldCheck className="size-3.5" /> },
  { view: "org-rules", label: "Org-wide rules", icon: <Globe className="size-3.5" /> },
  { view: "users", label: "Users", icon: <Users className="size-3.5" /> },
];

export interface SidebarProps {
  view: SidebarView;
  onViewChange: (view: SidebarView) => void;
  projects: Project[];
  selectedSlug: string | null;
  activeSlug: string | null;
  onSelect: (slug: string) => void;
  onNewProject: () => void;
  onNewTemplate: () => void;
  onOpenPalette: () => void;
  loading: boolean;
  /** Server mode + admin unlocks the admin nav (moderation/users/org-rules). */
  serverMode?: boolean;
  isAdmin?: boolean;
}

export function Sidebar({
  view,
  onViewChange,
  projects,
  selectedSlug,
  activeSlug,
  onSelect,
  onNewProject,
  onNewTemplate,
  onOpenPalette,
  loading,
  serverMode,
  isAdmin,
}: SidebarProps) {
  const showAdmin = Boolean(serverMode && isAdmin);
  // Read before the branches below narrow `view` away from this value.
  const isIntegrations = view === "integrations";
  return (
    <aside className="flex h-full w-72 shrink-0 flex-col border-r border-border bg-card">
      <div className="flex items-center gap-2.5 px-4 py-4">
        <div className="flex size-9 items-center justify-center rounded-lg bg-primary/15 text-primary">
          <Brain className="size-5" />
        </div>
        <div className="leading-tight">
          <p className="text-sm font-semibold">Claude Memory</p>
          <p className="text-xs text-muted-foreground">Project memory DB</p>
        </div>
      </div>

      <div className="px-3 pb-2">
        <button
          onClick={onOpenPalette}
          className="flex w-full items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <span className="flex items-center gap-2">
            <CommandIcon className="size-3.5" />
            Commands
          </span>
          <kbd className="rounded border border-border px-1.5 py-0.5 text-[10px]">
            ⌘K
          </kbd>
        </button>
      </div>

      <div className="px-3 pb-1 pt-1">
        <div className="grid grid-cols-2 gap-1 rounded-lg bg-muted p-1">
          <button
            onClick={() => onViewChange("projects")}
            className={cn(
              "rounded-md px-2 py-1 text-xs font-medium transition-colors",
              view === "projects"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            Projects
          </button>
          <button
            onClick={() => onViewChange("templates")}
            className={cn(
              "flex items-center justify-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium transition-colors",
              view === "templates"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            <LayoutTemplate className="size-3.5" />
            Templates
          </button>
        </div>
      </div>

      {showAdmin && (
        <div className="px-3 pt-2">
          <div className="px-1 pb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Admin
          </div>
          <div className="space-y-1">
            {ADMIN_NAV.map((item) => (
              <button
                key={item.view}
                onClick={() => onViewChange(item.view)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-sm transition-colors",
                  view === item.view
                    ? "bg-primary/10 text-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground"
                )}
              >
                {item.icon}
                {item.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {view === "projects" ? (
        <>
          <div className="px-4 pb-1.5 pt-3 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Projects
          </div>

          <nav className="flex-1 space-y-1 overflow-y-auto px-3 pb-3 scrollbar-thin">
            {loading && projects.length === 0 && (
              <div className="space-y-1.5 px-1 py-2">
                {[0, 1, 2].map((i) => (
                  <div
                    key={i}
                    className="h-14 animate-pulse rounded-md bg-muted"
                  />
                ))}
              </div>
            )}
            {!loading && projects.length === 0 && (
              <p className="px-1 py-4 text-sm text-muted-foreground">
                No projects yet. Create one to get started.
              </p>
            )}
            {projects.map((p) => {
              const selected = p.slug === selectedSlug;
              const active = p.slug === activeSlug;
              return (
                <button
                  key={p.slug}
                  onClick={() => onSelect(p.slug)}
                  className={cn(
                    "w-full rounded-md border px-3 py-2 text-left transition-colors",
                    selected
                      ? "border-primary/40 bg-primary/10"
                      : "border-transparent hover:bg-accent"
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">
                      {p.display_name}
                    </span>
                    {active && (
                      <Badge variant="success" className="shrink-0">
                        active
                      </Badge>
                    )}
                  </div>
                  <div className="mt-0.5 flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-xs text-muted-foreground">
                      {p.slug}
                    </span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {p.memory_count} mem
                    </span>
                  </div>
                </button>
              );
            })}
          </nav>

          <div className="border-t border-border p-3">
            <Button className="w-full" onClick={onNewProject}>
              <Plus />
              New Project
            </Button>
          </div>
        </>
      ) : view === "templates" ? (
        <>
          <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
            <div className="flex size-12 items-center justify-center rounded-xl bg-primary/15 text-primary">
              <LayoutTemplate className="size-6" />
            </div>
            <p className="mt-3 text-sm font-medium">Reusable rule sets</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Define baseline rules once, then seed new projects from them.
            </p>
          </div>

          <div className="border-t border-border p-3">
            <Button className="w-full" onClick={onNewTemplate}>
              <Plus />
              New Template
            </Button>
          </div>
        </>
      ) : (
        <div className="flex-1" />
      )}

      {/* Global, not per project: the endpoints and the token are machine-wide,
          and putting them on a project would imply otherwise. Rendered outside
          the view branches so it is reachable from all of them. */}
      <div className="border-t border-border p-3">
        <button
          onClick={() => onViewChange("integrations")}
          className={cn(
            "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs font-medium transition-colors",
            isIntegrations
              ? "bg-accent text-foreground"
              : "text-muted-foreground hover:bg-accent hover:text-foreground"
          )}
        >
          <Plug className="size-3.5" />
          Integrations
        </button>
      </div>
    </aside>
  );
}
