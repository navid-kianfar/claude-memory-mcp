import { useCallback, useEffect, useMemo, useState } from "react";
import { Check } from "lucide-react";
import type { Memory, Project, Template } from "../types";
import { api } from "../lib/api";
import { cn } from "../lib/utils";
import { Button } from "./ui/Button";
import { Label } from "./ui/Label";
import { Select } from "./ui/Select";
import { Badge, CategoryBadge } from "./ui/Badge";
import { Tabs } from "./ui/Tabs";

export interface ImportRulesPanelProps {
  /** target project the rules will be applied to */
  targetSlug: string;
  /** all projects (the target is filtered out of the source picker) */
  projects: Project[];
  /** called after a successful apply/import so the parent can refresh + close */
  onDone: (summary: string) => void;
}

type Source = "template" | "project";

interface CheckRow {
  id: string;
  category: string;
  title: string;
  content: string;
}

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
 * Reusable picker for seeding a project's rules — from a template or from
 * another project. Each candidate item gets a checkbox; only checked items
 * are applied. Used by the new-project flow and the standalone import dialog.
 */
export function ImportRulesPanel({
  targetSlug,
  projects,
  onDone,
}: ImportRulesPanelProps) {
  const [source, setSource] = useState<Source>("template");

  // template source
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [templateId, setTemplateId] = useState<string>("");

  // project source
  const sourceProjects = useMemo(
    () => projects.filter((p) => p.slug !== targetSlug),
    [projects, targetSlug]
  );
  const [sourceSlug, setSourceSlug] = useState<string>("");
  const [projectRules, setProjectRules] = useState<Memory[]>([]);
  const [rulesLoading, setRulesLoading] = useState(false);

  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // load templates once
  useEffect(() => {
    let cancelled = false;
    setTemplatesLoading(true);
    api
      .listTemplates()
      .then((res) => {
        if (cancelled) return;
        setTemplates(res.templates);
        if (res.templates.length > 0) {
          setTemplateId((cur) => cur || String(res.templates[0].id));
        }
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "Failed to load");
      })
      .finally(() => {
        if (!cancelled) setTemplatesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // load project rules when a source project is picked
  useEffect(() => {
    if (source !== "project" || !sourceSlug) {
      setProjectRules([]);
      return;
    }
    let cancelled = false;
    setRulesLoading(true);
    setError(null);
    api
      .getRules(sourceSlug)
      .then((res) => {
        if (cancelled) return;
        setProjectRules([...res.mandatory_rules, ...res.forbidden_rules]);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "Failed to load");
      })
      .finally(() => {
        if (!cancelled) setRulesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [source, sourceSlug]);

  const activeTemplate = useMemo(
    () => templates.find((t) => String(t.id) === templateId) ?? null,
    [templates, templateId]
  );

  const rows: CheckRow[] = useMemo(() => {
    if (source === "template") {
      return (activeTemplate?.items ?? []).map((it) => ({
        id: `t:${it.id}`,
        category: it.category,
        title: it.title,
        content: it.content,
      }));
    }
    return projectRules.map((m) => ({
      id: `p:${m.id}`,
      category: m.category,
      title: m.title,
      content: m.content,
    }));
  }, [source, activeTemplate, projectRules]);

  // select-all by default whenever the candidate set changes
  useEffect(() => {
    setChecked(new Set(rows.map((r) => r.id)));
  }, [rows]);

  const toggle = useCallback((id: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const allChecked = rows.length > 0 && checked.size === rows.length;
  const toggleAll = useCallback(() => {
    setChecked((prev) =>
      prev.size === rows.length ? new Set() : new Set(rows.map((r) => r.id))
    );
  }, [rows]);

  const loading =
    source === "template" ? templatesLoading : rulesLoading;

  const handleApply = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      if (source === "template") {
        if (!activeTemplate) return;
        const itemIds = activeTemplate.items
          .filter((it) => checked.has(`t:${it.id}`))
          .map((it) => it.id);
        const res = await api.applyTemplate(targetSlug, {
          template_id: activeTemplate.id,
          item_ids: itemIds,
        });
        onDone(`${res.applied} item(s) applied, ${res.memories} memories`);
      } else {
        if (!sourceSlug) return;
        const memoryIds = projectRules
          .filter((m) => checked.has(`p:${m.id}`))
          .map((m) => m.id);
        const res = await api.importRules(targetSlug, {
          source_project: sourceSlug,
          memory_ids: memoryIds,
        });
        onDone(
          `${res.imported} rule(s) imported, ${res.skipped} skipped`
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setBusy(false);
    }
  }, [
    source,
    activeTemplate,
    checked,
    targetSlug,
    sourceSlug,
    projectRules,
    onDone,
  ]);

  const templateOptions = templates.map((t) => ({
    value: String(t.id),
    label: t.name,
  }));
  const projectOptions = sourceProjects.map((p) => ({
    value: p.slug,
    label: p.display_name,
  }));

  const canApply =
    !busy &&
    checked.size > 0 &&
    (source === "template" ? Boolean(activeTemplate) : Boolean(sourceSlug));

  return (
    <div className="space-y-4">
      <Tabs
        tabs={[
          { value: "template", label: "From template" },
          { value: "project", label: "From project" },
        ]}
        value={source}
        onValueChange={(v) => setSource(v as Source)}
      />

      {source === "template" ? (
        <div className="space-y-1.5">
          <Label htmlFor="seed-template">Template</Label>
          {templates.length === 0 && !templatesLoading ? (
            <p className="text-sm text-muted-foreground">
              No templates yet. Create one from the Templates view.
            </p>
          ) : (
            <Select
              id="seed-template"
              options={templateOptions}
              value={templateId}
              onValueChange={setTemplateId}
              placeholder="Select a template…"
            />
          )}
        </div>
      ) : (
        <div className="space-y-1.5">
          <Label htmlFor="seed-project">Source project</Label>
          {sourceProjects.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No other projects to import rules from.
            </p>
          ) : (
            <Select
              id="seed-project"
              options={projectOptions}
              value={sourceSlug}
              onValueChange={setSourceSlug}
              placeholder="Select a project…"
            />
          )}
        </div>
      )}

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading && (
        <div className="space-y-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-12 animate-pulse rounded-md bg-muted" />
          ))}
        </div>
      )}

      {!loading && rows.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
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
            <Badge variant="secondary">{checked.size} selected</Badge>
          </div>
          <div className="max-h-64 space-y-1.5 overflow-y-auto rounded-md border border-border p-2 scrollbar-thin">
            {rows.map((row) => {
              const isChecked = checked.has(row.id);
              return (
                <div
                  key={row.id}
                  onClick={() => toggle(row.id)}
                  className={cn(
                    "flex cursor-pointer gap-2.5 rounded-md border px-3 py-2 transition-colors",
                    isChecked
                      ? "border-primary/40 bg-primary/5"
                      : "border-transparent hover:bg-accent"
                  )}
                >
                  <div className="pt-0.5">
                    <Checkbox
                      checked={isChecked}
                      onChange={() => toggle(row.id)}
                    />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <CategoryBadge category={row.category} />
                      <span className="truncate text-sm font-medium">
                        {row.title}
                      </span>
                    </div>
                    {row.content && (
                      <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                        {row.content}
                      </p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {!loading &&
        rows.length === 0 &&
        ((source === "template" && activeTemplate) ||
          (source === "project" && sourceSlug)) && (
          <div className="rounded-lg border border-dashed border-border py-8 text-center">
            <p className="text-sm text-muted-foreground">
              {source === "template"
                ? "This template has no items."
                : "This project has no rules to import."}
            </p>
          </div>
        )}

      <div className="flex justify-end">
        <Button onClick={() => void handleApply()} disabled={!canApply}>
          {busy ? "Importing…" : `Import selected (${checked.size})`}
        </Button>
      </div>
    </div>
  );
}
