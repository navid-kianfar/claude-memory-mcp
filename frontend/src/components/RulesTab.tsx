import { Layers, Plus, ShieldCheck, ShieldX } from "lucide-react";
import type { Memory } from "../types";
import { formatRelative } from "../lib/utils";
import { Card } from "./ui/Card";
import { Button } from "./ui/Button";

export interface RulesTabProps {
  mandatory: Memory[];
  forbidden: Memory[];
  loading: boolean;
  error: string | null;
  onAdd: (category: "mandatory_rules" | "forbidden_rules") => void;
  onEdit: (memory: Memory) => void;
  onDelete: (memory: Memory) => void;
  onBulkAdd: () => void;
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
}: {
  title: string;
  tone: "mandatory" | "forbidden";
  icon: React.ReactNode;
  rules: Memory[];
  loading: boolean;
  onAdd: () => void;
  onEdit: (memory: Memory) => void;
  onDelete: (memory: Memory) => void;
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
        rules.map((rule) => (
          <Card key={rule.id} className={`border ${accent}`}>
            <div className="p-4">
              <p className="text-sm font-semibold">{rule.title}</p>
              <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                {rule.content}
              </p>
              <div className="mt-2 flex items-center justify-between">
                <span className="text-xs text-muted-foreground">
                  {formatRelative(rule.updated_at || rule.created_at)}
                </span>
                <div className="flex gap-1.5">
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
        ))}
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
        />
      </div>
    </div>
  );
}
