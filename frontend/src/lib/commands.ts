import type { Project } from "../types";
import { titleCase } from "./utils";

export type CommandGroup =
  | "Projects"
  | "Create"
  | "Navigate"
  | "Filters"
  | "Actions";

export interface Command {
  id: string;
  group: CommandGroup;
  label: string;
  hint?: string;
  keywords?: string;
  run: () => void;
}

export interface CommandContext {
  projects: Project[];
  activeSlug: string | null;
  selectedSlug: string | null;
  selectedProjectName: string | null;
  categories: string[];
  selectProject: (slug: string) => void;
  setActiveProject: (slug: string) => void;
  newMemory: (category?: string) => void;
  newProject: () => void;
  newTemplate: () => void;
  goToTab: (tab: "memories" | "rules" | "sessions") => void;
  goToTemplates: () => void;
  focusSearch: () => void;
  filterByCategory: (category: string) => void;
  importClaudeMd: () => void;
  importRules: () => void;
  bulkAddRule: () => void;
  refresh: () => void;
  toggleTheme: () => void;
}

/**
 * Builds the full, ordered command list from current app state.
 * Extend by appending more `Command` objects here.
 */
export function buildCommands(ctx: CommandContext): Command[] {
  const commands: Command[] = [];

  for (const p of ctx.projects) {
    commands.push({
      id: `switch:${p.slug}`,
      group: "Projects",
      label: `Switch to project: ${p.display_name}`,
      hint: p.slug,
      keywords: `${p.slug} project switch open`,
      run: () => ctx.selectProject(p.slug),
    });
  }
  for (const p of ctx.projects) {
    commands.push({
      id: `active:${p.slug}`,
      group: "Projects",
      label: `Set active project: ${p.display_name}`,
      hint: p.slug,
      keywords: `${p.slug} active default`,
      run: () => ctx.setActiveProject(p.slug),
    });
  }

  commands.push(
    {
      id: "new:memory",
      group: "Create",
      label: "New memory",
      keywords: "create add memory",
      run: () => ctx.newMemory(),
    },
    {
      id: "new:mandatory",
      group: "Create",
      label: "New mandatory rule",
      keywords: "create add rule mandatory must",
      run: () => ctx.newMemory("mandatory_rules"),
    },
    {
      id: "new:forbidden",
      group: "Create",
      label: "New forbidden rule",
      keywords: "create add rule forbidden never",
      run: () => ctx.newMemory("forbidden_rules"),
    },
    {
      id: "new:project",
      group: "Create",
      label: "New project",
      keywords: "create add project",
      run: () => ctx.newProject(),
    },
    {
      id: "new:template",
      group: "Create",
      label: "New template",
      keywords: "create add template rule set baseline",
      run: () => ctx.newTemplate(),
    }
  );

  commands.push(
    {
      id: "go:memories",
      group: "Navigate",
      label: "Go to Memories",
      keywords: "tab memories list",
      run: () => ctx.goToTab("memories"),
    },
    {
      id: "go:rules",
      group: "Navigate",
      label: "Go to Rules",
      keywords: "tab rules mandatory forbidden",
      run: () => ctx.goToTab("rules"),
    },
    {
      id: "go:sessions",
      group: "Navigate",
      label: "Go to Sessions",
      keywords: "tab sessions history",
      run: () => ctx.goToTab("sessions"),
    },
    {
      id: "go:templates",
      group: "Navigate",
      label: "Go to Templates",
      keywords: "templates rule set baseline reusable",
      run: () => ctx.goToTemplates(),
    },
    {
      id: "search:focus",
      group: "Navigate",
      label: "Search memories…",
      keywords: "find search query semantic",
      run: () => ctx.focusSearch(),
    }
  );

  for (const cat of ctx.categories) {
    commands.push({
      id: `filter:${cat}`,
      group: "Filters",
      label: `Filter by category: ${titleCase(cat)}`,
      hint: cat,
      keywords: `${cat} filter category`,
      run: () => ctx.filterByCategory(cat),
    });
  }

  commands.push(
    {
      id: "action:import",
      group: "Actions",
      label: "Import CLAUDE.md",
      keywords: "import claude md markdown",
      run: () => ctx.importClaudeMd(),
    }
  );

  if (ctx.selectedSlug) {
    commands.push({
      id: "action:import-rules",
      group: "Actions",
      label: `Import rules into ${ctx.selectedProjectName ?? ctx.selectedSlug}`,
      hint: ctx.selectedSlug,
      keywords: "import rules template project seed baseline",
      run: () => ctx.importRules(),
    });
  }

  commands.push(
    {
      id: "action:bulk-rule",
      group: "Actions",
      label: "Add a rule to multiple projects",
      keywords: "bulk rule all projects global mass apply many",
      run: () => ctx.bulkAddRule(),
    },
    {
      id: "action:refresh",
      group: "Actions",
      label: "Refresh data",
      keywords: "refresh reload sync",
      run: () => ctx.refresh(),
    },
    {
      id: "action:theme",
      group: "Actions",
      label: "Toggle light/dark theme",
      keywords: "theme dark light appearance",
      run: () => ctx.toggleTheme(),
    }
  );

  return commands;
}

export function filterCommands(commands: Command[], query: string): Command[] {
  const q = query.trim().toLowerCase();
  if (!q) return commands;
  const terms = q.split(/\s+/);
  return commands.filter((c) => {
    const haystack = `${c.label} ${c.hint ?? ""} ${
      c.keywords ?? ""
    }`.toLowerCase();
    return terms.every((t) => haystack.includes(t));
  });
}
