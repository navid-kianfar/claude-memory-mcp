import { useCallback, useEffect, useState } from "react";
import { Check, ShieldCheck, Undo2 } from "lucide-react";
import type { PendingRuleEntry } from "../../types";
import { api } from "../../lib/api";
import { useToast } from "../ui/Toast";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Badge } from "../ui/Badge";
import { CategoryBadge } from "../ui/Badge";

export function ModerationQueue() {
  const { toast } = useToast();
  const [pending, setPending] = useState<PendingRuleEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listPendingRules();
      setPending(res.pending);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load queue");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (
    entry: PendingRuleEntry,
    action: "approve" | "revoke"
  ) => {
    try {
      if (action === "approve") {
        await api.approveRule(entry.project.slug, entry.rule.id);
        toast({ title: "Rule approved", variant: "success" });
      } else {
        await api.revokeRule(entry.project.slug, entry.rule.id);
        toast({ title: "Rule revoked", variant: "success" });
      }
      void load();
    } catch (err) {
      toast({
        title: `Failed to ${action} rule`,
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <ShieldCheck className="size-5 text-primary" />
          Moderation queue
        </h1>
        <p className="text-sm text-muted-foreground">
          Rules proposed by members across all projects, awaiting review.
        </p>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading && (
        <div className="space-y-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-24 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      )}

      {!loading && pending.length === 0 && (
        <div className="rounded-lg border border-dashed border-border py-12 text-center">
          <p className="text-sm text-muted-foreground">
            Nothing to review — the queue is empty.
          </p>
        </div>
      )}

      {!loading &&
        pending.map((entry) => (
          <Card key={`${entry.project.slug}:${entry.rule.id}`} className="border-border">
            <div className="p-4">
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-semibold">{entry.rule.title}</p>
                  <CategoryBadge category={entry.rule.category} />
                </div>
                <Badge variant="outline">{entry.project.display_name}</Badge>
              </div>
              <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                {entry.rule.content}
              </p>
              {entry.rule.created_by && (
                <p className="mt-1.5 text-xs text-muted-foreground">
                  Proposed by {entry.rule.created_by}
                </p>
              )}
              <div className="mt-3 flex justify-end gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  className="text-amber-500"
                  onClick={() => void act(entry, "revoke")}
                >
                  <Undo2 className="size-3.5" />
                  Reject
                </Button>
                <Button size="sm" onClick={() => void act(entry, "approve")}>
                  <Check className="size-3.5" />
                  Approve
                </Button>
              </div>
            </div>
          </Card>
        ))}
    </div>
  );
}
