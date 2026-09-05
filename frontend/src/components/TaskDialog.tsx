import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Archive,
  Bot,
  Calendar,
  CalendarClock,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  Flag,
  History,
  ListTree,
  Lock,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Play,
  Plus,
  Trash2,
  Square,
  Timer,
  Unlock,
  Upload,
  User,
  X,
} from "lucide-react";
import type { Task, TaskActivityEntry, TaskDetail } from "../types";
import { api } from "../lib/api";
import { cn } from "../lib/utils";
import { useToast } from "./ui/Toast";
import { ConfirmDialog } from "./ConfirmDialog";
import { Input } from "./ui/Input";
import { Markdown } from "./ui/Markdown";
import { Textarea } from "./ui/Textarea";
import {
  PRIORITY_LABELS,
  STATE_COLORS,
  STATE_LABELS,
  STATE_OPTIONS,
  activityLabel,
  avatarColor,
  entryMinutes,
  formatDate,
  formatDateTime,
  formatDuration,
  formatTime,
  initials,
  isOverdue,
  labelColor,
  pad,
  priorityColor,
  splitDuration,
  toDateInput,
} from "../lib/tasks";

export interface TaskDialogProps {
  projectSlug: string;
  taskId: string;
  onClose: () => void;
  onChanged: () => void;
  /** Open a different task in this dialog — a sub-task is a task too. */
  onOpenTask?: (taskId: string) => void;
}

const COMMENT_KINDS = ["note", "rule", "decision", "reminder"] as const;

/**
 * The task dialog, modelled on asoode's task modal: a centred panel with a
 * breadcrumb header, a main column (title, quick properties, description,
 * sub-tasks, comments/activity) and a 280px properties sidebar.
 *
 * Everything edits in place and saves immediately — there is no draft state to
 * lose, and the list behind it refreshes through onChanged.
 */
export function TaskDialog({
  projectSlug,
  taskId,
  onClose,
  onChanged,
  onOpenTask,
}: TaskDialogProps) {
  const { toast } = useToast();
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [visible, setVisible] = useState(false);
  const [tab, setTab] = useState<"comments" | "activity">("comments");
  const [activity, setActivity] = useState<TaskActivityEntry[] | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // The parent of an open sub-task, for the breadcrumb. The task endpoint
  // returns `parent_id` but no parent summary, so its title costs one more
  // request — only ever made for a sub-task.
  const [parent, setParent] = useState<Task | null>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await api.getTask(projectSlug, taskId));
    } catch (err) {
      toast({
        title: "Failed to load task",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setLoading(false);
    }
  }, [projectSlug, taskId, toast]);

  useEffect(() => {
    void load();
    const id = requestAnimationFrame(() => setVisible(true));
    return () => cancelAnimationFrame(id);
  }, [load]);

  // Opening a sub-task swaps taskId on this same instance, so per-task view
  // state has to reset with it - otherwise the Activity tab shows the history
  // of the task you just navigated away from, and the panel shows the previous
  // task's fields for as long as the new one takes to load.
  useEffect(() => {
    setActivity(null);
    setTab("comments");
    setDetail(null);
    setParent(null);
    setLoading(true);
  }, [taskId]);

  // Breadcrumb for a sub-task. A failure here is silent on purpose: the
  // breadcrumb falls back to "Parent task" rather than raising a toast about a
  // task the user did not ask to open.
  const parentId = detail?.task.parent_id ?? null;
  useEffect(() => {
    if (!parentId) {
      setParent(null);
      return;
    }
    let cancelled = false;
    api
      .getTask(projectSlug, parentId)
      .then((res) => {
        if (!cancelled) setParent(res.task);
      })
      .catch(() => {
        if (!cancelled) setParent(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectSlug, parentId]);

  // Escape closes, and the body must not scroll behind the panel.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // An inline editor owns Escape while it is focused - cancelling a rename
      // must not also throw away the dialog around it.
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        target?.isContentEditable
      ) {
        return;
      }
      onClose();
    };
    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [onClose]);

  const refresh = useCallback(async () => {
    await load();
    onChanged();
  }, [load, onChanged]);

  const fail = (err: unknown, what: string) =>
    toast({
      title: what,
      description: err instanceof Error ? err.message : undefined,
      variant: "error",
    });

  const patch = async (input: Parameters<typeof api.updateTask>[2], what: string) => {
    try {
      await api.updateTask(projectSlug, taskId, input);
      await refresh();
    } catch (err) {
      fail(err, what);
    }
  };

  const loadActivity = useCallback(async () => {
    try {
      const res = await api.taskActivity(projectSlug, taskId);
      setActivity(res.activity);
    } catch {
      setActivity([]);
    }
  }, [projectSlug, taskId]);

  const task = detail?.task;

  return createPortal(
    <div
      onClick={(e) => e.target === e.currentTarget && onClose()}
      className={cn(
        // Below the app's Dialog layer (z-50) on purpose: a confirm opened from
        // inside this panel, and toasts at z-[100], must land ON TOP of it.
        "fixed inset-0 z-40 flex items-center justify-center bg-black/50 p-6 backdrop-blur-[3px] transition-opacity duration-200 max-md:p-0",
        visible ? "opacity-100" : "opacity-0"
      )}
    >
      <div
        className={cn(
          "flex h-[min(88vh,860px)] w-[min(92vw,1100px)] flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-2xl transition-transform duration-200 max-md:h-full max-md:w-full max-md:rounded-none",
          visible ? "scale-100" : "scale-95"
        )}
      >
        {loading || !detail || !task ? (
          <DialogSkeleton onClose={onClose} />
        ) : (
          <>
            {/* Header */}
            <div className="shrink-0 border-b border-border">
              <div className="flex h-[52px] items-center justify-between gap-3 px-5">
                <div className="flex min-w-0 items-center gap-1.5 text-sm">
                  <span
                    className="inline-flex items-center gap-1.5 whitespace-nowrap text-muted-foreground"
                  >
                    <span
                      className="size-2 rounded-full"
                      style={{ background: STATE_COLORS[task.state] }}
                    />
                    {STATE_LABELS[task.state]}
                  </span>
                  <ChevronRight className="size-3 shrink-0 text-muted-foreground/60" />
                  {task.parent_id && (
                    // A sub-task says whose it is, and the crumb navigates: the
                    // dialog swaps taskId rather than opening a second panel.
                    <>
                      <button
                        type="button"
                        disabled={!onOpenTask}
                        title={parent ? `Open "${parent.title}"` : undefined}
                        onClick={() => onOpenTask?.(task.parent_id as string)}
                        className={cn(
                          "flex max-w-[220px] shrink items-center gap-1.5 truncate text-muted-foreground",
                          onOpenTask && "transition-colors hover:text-primary hover:underline"
                        )}
                      >
                        <ListTree className="size-3.5 shrink-0" />
                        <span className="truncate">{parent?.title ?? "Parent task"}</span>
                      </button>
                      <ChevronRight className="size-3 shrink-0 text-muted-foreground/60" />
                    </>
                  )}
                  <span className="truncate font-medium">{task.title}</span>
                </div>
                <div className="flex shrink-0 items-center gap-0.5">
                  {task.parent_id && (
                    <button
                      type="button"
                      title="Make this a top-level task"
                      onClick={async () => {
                        try {
                          await api.convertTaskToTop(projectSlug, taskId);
                          await refresh();
                        } catch (err) {
                          fail(err, "Failed to convert");
                        }
                      }}
                      className="flex size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-foreground/[0.06] hover:text-foreground"
                    >
                      <Upload className="size-4" />
                    </button>
                  )}
                  <button
                    type="button"
                    title="Archive"
                    onClick={async () => {
                      try {
                        await api.archiveTask(projectSlug, taskId);
                        onChanged();
                        onClose();
                      } catch (err) {
                        fail(err, "Failed to archive");
                      }
                    }}
                    className="flex size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-foreground/[0.06] hover:text-foreground"
                  >
                    <Archive className="size-4" />
                  </button>
                  <button
                    type="button"
                    title="Delete permanently"
                    onClick={() => setConfirmDelete(true)}
                    className="flex size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
                  >
                    <Trash2 className="size-4" />
                  </button>
                  <div className="mx-1 h-5 w-px bg-border" />
                  <button
                    type="button"
                    title="Close"
                    onClick={onClose}
                    className="flex size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
                  >
                    <X className="size-5" />
                  </button>
                </div>
              </div>
              {task.archived_at && (
                <div className="flex items-center justify-center gap-1.5 bg-amber-500/15 px-4 py-1.5 text-[0.78rem] font-medium text-amber-600 dark:text-amber-400">
                  <Archive className="size-3.5" />
                  Archived {formatDate(task.archived_at)}
                </div>
              )}
            </div>

            <div className="flex flex-1 overflow-hidden max-md:flex-col max-md:overflow-y-auto">
              {/* Keyed by task: opening a sub-task reuses this instance, and
                  without a remount the inline editors would still hold the
                  previous task's title, description and dates — saving one
                  would write the parent's values onto the sub-task. */}
              <TaskDialogMain
                key={`main:${task.id}`}
                projectSlug={projectSlug}
                detail={detail}
                tab={tab}
                activity={activity}
                onTab={(next) => {
                  setTab(next);
                  if (next === "activity" && activity === null) void loadActivity();
                }}
                onPatch={patch}
                onRefresh={refresh}
                onFail={fail}
                onOpenTask={onOpenTask}
              />
              <TaskDialogSidebar
                // Siblings need distinct keys; the task id alone would collide
                // with the main column's and React would warn.
                key={`sidebar:${task.id}`}
                projectSlug={projectSlug}
                detail={detail}
                onPatch={patch}
                onRefresh={refresh}
                onFail={fail}
              />
            </div>

            <ConfirmDialog
              open={confirmDelete}
              destructive
              busy={deleting}
              title={`Delete "${task.title}"?`}
              description="This removes the task for good, with its comments and tracked time. Sub-tasks are kept and promoted to tasks of their own. Archiving instead takes it out of the list but keeps it findable."
              confirmLabel="Delete permanently"
              onClose={() => setConfirmDelete(false)}
              onConfirm={async () => {
                setDeleting(true);
                try {
                  await api.deleteTask(projectSlug, taskId);
                  onChanged();
                  onClose();
                } catch (err) {
                  fail(err, "Failed to delete");
                } finally {
                  setDeleting(false);
                  setConfirmDelete(false);
                }
              }}
            />
          </>
        )}
      </div>
    </div>,
    document.body
  );
}

