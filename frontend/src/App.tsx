import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BookText,
  CheckCircle2,
  ListChecks,
  Moon,
  ShieldAlert,
  Sun,
} from "lucide-react";
import type {
  Memory,
  MemoryStatus,
  Meta,
  Project,
  Session,
} from "./types";
import { api } from "./lib/api";
import { buildCommands } from "./lib/commands";
import { useTheme } from "./hooks/useTheme";
import { useHotkey } from "./hooks/useHotkey";
import { ToastProvider, useToast } from "./components/ui/Toast";
import { Sidebar } from "./components/Sidebar";
import { Tabs } from "./components/ui/Tabs";
import { Button } from "./components/ui/Button";
import { CommandPalette } from "./components/ui/CommandPalette";
import { MemoriesTab } from "./components/MemoriesTab";
import { RulesTab } from "./components/RulesTab";
import { SessionsTab } from "./components/SessionsTab";
import {
  MemoryEditorDialog,
  type MemoryEditorValue,
} from "./components/MemoryEditorDialog";
import { NewProjectDialog } from "./components/NewProjectDialog";
import { ImportDialog } from "./components/ImportDialog";
import { ConfirmDialog } from "./components/ConfirmDialog";

type TabValue = "memories" | "rules" | "sessions";

interface EditorState {
  open: boolean;
  memory?: Memory;
  presetCategory?: string;
  lockCategory?: boolean;
}

