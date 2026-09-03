import type { Task, TaskState } from "../types";

/**
 * Task presentation constants, shared by the list view and the task dialog.
 *
 * The state vocabulary and its colours are asoode's, verbatim — the local task
 * store uses the same nine states so the eventual bridge maps losslessly, and
 * using the same colours means the two apps read the same way side by side.
 */

export const STATE_COLORS: Record<TaskState, string> = {
  todo: "#cccccc",
  in_progress: "#59a8ef",
  done: "#5eb258",
  paused: "#666666",
  blocked: "#b33634",
  cancelled: "#666666",
  duplicate: "#808080",
  incomplete: "#b3b3b3",
  blocker: "#eb973e",
};

export const STATE_LABELS: Record<TaskState, string> = {
  todo: "To Do",
  in_progress: "In Progress",
  done: "Done",
  paused: "Paused",
  blocked: "Blocked",
  cancelled: "Cancelled",
  duplicate: "Duplicate",
  incomplete: "Incomplete",
  blocker: "Blocker",
};

/** Group order in the list view: what is underway, then next, then stuck, then closed. */
export const STATE_ORDER: TaskState[] = [
  "in_progress",
  "todo",
  "blocker",
  "blocked",
  "paused",
  "incomplete",
  "done",
  "cancelled",
  "duplicate",
];

export const OPEN_STATES: TaskState[] = [
  "todo",
  "in_progress",
  "paused",
  "blocked",
  "blocker",
  "incomplete",
];

export const STATE_OPTIONS = STATE_ORDER.map((value) => ({
  value,
  label: STATE_LABELS[value],
  color: STATE_COLORS[value],
}));

/** Priority is memory-mcp's 0–3; the colour ramp is asoode's flag ramp. */
export const PRIORITY_COLORS: Record<number, string> = {
  0: "#87909e",
  1: "#59a8ef",
  2: "#fbb900",
  3: "#d50102",
};

export const PRIORITY_LABELS: Record<number, string> = {
  0: "Normal",
  1: "Low",
  2: "High",
  3: "Urgent",
};

export function priorityColor(priority: number | null | undefined): string {
  return PRIORITY_COLORS[priority ?? 0] ?? PRIORITY_COLORS[0];
}

export function isClosed(state: TaskState): boolean {
  return state === "done" || state === "cancelled" || state === "duplicate";
}

export function isOverdue(task: Task): boolean {
  if (!task.due_at || isClosed(task.state)) return false;
  const due = new Date(task.due_at);
  if (Number.isNaN(due.getTime())) return false;
  return due.getTime() < Date.now();
}

/** Initials for a free-text assignee — this store has names, not user records. */
export function initials(name: string | null | undefined): string {
  const value = (name ?? "").trim();
  if (!value) return "?";
  const parts = value.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** Stable colour per name, so the same assignee always looks the same. */
export function avatarColor(name: string | null | undefined): string {
  const value = (name ?? "").trim() || "?";
  const hash = value
    .split("")
    .reduce((acc, char) => char.charCodeAt(0) + ((acc << 5) - acc), 0);
  return `hsl(${Math.abs(hash) % 360}, 55%, 48%)`;
}

/** Same idea for labels, which are free-text here rather than board-scoped rows. */
export function labelColor(label: string): string {
  const hash = label
    .split("")
    .reduce((acc, char) => char.charCodeAt(0) + ((acc << 5) - acc), 0);
  return `hsl(${Math.abs(hash) % 360}, 45%, 45%)`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** For the date <input type="date"> round-trip. */
export function toDateInput(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Split minutes into the days:hours:minutes the dialog's timer shows. */
export function splitDuration(minutes: number): {
  days: number;
  hours: number;
  minutes: number;
} {
  const total = Math.max(0, Math.floor(minutes));
  return {
    days: Math.floor(total / 1440),
    hours: Math.floor((total % 1440) / 60),
    minutes: total % 60,
  };
}

export function formatDuration(minutes: number): string {
  const { days, hours, minutes: mins } = splitDuration(minutes);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

/** Minutes between two timestamps, counting an open entry up to now. */
export function entryMinutes(begin: string, end: string | null): number {
  const from = new Date(begin).getTime();
  const to = end ? new Date(end).getTime() : Date.now();
  if (Number.isNaN(from) || Number.isNaN(to)) return 0;
  return Math.max(0, Math.floor((to - from) / 60000));
}

/** Provenance operations rendered as something a person can read. */
const ACTIVITY_LABELS: Record<string, string> = {
  task_create: "created this task",
  task_update: "updated this task",
  task_comment: "commented",
  task_start: "started the clock",
  task_stop: "stopped the clock",
  task_done: "marked it done",
  task_archive: "archived it",
  task_claim: "claimed it",
  task_release: "released the claim",
};

export function activityLabel(operation: string, details?: Record<string, unknown> | null): string {
  const base = ACTIVITY_LABELS[operation] ?? operation.replace(/_/g, " ");
  if (operation === "task_update" && details) {
    const from = details.state_from as string | undefined;
    const to = details.state_to as string | undefined;
    if (from && to && from !== to) {
      return `moved it from ${STATE_LABELS[from as TaskState] ?? from} to ${
        STATE_LABELS[to as TaskState] ?? to
      }`;
    }
    const changed = details.changed as string[] | undefined;
    if (changed?.length) return `changed ${changed.join(", ")}`;
  }
  if (operation === "task_comment" && details?.kind) {
    return `added a ${String(details.kind)}`;
  }
  if (operation === "task_claim" && details?.session_id) {
    return `claimed it for session ${String(details.session_id).slice(0, 8)}`;
  }
  return base;
}
