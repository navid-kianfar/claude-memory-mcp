import { useCallback, useEffect, useMemo, useState } from "react";
import { Check } from "lucide-react";
import type { BulkAddRuleResult, Project } from "../types";
import { api } from "../lib/api";
import { cn } from "../lib/utils";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";
import { Select } from "./ui/Select";
import { Badge } from "./ui/Badge";

export interface BulkAddRuleDialogProps {
  open: boolean;
  onClose: () => void;
  projects: Project[];
  /** called after a successful bulk add so the parent can refresh */
  onDone: (result: BulkAddRuleResult) => void;
}

type RuleType = "mandatory" | "forbidden";

const RULE_TYPE_OPTIONS = [
  { value: "mandatory", label: "Mandatory" },
  { value: "forbidden", label: "Forbidden" },
];

const PRIORITY_OPTIONS = [
  { value: "1", label: "Low" },
  { value: "2", label: "Normal" },
  { value: "3", label: "High" },
];

function Checkbox({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      onClick={(e) => {
        e.stopPropagation();
        onChange();
      }}
      className={cn(
        "flex size-4 shrink-0 items-center justify-center rounded border transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background",
        checked
          ? "border-primary bg-primary text-primary-foreground"
          : "border-input bg-transparent"
      )}
    >
      {checked && <Check className="size-3" />}
    </button>
  );
}

/**
 * Applies a single rule to many projects at once. Shows a project picker
 * with select-all, reusing the checkbox style from ImportRulesPanel.
 */
export function BulkAddRuleDialog({
  open,
  onClose,
  projects,
  onDone,
}: BulkAddRuleDialogProps) {
  const [ruleType, setRuleType] = useState<RuleType>("mandatory");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [priority, setPriority] = useState("2");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // reset + select all on open
  useEffect(() => {
    if (open) {
      setRuleType("mandatory");
      setTitle("");
      setContent("");
      setPriority("2");
      setSelected(new Set(projects.map((p) => p.slug)));
      setError(null);
      setBusy(false);
    }
  }, [open, projects]);

  const toggle = useCallback((slug: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  }, []);

  const allChecked =
    projects.length > 0 && selected.size === projects.length;
  const toggleAll = useCallback(() => {
    setSelected((prev) =>
      prev.size === projects.length
        ? new Set()
        : new Set(projects.map((p) => p.slug))
    );
  }, [projects]);

  const canSave =
    !busy &&
    title.trim().length > 0 &&
    content.trim().length > 0 &&
    selected.size > 0;

  const selectAll = useMemo(
    () => selected.size === projects.length,
    [selected, projects]
  );

  const handleSave = useCallback(async () => {
    if (!canSave) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.bulkAddRule({
        rule_type: ruleType,
        title: title.trim(),
        content: content.trim(),
        priority: Number(priority),
        // omit projects when every project is selected → server applies to all
        projects: selectAll ? undefined : Array.from(selected),
      });
      onDone(res);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to add rule"
      );
    } finally {
      setBusy(false);
    }
  }, [
    canSave,
    ruleType,
    title,
    content,
    priority,
    selectAll,
    selected,
    onDone,
  ]);

  return (
    <Dialog open={open} onClose={onClose} className="max-w-xl">
      <DialogHeader
        title="Add rule to multiple projects"
        description="Apply one rule across all or selected projects in a single action."
        onClose={onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="bulk-rule-type">Rule type</Label>
          <Select
            id="bulk-rule-type"
            options={RULE_TYPE_OPTIONS}
            value={ruleType}
            onValueChange={(v) => setRuleType(v as RuleType)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="bulk-rule-title">Title</Label>
          <Input
            id="bulk-rule-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Short rule title"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="bulk-rule-content">Content</Label>
          <Textarea
            id="bulk-rule-content"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="The rule to apply…"
            className="min-h-[90px]"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="bulk-rule-priority">Priority</Label>
          <Select
            id="bulk-rule-priority"
            options={PRIORITY_OPTIONS}
            value={priority}
            onValueChange={setPriority}
            className="w-40"
          />
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label>Projects</Label>
            <Badge variant="secondary">{selected.size} selected</Badge>
          </div>
          {projects.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No projects available.
            </p>
          ) : (
            <>
              <div
                role="button"
                tabIndex={0}
                onClick={toggleAll}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggleAll();
                  }
                }}
                className="flex cursor-pointer items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
              >
                <Checkbox checked={allChecked} onChange={toggleAll} />
                {allChecked ? "Deselect all" : "Select all"}
              </div>
              <div className="max-h-56 space-y-1.5 overflow-y-auto rounded-md border border-border p-2 scrollbar-thin">
                {projects.map((p) => {
                  const isChecked = selected.has(p.slug);
                  return (
                    <div
                      key={p.slug}
                      onClick={() => toggle(p.slug)}
                      className={cn(
                        "flex cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 transition-colors",
                        isChecked
                          ? "border-primary/40 bg-primary/5"
                          : "border-transparent hover:bg-accent"
                      )}
                    >
                      <Checkbox
                        checked={isChecked}
                        onChange={() => toggle(p.slug)}
                      />
                      <div className="min-w-0 flex-1">
                        <span className="truncate text-sm font-medium">
                          {p.display_name}
                        </span>
                        <span className="ml-2 truncate font-mono text-xs text-muted-foreground">
                          {p.slug}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button onClick={() => void handleSave()} disabled={!canSave}>
          {busy
            ? "Adding…"
            : `Add rule to ${selected.size} project${
                selected.size === 1 ? "" : "s"
              }`}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
