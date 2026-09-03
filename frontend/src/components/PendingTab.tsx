import { useCallback, useEffect, useState } from "react";
import { Sparkles, Trash2, Wand2 } from "lucide-react";
import type { ImportOrigin, Memory } from "../types";
import { api } from "../lib/api";
import { useToast } from "./ui/Toast";
import { Card } from "./ui/Card";
import { Button } from "./ui/Button";
import { Badge, CategoryBadge } from "./ui/Badge";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";

export interface PendingTabProps {
  projectSlug: string;
  onChanged?: () => void;
}

function origin(memory: Memory): ImportOrigin | null {
  const raw = (memory.metadata ?? {})["imported_from"];
  if (!raw || typeof raw !== "object") return null;
  return raw as ImportOrigin;
}

/**
 * Imports waiting to be rewritten for this project.
 *
 * A rule written elsewhere carries that project's component names, paths and
 * stack. Until someone rewrites it here it stays out of the rule block, out of
 * search and out of the git snapshot - so this view is where it becomes real,
 * either by hand or (usually) by asking Claude to adapt it in a session.
 */
export function PendingTab({ projectSlug, onChanged }: PendingTabProps) {
  const { toast } = useToast();
  const [pending, setPending] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { title: string; content: string }>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listPendingAdaptations(projectSlug);
      setPending(res.pending);
      setDrafts(
        Object.fromEntries(
          res.pending.map((m) => [m.id, { title: m.title, content: m.content }])
        )
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load pending imports");
    } finally {
      setLoading(false);
    }
  }, [projectSlug]);

  useEffect(() => {
    void load();
  }, [load]);

  const adapt = async (memory: Memory) => {
    const draft = drafts[memory.id];
    if (!draft?.title.trim() || !draft?.content.trim()) {
      toast({ title: "Title and content are required", variant: "error" });
      return;
    }
    try {
      await api.adaptPending(projectSlug, memory.id, {
        title: draft.title.trim(),
        content: draft.content.trim(),
      });
      toast({ title: "Adapted and now in force", variant: "success" });
      await load();
      onChanged?.();
    } catch (err) {
      toast({
        title: "Failed to adapt",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  const discard = async (memory: Memory) => {
    try {
      await api.discardPending(projectSlug, memory.id, "discarded in the UI");
      toast({ title: "Import discarded", variant: "success" });
      await load();
      onChanged?.();
    } catch (err) {
      toast({
        title: "Failed to discard",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading pending imports…</p>;
  }
  if (error) {
    return <p className="text-sm text-destructive">{error}</p>;
  }
  if (pending.length === 0) {
    return (
      <Card className="p-6 text-center">
        <Sparkles className="mx-auto size-6 text-muted-foreground" />
        <p className="mt-2 text-sm font-medium">Nothing waiting to be adapted</p>
        <p className="mt-1 text-sm text-muted-foreground">
          Memories imported from another project land here first. They stay out of
          this project's rules until they have been rewritten for it.
        </p>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <Card className="border-amber-500/30 bg-amber-500/10 p-4">
        <p className="text-sm">
          <strong>{pending.length}</strong>{" "}
          {pending.length === 1 ? "import is" : "imports are"} waiting to be adapted to
          this project. None of them are in force, searchable, or in the git
          snapshot yet. Ask Claude in a session for this project to adapt them —
          it will rewrite each one against this codebase and ask you about
          anything it cannot work out — or edit and apply them here.
        </p>
      </Card>

      {pending.map((memory) => {
        const src = origin(memory);
        const draft = drafts[memory.id] ?? { title: memory.title, content: memory.content };
        return (
          <Card key={memory.id} className="space-y-4 p-4">
            <div className="flex flex-wrap items-center gap-2">
              <CategoryBadge category={memory.category} />
              <Badge className="border-amber-500/30 bg-amber-500/15 text-amber-500">
                Pending adaptation
              </Badge>
              {src && (
                <span className="text-xs text-muted-foreground">
                  imported from <code className="font-mono">{src.project}</code>
                </span>
              )}
            </div>

            {src && (
              <div className="rounded-md border border-border bg-muted/40 p-3">
                <p className="text-xs font-medium text-muted-foreground">
                  Original, as written for {src.project}
                </p>
                <p className="mt-1 text-sm font-medium">{src.title}</p>
                <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                  {src.content}
                </p>
              </div>
            )}

            <div className="space-y-2">
              <Label htmlFor={`title-${memory.id}`}>Title for this project</Label>
              <Input
                id={`title-${memory.id}`}
                value={draft.title}
                onChange={(e) =>
                  setDrafts((d) => ({
                    ...d,
                    [memory.id]: { ...draft, title: e.target.value },
                  }))
                }
              />
              <Label htmlFor={`content-${memory.id}`}>Content for this project</Label>
              <Textarea
                id={`content-${memory.id}`}
                rows={4}
                value={draft.content}
                onChange={(e) =>
                  setDrafts((d) => ({
                    ...d,
                    [memory.id]: { ...draft, content: e.target.value },
                  }))
                }
              />
            </div>

            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => void discard(memory)}>
                <Trash2 className="size-4" />
                Discard
              </Button>
              <Button onClick={() => void adapt(memory)}>
                <Wand2 className="size-4" />
                Apply to this project
              </Button>
            </div>
          </Card>
        );
      })}
    </div>
  );
}
