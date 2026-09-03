import { useMemo, useState } from "react";
import {
  Calendar,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  Flag,
  GripVertical,
  ListTree,
  Lock,
  MessageSquare,
  Plus,
  Timer,
  User,
  X,
} from "lucide-react";
import type { Task, TaskRowMeta, TaskState } from "../types";
import { cn } from "../lib/utils";
import {
  STATE_COLORS,
  STATE_LABELS,
  STATE_ORDER,
  avatarColor,
  formatDate,
  formatDuration,
  initials,
  isOverdue,
  priorityColor,
} from "../lib/tasks";
import { Input } from "./ui/Input";

export interface TaskListViewProps {
  tasks: Task[];
  meta: Record<string, TaskRowMeta>;
  onOpenTask: (task: Task) => void;
  onCreateTask: (title: string, state: TaskState) => Promise<void> | void;
  onReorder: (orderedIds: string[]) => Promise<void> | void;
  collapsedStates: Set<TaskState>;
  onToggleState: (state: TaskState) => void;
}

/**
 * Tasks in asoode's list mode: one collapsible group per state, a fixed column
 * grid, and a row that opens the task dialog.
 *
 * asoode groups by board column; this store has no columns, so the state IS the
 * group — which is why the group pill is painted with the state colour and the
 * inline "add task" row inside a group creates a task already in that state.
 */
export function TaskListView({
  tasks,
  meta,
  onOpenTask,
  onCreateTask,
  onReorder,
  collapsedStates,
  onToggleState,
}: TaskListViewProps) {
  const groups = useMemo(() => {
    const byState = new Map<TaskState, Task[]>();
    for (const task of tasks) {
      const bucket = byState.get(task.state);
      if (bucket) bucket.push(task);
      else byState.set(task.state, [task]);
    }
    return STATE_ORDER.filter((state) => byState.has(state)).map((state) => ({
      state,
      tasks: byState.get(state) as Task[],
    }));
  }, [tasks]);

  // "todo" always shows, even when empty, so there is somewhere to add the
  // first task without hunting for a button.
  const withTodo = useMemo(() => {
    if (groups.some((g) => g.state === "todo")) return groups;
    return [{ state: "todo" as TaskState, tasks: [] }, ...groups];
  }, [groups]);

  return (
    <div className="min-w-fit">
      {/* Column header */}
      <div className="flex h-9 min-h-9 min-w-fit select-none items-center border-b-2 border-border text-[0.7rem] font-semibold uppercase tracking-wide text-muted-foreground">
        <div className="w-5 min-w-5" />
        <div className="flex min-w-[280px] flex-1 items-center px-3">Name</div>
        <div className="flex w-[120px] min-w-[120px] items-center justify-center px-3">
          Assignee
        </div>
        <div className="flex w-[140px] min-w-[140px] items-center justify-center px-3">
          Due date
        </div>
        <div className="flex w-[100px] min-w-[100px] items-center justify-center px-3">
          Priority
        </div>
        <div className="w-10 min-w-10" />
      </div>

      {withTodo.map((group) => (
        <StateGroup
          key={group.state}
          state={group.state}
          tasks={group.tasks}
          meta={meta}
          expanded={!collapsedStates.has(group.state)}
          onToggle={() => onToggleState(group.state)}
          onOpenTask={onOpenTask}
          onCreateTask={onCreateTask}
          onReorder={onReorder}
        />
      ))}
    </div>
  );
}

interface StateGroupProps {
  state: TaskState;
  tasks: Task[];
  meta: Record<string, TaskRowMeta>;
  expanded: boolean;
  onToggle: () => void;
  onOpenTask: (task: Task) => void;
  onCreateTask: (title: string, state: TaskState) => Promise<void> | void;
  onReorder: (orderedIds: string[]) => Promise<void> | void;
}