function DialogSkeleton({ onClose }: { onClose: () => void }) {
  return (
    <div className="flex flex-1 flex-col">
      <div className="flex h-[52px] items-center justify-between border-b border-border px-5">
        <div className="h-3 w-48 animate-pulse rounded bg-foreground/10" />
        <button
          type="button"
          onClick={onClose}
          className="flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-foreground/[0.06]"
        >
          <X className="size-5" />
        </button>
      </div>
      <div className="flex-1 space-y-4 p-7">
        <div className="h-6 w-2/3 animate-pulse rounded bg-foreground/10" />
        <div className="h-4 w-1/3 animate-pulse rounded bg-foreground/10" />
        <div className="h-24 w-full animate-pulse rounded bg-foreground/5" />
      </div>
    </div>
  );
}

/* ── Main column ─────────────────────────────────────────────────────── */

interface MainProps {
  projectSlug: string;
  detail: TaskDetail;
  tab: "comments" | "activity";
  activity: TaskActivityEntry[] | null;
  onTab: (tab: "comments" | "activity") => void;
  onPatch: (input: Parameters<typeof api.updateTask>[2], what: string) => Promise<void>;
  onRefresh: () => Promise<void>;
  onFail: (err: unknown, what: string) => void;
  onOpenTask?: (taskId: string) => void;
}

