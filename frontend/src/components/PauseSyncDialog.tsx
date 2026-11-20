import { useState } from "react";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Label } from "./ui/Label";
import { Textarea } from "./ui/Textarea";
import type { PauseSyncInput } from "../types";
import { MAX_PAUSE_REASON_LENGTH, todayIsoDate } from "../lib/syncPause";

/** Mount this only while it is open; its fields are seeded on mount. */
export interface PauseSyncDialogProps {
  projectName: string;
  /** True when the project is already paused: the dialog edits that pause. */
  isEditing: boolean;
  initialUntil: string | null;
  initialReason: string | null;
  onClose: () => void;
  /** Rejects with the server's message, which is shown in the dialog. */
  onSubmit: (input: PauseSyncInput) => Promise<void>;
}

/** Pause, or change an existing pause: an optional end date and reason. */
export function PauseSyncDialog({
  projectName,
  isEditing,
  initialUntil,
  initialReason,
  onClose,
  onSubmit,
}: PauseSyncDialogProps) {
  // Seeded once per mount: the parent mounts the dialog only while it is open,
  // so the first frame already shows the current pause, never empty fields.
  const [until, setUntil] = useState(initialUntil ?? "");
  const [reason, setReason] = useState(initialReason ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const today = todayIsoDate();
  // ISO dates compare correctly as strings.
  const isUntilPast = until !== "" && until < today;
  const isReasonTooLong = reason.trim().length > MAX_PAUSE_REASON_LENGTH;
  const canSubmit = !busy && !isUntilPast && !isReasonTooLong;

  const title = isEditing ? "Change the pause" : "Pause sync with asoode";
  const submitLabel = isEditing ? "Save changes" : "Pause sync";
  const busyLabel = isEditing ? "Saving…" : "Pausing…";

  async function handleSubmit() {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const trimmedReason = reason.trim();
    const input: PauseSyncInput = {
      ...(until ? { until } : {}),
      ...(trimmedReason ? { reason: trimmedReason } : {}),
    };
    try {
      await onSubmit(input);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The pause could not be saved.");
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={onClose} dismissible={!busy} className="max-w-md">
      <DialogHeader
        title={title}
        description={`Tasks in “${projectName}” keep working here as usual. Changes wait on this machine and are sent to the board together when you resume.`}
        onClose={busy ? undefined : onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="pause-sync-until">Pause until (optional)</Label>
          <div className="flex gap-2">
            <Input
              id="pause-sync-until"
              type="date"
              min={today}
              value={until}
              aria-invalid={isUntilPast}
              aria-describedby="pause-sync-until-help"
              onChange={(e) => {
                setUntil(e.target.value);
                if (error) setError(null);
              }}
            />
            {until && (
              <Button variant="ghost" onClick={() => setUntil("")} disabled={busy}>
                Clear
              </Button>
            )}
          </div>
          <p
            id="pause-sync-until-help"
            className={isUntilPast ? "text-xs text-destructive" : "text-xs text-muted-foreground"}
          >
            {isUntilPast
              ? "Please pick today or a later date."
              : "Sync does not restart by itself. After this date the pause is flagged so you can resume it. Leave it empty to pause until you resume by hand."}
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="pause-sync-reason">Reason (optional)</Label>
          <Textarea
            id="pause-sync-reason"
            value={reason}
            rows={3}
            className="min-h-[72px]"
            placeholder="For example: working offline on the redesign for two weeks"
            aria-invalid={isReasonTooLong}
            aria-describedby="pause-sync-reason-count"
            onChange={(e) => {
              setReason(e.target.value);
              if (error) setError(null);
            }}
          />
          <p
            id="pause-sync-reason-count"
            className={
              isReasonTooLong ? "text-right text-xs text-destructive" : "text-right text-xs text-muted-foreground"
            }
          >
            {reason.trim().length} / {MAX_PAUSE_REASON_LENGTH}
          </p>
        </div>

        {error && (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            {error}
          </div>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button onClick={() => void handleSubmit()} disabled={!canSubmit}>
          {busy ? busyLabel : submitLabel}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
