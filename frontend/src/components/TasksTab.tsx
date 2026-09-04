import { useCallback, useEffect, useMemo, useState } from "react";
import { Columns3, List, ListTodo, RefreshCw, Search } from "lucide-react";
import type { Task, TaskRowMeta, TaskState } from "../types";
import { api } from "../lib/api";
import { useToast } from "./ui/Toast";
import { Card } from "./ui/Card";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Switch } from "./ui/Switch";
import { TaskListView } from "./TaskListView";
import { TaskBoardView } from "./TaskBoardView";
import { TaskDialog } from "./TaskDialog";

export interface TasksTabProps {
  projectSlug: string;
  onChanged?: () => void;
}

/**
 * The task list: requirements parked for later, in asoode's list mode.
 *
 * The capture box at the top is the whole point — a requirement can be dropped
 * in at any moment, from here or from a Claude session, and it waits until
 * someone picks it up. Nothing here starts work; Claude surfaces this list at
 * session start and leaves it alone unless asked.
 */
export function TasksTab({ projectSlug, onChanged }: TasksTabProps) {
  const { toast } = useToast();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [meta, setMeta] = useState<Record<string, TaskRowMeta>>({});
  const [openCount, setOpenCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showDone, setShowDone] = useState(false);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<TaskState>>(new Set());
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);
  // Board or list. Kept per project in localStorage rather than in state alone:
  // the choice is a working preference, and losing it on every reload is the
  // kind of small friction that stops a view being used at all.
  const [mode, setMode] = useState<"list" | "board">(() => {
    try {
      return localStorage.getItem(`tasks-view:${projectSlug}`) === "board"
        ? "board"
        : "list";
    } catch {
      return "list";
    }
  });

  const setViewMode = useCallback(
    (next: "list" | "board") => {
      setMode(next);
      try {
        localStorage.setItem(`tasks-view:${projectSlug}`, next);
      } catch {
        /* a browser with storage blocked still works, it just forgets */
      }
    },
    [projectSlug]
  );


  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.listTasks(projectSlug, { includeDone: showDone });
      setTasks(res.tasks);
      setMeta(res.meta ?? {});
      setOpenCount(res.open);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tasks");
    } finally {
      setLoading(false);
    }
  }, [projectSlug, showDone]);

  useEffect(() => {
    setLoading(true);
    void load();
  }, [load]);

  const refresh = useCallback(async () => {
    await load();
    onChanged?.();
  }, [load, onChanged]);

  /** Dropping a card on a column is the state change - that is what board mode
      is for. Everything else about a task still goes through the dialog. */
  const changeState = useCallback(
    async (task: Task, state: TaskState) => {
      try {
        await api.updateTask(projectSlug, task.id, { state });
        await refresh();
      } catch (err) {
        toast({
          title: "Could not move the task",
          description: err instanceof Error ? err.message : String(err),
          variant: "error",
        });
      }
    },
    [projectSlug, refresh, toast]
  );

  const fail = useCallback(
    (err: unknown, what: string) =>
      toast({
        title: what,
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      }),
    [toast]
  );

  const createTask = useCallback(
    async (title: string, state: TaskState) => {
      try {
        const created = await api.createTask(projectSlug, { title, source: "user" });
        // Tasks are always born as todo; typing one into another group means
        // "put it here", so move it once rather than widening the create API.
        if (state !== "todo") {
          await api.updateTask(projectSlug, created.task.id, { state });
        }
        await refresh();
      } catch (err) {
        fail(err, "Failed to add task");
      }
    },
    [projectSlug, refresh, fail]
  );

  const reorder = useCallback(
    async (orderedIds: string[]) => {
      // Optimistic: the row must not snap back to its old slot while the
      // request is in flight.
      setTasks((prev) => {
        const byId = new Map(prev.map((t) => [t.id, t]));
        const moved = orderedIds
          .map((id) => byId.get(id))
          .filter((t): t is Task => !!t);
        const movedIds = new Set(orderedIds);
        return [...moved, ...prev.filter((t) => !movedIds.has(t.id))];
      });
      try {
        await api.reorderTasks(projectSlug, orderedIds);
      } catch (err) {
        fail(err, "Failed to reorder");
      }
      await refresh();
    },
    [projectSlug, refresh, fail]
  );

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tasks;
    return tasks.filter(
      (task) =>
        task.title.toLowerCase().includes(q) ||
        (task.description ?? "").toLowerCase().includes(q) ||
        task.labels.some((l) => l.toLowerCase().includes(q)) ||
        (task.assignee ?? "").toLowerCase().includes(q)
    );
  }, [tasks, query]);

  const toggleState = (state: TaskState) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(state)) next.delete(state);
      else next.add(state);
      return next;
    });

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-[200px] flex-1">
          <Search className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            placeholder="Filter tasks…"
            onChange={(e) => setQuery(e.target.value)}
            className="h-9 pl-8"
          />
        </div>
        <p className="whitespace-nowrap text-sm text-muted-foreground">
          {openCount} waiting
          {visible.length !== tasks.length && ` · ${visible.length} shown`}
        </p>
        <label className="flex items-center gap-2 whitespace-nowrap text-sm text-muted-foreground">
          <Switch checked={showDone} onCheckedChange={setShowDone} />
          Show finished
        </label>
        <div className="flex items-center gap-0.5 rounded-md border border-input p-0.5">
          <Button
            variant={mode === "list" ? "secondary" : "ghost"}
            size="icon"
            title="List"
            className="size-7"
            onClick={() => setViewMode("list")}
          >
            <List className="size-3.5" />
          </Button>
          <Button
            variant={mode === "board" ? "secondary" : "ghost"}
            size="icon"
            title="Board — drag a card to change its state"
            className="size-7"
            onClick={() => setViewMode("board")}
          >
            <Columns3 className="size-3.5" />
          </Button>
        </div>
        <Button variant="ghost" size="icon" title="Refresh" onClick={() => void refresh()}>
          <RefreshCw className="size-4" />
        </Button>
      </div>

      {loading && <p className="text-sm text-muted-foreground">Loading tasks…</p>}
      {error && <p className="text-sm text-destructive">{error}</p>}

      {!loading && !error && (
        <>
          {tasks.length === 0 && (
            <Card className="flex items-start gap-3 p-4">
              <ListTodo className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
              <div>
                <p className="text-sm font-medium">Nothing queued yet</p>
                <p className="mt-1 text-sm text-muted-foreground">
                  Use <span className="font-medium">+ Add task</span> below, or ask Claude
                  to queue one mid-session with memory_task_add — neither interrupts the
                  work in progress. Tasks wait here until you ask for them.
                </p>
              </div>
            </Card>
          )}

          {/* Always rendered: the list forces a To Do group, so there is always
              somewhere to add the first task. */}
          <Card className="overflow-x-auto p-4">
            {mode === "board" ? (
              <TaskBoardView
                tasks={visible}
                meta={meta}
                onOpenTask={(task) => setOpenTaskId(task.id)}
                onChangeState={changeState}
              />
            ) : (
              <TaskListView
                tasks={visible}
                meta={meta}
                collapsedStates={collapsed}
                onToggleState={toggleState}
                onOpenTask={(task) => setOpenTaskId(task.id)}
                onCreateTask={createTask}
                onReorder={reorder}
              />
            )}
          </Card>
        </>
      )}

      {openTaskId && (
        <TaskDialog
          projectSlug={projectSlug}
          taskId={openTaskId}
          onClose={() => setOpenTaskId(null)}
          onChanged={() => void refresh()}
          onOpenTask={(id) => setOpenTaskId(id)}
        />
      )}
    </div>
  );
}
