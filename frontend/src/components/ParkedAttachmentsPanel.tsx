import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Paperclip, RefreshCw, X } from "lucide-react";
import { api } from "../lib/api";
import type { ParkedAttachment } from "../types";
import { formatDate, formatRelative } from "../lib/utils";
import { pluralize } from "../lib/syncPause";
import { Button } from "./ui/Button";
import { Card } from "./ui/Card";
import { useToast } from "./ui/Toast";

/** One page of the inbox; the server allows 1-200. */
const PAGE_SIZE = 20;
const BYTES_PER_KILOBYTE = 1024;
const HTTP_NOT_FOUND = 404;
const HTTP_CONFLICT = 409;
const SIZE_UNITS: readonly string[] = ["B", "KB", "MB", "GB"];

export interface ParkedAttachmentsPanelProps {
  projectSlug: string;
  /** Bumped by the parent's Refresh to reload the first page in place. */
  reloadSignal?: number;
}

/** 1536 -> "1.5 KB". */
function formatSize(bytes: number): string {
  let value = bytes;
  let unitIndex = 0;
  while (value >= BYTES_PER_KILOBYTE && unitIndex < SIZE_UNITS.length - 1) {
    value /= BYTES_PER_KILOBYTE;
    unitIndex += 1;
  }
  const rounded = unitIndex === 0 ? String(value) : value.toFixed(1);
  return `${rounded} ${SIZE_UNITS[unitIndex]}`;
}

/**
 * Files pasted into a Claude session that never reached a task, so they can be
 * cleared out. Renders nothing while loading, when the list is empty, or when
 * the daemon is too old to have the route (404).
 */
export function ParkedAttachmentsPanel({ projectSlug, reloadSignal = 0 }: ParkedAttachmentsPanelProps) {
  const { toast } = useToast();
  const [items, setItems] = useState<readonly ParkedAttachment[]>([]);
  const [total, setTotal] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [dismissingId, setDismissingId] = useState<string | null>(null);
  // Answers for a project the user has since left are dropped.
  const currentSlug = useRef(projectSlug);
  currentSlug.current = projectSlug;

  const load = useCallback(async () => {
    const forSlug = projectSlug;
    try {
      const page = await api.listParkedAttachments(forSlug, { limit: PAGE_SIZE });
      if (currentSlug.current !== forSlug) return;
      setItems(page.items);
      setTotal(page.total);
      setLoadError(null);
    } catch (err) {
      if (currentSlug.current !== forSlug) return;
      setItems([]);
      setTotal(0);
      // An older daemon has no inbox route: nothing to show, not a failure.
      const isMissingRoute = api.isApiError(err) && err.status === HTTP_NOT_FOUND;
      if (isMissingRoute) return;
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, [projectSlug]);

  // A new project starts empty; the old project's files must not linger.
  useEffect(() => {
    setItems([]);
    setTotal(0);
    setLoadError(null);
  }, [projectSlug]);

  // A refresh keeps the current rows on screen until the new page arrives.
  useEffect(() => {
    void load();
  }, [load, reloadSignal]);

  const loadMore = async () => {
    const forSlug = projectSlug;
    setIsLoadingMore(true);
    try {
      const page = await api.listParkedAttachments(forSlug, {
        limit: PAGE_SIZE,
        offset: items.length,
      });
      if (currentSlug.current !== forSlug) return;
      const known = new Set(items.map((item) => item.pending_id));
      const fresh = page.items.filter((item) => !known.has(item.pending_id));
      setItems([...items, ...fresh]);
      setTotal(page.total);
    } catch (err) {
      toast({
        title: "Could not load more parked files",
        description: err instanceof Error ? err.message : String(err),
        variant: "error",
      });
    } finally {
      setIsLoadingMore(false);
    }
  };

  const removeFromList = (pendingId: string) => {
    setItems((previous) => previous.filter((item) => item.pending_id !== pendingId));
    setTotal((previous) => Math.max(0, previous - 1));
  };

  const dismiss = async (item: ParkedAttachment) => {
    setDismissingId(item.pending_id);
    try {
      await api.dismissParkedAttachment(projectSlug, item.pending_id);
      removeFromList(item.pending_id);
    } catch (err) {
      const status = api.isApiError(err) ? err.status : null;
      if (status === HTTP_CONFLICT) {
        removeFromList(item.pending_id);
        toast({
          title: `${item.filename} is already attached to a task`,
          description: "It was removed from this list.",
        });
        return;
      }
      if (status === HTTP_NOT_FOUND) {
        removeFromList(item.pending_id);
        toast({ title: `${item.filename} is no longer parked` });
        return;
      }
      toast({
        title: `Could not dismiss ${item.filename}`,
        description: err instanceof Error ? err.message : String(err),
        variant: "error",
      });
    } finally {
      setDismissingId(null);
    }
  };

  if (loadError) {
    return (
      <Card className="flex items-center justify-between gap-3 p-3">
        <p className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
          <AlertTriangle className="size-3.5 shrink-0" />
          <span className="truncate">Parked files could not be loaded: {loadError}</span>
        </p>
        <Button variant="ghost" size="sm" onClick={() => void load()}>
          <RefreshCw className="size-3" /> Try again
        </Button>
      </Card>
    );
  }
  if (items.length === 0) return null;

  const hasMore = items.length < total;

  return (
    <Card className="p-4">
      <div className="mb-1 flex items-center gap-2">
        <Paperclip className="size-4 text-muted-foreground" />
        <h3 className="text-sm font-semibold">Parked files</h3>
        <span className="text-xs text-muted-foreground">{total}</span>
      </div>
      <p className="mb-3 text-xs text-muted-foreground">
        Files pasted into a session that were never attached to a task. Dismiss the
        ones you no longer need.
      </p>
      <ul className="max-h-72 divide-y divide-border/60 overflow-y-auto pr-1">
        {items.map((item) => {
          const isDismissing = dismissingId === item.pending_id;
          const sizeLabel = formatSize(item.size_bytes);
          const whenLabel = formatRelative(item.created_at);
          const whenTitle = formatDate(item.created_at);
          return (
            <li key={item.pending_id} className="flex items-center gap-3 py-2">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm" title={item.filename}>
                  {item.filename}
                </p>
                <p className="text-xs text-muted-foreground">
                  {sizeLabel} · <span title={whenTitle}>{whenLabel}</span>
                </p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                disabled={isDismissing}
                aria-label={`Dismiss ${item.filename}`}
                onClick={() => void dismiss(item)}
              >
                <X className="size-3" />
                {isDismissing ? "Dismissing…" : "Dismiss"}
              </Button>
            </li>
          );
        })}
      </ul>
      {hasMore && (
        <div className="mt-2 flex justify-center">
          <Button
            variant="ghost"
            size="sm"
            disabled={isLoadingMore}
            onClick={() => void loadMore()}
          >
            {isLoadingMore
              ? "Loading…"
              : `Show more (${pluralize(total - items.length, "file", "files")} left)`}
          </Button>
        </div>
      )}
    </Card>
  );
}
