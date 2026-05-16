import { CircleDot, CircleCheck } from "lucide-react";
import type { Session } from "../types";
import { formatDate } from "../lib/utils";
import { Card } from "./ui/Card";
import { Badge } from "./ui/Badge";

export interface SessionsTabProps {
  sessions: Session[];
  loading: boolean;
  error: string | null;
}

export function SessionsTab({ sessions, loading, error }: SessionsTabProps) {
  if (error) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
        {error}
      </div>
    );
  }

  if (loading) {
    return (
      <div className="space-y-2">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-24 animate-pulse rounded-lg bg-muted" />
        ))}
      </div>
    );
  }

  if (sessions.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-border py-16 text-center">
        <p className="text-sm text-muted-foreground">No sessions recorded.</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {sessions.map((s) => {
        const open = !s.ended_at;
        return (
          <Card key={s.id}>
            <div className="p-4">
              <div className="flex flex-wrap items-center gap-2">
                {open ? (
                  <Badge variant="success">
                    <CircleDot className="mr-1 size-3" />
                    open
                  </Badge>
                ) : (
                  <Badge variant="secondary">
                    <CircleCheck className="mr-1 size-3" />
                    ended
                  </Badge>
                )}
                <span className="font-mono text-xs text-muted-foreground">
                  {s.id}
                </span>
              </div>
              <p className="mt-2 whitespace-pre-wrap text-sm">
                {s.summary || (
                  <span className="text-muted-foreground">
                    No summary recorded.
                  </span>
                )}
              </p>
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>Started: {formatDate(s.started_at)}</span>
                <span>
                  Ended: {s.ended_at ? formatDate(s.ended_at) : "—"}
                </span>
                <span>{s.memories_created} created</span>
                <span>{s.memories_accessed} accessed</span>
              </div>
            </div>
          </Card>
        );
      })}
    </div>
  );
}
