import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BookText,
  CheckCircle2,
  Download,
  FolderGit2,
  Link2,
  ListChecks,
  ListTodo,
  Moon,
  Pencil,
  ShieldAlert,
  Sparkles,
  Sun,
} from "lucide-react";
import type {
  BulkAddRuleResult,
  Memory,
  MemoryStatus,
  Meta,
  Project,
  ProjectUpdate,
  Session,
} from "./types";
import { api, setUnauthorizedHandler } from "./lib/api";
import { buildCommands } from "./lib/commands";
import { LoginScreen } from "./components/auth/LoginScreen";
import { UserMenu } from "./components/auth/UserMenu";
import { ModerationQueue } from "./components/admin/ModerationQueue";
import { UsersView } from "./components/admin/UsersView";
import { OrgRulesView } from "./components/admin/OrgRulesView";
import { useTheme } from "./hooks/useTheme";
import { useHotkey } from "./hooks/useHotkey";
import { ToastProvider, useToast } from "./components/ui/Toast";
import { Sidebar, type SidebarView } from "./components/Sidebar";
import { Tabs } from "./components/ui/Tabs";
import { Button } from "./components/ui/Button";
import { CommandPalette } from "./components/ui/CommandPalette";
import { MemoriesTab } from "./components/MemoriesTab";
import { PendingTab } from "./components/PendingTab";
import { TasksTab } from "./components/TasksTab";
import { RulesTab } from "./components/RulesTab";
import { SessionsTab } from "./components/SessionsTab";
import {
  MemoryEditorDialog,
  type MemoryEditorValue,
} from "./components/MemoryEditorDialog";
import { NewProjectDialog } from "./components/NewProjectDialog";
import { LinkFolderDialog } from "./components/LinkFolderDialog";
import { ImportDialog } from "./components/ImportDialog";
import { ImportRulesDialog } from "./components/ImportRulesDialog";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { TemplatesView } from "./components/TemplatesView";
import { IntegrationsView } from "./components/IntegrationsView";
import { EditProjectDialog } from "./components/EditProjectDialog";
import { BulkAddRuleDialog } from "./components/BulkAddRuleDialog";

type TabValue = "memories" | "rules" | "tasks" | "pending" | "sessions";

interface EditorState {
  open: boolean;
  memory?: Memory;
  presetCategory?: string;
  lockCategory?: boolean;
}

