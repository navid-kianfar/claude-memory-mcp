import { useCallback, useEffect, useMemo, useState } from "react";
import { ListTodo, RefreshCw, Search } from "lucide-react";
import type { Task, TaskRowMeta, TaskState } from "../types";
import { api } from "../lib/api";
import { useToast } from "./ui/Toast";
import { Card } from "./ui/Card";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Switch } from "./ui/Switch";
import { TaskListView } from "./TaskListView";
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
            <TaskListView
              tasks={visible}
              meta={meta}
              collapsedStates={collapsed}
              onToggleState={toggleState}
              onOpenTask={(task) => setOpenTaskId(task.id)}
              onCreateTask={createTask}
              onReorder={reorder}
            />
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