function AppInner() {
  const { theme, toggleTheme } = useTheme();
  const { toast } = useToast();

  // boot state
  const [meta, setMeta] = useState<Meta | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [bootLoading, setBootLoading] = useState(true);
  const [bootError, setBootError] = useState<string | null>(null);

  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const [tab, setTab] = useState<TabValue>("memories");

  // memories tab
  const [memories, setMemories] = useState<Memory[]>([]);
  const [memTotal, setMemTotal] = useState(0);
  const [memMode, setMemMode] = useState<"list" | "search">("list");
  const [memLoading, setMemLoading] = useState(false);
  const [memError, setMemError] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [activeQuery, setActiveQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState<MemoryStatus>("active");

  // rules tab
  const [mandatory, setMandatory] = useState<Memory[]>([]);
  const [forbidden, setForbidden] = useState<Memory[]>([]);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [rulesError, setRulesError] = useState<string | null>(null);

  // sessions tab
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState<string | null>(null);

  // dialogs
  const [editor, setEditor] = useState<EditorState>({ open: false });
  const [editorSaving, setEditorSaving] = useState(false);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [projectSaving, setProjectSaving] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importSaving, setImportSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Memory | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);

  const searchRef = useRef<HTMLInputElement>(null);

  const categories = meta?.categories ?? [];
  const selectedProject = useMemo(
    () => projects.find((p) => p.slug === selectedSlug) ?? null,
    [projects, selectedSlug]
  );

  // ----- boot -----
  const loadProjects = useCallback(async () => {
    const res = await api.listProjects();
    setProjects(res.projects);
    return res.projects;
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setBootLoading(true);
      setBootError(null);
      try {
        const [m, projectList] = await Promise.all([
          api.getMeta(),
          api.listProjects().then((r) => r.projects),
        ]);
        if (cancelled) return;
        setMeta(m);
        setProjects(projectList);
        setActiveSlug(m.active_project);
        const initial =
          m.active_project ?? projectList[0]?.slug ?? null;
        setSelectedSlug(initial);
      } catch (err) {
        if (!cancelled) {
          setBootError(
            err instanceof Error ? err.message : "Failed to load"
          );
        }
      } finally {
        if (!cancelled) setBootLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ----- data loaders -----
  const loadMemories = useCallback(async () => {
    if (!selectedSlug) return;
    setMemLoading(true);
    setMemError(null);
    try {
      const res = await api.listMemories(selectedSlug, {
        q: activeQuery || undefined,
        category: categoryFilter === "all" ? undefined : categoryFilter,
        status: statusFilter,
        limit: 100,
      });
      setMemories(res.memories);
      setMemTotal(res.total);
      setMemMode(res.mode);
    } catch (err) {
      setMemError(err instanceof Error ? err.message : "Failed to load");
      setMemories([]);
      setMemTotal(0);
    } finally {
      setMemLoading(false);
    }
  }, [selectedSlug, activeQuery, categoryFilter, statusFilter]);

  const loadRules = useCallback(async () => {
    if (!selectedSlug) return;
    setRulesLoading(true);
    setRulesError(null);
    try {
      const res = await api.getRules(selectedSlug);
      setMandatory(res.mandatory_rules);
      setForbidden(res.forbidden_rules);
    } catch (err) {
      setRulesError(err instanceof Error ? err.message : "Failed to load");
    } finally {
      setRulesLoading(false);
    }
  }, [selectedSlug]);

  const loadSessions = useCallback(async () => {
    if (!selectedSlug) return;
    setSessionsLoading(true);
    setSessionsError(null);
    try {
      const res = await api.getSessions(selectedSlug);
      setSessions(res.sessions);
    } catch (err) {
      setSessionsError(
        err instanceof Error ? err.message : "Failed to load"
      );
    } finally {
      setSessionsLoading(false);
    }
  }, [selectedSlug]);

  useEffect(() => {
    if (selectedSlug && tab === "memories") void loadMemories();
  }, [selectedSlug, tab, loadMemories]);

  useEffect(() => {
    if (selectedSlug && tab === "rules") void loadRules();
  }, [selectedSlug, tab, loadRules]);

  useEffect(() => {
    if (selectedSlug && tab === "sessions") void loadSessions();
  }, [selectedSlug, tab, loadSessions]);

  // ----- actions -----
  const refreshAll = useCallback(() => {
    void loadProjects();
    if (tab === "memories") void loadMemories();
    if (tab === "rules") void loadRules();
    if (tab === "sessions") void loadSessions();
    toast({ title: "Refreshed", variant: "success" });
  }, [tab, loadProjects, loadMemories, loadRules, loadSessions, toast]);

  const setActiveProject = useCallback(
    async (slug: string) => {
      try {
        const res = await api.setActive(slug);
        setActiveSlug(res.active_project);
        const project = projects.find((p) => p.slug === slug);
        toast({
          title: "Active project set",
          description: project?.display_name ?? slug,
          variant: "success",
        });
      } catch (err) {
        toast({
          title: "Failed to set active project",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      }
    },
    [projects, toast]
  );

  const handleSearchSubmit = useCallback(() => {
    setActiveQuery(searchInput.trim());
  }, [searchInput]);

  const handleClearSearch = useCallback(() => {
    setSearchInput("");
    setActiveQuery("");
  }, []);

  const focusSearch = useCallback(() => {
    setTab("memories");
    window.setTimeout(() => searchRef.current?.focus(), 60);
  }, []);

  const filterByCategory = useCallback((category: string) => {
    setTab("memories");
    setCategoryFilter(category);
  }, []);

  const openNewMemory = useCallback((category?: string) => {
    const isRule =
      category === "mandatory_rules" || category === "forbidden_rules";
    setEditor({
      open: true,
      presetCategory: category,
      lockCategory: isRule,
    });
    if (isRule) setTab("rules");
  }, []);

  const saveMemory = useCallback(
    async (value: MemoryEditorValue) => {
      if (!selectedSlug) return;
      setEditorSaving(true);
      try {
        if (editor.memory) {
          await api.updateMemory(selectedSlug, editor.memory.id, {
            title: value.title,
            content: value.content,
            tags: value.tags,
            priority: value.priority,
            status: value.status,
          });
          toast({ title: "Memory updated", variant: "success" });
        } else {
          await api.createMemory(selectedSlug, {
            category: value.category,
            title: value.title,
            content: value.content,
            tags: value.tags,
            priority: value.priority,
          });
          toast({ title: "Memory created", variant: "success" });
        }
        setEditor({ open: false });
        void loadMemories();
        void loadRules();
        void loadProjects();
      } catch (err) {
        toast({
          title: "Failed to save memory",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      } finally {
        setEditorSaving(false);
      }
    },
    [selectedSlug, editor.memory, loadMemories, loadRules, loadProjects, toast]
  );

  const confirmDelete = useCallback(async () => {
    if (!selectedSlug || !deleteTarget) return;
    setDeleteBusy(true);
    try {
      await api.deleteMemory(selectedSlug, deleteTarget.id, {
        reason: "Deleted from management UI",
      });
      toast({ title: "Memory deleted", variant: "success" });
      setDeleteTarget(null);
      void loadMemories();
      void loadRules();
      void loadProjects();
    } catch (err) {
      toast({
        title: "Failed to delete memory",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setDeleteBusy(false);
    }
  }, [selectedSlug, deleteTarget, loadMemories, loadRules, loadProjects, toast]);

  const createProject = useCallback(
    async (input: { slug: string; display_name: string; description?: string }) => {
      setProjectSaving(true);
      try {
        const res = await api.createProject(input);
        toast({ title: "Project created", variant: "success" });
        setNewProjectOpen(false);
        await loadProjects();
        setSelectedSlug(res.project.slug);
      } catch (err) {
        toast({
          title: "Failed to create project",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      } finally {
        setProjectSaving(false);
      }
    },
    [loadProjects, toast]
  );

  const runImport = useCallback(
    async (input: { path: string; stub_rewrite: boolean }) => {
      if (!selectedSlug) return;
      setImportSaving(true);
      try {
        const res = await api.importClaudeMd(selectedSlug, input);
        toast({
          title: "CLAUDE.md imported",
          description: `${res.imported} section(s), ${res.memories} memories`,
          variant: "success",
        });
        setImportOpen(false);
        void loadMemories();
        void loadRules();
        void loadProjects();
      } catch (err) {
        toast({
          title: "Import failed",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      } finally {
        setImportSaving(false);
      }
    },
    [selectedSlug, loadMemories, loadRules, loadProjects, toast]
  );

  // ----- command palette -----
  const commands = useMemo(
    () =>
      buildCommands({
        projects,
        activeSlug,
        selectedSlug,
        categories,
        selectProject: (slug) => setSelectedSlug(slug),
        setActiveProject: (slug) => void setActiveProject(slug),
        newMemory: openNewMemory,
        newProject: () => setNewProjectOpen(true),
        goToTab: (t) => setTab(t),
        focusSearch,
        filterByCategory,
        importClaudeMd: () => setImportOpen(true),
        refresh: refreshAll,
        toggleTheme,
      }),
    [
      projects,
      activeSlug,
      selectedSlug,
      categories,
      setActiveProject,
      openNewMemory,
      focusSearch,
      filterByCategory,
      refreshAll,
      toggleTheme,
    ]
  );

  useHotkey("mod+k", (e) => {
    e.preventDefault();
    setPaletteOpen((o) => !o);
  });

  const tabs = [
    { value: "memories", label: "Memories", icon: <BookText /> },
    { value: "rules", label: "Rules", icon: <ShieldAlert /> },
    { value: "sessions", label: "Sessions", icon: <ListChecks /> },
  ];

  return (
    <div className="flex h-full overflow-hidden">
      <Sidebar
        projects={projects}
        selectedSlug={selectedSlug}
        activeSlug={activeSlug}
        onSelect={setSelectedSlug}
        onNewProject={() => setNewProjectOpen(true)}
        onOpenPalette={() => setPaletteOpen(true)}
        loading={bootLoading}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-start justify-between gap-4 border-b border-border px-6 py-4">
          <div className="min-w-0">
            {selectedProject ? (
              <>
                <div className="flex items-center gap-2">
                  <h1 className="truncate text-lg font-semibold">
                    {selectedProject.display_name}
                  </h1>
                  {activeSlug === selectedProject.slug && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-500">
                      <CheckCircle2 className="size-3" />
                      active
                    </span>
                  )}
                </div>
                <p className="truncate text-sm text-muted-foreground">
                  {selectedProject.description || selectedProject.slug}
                </p>
              </>
            ) : (
              <h1 className="text-lg font-semibold">No project selected</h1>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {selectedProject && activeSlug !== selectedProject.slug && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => void setActiveProject(selectedProject.slug)}
              >
                Set as active
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon"
              onClick={toggleTheme}
              aria-label="Toggle theme"
            >
              {theme === "dark" ? <Sun /> : <Moon />}
            </Button>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5 scrollbar-thin">
          {bootError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
              {bootError}
            </div>
          )}

          {!bootError && !selectedProject && !bootLoading && (
            <div className="rounded-lg border border-dashed border-border py-20 text-center">
              <p className="text-sm text-muted-foreground">
                Create or select a project from the sidebar.
              </p>
            </div>
          )}

          {selectedProject && (
            <div className="space-y-5">
              <Tabs
                tabs={tabs}
                value={tab}
                onValueChange={(v) => setTab(v as TabValue)}
              />

              {tab === "memories" && (
                <MemoriesTab
                  ref={searchRef}
                  projectSlug={selectedProject.slug}
                  memories={memories}
                  total={memTotal}
                  mode={memMode}
                  loading={memLoading}
                  error={memError}
                  categories={categories}
                  searchInput={searchInput}
                  categoryFilter={categoryFilter}
                  statusFilter={statusFilter}
                  onSearchInputChange={setSearchInput}
                  onSearchSubmit={handleSearchSubmit}
                  onClearSearch={handleClearSearch}
                  onCategoryChange={setCategoryFilter}
                  onStatusChange={setStatusFilter}
                  onNewMemory={() => openNewMemory()}
                  onImport={() => setImportOpen(true)}
                  onRefresh={() => void loadMemories()}
                  onEdit={(m) => setEditor({ open: true, memory: m })}
                  onDelete={(m) => setDeleteTarget(m)}
                />
              )}

              {tab === "rules" && (
                <RulesTab
                  mandatory={mandatory}
                  forbidden={forbidden}
                  loading={rulesLoading}
                  error={rulesError}
                  onAdd={(category) => openNewMemory(category)}
                  onEdit={(m) =>
                    setEditor({ open: true, memory: m, lockCategory: true })
                  }
                  onDelete={(m) => setDeleteTarget(m)}
                />
              )}

              {tab === "sessions" && (
                <SessionsTab
                  sessions={sessions}
                  loading={sessionsLoading}
                  error={sessionsError}
                />
              )}
            </div>
          )}
        </div>
      </main>

      <MemoryEditorDialog
        open={editor.open}
        onClose={() => setEditor({ open: false })}
        memory={editor.memory}
        presetCategory={editor.presetCategory}
        lockCategory={editor.lockCategory}
        categories={categories}
        saving={editorSaving}
        onSave={saveMemory}
      />

      <NewProjectDialog
        open={newProjectOpen}
        onClose={() => setNewProjectOpen(false)}
        saving={projectSaving}
        onCreate={createProject}
      />

      <ImportDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        saving={importSaving}
        onImport={runImport}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="Delete memory"
        description={
          deleteTarget
            ? `“${deleteTarget.title}” will be archived. This can be undone from the backend.`
            : undefined
        }
        confirmLabel="Delete"
        destructive
        busy={deleteBusy}
        onConfirm={confirmDelete}
        onClose={() => setDeleteTarget(null)}
      />

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        commands={commands}
      />
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <AppInner />
    </ToastProvider>
  );
}
