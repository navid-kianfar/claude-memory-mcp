import { useMemo, useState } from "react";
import { Clock, MessageSquare, Paperclip } from "lucide-react";
import type { Task, TaskRowMeta, TaskState } from "../types";
import {
  STATE_COLORS,
  STATE_LABELS,
  STATE_ORDER,
  formatDuration,
  isOverdue,
  labelColor,
  priorityColor,
} from "../lib/tasks";
import { cn } from "../lib/utils";
import { Badge } from "./ui/Badge";

export interface TaskBoardViewProps {
  tasks: Task[];
  meta: Record<string, TaskRowMeta>;
  onOpenTask: (task: Task) => void;
  /** Drop onto a column = change state. The board's whole point. */
  onChangeState: (task: Task, state: TaskState) => void;
}

/**
 * Board mode: a column per state, cards dragged between them.
 *
 * The counterpart to list mode, and the reason it earns its place is that
 * dragging a card IS the state change — in list mode changing state means
 * opening the dialog. Columns are the same STATE_ORDER the list groups by, so
 * the two views never disagree about what exists or where it belongs.
 *
 * Empty columns are still rendered: a board that hides "Blocked" until something
 * is blocked gives no clue the state exists, and a drop target has to be visible
 * before you can drop on it.
 */
export function TaskBoardView({
  tasks,
  meta,
  onOpenTask,
  onChangeState,
}: TaskBoardViewProps) {
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<TaskState | null>(null);

  const columns = useMemo(() => {
    const byState = new Map<TaskState, Task[]>();
    STATE_ORDER.forEach((s) => byState.set(s, []));
    tasks.forEach((t) => byState.get(t.state)?.push(t));
    return STATE_ORDER.map((state) => ({ state, items: byState.get(state) ?? [] }));
  }, [tasks]);

  return (
    <div className="flex gap-3 overflow-x-auto p-3">
      {columns.map(({ state, items }) => (
        <div
          key={state}
          onDragOver={(e) => {
            e.preventDefault();
            setOver(state);
          }}
          onDragLeave={() => setOver((s) => (s === state ? null : s))}
          onDrop={(e) => {
            e.preventDefault();
            setOver(null);
            const id = e.dataTransfer.getData("text/plain") || dragging;
            const task = tasks.find((t) => t.id === id);
            // A drop back onto the same column is not a change; firing anyway
            // would write a pointless mutation and mirror it to the board.
            if (task && task.state !== state) onChangeState(task, state);
            setDragging(null);
          }}
          className={cn(
            "flex w-64 shrink-0 flex-col rounded-lg border bg-muted/30 transition-colors",
            over === state ? "border-primary bg-primary/5" : "border-border/60"
          )}
        >
          <div className="flex items-center justify-between gap-2 border-b border-border/60 px-3 py-2">
            <span
              className={cn(
                "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                STATE_COLORS[state]
              )}
            >
              {STATE_LABELS[state]}
            </span>
            <span className="text-xs text-muted-foreground">{items.length}</span>
          </div>

          <div className="flex min-h-24 flex-col gap-2 p-2">
            {items.map((task) => {
              const m = meta[task.id];
              return (
                <button
                  key={task.id}
                  draggable
                  onDragStart={(e) => {
                    e.dataTransfer.setData("text/plain", task.id);
                    setDragging(task.id);
                  }}
                  onDragEnd={() => setDragging(null)}
                  onClick={() => onOpenTask(task)}
                  className={cn(
                    "rounded-md border border-border bg-card p-2 text-left transition-opacity hover:border-primary/50",
                    dragging === task.id && "opacity-40"
                  )}
                >
                  <div className="flex items-start gap-1.5">
                    <span
                      className={cn(
                        "mt-1 size-1.5 shrink-0 rounded-full",
                        priorityColor(task.priority)
                      )}
                    />
                    <span className="line-clamp-3 text-xs leading-snug">
                      {task.title}
                    </span>
                  </div>

                  {task.role && (
                    <div className="mt-1.5">
                      <span
                        title={`Routed to the ${task.role} agent`}
                        className="rounded border border-primary/30 px-1 py-0.5 text-[9px] font-semibold text-primary"
                      >
                        agent:{task.role}
                      </span>
                    </div>
                  )}
                  {task.labels.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {task.labels.slice(0, 3).map((l) => (
                        <span
                          key={l}
                          className={cn(
                            "rounded px-1 py-0.5 text-[9px]",
                            labelColor(l)
                          )}
                        >
                          {l}
                        </span>
                      ))}
                    </div>
                  )}

                  <div className="mt-1.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                    {m?.comments ? (
                      <span className="flex items-center gap-0.5">
                        <MessageSquare className="size-2.5" />
                        {m.comments}
                      </span>
                    ) : null}
                    {m?.attachments ? (
                      <span className="flex items-center gap-0.5">
                        <Paperclip className="size-2.5" />
                        {m.attachments}
                      </span>
                    ) : null}
                    {m?.minutes_spent ? (
                      <span className="flex items-center gap-0.5">
                        <Clock className="size-2.5" />
                        {formatDuration(m.minutes_spent)}
                      </span>
                    ) : null}
                    {isOverdue(task) && (
                      <Badge variant="destructive" className="px-1 py-0 text-[9px]">
                        overdue
                      </Badge>
                    )}
                  </div>
                </button>
              );
            })}
            {items.length === 0 && (
              <p className="px-1 py-3 text-center text-[10px] text-muted-foreground/60">
                drop here
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