function AppInner({ onLoggedOut }: { onLoggedOut: () => void }) {
  const { theme, toggleTheme } = useTheme();
  const { toast } = useToast();

  // boot state
  const [meta, setMeta] = useState<Meta | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [bootLoading, setBootLoading] = useState(true);
  const [bootError, setBootError] = useState<string | null>(null);

  // Governance is only active in server mode; in local mode these are all
  // falsy/null and every governance affordance stays hidden.
  const serverMode = meta?.mode === "server";
  const currentUser = meta?.current_user ?? null;
  const isAdmin = meta?.role === "admin";

  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const [tab, setTab] = useState<TabValue>("memories");
  const [pendingCount, setPendingCount] = useState(0);
  const [openTaskCount, setOpenTaskCount] = useState(0);
  const [view, setView] = useState<SidebarView>("projects");
  const isAdminView =
    view === "moderation" || view === "users" || view === "org-rules";
  const [newTemplateNonce, setNewTemplateNonce] = useState(0);

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
  const [importRulesOpen, setImportRulesOpen] = useState(false);
  const [linkFolderOpen, setLinkFolderOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Memory | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [editProjectOpen, setEditProjectOpen] = useState(false);
  const [editProjectSaving, setEditProjectSaving] = useState(false);
  const [bulkRuleOpen, setBulkRuleOpen] = useState(false);

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

  const loadTaskCount = useCallback(async () => {
    if (!selectedSlug) {
      setOpenTaskCount(0);
      return;
    }
    try {
      const res = await api.listTasks(selectedSlug);
      setOpenTaskCount(res.open);
    } catch {
      setOpenTaskCount(0); // a count is a hint, never a reason to break the view
    }
  }, [selectedSlug]);

  const loadPendingCount = useCallback(async () => {
    if (!selectedSlug) {
      setPendingCount(0);
      return;
    }
    try {
      const res = await api.listPendingAdaptations(selectedSlug);
      setPendingCount(res.total);
    } catch {
      setPendingCount(0); // a count is a hint, never a reason to break the view
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

  // The pending count drives the tab label, so it loads with the project rather
  // than only when its own tab is open - otherwise nothing would reveal that
  // imports are sitting there un-adapted.
  useEffect(() => {
    void loadPendingCount();
  }, [loadPendingCount]);

  useEffect(() => {
    void loadTaskCount();
  }, [loadTaskCount]);

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
    setView("projects");
    setTab("memories");
    window.setTimeout(() => searchRef.current?.focus(), 60);
  }, []);

  const filterByCategory = useCallback((category: string) => {
    setView("projects");
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

  const approveRule = useCallback(
    async (memory: Memory) => {
      if (!selectedSlug) return;
      try {
        await api.approveRule(selectedSlug, memory.id);
        toast({ title: "Rule approved", variant: "success" });
        void loadRules();
      } catch (err) {
        toast({
          title: "Failed to approve rule",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      }
    },
    [selectedSlug, loadRules, toast]
  );

  const revokeRule = useCallback(
    async (memory: Memory) => {
      if (!selectedSlug) return;
      try {
        await api.revokeRule(selectedSlug, memory.id);
        toast({ title: "Rule revoked", variant: "success" });
        void loadRules();
      } catch (err) {
        toast({
          title: "Failed to revoke rule",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      }
    },
    [selectedSlug, loadRules, toast]
  );

  const createProject = useCallback(
    async (input: {
      slug: string;
      display_name: string;
      description?: string;
      project_path?: string;
    }): Promise<string | null> => {
      setProjectSaving(true);
      try {
        const res = await api.createProject(input);
        toast({ title: "Project created", variant: "success" });
        await loadProjects();
        setSelectedSlug(res.project.slug);
        return res.project.slug;
      } catch (err) {
        toast({
          title: "Failed to create project",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
        return null;
      } finally {
        setProjectSaving(false);
      }
    },
    [loadProjects, toast]
  );

  const linkFolder = useCallback(
    async (path: string) => {
      if (!selectedSlug) return;
      const res = await api.linkFolder(selectedSlug, path);
      await loadProjects();
      setLinkFolderOpen(false);
      toast({
        title: "Folder linked",
        description: res.project.project_path ?? path,
        variant: "success",
      });
    },
    [selectedSlug, loadProjects, toast]
  );

  const updateProject = useCallback(
    async (slug: string, input: ProjectUpdate) => {
      setEditProjectSaving(true);
      try {
        await api.updateProject(slug, input);
        toast({ title: "Project updated", variant: "success" });
        setEditProjectOpen(false);
        await loadProjects();
      } catch (err) {
        toast({
          title: "Failed to update project",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      } finally {
        setEditProjectSaving(false);
      }
    },
    [loadProjects, toast]
  );

  const handleSeeded = useCallback(
    (summary: string) => {
      toast({
        title: "Rules imported",
        description: summary,
        variant: "success",
      });
      void loadProjects();
      if (tab === "memories") void loadMemories();
      if (tab === "rules") void loadRules();
    },
    [tab, loadProjects, loadMemories, loadRules, toast]
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
        selectedProjectName: selectedProject?.display_name ?? null,
        categories,
        selectProject: (slug) => {
          setView("projects");
          setSelectedSlug(slug);
        },
        setActiveProject: (slug) => void setActiveProject(slug),
        newMemory: openNewMemory,
        newProject: () => setNewProjectOpen(true),
        newTemplate: () => {
          setView("templates");
          setNewTemplateNonce((n) => n + 1);
        },
        goToTab: (t) => {
          setView("projects");
          setTab(t);
        },
        goToTemplates: () => setView("templates"),
        focusSearch,
        filterByCategory,
        importClaudeMd: () => setImportOpen(true),
        importRules: () => {
          setView("projects");
          setImportRulesOpen(true);
        },
        bulkAddRule: () => {
          setView("projects");
          setBulkRuleOpen(true);
        },
        refresh: refreshAll,
        toggleTheme,
        admin:
          serverMode && isAdmin
            ? {
                goToModeration: () => setView("moderation"),
                goToUsers: () => setView("users"),
                goToOrgRules: () => setView("org-rules"),
                logout: () => {
                  void api.logout().finally(onLoggedOut);
                },
              }
            : undefined,
      }),
    [
      projects,
      activeSlug,
      selectedSlug,
      selectedProject,
      categories,
      setActiveProject,
      openNewMemory,
      focusSearch,
      filterByCategory,
      refreshAll,
      toggleTheme,
      serverMode,
      isAdmin,
      onLoggedOut,
    ]
  );

  useHotkey("mod+k", (e) => {
    e.preventDefault();
    setPaletteOpen((o) => !o);
  });

  const tabs = [
    { value: "memories", label: "Memories", icon: <BookText /> },
    { value: "rules", label: "Rules", icon: <ShieldAlert /> },
    {
      value: "pending",
      // The count is the point: un-adapted imports are invisible everywhere
      // else, so the tab label is the only place they announce themselves.
      label: pendingCount > 0 ? `Pending (${pendingCount})` : "Pending",
      icon: <Sparkles />,
    },
    {
      value: "tasks",
      // Same trick as Pending: the count is how a queued requirement announces
      // itself without anyone having to open the tab.
      label: openTaskCount > 0 ? `Tasks (${openTaskCount})` : "Tasks",
      icon: <ListTodo />,
    },
    { value: "sessions", label: "Sessions", icon: <ListChecks /> },
  ];

  return (
    <div className="flex h-full overflow-hidden">
      <Sidebar
        view={view}
        onViewChange={setView}
        projects={projects}
        selectedSlug={selectedSlug}
        activeSlug={activeSlug}
        onSelect={(slug) => {
          setView("projects");
          setSelectedSlug(slug);
        }}
        onNewProject={() => setNewProjectOpen(true)}
        onNewTemplate={() => {
          setView("templates");
          setNewTemplateNonce((n) => n + 1);
        }}
        onOpenPalette={() => setPaletteOpen(true)}
        loading={bootLoading}
        serverMode={serverMode}
        isAdmin={isAdmin}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-start justify-between gap-4 border-b border-border px-6 py-4">
          <div className="min-w-0">
            {isAdminView ? (
              <h1 className="text-lg font-semibold capitalize">
                {view === "org-rules" ? "Org-wide rules" : view}
              </h1>
            ) : view === "integrations" ? (
              <>
                <h1 className="text-lg font-semibold">Integrations</h1>
                <p className="truncate text-sm text-muted-foreground">
                  asoode endpoints, the machine-wide token, and which board each
                  project mirrors to.
                </p>
              </>
            ) : view === "templates" ? (
              <>
                <h1 className="text-lg font-semibold">Templates</h1>
                <p className="truncate text-sm text-muted-foreground">
                  Reusable rule sets for seeding new projects.
                </p>
              </>
            ) : selectedProject ? (
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
                {selectedProject.project_path ? (
                  <p className="mt-1 flex items-center gap-1.5 truncate text-xs text-muted-foreground">
                    <FolderGit2 className="size-3.5 shrink-0" />
                    <span className="truncate font-mono">
                      {selectedProject.project_path}
                    </span>
                  </p>
                ) : (
                  <button
                    onClick={() => setLinkFolderOpen(true)}
                    className="mt-1 inline-flex items-center gap-1.5 rounded-md text-xs text-muted-foreground transition-colors hover:text-foreground"
                  >
                    <Link2 className="size-3.5" />
                    Link folder
                  </button>
                )}
              </>
            ) : (
              <h1 className="text-lg font-semibold">No project selected</h1>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {view === "projects" && selectedProject && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setEditProjectOpen(true)}
              >
                <Pencil />
                Edit
              </Button>
            )}
            {view === "projects" && selectedProject && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setImportRulesOpen(true)}
              >
                <Download />
                Import rules
              </Button>
            )}
            {view === "projects" &&
              selectedProject &&
              activeSlug !== selectedProject.slug && (
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
            {serverMode && currentUser && (
              <UserMenu
                username={currentUser.username}
                role={currentUser.role}
                onLoggedOut={onLoggedOut}
              />
            )}
          </div>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5 scrollbar-thin">
          {bootError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
              {bootError}
            </div>
          )}

          {view === "integrations" && <IntegrationsView projects={projects} />}

          {view === "templates" && (
            <TemplatesView
              categories={categories}
              newTemplateNonce={newTemplateNonce}
            />
          )}

          {/* Admin views: server mode + admin only (server also enforces 403). */}
          {serverMode && isAdmin && view === "moderation" && <ModerationQueue />}
          {serverMode && isAdmin && view === "users" && <UsersView />}
          {serverMode && isAdmin && view === "org-rules" && <OrgRulesView />}

          {view === "projects" &&
            !bootError &&
            !selectedProject &&
            !bootLoading && (
              <div className="rounded-lg border border-dashed border-border py-20 text-center">
                <p className="text-sm text-muted-foreground">
                  Create or select a project from the sidebar.
                </p>
              </div>
            )}

          {view === "projects" && selectedProject && (
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
                  onBulkAdd={() => setBulkRuleOpen(true)}
                  serverMode={serverMode}
                  isAdmin={isAdmin}
                  onApprove={approveRule}
                  onRevoke={revokeRule}
                />
              )}

              {tab === "tasks" && (
                <TasksTab
                  projectSlug={selectedProject.slug}
                  onChanged={() => void loadTaskCount()}
                />
              )}

              {tab === "pending" && (
                <PendingTab
                  projectSlug={selectedProject.slug}
                  onChanged={() => {
                    void loadPendingCount();
                    void loadRules();
                    void loadMemories();
                  }}
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
        projects={projects}
        onCreate={createProject}
        onSeeded={handleSeeded}
      />

      <ImportDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        saving={importSaving}
        onImport={runImport}
      />

      {selectedProject && (
        <LinkFolderDialog
          open={linkFolderOpen}
          onClose={() => setLinkFolderOpen(false)}
          projectName={selectedProject.display_name}
          onLink={linkFolder}
        />
      )}

      {selectedProject && (
        <ImportRulesDialog
          open={importRulesOpen}
          onClose={() => setImportRulesOpen(false)}
          targetSlug={selectedProject.slug}
          targetName={selectedProject.display_name}
          projects={projects}
          onDone={(summary) => {
            setImportRulesOpen(false);
            handleSeeded(summary);
          }}
        />
      )}

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

      {selectedProject && (
        <EditProjectDialog
          open={editProjectOpen}
          onClose={() => setEditProjectOpen(false)}
          saving={editProjectSaving}
          project={selectedProject}
          onSave={updateProject}
        />
      )}

      <BulkAddRuleDialog
        open={bulkRuleOpen}
        onClose={() => setBulkRuleOpen(false)}
        projects={projects}
        onDone={(result: BulkAddRuleResult) => {
          setBulkRuleOpen(false);
          toast({
            title: `Rule added to ${result.added} project${
              result.added === 1 ? "" : "s"
            }`,
            description:
              result.added < result.total
                ? `${result.total - result.added} project(s) failed`
                : undefined,
            variant: "success",
          });
          void loadProjects();
          if (tab === "rules") void loadRules();
        }}
      />

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        commands={commands}
      />
    </div>
  );
}

/**
 * Gates the app behind login in server mode. In local mode (or any backend that
 * doesn't report a mode) it renders the app immediately - byte-for-byte the
 * previous behavior.
 */
function AuthGate() {
  const [phase, setPhase] = useState<"loading" | "login" | "app" | "error">(
    "loading"
  );
  const [error, setError] = useState<string | null>(null);

  const check = useCallback(async () => {
    setPhase("loading");
    setError(null);
    try {
      const m = await api.getMeta();
      if (m.mode === "server" && !m.current_user) setPhase("login");
      else setPhase("app");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reach the server");
      setPhase("error");
    }
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  // A stale session surfaces as a 401 mid-app: re-check and fall back to login.
  useEffect(() => {
    setUnauthorizedHandler(() => void check());
    return () => setUnauthorizedHandler(null);
  }, [check]);

  if (phase === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }
  if (phase === "error") {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 p-4 text-center">
        <p className="text-sm text-destructive">{error}</p>
        <Button variant="outline" size="sm" onClick={() => void check()}>
          Retry
        </Button>
      </div>
    );
  }
  if (phase === "login") {
    return <LoginScreen onLoggedIn={check} />;
  }
  return <AppInner onLoggedOut={check} />;
}

export default function App() {
  return (
    <ToastProvider>
      <AuthGate />
    </ToastProvider>
  );
}