function StateGroup({
  state,
  tasks,
  meta,
  expanded,
  onToggle,
  onOpenTask,
  onCreateTask,
  onReorder,
}: StateGroupProps) {
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [saving, setSaving] = useState(false);
  // Drag state is per group: a task is reordered within its own state, the way
  // asoode reorders within a list. Changing state is what the dialog is for.
  const [dragId, setDragId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);

  const drop = async (targetId: string) => {
    const from = tasks.findIndex((t) => t.id === dragId);
    const to = tasks.findIndex((t) => t.id === targetId);
    setDragId(null);
    setOverId(null);
    if (from === -1 || to === -1 || from === to) return;
    const next = [...tasks];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    await onReorder(next.map((t) => t.id));
  };

  const create = async () => {
    const value = title.trim();
    if (!value || saving) return;
    setSaving(true);
    try {
      await onCreateTask(value, state);
      // Stay open and reset, so several can be typed in a row.
      setTitle("");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mb-2">
      <div
        onClick={onToggle}
        className="group/header mt-3 flex h-[38px] cursor-pointer select-none items-center gap-2 border-b border-border px-1"
      >
        {expanded ? (
          <ChevronDown className="size-4 text-muted-foreground" />
        ) : (
          <ChevronRight className="size-4 text-muted-foreground" />
        )}
        <div
          className="min-w-[60px] rounded px-2.5 py-0.5 text-center text-[0.7rem] font-bold uppercase tracking-wide text-white"
          style={{ backgroundColor: STATE_COLORS[state] }}
        >
          {STATE_LABELS[state]}
        </div>
        <span className="ml-0.5 text-[0.8rem] text-muted-foreground">{tasks.length}</span>
        <div className="ml-auto flex items-center gap-2 pr-3 text-muted-foreground opacity-0 transition-opacity group-hover/header:opacity-100">
          <Plus
            className="size-4"
            onClick={(e) => {
              e.stopPropagation();
              setAdding(true);
            }}
          />
        </div>
      </div>

      {expanded && (
        <>
          {tasks.map((task) => (
            <TaskRow
              key={task.id}
              task={task}
              meta={meta[task.id]}
              onOpen={() => onOpenTask(task)}
              dragging={dragId === task.id}
              dropTarget={overId === task.id && dragId !== task.id}
              onDragStart={() => setDragId(task.id)}
              onDragEnd={() => {
                setDragId(null);
                setOverId(null);
              }}
              onDragOver={() => dragId && setOverId(task.id)}
              onDrop={() => void drop(task.id)}
            />
          ))}

          {!adding ? (
            <div
              onClick={() => setAdding(true)}
              className="flex h-10 cursor-pointer items-center pl-[50px] text-[0.85rem] font-medium text-muted-foreground transition-colors hover:bg-primary/[0.04] hover:text-primary"
            >
              <Plus className="mr-2 size-4" />
              <span>Add task</span>
            </div>
          ) : (
            <div className="flex min-h-10 items-center border-b border-border bg-primary/[0.02]">
              <div className="w-5 min-w-5" />
              <div className="flex w-6 min-w-6 items-center justify-center">
                <Circle className="size-[0.9rem] text-muted-foreground/50" />
              </div>
              <div className="flex min-w-[280px] flex-1 items-center gap-2 pr-4">
                <Input
                  autoFocus
                  value={title}
                  placeholder="Task name, then Enter"
                  onChange={(e) => setTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void create();
                    } else if (e.key === "Escape") {
                      setAdding(false);
                      setTitle("");
                    }
                  }}
                  className="h-9 flex-1 border-0 bg-transparent text-[0.85rem] shadow-none focus-visible:ring-0"
                />
                <button
                  type="button"
                  disabled={!title.trim() || saving}
                  onClick={() => void create()}
                  className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
                >
                  <Check className="size-4" />
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setAdding(false);
                    setTitle("");
                  }}
                  className="flex size-7 items-center justify-center rounded-md bg-foreground/[0.06] text-muted-foreground transition-colors hover:bg-foreground/10"
                >
                  <X className="size-4" />
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function TaskRow({
  task,
  meta,
  onOpen,
  dragging,
  dropTarget,
  onDragStart,
  onDragEnd,
  onDragOver,
  onDrop,
}: {
  task: Task;
  meta?: TaskRowMeta;
  onOpen: () => void;
  dragging: boolean;
  dropTarget: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
  onDragOver: () => void;
  onDrop: () => void;
}) {
  const overdue = isOverdue(task);
  return (
    <div
      onClick={onOpen}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = "move";
        // Firefox refuses to start a drag without payload.
        e.dataTransfer.setData("text/plain", task.id);
        onDragStart();
      }}
      onDragEnd={onDragEnd}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        onDragOver();
      }}
      onDrop={(e) => {
        e.preventDefault();
        onDrop();
      }}
      className={cn(
        "group/row flex min-h-10 min-w-fit cursor-pointer items-center border-b border-border transition-colors hover:bg-foreground/[0.02]",
        task.archived_at && "opacity-50",
        dragging && "opacity-40",
        dropTarget && "border-t-2 border-t-primary"
      )}
    >
      <div className="flex w-5 min-w-5 cursor-grab items-center justify-center text-muted-foreground opacity-0 transition-opacity group-hover/row:opacity-100 active:cursor-grabbing">
        <GripVertical className="size-3.5" />
      </div>

      {/* State */}
      <div className="flex w-6 min-w-6 items-center justify-center">
        <Circle
          className="size-[0.9rem]"
          style={{ color: STATE_COLORS[task.state] }}
          fill={task.state === "done" ? STATE_COLORS[task.state] : "none"}
        />
      </div>

      {/* Title + inline meta */}
      <div className="flex min-w-[280px] flex-1 items-center gap-2 overflow-hidden px-3">
        <span className="truncate text-[0.85rem] text-foreground">{task.title}</span>
        <div className="flex shrink-0 items-center gap-1.5 text-[0.7rem] text-muted-foreground">
          {!!meta?.comments && (
            <span className="inline-flex items-center gap-0.5" title="Comments">
              <MessageSquare className="size-3" />
              {meta.comments}
            </span>
          )}
          {!!meta?.subtasks_total && (
            <span className="inline-flex items-center gap-0.5" title="Sub-tasks">
              <ListTree className="size-3" />
              {meta.subtasks_done}/{meta.subtasks_total}
            </span>
          )}
          {!!meta?.minutes_spent && (
            <span
              className={cn(
                "inline-flex items-center gap-0.5",
                meta.running && "font-medium text-sky-500"
              )}
              title={meta.running ? "Clock running" : "Time tracked"}
            >
              <Timer className="size-3" />
              {formatDuration(meta.minutes_spent)}
            </span>
          )}
          {task.claimed_by && (
            <span
              className="inline-flex items-center gap-0.5"
              title={`Claimed by session ${task.claimed_by}`}
            >
              <Lock className="size-3" />
            </span>
          )}
        </div>
        {task.labels.length > 0 && (
          <div className="flex shrink-0 items-center gap-1">
            {task.labels.slice(0, 3).map((label) => (
              <span
                key={label}
                className="max-w-[120px] truncate rounded bg-foreground/[0.05] px-1.5 py-0.5 text-[0.65rem] font-medium text-muted-foreground"
              >
                {label}
              </span>
            ))}
          </div>
        )}
        {task.source === "claude" && (
          <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[0.6rem] font-semibold uppercase tracking-wide text-primary">
            claude
          </span>
        )}
      </div>

      {/* Assignee */}
      <div className="flex w-[120px] min-w-[120px] items-center justify-center px-3">
        {task.assignee ? (
          <div
            title={task.assignee}
            style={{ background: avatarColor(task.assignee) }}
            className="flex size-[22px] shrink-0 items-center justify-center rounded-full text-[0.6rem] font-semibold text-white"
          >
            {initials(task.assignee)}
          </div>
        ) : (
          <User className="size-4 text-muted-foreground/40" />
        )}
      </div>

      {/* Due date */}
      <div className="flex w-[140px] min-w-[140px] items-center justify-center px-3 text-[0.75rem] text-muted-foreground">
        {task.due_at ? (
          <div
            className={cn(
              "flex items-center gap-1",
              overdue && "font-medium text-amber-600 dark:text-amber-500"
            )}
          >
            <Calendar className="size-3.5" />
            <span>{formatDate(task.due_at)}</span>
          </div>
        ) : (
          <Calendar className="size-4 text-muted-foreground/40" />
        )}
      </div>

      {/* Priority */}
      <div className="flex w-[100px] min-w-[100px] items-center justify-center px-3">
        <Flag
          className="size-4"
          style={{ color: priorityColor(task.priority) }}
          fill={task.priority > 0 ? priorityColor(task.priority) : "none"}
        />
      </div>

      <div className="w-10 min-w-10" />
    </div>
  );
}
