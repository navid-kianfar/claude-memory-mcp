import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, Pause, Play, X } from "lucide-react";
import { useSyncState } from "../hooks/useSyncState";
import type { AsoodeDrain, AsoodeSyncState, PauseSyncInput } from "../types";
import { formatDate } from "../lib/utils";
import { formatPauseDate, pluralize } from "../lib/syncPause";
import { Button } from "./ui/Button";
import { Badge } from "./ui/Badge";
import { PauseSyncDialog } from "./PauseSyncDialog";

export interface SyncPauseControlProps {
  projectSlug: string;
  projectName: string;
}

const WARNING_PANEL =
  "rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300";

/**
 * Pause and resume a linked project's board sync, with the paused badge and
 * the resume's live progress.
 *
 * Renders nothing for an unlinked project: there is no sync to pause, and the
 * server would refuse the request anyway.
 */
export function SyncPauseControl({ projectSlug, projectName }: SyncPauseControlProps) {
  const sync = useSyncState(projectSlug);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [isResuming, setIsResuming] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // A finished drain stays in the daemon's memory until it restarts, so its
  // result is shown only when this view started or watched it - not as a
  // stale banner on every visit.
  const [isWatchingDrain, setIsWatchingDrain] = useState(false);

  useEffect(() => {
    if (sync.isDraining) setIsWatchingDrain(true);
  }, [sync.isDraining]);

  const state = sync.state;
  if (sync.error && !state) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <AlertTriangle className="size-3 shrink-0" />
        Sync status is not available right now: {sync.error}
      </p>
    );
  }
  if (!state) {
    return <p className="text-xs text-muted-foreground">Checking sync status…</p>;
  }
  if (!state.linked) return null;

  const handlePause = async (input: PauseSyncInput) => {
    setActionError(null);
    await sync.pause(input);
    setIsDialogOpen(false);
  };

  const handleResume = async () => {
    setIsResuming(true);
    setActionError(null);
    setIsWatchingDrain(true);
    try {
      await sync.resume();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsResuming(false);
    }
  };

  const drain = isWatchingDrain ? state.drain : null;

  return (
    <div className="space-y-2">
      {state.paused ? (
        <PausedPanel
          state={state}
          isResuming={isResuming || sync.isDraining}
          onEdit={() => setIsDialogOpen(true)}
          onResume={() => void handleResume()}
        />
      ) : (
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            Changes are sent to the board as they happen.
          </p>
          <Button
            variant="outline"
            size="sm"
            disabled={sync.isDraining}
            onClick={() => setIsDialogOpen(true)}
          >
            <Pause className="size-3" /> Pause sync
          </Button>
        </div>
      )}

      {actionError && (
        <p role="alert" className="flex items-start gap-1.5 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 size-3 shrink-0" />
          {actionError}
        </p>
      )}

      {drain && (
        <DrainStatus drain={drain} onDismiss={() => setIsWatchingDrain(false)} />
      )}

      {isDialogOpen && (
        <PauseSyncDialog
          projectName={projectName}
          isEditing={state.paused}
          initialUntil={state.until}
          initialReason={state.reason}
          onClose={() => setIsDialogOpen(false)}
          onSubmit={handlePause}
        />
      )}
    </div>
  );
}

function PausedPanel({
  state,
  isResuming,
  onEdit,
  onResume,
}: {
  state: AsoodeSyncState;
  isResuming: boolean;
  onEdit: () => void;
  onResume: () => void;
}) {
  const sinceLabel = state.since ? formatDate(state.since) : null;
  const untilLabel = state.until ? formatPauseDate(state.until) : null;
  const queuedLabel = pluralize(state.queued_ops, "change", "changes");

  return (
    <div className={WARNING_PANEL}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge className="border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300">
              <Pause className="mr-1 size-3" /> Sync paused
            </Badge>
            <span className="text-foreground/80">
              {queuedLabel} waiting to be sent
            </span>
          </div>
          <p className="text-foreground/80">
            {sinceLabel ? `Paused since ${sinceLabel}` : "Paused"}
            {untilLabel ? `, until ${untilLabel}.` : ", until you resume it."}
          </p>
          {state.overdue && untilLabel && (
            <p className="font-medium">
              The pause ended on {untilLabel}. Would you like to resume sync now?
            </p>
          )}
          {state.reason && (
            <p className="whitespace-pre-wrap break-words text-foreground/70">
              “{state.reason}”
            </p>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="ghost" size="sm" disabled={isResuming} onClick={onEdit}>
            Change pause
          </Button>
          <Button size="sm" disabled={isResuming} onClick={onResume}>
            {isResuming ? (
              <Loader2 className="size-3 animate-spin" />
            ) : (
              <Play className="size-3" />
            )}
            {isResuming ? "Resuming…" : "Resume sync"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function DrainStatus({ drain, onDismiss }: { drain: AsoodeDrain; onDismiss: () => void }) {
  const sent = pluralize(drain.flushed, "change", "changes");

  if (drain.state === "running") {
    return (
      <p
        role="status"
        aria-live="polite"
        className="flex items-center gap-1.5 text-xs text-muted-foreground"
      >
        <Loader2 className="size-3 shrink-0 animate-spin" />
        Sending the queued changes… {sent} sent so far
        {drain.remaining > 0 ? `, ${drain.remaining} still to go.` : "."}
      </p>
    );
  }

  const isClean =
    drain.failed === 0 && drain.remaining === 0 && !drain.error && !drain.stopped_by_pause;
  const summaryParts = [
    `Sent ${sent}`,
    ...(drain.failed > 0 ? [`${drain.failed} could not be sent`] : []),
    ...(drain.remaining > 0 ? [`${drain.remaining} still waiting`] : []),
  ];
  const importedNote =
    drain.imported && drain.imported > 0
      ? ` ${pluralize(drain.imported, "task was", "tasks were")} brought in from the board.`
      : "";

  return (
    <div
      role="status"
      aria-live="polite"
      className={
        isClean
          ? "flex items-start justify-between gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-xs text-emerald-700 dark:text-emerald-300"
          : `flex items-start justify-between gap-2 ${WARNING_PANEL}`
      }
    >
      <div className="flex min-w-0 items-start gap-1.5">
        {isClean ? (
          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0" />
        ) : (
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
        )}
        <div className="min-w-0 space-y-1">
          <p>
            {isClean ? "All caught up. " : ""}
            {summaryParts.join(", ")}.{importedNote}
          </p>
          {drain.stopped_by_pause && (
            <p>Sending stopped because sync was paused again.</p>
          )}
          {drain.error && <p className="break-words">{drain.error}</p>}
          {!isClean && drain.remaining > 0 && !drain.stopped_by_pause && (
            <p>The rest stay queued on this machine for the next sync.</p>
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss the resume result"
        className="rounded-md p-0.5 opacity-70 transition-opacity hover:opacity-100"
      >
        <X className="size-3.5" />
      </button>
    </div>
  );
}