function TaskDialogMain({
  projectSlug,
  detail,
  tab,
  activity,
  onTab,
  onPatch,
  onRefresh,
  onFail,
  onOpenTask,
}: MainProps) {
  const task = detail.task;
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(task.title);
  const [editingDesc, setEditingDesc] = useState(false);
  const [descDraft, setDescDraft] = useState(task.description ?? "");
  const [showStates, setShowStates] = useState(false);
  const statesRef = useRef<HTMLDivElement>(null);

  const [newComment, setNewComment] = useState("");
  const [commentKind, setCommentKind] = useState<string>("note");
  const [showSubtaskInput, setShowSubtaskInput] = useState(false);
  const [subtaskTitle, setSubtaskTitle] = useState("");

  useEffect(() => {
    if (!showStates) return;
    const onClick = (e: MouseEvent) => {
      if (statesRef.current && !statesRef.current.contains(e.target as Node)) {
        setShowStates(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [showStates]);

  const overdue = isOverdue(task);
  const subtaskProgress = detail.subtasks.length
    ? Math.round(
        (detail.subtasks.filter((s) => s.state === "done").length /
          detail.subtasks.length) *
          100
      )
    : 0;

  const addComment = async () => {
    if (!newComment.trim()) return;
    try {
      await api.commentTask(projectSlug, task.id, {
        body: newComment.trim(),
        kind: commentKind,
      });
      setNewComment("");
      await onRefresh();
    } catch (err) {
      onFail(err, "Failed to comment");
    }
  };

  const addSubtask = async () => {
    if (!subtaskTitle.trim()) return;
    try {
      await api.createTask(projectSlug, {
        title: subtaskTitle.trim(),
        parent_id: task.id,
        source: "user",
      });
      setSubtaskTitle("");
      await onRefresh();
    } catch (err) {
      onFail(err, "Failed to add sub-task");
    }
  };

  const toggleSubtask = async (subtask: Task) => {
    try {
      await api.updateTask(projectSlug, subtask.id, {
        state: subtask.state === "done" ? "todo" : "done",
      });
      await onRefresh();
    } catch (err) {
      onFail(err, "Failed to update sub-task");
    }
  };

  return (
    <div className="flex-1 overflow-y-auto px-7 pb-10 pt-6 max-md:px-4">
      {/* Title */}
      <div className="mb-7">
        {!editingTitle ? (
          <h1
            onClick={() => {
              setTitleDraft(task.title);
              setEditingTitle(true);
            }}
            className="group flex cursor-pointer items-center gap-2 py-1 text-xl font-semibold leading-snug"
          >
            {task.title}
            <Pencil className="size-3.5 text-muted-foreground/60 opacity-0 transition-opacity group-hover:opacity-100" />
          </h1>
        ) : (
          <div className="flex flex-col gap-2">
            <input
              autoFocus
              value={titleDraft}
              onChange={(e) => setTitleDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  void onPatch({ title: titleDraft.trim() }, "Failed to rename").then(
                    () => setEditingTitle(false)
                  );
                }
                if (e.key === "Escape") setEditingTitle(false);
              }}
              className="w-full border-0 border-b-2 border-primary bg-transparent py-1 text-xl font-semibold outline-none"
            />
            <EditActions
              onSave={() =>
                void onPatch({ title: titleDraft.trim() }, "Failed to rename").then(() =>
                  setEditingTitle(false)
                )
              }
              onCancel={() => setEditingTitle(false)}
            />
          </div>
        )}
      </div>

      {/* Quick properties */}
      <div className="mb-6 flex flex-wrap gap-2 border-b border-border pb-5">
        <div ref={statesRef} className="relative">
          <button
            type="button"
            onClick={() => setShowStates((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-[0.78rem] font-medium transition-colors hover:bg-foreground/[0.04]"
          >
            <span
              className="size-2.5 shrink-0 rounded-full"
              style={{ background: STATE_COLORS[task.state] }}
            />
            <span>{STATE_LABELS[task.state]}</span>
            <ChevronDown className="size-3 text-muted-foreground" />
          </button>
          {showStates && (
            <div className="absolute left-0 top-[calc(100%+4px)] z-20 min-w-[180px] rounded-md border border-border bg-popover p-1 shadow-lg">
              {STATE_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => {
                    setShowStates(false);
                    void onPatch({ state: option.value }, "Failed to change state");
                  }}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-sm px-3 py-2 text-[0.8rem] transition-colors hover:bg-foreground/[0.04]",
                    task.state === option.value && "bg-primary/[0.06]"
                  )}
                >
                  <span
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ background: option.color }}
                  />
                  <span>{option.label}</span>
                  {task.state === option.value && (
                    <Check className="ml-auto size-3.5 text-emerald-500" />
                  )}
                </button>
              ))}
            </div>
          )}
        </div>

        <PropPill icon={<User className="size-3.5" />}>
          {task.assignee ? (
            <span className="inline-flex items-center gap-1.5">
              <span
                style={{ background: avatarColor(task.assignee) }}
                className="flex size-[18px] items-center justify-center rounded-full text-[0.5rem] font-bold text-white"
              >
                {initials(task.assignee)}
              </span>
              {task.assignee}
            </span>
          ) : (
            <span className="font-normal text-muted-foreground">Unassigned</span>
          )}
        </PropPill>

        <PropPill icon={<Flag className="size-3.5" style={{ color: priorityColor(task.priority) }} />}>
          {PRIORITY_LABELS[task.priority] ?? `P${task.priority}`}
        </PropPill>

        {task.role && (
          <PropPill icon={<Bot className="size-3.5" />}>
            <span title="The agent role this task is routed to">agent:{task.role}</span>
          </PropPill>
        )}

        <PropPill
          icon={<Calendar className="size-3.5" />}
          className={overdue ? "border-destructive text-destructive" : undefined}
        >
          {task.due_at ? (
            formatDate(task.due_at)
          ) : (
            <span className="font-normal text-muted-foreground">No due date</span>
          )}
        </PropPill>

        <PropPill icon={<Timer className="size-3.5" />}>
          {detail.minutes_spent || task.estimated_minutes ? (
            <span>
              {formatDuration(detail.minutes_spent)}
              {task.estimated_minutes ? ` / ${formatDuration(task.estimated_minutes)}` : ""}
            </span>
          ) : (
            <span className="font-normal text-muted-foreground">No time tracked</span>
          )}
        </PropPill>
      </div>

      {/* Description */}
      <div className="mb-7">
        {!editingDesc ? (
          <div
            onClick={() => {
              // Selecting a line of the rendered description must not open the
              // editor underneath the selection.
              if (window.getSelection()?.toString()) return;
              setDescDraft(task.description ?? "");
              setEditingDesc(true);
            }}
            className="group flex cursor-pointer items-start gap-2 py-2 text-sm leading-relaxed"
          >
            {task.description?.trim() ? (
              // Rendered markdown; the textarea below still edits the source.
              <Markdown source={task.description} className="min-w-0 flex-1" />
            ) : (
              <span className="italic text-muted-foreground">No description yet</span>
            )}
            <Pencil className="mt-0.5 size-3.5 shrink-0 text-muted-foreground/60 opacity-0 transition-opacity group-hover:opacity-100" />
          </div>
        ) : (
          <div className="flex w-full flex-col gap-2">
            <Textarea
              autoFocus
              rows={5}
              value={descDraft}
              onChange={(e) => setDescDraft(e.target.value)}
              placeholder="What does this task involve?"
            />
            <EditActions
              onSave={() =>
                void onPatch({ description: descDraft }, "Failed to save description").then(
                  () => setEditingDesc(false)
                )
              }
              onCancel={() => setEditingDesc(false)}
            />
          </div>
        )}
      </div>

      {/* Sub-tasks — hidden entirely on a sub-task, because a task with a
          parent can never have children: one level, no deeper. The server
          rejects a grandchild, so the UI must not offer the action at all. */}
      {!task.parent_id && (
        <div className="mb-7">
          <SectionHeader
            icon={<ListTree className="size-4" />}
            title="Sub-tasks"
            badge={
              detail.subtasks.length
                ? `${detail.subtasks.filter((s) => s.state === "done").length}/${
                    detail.subtasks.length
                  }`
                : undefined
            }
            onAdd={() => setShowSubtaskInput(true)}
          />
          {detail.subtasks.length > 0 && (
            <div className="mb-3 h-1 overflow-hidden rounded-full bg-foreground/[0.08]">
              <div
                className="h-full rounded-full bg-emerald-500 transition-all"
                style={{ width: `${subtaskProgress}%` }}
              />
            </div>
          )}
          {showSubtaskInput && (
            <div className="mb-2 flex items-center gap-2">
              <Input
                autoFocus
                value={subtaskTitle}
                placeholder="Sub-task title"
                onChange={(e) => setSubtaskTitle(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void addSubtask();
                  }
                  if (e.key === "Escape") setShowSubtaskInput(false);
                }}
                className="h-8 flex-1 text-[0.82rem]"
              />
              <button
                type="button"
                onClick={() => void addSubtask()}
                className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground transition-opacity hover:opacity-90"
              >
                <Check className="size-4" />
              </button>
              <button
                type="button"
                onClick={() => setShowSubtaskInput(false)}
                className="flex size-7 items-center justify-center rounded-md bg-foreground/[0.06] text-muted-foreground transition-colors hover:bg-foreground/10"
              >
                <X className="size-4" />
              </button>
            </div>
          )}
          {detail.subtasks.length === 0 && !showSubtaskInput ? (
            <p className="text-[0.8rem] italic text-muted-foreground">No sub-tasks</p>
          ) : (
            <div className="flex flex-col gap-1">
              {detail.subtasks.map((subtask) => (
                <SubtaskRow
                  key={subtask.id}
                  projectSlug={projectSlug}
                  subtask={subtask}
                  onToggle={() => void toggleSubtask(subtask)}
                  onOpen={onOpenTask ? () => onOpenTask(subtask.id) : undefined}
                  onRefresh={onRefresh}
                  onFail={onFail}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Comments / activity */}
      <div className="mb-7">
        <div className="mb-4 flex gap-1 border-b border-border">
          <TabButton
            active={tab === "comments"}
            onClick={() => onTab("comments")}
            icon={<MessageSquare className="size-3.5" />}
            label="Comments"
            count={detail.comments.length}
          />
          <TabButton
            active={tab === "activity"}
            onClick={() => onTab("activity")}
            icon={<History className="size-3.5" />}
            label="Activity"
          />
        </div>

        {tab === "comments" ? (
          <div>
            <div className="mb-5 flex flex-col gap-2">
              <Textarea
                rows={2}
                value={newComment}
                placeholder="Add a comment"
                onChange={(e) => setNewComment(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    void addComment();
                  }
                }}
              />
              <div className="flex items-center gap-2">
                <div className="flex gap-1">
                  {COMMENT_KINDS.map((kind) => (
                    <button
                      key={kind}
                      type="button"
                      onClick={() => setCommentKind(kind)}
                      className={cn(
                        "rounded-full border border-border px-2.5 py-1 text-[0.7rem] font-medium capitalize transition-colors",
                        commentKind === kind
                          ? "border-primary bg-primary/10 text-primary"
                          : "text-muted-foreground hover:bg-foreground/[0.04]"
                      )}
                    >
                      {kind}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  disabled={!newComment.trim()}
                  onClick={() => void addComment()}
                  className="ml-auto rounded-md bg-primary px-3 py-1.5 text-[0.78rem] font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
                >
                  Comment
                </button>
              </div>
            </div>

            {detail.comments.length === 0 ? (
              <p className="text-[0.8rem] italic text-muted-foreground">No comments yet</p>
            ) : (
              <div className="flex flex-col gap-4">
                {detail.comments.map((comment) => (
                  <div key={comment.id} className="flex gap-2.5">
                    <span
                      style={{ background: avatarColor(comment.author ?? comment.kind) }}
                      className="flex size-[30px] shrink-0 items-center justify-center rounded-full text-[0.6rem] font-bold text-white"
                    >
                      {initials(comment.author ?? comment.kind)}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="mb-0.5 flex items-baseline gap-2">
                        <span className="text-[0.8rem] font-semibold">
                          {comment.author ?? "Claude"}
                        </span>
                        <span className="rounded bg-foreground/[0.06] px-1.5 py-px text-[0.62rem] font-medium capitalize text-muted-foreground">
                          {comment.kind}
                        </span>
                        <span className="text-[0.68rem] text-muted-foreground">
                          {formatDateTime(comment.created_at)}
                        </span>
                      </div>
                      <Markdown
                        source={comment.body}
                        className="text-[0.82rem] leading-relaxed"
                      />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div>
            {activity === null ? (
              <p className="text-[0.8rem] text-muted-foreground">Loading activity…</p>
            ) : activity.length === 0 ? (
              <div className="rounded-md bg-foreground/[0.02] px-4 py-5 text-center text-[0.8rem] text-muted-foreground">
                No activity recorded
              </div>
            ) : (
              <div className="flex flex-col gap-3">
                {[...activity].reverse().map((entry) => (
                  <div key={entry.id} className="flex gap-2.5 rounded-sm p-2">
                    <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[0.5rem] font-bold text-primary">
                      {initials(entry.actor ?? "Claude")}
                    </span>
                    <div className="flex min-w-0 flex-col gap-px">
                      <span className="text-[0.78rem] font-semibold">
                        {entry.actor ?? "Claude"}
                      </span>
                      <span className="break-words text-[0.75rem] text-muted-foreground">
                        {activityLabel(entry.operation, entry.details)}
                      </span>
                      <span className="text-[0.65rem] text-muted-foreground/70">
                        {formatDateTime(entry.created_at)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Sidebar ─────────────────────────────────────────────────────────── */

interface SidebarProps {
  projectSlug: string;
  detail: TaskDetail;
  onPatch: (input: Parameters<typeof api.updateTask>[2], what: string) => Promise<void>;
  onRefresh: () => Promise<void>;
  onFail: (err: unknown, what: string) => void;
}

function TaskDialogSidebar({
  projectSlug,
  detail,
  onPatch,
  onRefresh,
  onFail,
}: SidebarProps) {
  const task = detail.task;
  const [editingAssignee, setEditingAssignee] = useState(false);
  const [assigneeDraft, setAssigneeDraft] = useState(task.assignee ?? "");
  const [editingRole, setEditingRole] = useState(false);
  const [roleDraft, setRoleDraft] = useState(task.role ?? "");
  const [editingLabels, setEditingLabels] = useState(false);
  const [labelDraft, setLabelDraft] = useState("");
  const [editingDates, setEditingDates] = useState(false);
  const [dueDraft, setDueDraft] = useState(toDateInput(task.due_at));
  const [beginDraft, setBeginDraft] = useState(toDateInput(task.begin_at));
  const [endDraft, setEndDraft] = useState(toDateInput(task.end_at));
  const [estimateDraft, setEstimateDraft] = useState(
    task.estimated_minutes ? String(task.estimated_minutes) : ""
  );
  const [busy, setBusy] = useState(false);

  // The running clock ticks in the panel, so the total is live rather than
  // frozen at whatever it was when the dialog opened.
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!detail.running) return;
    const id = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, [detail.running]);

  const totalMinutes = useMemo(() => {
    void now;
    return detail.time_entries.reduce(
      (sum, entry) => sum + entryMinutes(entry.begin_at, entry.end_at),
      0
    );
  }, [detail.time_entries, now]);

  const total = splitDuration(totalMinutes);

  const toggleClock = async () => {
    setBusy(true);
    try {
      if (detail.running) await api.stopTask(projectSlug, task.id);
      else await api.startTask(projectSlug, task.id);
      await onRefresh();
    } catch (err) {
      onFail(err, detail.running ? "Failed to stop" : "Failed to start");
    } finally {
      setBusy(false);
    }
  };

  const addLabel = async () => {
    const value = labelDraft.trim();
    if (!value || task.labels.includes(value)) {
      setLabelDraft("");
      return;
    }
    await onPatch({ labels: [...task.labels, value] }, "Failed to add label");
    setLabelDraft("");
  };

  const removeLabel = (label: string) =>
    onPatch(
      { labels: task.labels.filter((l) => l !== label) },
      "Failed to remove label"
    );

  return (
    <div className="w-[280px] shrink-0 overflow-y-auto border-l border-border bg-muted/40 px-4 py-5 max-md:w-full max-md:border-l-0 max-md:border-t">
      {/* Assignee */}
      <SidebarSection
        label="Assignee"
        action={
          <AddButton
            onClick={() => {
              setAssigneeDraft(task.assignee ?? "");
              setEditingAssignee((v) => !v);
            }}
            icon={editingAssignee ? <X className="size-3" /> : <Pencil className="size-3" />}
          />
        }
      >
        {editingAssignee ? (
          <div className="flex items-center gap-1">
            <Input
              autoFocus
              value={assigneeDraft}
              placeholder="Who owns this?"
              onChange={(e) => setAssigneeDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  void onPatch(
                    { assignee: assigneeDraft.trim() },
                    "Failed to set assignee"
                  ).then(() => setEditingAssignee(false));
                }
                if (e.key === "Escape") setEditingAssignee(false);
              }}
              className="h-7 text-[0.78rem]"
            />
          </div>
        ) : task.assignee ? (
          <div className="flex items-center gap-2">
            <span
              style={{ background: avatarColor(task.assignee) }}
              className="flex size-7 items-center justify-center rounded-full text-[0.6rem] font-bold text-white"
            >
              {initials(task.assignee)}
            </span>
            <span className="min-w-0 flex-1 truncate text-[0.8rem]">{task.assignee}</span>
          </div>
        ) : (
          <EmptyHint>Nobody assigned</EmptyHint>
        )}
      </SidebarSection>

      {/* Agent role */}
      <SidebarSection
        label="Agent"
        action={
          <AddButton
            onClick={() => {
              setRoleDraft(task.role ?? "");
              setEditingRole((v) => !v);
            }}
            icon={editingRole ? <X className="size-3" /> : <Pencil className="size-3" />}
          />
        }
      >
        {editingRole ? (
          <Input
            autoFocus
            value={roleDraft}
            placeholder="backend, frontend, test... (empty = anyone)"
            onChange={(e) => setRoleDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                // "" clears it: the task goes back to being claimable by any agent.
                void onPatch({ role: roleDraft.trim() }, "Failed to set agent role").then(
                  () => setEditingRole(false)
                );
              }
              if (e.key === "Escape") setEditingRole(false);
            }}
            className="h-7 text-[0.78rem]"
          />
        ) : task.role ? (
          <div className="flex items-center gap-2 text-[0.8rem]">
            <Bot className="size-4 text-muted-foreground" />
            <span className="truncate">agent:{task.role}</span>
          </div>
        ) : (
          <EmptyHint>Any agent can claim it</EmptyHint>
        )}
      </SidebarSection>

      {/* Labels */}
      <SidebarSection
        label="Labels"
        action={
          <AddButton
            onClick={() => setEditingLabels((v) => !v)}
            icon={editingLabels ? <X className="size-3" /> : <Plus className="size-3" />}
          />
        }
      >
        {task.labels.length === 0 && !editingLabels ? (
          <EmptyHint>No labels</EmptyHint>
        ) : (
          <div className="flex flex-wrap gap-1">
            {task.labels.map((label) => (
              <span
                key={label}
                className="group inline-flex min-w-[30px] items-center gap-1 rounded px-2 py-0.5 text-center text-[0.68rem] font-medium text-white"
                style={{ background: labelColor(label) }}
              >
                {label}
                <button
                  type="button"
                  onClick={() => void removeLabel(label)}
                  className="opacity-60 transition-opacity hover:opacity-100"
                >
                  <X className="size-2.5" />
                </button>
              </span>
            ))}
          </div>
        )}
        {editingLabels && (
          <Input
            autoFocus
            value={labelDraft}
            placeholder="Label, then Enter"
            onChange={(e) => setLabelDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void addLabel();
              }
              if (e.key === "Escape") setEditingLabels(false);
            }}
            className="mt-2 h-7 text-[0.78rem]"
          />
        )}
      </SidebarSection>

      {/* Priority */}
      <SidebarSection label="Priority">
        <div className="flex gap-1">
          {[0, 1, 2, 3].map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => void onPatch({ priority: value }, "Failed to set priority")}
              title={PRIORITY_LABELS[value]}
              className={cn(
                "flex flex-1 items-center justify-center rounded-md border border-border py-1.5 transition-colors hover:bg-foreground/[0.04]",
                task.priority === value && "border-primary bg-primary/[0.08]"
              )}
            >
              <Flag
                className="size-3.5"
                style={{ color: priorityColor(value) }}
                fill={value > 0 ? priorityColor(value) : "none"}
              />
            </button>
          ))}
        </div>
      </SidebarSection>

      {/* Dates */}
      <SidebarSection
        label="Dates"
        action={
          <AddButton
            onClick={() => setEditingDates((v) => !v)}
            icon={editingDates ? <X className="size-3" /> : <Pencil className="size-3" />}
          />
        }
      >
        {!editingDates ? (
          <div className="flex flex-col gap-1">
            <DateRow icon={<Calendar className="size-3.5" />} label="Start" value={task.begin_at} />
            <DateRow icon={<Calendar className="size-3.5" />} label="End" value={task.end_at} />
            <DateRow
              icon={<CalendarClock className="size-3.5" />}
              label="Due"
              value={task.due_at}
              highlight={isOverdue(task)}
            />
            {!!task.estimated_minutes && (
              <div className="flex items-center gap-1.5 py-1 text-[0.78rem]">
                <Timer className="size-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-[52px] text-[0.66rem] uppercase tracking-wide text-muted-foreground">
                  Estimate
                </span>
                <span className="font-medium">{formatDuration(task.estimated_minutes)}</span>
              </div>
            )}
            {!task.begin_at && !task.end_at && !task.due_at && !task.estimated_minutes && (
              <EmptyHint>No dates set</EmptyHint>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <LabeledDate label="Start" value={beginDraft} onChange={setBeginDraft} />
            <LabeledDate label="End" value={endDraft} onChange={setEndDraft} />
            <LabeledDate label="Due" value={dueDraft} onChange={setDueDraft} />
            <div className="flex flex-col gap-1">
              <span className="text-[0.66rem] uppercase tracking-wide text-muted-foreground">
                Estimate (minutes)
              </span>
              <Input
                type="number"
                min={0}
                value={estimateDraft}
                onChange={(e) => setEstimateDraft(e.target.value)}
                className="h-7 text-[0.78rem]"
              />
            </div>
            <EditActions
              onSave={() => {
                const input: Parameters<typeof api.updateTask>[2] = {};
                if (beginDraft) input.begin_at = new Date(beginDraft).toISOString();
                if (endDraft) input.end_at = new Date(endDraft).toISOString();
                if (dueDraft) input.due_at = new Date(dueDraft).toISOString();
                if (estimateDraft) input.estimated_minutes = Number(estimateDraft);
                void onPatch(input, "Failed to save dates").then(() =>
                  setEditingDates(false)
                );
              }}
              onCancel={() => setEditingDates(false)}
            />
          </div>
        )}
      </SidebarSection>

      {/* Time management */}
      <SidebarSection label="Time management">
        <div className="mb-2.5 flex items-center justify-between border-b border-border py-2.5">
          <div className="flex items-center gap-0.5">
            {(["days", "hours", "minutes"] as const).map((unit, index) => (
              <span key={unit} className="flex items-center gap-0.5">
                {index > 0 && (
                  <span className="relative -top-1 px-px text-base font-semibold text-muted-foreground/70">
                    :
                  </span>
                )}
                <span className="flex flex-col items-center">
                  <span className="min-w-[24px] text-center text-[1.1rem] font-semibold">
                    {pad(total[unit])}
                  </span>
                  <span className="text-[0.5rem] uppercase text-muted-foreground/70">
                    {unit}
                  </span>
                </span>
              </span>
            ))}
          </div>
          <button
            type="button"
            disabled={busy}
            onClick={() => void toggleClock()}
            title={detail.running ? "Stop the clock" : "Start the clock"}
            className="flex size-8 items-center justify-center rounded-full border-2 border-sky-500 text-sky-500 transition-colors hover:bg-sky-500/[0.06] disabled:opacity-40"
          >
            {detail.running ? (
              <Square className="size-3.5" fill="currentColor" />
            ) : (
              <Play className="size-3.5" fill="currentColor" />
            )}
          </button>
        </div>

        {detail.time_entries.length === 0 ? (
          <EmptyHint>No time tracked</EmptyHint>
        ) : (
          <div className="flex flex-col gap-1.5">
            {[...detail.time_entries].reverse().map((entry) => (
              <div
                key={entry.id}
                className="flex items-center gap-1.5 rounded-md border border-foreground/[0.06] p-1.5"
              >
                <Clock className="size-3.5 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <span className="block text-[0.7rem] font-medium">
                    {formatDate(entry.begin_at)}
                  </span>
                  <span className="block text-[0.65rem] text-muted-foreground">
                    {formatTime(entry.begin_at)}
                    {entry.end_at ? (
                      ` – ${formatTime(entry.end_at)}`
                    ) : (
                      <span className="font-medium text-sky-500"> – running</span>
                    )}
                  </span>
                </div>
                <span className="shrink-0 text-[0.72rem] font-medium">
                  {formatDuration(entryMinutes(entry.begin_at, entry.end_at))}
                </span>
              </div>
            ))}
          </div>
        )}
      </SidebarSection>

      {/* Claim */}
      <SidebarSection label="Claim">
        {task.claimed_by ? (
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-1.5 text-[0.78rem]">
              <Lock className="size-3.5 shrink-0 text-amber-500" />
              <span className="min-w-0 flex-1 truncate font-mono text-[0.7rem]">
                {task.claimed_by}
              </span>
            </div>
            {task.lease_expires_at && (
              <span className="text-[0.66rem] text-muted-foreground">
                Lease until {formatDateTime(task.lease_expires_at)}
              </span>
            )}
            <button
              type="button"
              onClick={async () => {
                try {
                  await api.releaseTask(projectSlug, task.id);
                  await onRefresh();
                } catch (err) {
                  onFail(err, "Failed to release");
                }
              }}
              className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border px-2 py-1.5 text-[0.75rem] font-medium transition-colors hover:bg-foreground/[0.04]"
            >
              <Unlock className="size-3.5" />
              Release claim
            </button>
          </div>
        ) : (
          <EmptyHint>Unclaimed — free for any session</EmptyHint>
        )}
      </SidebarSection>

      {/* Provenance */}
      <SidebarSection label="Details">
        <div className="flex flex-col gap-1 text-[0.72rem] text-muted-foreground">
          <DetailRow label="Source" value={task.source} />
          <DetailRow label="Created" value={formatDateTime(task.created_at)} />
          <DetailRow label="Updated" value={formatDateTime(task.updated_at)} />
          {task.done_at && <DetailRow label="Done" value={formatDateTime(task.done_at)} />}
          <DetailRow label="Id" value={task.id.slice(0, 8)} mono />
        </div>
      </SidebarSection>
    </div>
  );
}

/* ── Sub-task row ────────────────────────────────────────────────────── */

/**
 * A sub-task is a task, and its row says as much: the same things a card shows
 * — state, labels, agent role, priority and assignee — not just a checkbox and
 * a title. Clicking the title opens it in this dialog, where every remaining
 * property is editable. The ⋯ menu carries the three things that only make
 * sense from the parent's list — rename in place, promote it out of the parent,
 * or delete it.
 */
function SubtaskRow({
  projectSlug,
  subtask,
  onToggle,
  onOpen,
  onRefresh,
  onFail,
}: {
  projectSlug: string;
  subtask: Task;
  onToggle: () => void;
  onOpen?: () => void;
  onRefresh: () => Promise<void>;
  onFail: (err: unknown, what: string) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [draft, setDraft] = useState(subtask.title);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [menuOpen]);

  const rename = async () => {
    const title = draft.trim();
    setRenaming(false);
    if (!title || title === subtask.title) return;
    try {
      await api.updateTask(projectSlug, subtask.id, { title });
      await onRefresh();
    } catch (err) {
      onFail(err, "Failed to rename sub-task");
    }
  };

  const convert = async () => {
    setMenuOpen(false);
    try {
      await api.convertTaskToTop(projectSlug, subtask.id);
      await onRefresh();
    } catch (err) {
      onFail(err, "Failed to convert sub-task");
    }
  };

  const remove = async () => {
    setConfirming(false);
    try {
      await api.deleteTask(projectSlug, subtask.id);
      await onRefresh();
    } catch (err) {
      onFail(err, "Failed to delete sub-task");
    }
  };

  return (
    <div className="group flex items-center gap-2 rounded-md px-1 py-1.5 hover:bg-foreground/[0.03]">
      <button
        type="button"
        title={subtask.state === "done" ? "Reopen" : "Mark done"}
        onClick={onToggle}
        className="flex size-4 shrink-0 items-center justify-center rounded-full border transition-colors"
        style={{
          borderColor: STATE_COLORS[subtask.state],
          background: subtask.state === "done" ? STATE_COLORS.done : "transparent",
        }}
      >
        {subtask.state === "done" && (
          <Check className="size-2.5 text-white" strokeWidth={4} />
        )}
      </button>

      {renaming ? (
        <>
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void rename();
              if (e.key === "Escape") {
                setDraft(subtask.title);
                setRenaming(false);
              }
            }}
            className="min-w-0 flex-1 border-0 border-b border-primary bg-transparent py-0.5 text-[0.82rem] outline-none"
          />
          {/* Explicit buttons rather than save-on-blur: blurring to click
              "cancel" would otherwise save the very edit being cancelled. */}
          <button
            type="button"
            title="Save"
            onClick={() => void rename()}
            className="flex size-6 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground transition-opacity hover:opacity-90"
          >
            <Check className="size-3.5" />
          </button>
          <button
            type="button"
            title="Cancel"
            onClick={() => {
              setDraft(subtask.title);
              setRenaming(false);
            }}
            className="flex size-6 shrink-0 items-center justify-center rounded-md bg-foreground/[0.06] text-muted-foreground transition-colors hover:bg-foreground/10"
          >
            <X className="size-3.5" />
          </button>
        </>
      ) : (
        <>
          <button
            type="button"
            onClick={onOpen}
            disabled={!onOpen}
            title={onOpen ? "Open sub-task" : undefined}
            className={cn(
              "min-w-0 flex-1 truncate text-left text-[0.82rem]",
              onOpen && "hover:text-primary",
              subtask.state === "done" && "text-muted-foreground line-through"
            )}
          >
            {subtask.title}
          </button>

          {subtask.labels.length > 0 && (
            <div className="flex shrink-0 items-center gap-1">
              {subtask.labels.slice(0, 2).map((label) => (
                <span
                  key={label}
                  className="max-w-[90px] truncate rounded px-1.5 py-0.5 text-[0.6rem] font-medium text-white"
                  style={{ background: labelColor(label) }}
                >
                  {label}
                </span>
              ))}
              {subtask.labels.length > 2 && (
                <span className="text-[0.6rem] text-muted-foreground">
                  +{subtask.labels.length - 2}
                </span>
              )}
            </div>
          )}

          {subtask.role && (
            <span
              title={`Routed to the ${subtask.role} agent`}
              className="shrink-0 rounded border border-primary/30 px-1.5 py-0.5 text-[0.58rem] font-semibold text-primary"
            >
              agent:{subtask.role}
            </span>
          )}

          <span
            title={`State: ${STATE_LABELS[subtask.state]}`}
            className="shrink-0 rounded px-1.5 py-0.5 text-[0.58rem] font-bold uppercase tracking-wide text-white"
            style={{ background: STATE_COLORS[subtask.state] }}
          >
            {STATE_LABELS[subtask.state]}
          </span>

          <Flag
            className="size-3.5 shrink-0"
            style={{ color: priorityColor(subtask.priority) }}
            fill={subtask.priority > 0 ? priorityColor(subtask.priority) : "none"}
          />

          {subtask.assignee ? (
            <span
              title={subtask.assignee}
              style={{ background: avatarColor(subtask.assignee) }}
              className="flex size-[18px] shrink-0 items-center justify-center rounded-full text-[0.5rem] font-bold text-white"
            >
              {initials(subtask.assignee)}
            </span>
          ) : (
            <User className="size-3.5 shrink-0 text-muted-foreground/40" />
          )}
        </>
      )}

      <div ref={menuRef} className="relative shrink-0">
        <button
          type="button"
          title="Sub-task actions"
          onClick={() => setMenuOpen((v) => !v)}
          className="flex size-6 items-center justify-center rounded text-muted-foreground opacity-0 transition-all hover:bg-foreground/[0.06] hover:text-foreground group-hover:opacity-100"
        >
          <MoreHorizontal className="size-4" />
        </button>
        {menuOpen && (
          <div className="absolute right-0 top-[calc(100%+2px)] z-30 min-w-[190px] rounded-md border border-border bg-popover p-1 shadow-lg">
            <MenuItem
              icon={<Pencil className="size-3.5" />}
              label="Rename"
              onClick={() => {
                setMenuOpen(false);
                setDraft(subtask.title);
                setRenaming(true);
              }}
            />
            <MenuItem
              icon={<Upload className="size-3.5" />}
              label="Convert to task"
              onClick={() => void convert()}
            />
            <MenuItem
              icon={<Trash2 className="size-3.5" />}
              label="Delete"
              destructive
              onClick={() => {
                setMenuOpen(false);
                setConfirming(true);
              }}
            />
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirming}
        destructive
        title={`Delete "${subtask.title}"?`}
        description="This removes the sub-task for good. Convert it to a task instead if it should live on its own."
        confirmLabel="Delete permanently"
        onClose={() => setConfirming(false)}
        onConfirm={() => void remove()}
      />
    </div>
  );
}

function MenuItem({
  icon,
  label,
  onClick,
  destructive,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  destructive?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 rounded-sm px-3 py-2 text-left text-[0.8rem] transition-colors hover:bg-foreground/[0.05]",
        destructive && "text-destructive hover:bg-destructive/10"
      )}
    >
      {icon}
      {label}
    </button>
  );
}

/* ── Small shared pieces ─────────────────────────────────────────────── */

function PropPill({
  icon,
  children,
  className,
}: {
  icon: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-[0.78rem] font-medium",
        className
      )}
    >
      <span className="text-muted-foreground">{icon}</span>
      {children}
    </span>
  );
}

function SectionHeader({
  icon,
  title,
  badge,
  onAdd,
}: {
  icon: React.ReactNode;
  title: string;
  badge?: string;
  onAdd?: () => void;
}) {
  return (
    <div className="mb-3 flex items-center gap-2 text-[0.85rem] font-semibold">
      <span className="text-muted-foreground">{icon}</span>
      {title}
      {badge && (
        <span className="rounded-lg bg-foreground/[0.08] px-1.5 py-px text-[0.65rem] font-medium text-muted-foreground">
          {badge}
        </span>
      )}
      {onAdd && (
        <button
          type="button"
          onClick={onAdd}
          className="ml-auto flex size-6 items-center justify-center rounded-full border border-dashed border-muted-foreground/50 text-muted-foreground transition-colors hover:border-primary hover:text-primary"
        >
          <Plus className="size-3" />
        </button>
      )}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  count?: number;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "-mb-px flex items-center gap-1.5 border-b-2 px-3.5 py-2.5 text-[0.8rem] font-medium transition-colors",
        active
          ? "border-primary text-primary"
          : "border-transparent text-muted-foreground hover:text-foreground"
      )}
    >
      {icon}
      {label}
      {!!count && (
        <span className="rounded-lg bg-foreground/[0.08] px-1.5 py-px text-[0.65rem] text-muted-foreground">
          {count}
        </span>
      )}
    </button>
  );
}

function EditActions({ onSave, onCancel }: { onSave: () => void; onCancel: () => void }) {
  return (
    <div className="flex gap-1">
      <button
        type="button"
        onClick={onSave}
        className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground transition-opacity hover:opacity-90"
      >
        <Check className="size-4" />
      </button>
      <button
        type="button"
        onClick={onCancel}
        className="flex size-7 items-center justify-center rounded-md bg-foreground/[0.06] text-muted-foreground transition-colors hover:bg-foreground/10"
      >
        <X className="size-4" />
      </button>
    </div>
  );
}

function SidebarSection({
  label,
  action,
  children,
}: {
  label: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-5">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[0.66rem] font-semibold uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        {action}
      </div>
      {children}
    </div>
  );
}

function AddButton({ onClick, icon }: { onClick: () => void; icon: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex size-[22px] items-center justify-center rounded-full border border-dashed border-muted-foreground/50 text-muted-foreground transition-colors hover:border-primary hover:text-primary"
    >
      {icon}
    </button>
  );
}

function EmptyHint({ children }: { children: React.ReactNode }) {
  return <p className="text-[0.75rem] italic text-muted-foreground/80">{children}</p>;
}

function DateRow({
  icon,
  label,
  value,
  highlight,
}: {
  icon: React.ReactNode;
  label: string;
  value: string | null;
  highlight?: boolean;
}) {
  if (!value) return null;
  return (
    <div className="flex items-center gap-1.5 py-1 text-[0.78rem]">
      <span className={cn("shrink-0 text-muted-foreground", highlight && "text-amber-500")}>
        {icon}
      </span>
      <span className="min-w-[52px] text-[0.66rem] uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className={cn("font-medium", highlight && "text-amber-600 dark:text-amber-500")}>
        {formatDate(value)}
      </span>
    </div>
  );
}

function LabeledDate({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[0.66rem] uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <Input
        type="date"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-7 text-[0.78rem]"
      />
    </div>
  );
}

function DetailRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="uppercase tracking-wide">{label}</span>
      <span className={cn("truncate text-foreground/80", mono && "font-mono")}>{value}</span>
    </div>
  );
}
