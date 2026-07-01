import { Check, Layers, Plus, ShieldCheck, ShieldX, Undo2 } from "lucide-react";
import type { Memory } from "../types";
import { formatRelative } from "../lib/utils";
import { Card } from "./ui/Card";
import { Button } from "./ui/Button";
import { Badge } from "./ui/Badge";

export interface RulesTabProps {
  mandatory: Memory[];
  forbidden: Memory[];
  loading: boolean;
  error: string | null;
  onAdd: (category: "mandatory_rules" | "forbidden_rules") => void;
  onEdit: (memory: Memory) => void;
  onDelete: (memory: Memory) => void;
  onBulkAdd: () => void;
  // Governance (server mode). All optional; when serverMode is falsy the
  // component renders exactly as before - no badges, no approve/revoke.
  serverMode?: boolean;
  isAdmin?: boolean;
  onApprove?: (memory: Memory) => void;
  onRevoke?: (memory: Memory) => void;
}

function ApprovalBadge({ status }: { status?: string }) {
  if (status === "proposed") {
    return (
      <Badge className="border-amber-500/30 bg-amber-500/15 text-amber-500">
        Pending
      </Badge>
    );
  }
  if (status === "revoked") {
    return <Badge variant="destructive">Revoked</Badge>;
  }
  return <Badge variant="success">Approved</Badge>;
}

function RuleColumn({
  title,
  tone,
  icon,
  rules,
  loading,
  onAdd,
  onEdit,
  onDelete,
  serverMode,
  isAdmin,
  onApprove,
  onRevoke,
}: {
  title: string;
  tone: "mandatory" | "forbidden";
  icon: React.ReactNode;
  rules: Memory[];
  loading: boolean;
  onAdd: () => void;
  onEdit: (memory: Memory) => void;
  onDelete: (memory: Memory) => void;
  serverMode?: boolean;
  isAdmin?: boolean;
  onApprove?: (memory: Memory) => void;
  onRevoke?: (memory: Memory) => void;
}) {
  const accent =
    tone === "mandatory"
      ? "border-emerald-500/30"
      : "border-red-500/30";
  const headTint =
    tone === "mandatory" ? "text-emerald-500" : "text-red-500";

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h3 className={`flex items-center gap-2 text-sm font-semibold ${headTint}`}>
          {icon}
          {title}
          <span className="text-muted-foreground">({rules.length})</span>
        </h3>
        <Button size="sm" variant="outline" onClick={onAdd}>
          <Plus />
          Add
        </Button>
      </div>

      {loading && (
        <div className="space-y-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-20 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      )}

      {!loading && rules.length === 0 && (
        <div
          className={`rounded-lg border border-dashed ${accent} py-10 text-center`}
        >
          <p className="text-sm text-muted-foreground">No rules yet.</p>
        </div>
      )}

      {!loading &&
        rules.map((rule) => {
          const status = rule.approval_status;
          return (
            <Card key={rule.id} className={`border ${accent}`}>
              <div className="p-4">
                <div className="flex items-start justify-between gap-2">
                  <p className="text-sm font-semibold">{rule.title}</p>
                  {serverMode && <ApprovalBadge status={status} />}
                </div>
                <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                  {rule.content}
                </p>
                {serverMode && (rule.created_by || rule.approved_by) && (
                  <p className="mt-1.5 text-xs text-muted-foreground">
                    {rule.created_by && <>Proposed by {rule.created_by}</>}
                    {rule.approved_by && (
                      <> · Approved by {rule.approved_by}</>
                    )}
                  </p>
                )}
                <div className="mt-2 flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">
                    {formatRelative(rule.updated_at || rule.created_at)}
                  </span>
                  <div className="flex gap-1.5">
                    {serverMode && isAdmin && status !== "approved" && onApprove && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-emerald-500"
                        onClick={() => onApprove(rule)}
                      >
                        <Check className="size-3.5" />
                        Approve
                      </Button>
                    )}
                    {serverMode && isAdmin && status !== "revoked" && onRevoke && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-amber-500"
                        onClick={() => onRevoke(rule)}
                      >
                        <Undo2 className="size-3.5" />
                        Revoke
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => onEdit(rule)}
                    >
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => onDelete(rule)}
                    >
                      Delete
                    </Button>
                  </div>
                </div>
              </div>
            </Card>
          );
        })}
    </div>
  );
}

export function RulesTab({
  mandatory,
  forbidden,
  loading,
  error,
  onAdd,
  onEdit,
  onDelete,
  onBulkAdd,
  serverMode,
  isAdmin,
  onApprove,
  onRevoke,
}: RulesTabProps) {
  if (error) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
        {error}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button size="sm" variant="outline" onClick={onBulkAdd}>
          <Layers />
          Add to multiple projects…
        </Button>
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <RuleColumn
          title="Mandatory Rules"
          tone="mandatory"
          icon={<ShieldCheck className="size-4" />}
          rules={mandatory}
          loading={loading}
          onAdd={() => onAdd("mandatory_rules")}
          onEdit={onEdit}
          onDelete={onDelete}
          serverMode={serverMode}
          isAdmin={isAdmin}
          onApprove={onApprove}
          onRevoke={onRevoke}
        />
        <RuleColumn
          title="Forbidden Rules"
          tone="forbidden"
          icon={<ShieldX className="size-4" />}
          rules={forbidden}
          loading={loading}
          onAdd={() => onAdd("forbidden_rules")}
          onEdit={onEdit}
          onDelete={onDelete}
          serverMode={serverMode}
          isAdmin={isAdmin}
          onApprove={onApprove}
          onRevoke={onRevoke}
        />
      </div>
    </div>
  );
}
