import { useState } from "react";
import {
  ChevronDown,
  Clock,
  History,
  Pencil,
  Trash2,
  Hash,
} from "lucide-react";
import type { Memory, ProvenanceEntry } from "../types";
import { api } from "../lib/api";
import { cn, formatDate, formatRelative, priorityLabel } from "../lib/utils";
import { Card } from "./ui/Card";
import { Badge, CategoryBadge } from "./ui/Badge";
import { Button } from "./ui/Button";

export interface MemoryCardProps {
  memory: Memory;
  projectSlug: string;
  onEdit: (memory: Memory) => void;
  onDelete: (memory: Memory) => void;
}

export function MemoryCard({
  memory,
  projectSlug,
  onEdit,
  onDelete,
}: MemoryCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [history, setHistory] = useState<ProvenanceEntry[] | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);

  async function loadHistory() {
    if (showHistory) {
      setShowHistory(false);
      return;
    }
    setShowHistory(true);
    if (history) return;
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const res = await api.getProvenance(projectSlug, memory.id);
      setHistory(res.provenance);
    } catch (err) {
      setHistoryError(
        err instanceof Error ? err.message : "Failed to load history"
      );
    } finally {
      setHistoryLoading(false);
    }
  }

  const similarity =
    memory._similarity !== undefined
      ? Math.round(memory._similarity * 100)
      : null;

  return (
    <Card className="overflow-hidden transition-colors hover:border-border/80">
      <button
        onClick={() => setExpanded((e) => !e)}
        className="flex w-full items-start gap-3 p-4 text-left"
      >
        <ChevronDown
          className={cn(
            "mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-180"
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <CategoryBadge category={memory.category} />
            {memory.status === "archived" && (
              <Badge variant="secondary">archived</Badge>
            )}
            {similarity !== null && (
              <Badge variant="default">{similarity}% match</Badge>
            )}
            <span className="truncate text-sm font-semibold">
              {memory.title}
            </span>
          </div>
          <p
            className={cn(
              "mt-1.5 whitespace-pre-wrap text-sm text-muted-foreground",
              !expanded && "line-clamp-2"
            )}
          >
            {memory.content}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <Clock className="size-3" />
              {formatRelative(memory.updated_at || memory.created_at)}
            </span>
            <span>Priority: {priorityLabel(memory.priority)}</span>
            <span>Accessed {memory.access_count}×</span>
            {memory.tags.length > 0 && (
              <span className="flex items-center gap-1">
                {memory.tags.slice(0, 6).map((t) => (
                  <span
                    key={t}
                    className="inline-flex items-center gap-0.5 rounded bg-muted px-1.5 py-0.5"
                  >
                    <Hash className="size-2.5" />
                    {t}
                  </span>
                ))}
              </span>
            )}
          </div>
        </div>
      </button>

      {expanded && (
        <div className="border-t border-border bg-muted/30 px-4 py-3">
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-muted-foreground sm:grid-cols-3">
            <span>
              <span className="text-foreground/70">ID:</span>{" "}
              <span className="font-mono">{memory.id}</span>
            </span>
            <span>
              <span className="text-foreground/70">Created:</span>{" "}
              {formatDate(memory.created_at)}
            </span>
            <span>
              <span className="text-foreground/70">Source:</span>{" "}
              {memory.source || "—"}
            </span>
          </div>

          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => onEdit(memory)}>
              <Pencil />
              Edit
            </Button>
            <Button size="sm" variant="outline" onClick={loadHistory}>
              <History />
              History
            </Button>
            <Button
              size="sm"
              variant="destructive"
              onClick={() => onDelete(memory)}
            >
              <Trash2 />
              Delete
            </Button>
          </div>

          {showHistory && (
            <div className="mt-3 rounded-md border border-border bg-background p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Provenance
              </p>
              {historyLoading && (
                <p className="text-sm text-muted-foreground">Loading…</p>
              )}
              {historyError && (
                <p className="text-sm text-destructive">{historyError}</p>
              )}
              {history && history.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  No history recorded.
                </p>
              )}
              {history && history.length > 0 && (
                <ol className="space-y-2">
                  {history.map((entry) => (
                    <li
                      key={entry.id}
                      className="flex gap-3 border-l-2 border-border pl-3 text-sm"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <Badge variant="outline">{entry.operation}</Badge>
                          <span className="text-xs text-muted-foreground">
                            {formatDate(entry.created_at)}
                          </span>
                        </div>
                        {entry.details && (
                          <p className="mt-0.5 text-xs text-muted-foreground">
                            {entry.details}
                          </p>
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
