import { Pause } from "lucide-react";
import type { AsoodeSyncState } from "../types";
import { formatPauseDate, pluralize } from "../lib/syncPause";

/**
 * A small note beside the task list saying board sync is paused, so nobody
 * wonders why a change has not reached the board. Renders nothing otherwise.
 */
export function SyncPausedHint({ state }: { state: AsoodeSyncState | null }) {
  if (!state?.paused) return null;

  const untilLabel = state.until ? formatPauseDate(state.until) : null;
  const queued = pluralize(state.queued_ops, "change", "changes");
  const label = state.overdue ? "Pause ended, resume?" : "Board sync paused";
  const detail = [
    untilLabel ? `Paused until ${untilLabel}.` : "Paused until resumed by hand.",
    `${queued} waiting to be sent.`,
    state.reason ? `Reason: ${state.reason}` : null,
    "You can resume it under Integrations.",
  ]
    .filter((part): part is string => part !== null)
    .join(" ");

  return (
    <span
      title={detail}
      aria-label={`${label}. ${detail}`}
      className="inline-flex items-center gap-1 whitespace-nowrap rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-300"
    >
      <Pause className="size-3" />
      {label}
      <span className="font-normal opacity-80">· {state.queued_ops} waiting</span>
    </span>
  );
}
