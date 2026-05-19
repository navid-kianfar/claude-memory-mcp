import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, LayoutTemplate, Pencil, Plus, RefreshCw } from "lucide-react";
import type {
  Template,
  TemplateItem,
  TemplateItemInput,
  TemplateUpdate,
} from "../types";
import { api } from "../lib/api";
import { formatRelative, priorityLabel } from "../lib/utils";
import { useToast } from "./ui/Toast";
import { Card } from "./ui/Card";
import { Button } from "./ui/Button";
import { Badge, CategoryBadge } from "./ui/Badge";
import { NewTemplateDialog } from "./NewTemplateDialog";
import { EditTemplateDialog } from "./EditTemplateDialog";
import { TemplateItemDialog } from "./TemplateItemDialog";
import { ConfirmDialog } from "./ConfirmDialog";

export interface TemplatesViewProps {
  categories: string[];
  /** opens the New template dialog when set (command-palette trigger) */
  newTemplateNonce: number;
}

interface ItemEditorState {
  open: boolean;
  item?: TemplateItem;
}

export function TemplatesView({
  categories,
  newTemplateNonce,
}: TemplatesViewProps) {
  const { toast } = useToast();

  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const [newOpen, setNewOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [itemEditor, setItemEditor] = useState<ItemEditorState>({
    open: false,
  });
  const [itemSaving, setItemSaving] = useState(false);
  const [deleteTemplate, setDeleteTemplate] = useState<Template | null>(null);
  const [deleteItem, setDeleteItem] = useState<TemplateItem | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [editTpl, setEditTpl] = useState<Template | null>(null);
  const [editTplSaving, setEditTplSaving] = useState(false);
  const [editTplError, setEditTplError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listTemplates();
      setTemplates(res.templates);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // command-palette "New template" trigger
  useEffect(() => {
    if (newTemplateNonce > 0) setNewOpen(true);
  }, [newTemplateNonce]);

  const selected = useMemo(
    () => templates.find((t) => t.id === selectedId) ?? null,
    [templates, selectedId]
  );

  const createTemplate = useCallback(
    async (input: { name: string; description?: string }) => {
      setCreating(true);
      try {
        const res = await api.createTemplate(input);
        toast({ title: "Template created", variant: "success" });
        setNewOpen(false);
        await load();
        setSelectedId(res.template.id);
      } catch (err) {
        toast({
          title: "Failed to create template",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      } finally {
        setCreating(false);
      }
    },
    [load, toast]
  );

  const updateTemplate = useCallback(
    async (id: number, input: TemplateUpdate) => {
      setEditTplSaving(true);
      setEditTplError(null);
      try {
        await api.updateTemplate(id, input);
        toast({ title: "Template updated", variant: "success" });
        setEditTpl(null);
        await load();
      } catch (err) {
        setEditTplError(
          err instanceof Error ? err.message : "Failed to update template"
        );
      } finally {
        setEditTplSaving(false);
      }
    },
    [load, toast]
  );

  const confirmDeleteTemplate = useCallback(async () => {
    if (!deleteTemplate) return;
    setDeleteBusy(true);
    try {
      await api.deleteTemplate(deleteTemplate.id);
      toast({ title: "Template deleted", variant: "success" });
      if (selectedId === deleteTemplate.id) setSelectedId(null);
      setDeleteTemplate(null);
      await load();
    } catch (err) {
      toast({
        title: "Failed to delete template",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setDeleteBusy(false);
    }
  }, [deleteTemplate, selectedId, load, toast]);

  const saveItem = useCallback(
    async (value: TemplateItemInput) => {
      if (!selected) return;
      setItemSaving(true);
      try {
        if (itemEditor.item) {
          await api.updateTemplateItem(
            selected.id,
            itemEditor.item.id,
            value
          );
          toast({ title: "Item updated", variant: "success" });
        } else {
          await api.createTemplateItem(selected.id, value);
          toast({ title: "Item added", variant: "success" });
        }
        setItemEditor({ open: false });
        await load();
      } catch (err) {
        toast({
          title: "Failed to save item",
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      } finally {
        setItemSaving(false);
      }
    },
    [selected, itemEditor.item, load, toast]
  );

  const confirmDeleteItem = useCallback(async () => {
    if (!selected || !deleteItem) return;
    setDeleteBusy(true);
    try {
      await api.deleteTemplateItem(selected.id, deleteItem.id);
      toast({ title: "Item deleted", variant: "success" });
      setDeleteItem(null);
      await load();
    } catch (err) {
      toast({
        title: "Failed to delete item",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setDeleteBusy(false);
    }
  }, [selected, deleteItem, load, toast]);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {selected && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSelectedId(null)}
            >
              <ArrowLeft />
              All templates
            </Button>
          )}
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <LayoutTemplate className="size-4" />
            {selected ? selected.name : "Templates"}
          </h2>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="icon" onClick={() => void load()}>
            <RefreshCw />
          </Button>
          {selected ? (
            <>
              <Button variant="outline" onClick={() => setEditTpl(selected)}>
                <Pencil />
                Rename
              </Button>
              <Button onClick={() => setItemEditor({ open: true })}>
                <Plus />
                Add item
              </Button>
            </>
          ) : (
            <Button onClick={() => setNewOpen(true)}>
              <Plus />
              New template
            </Button>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading && (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-20 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      )}

      {/* ---- template list ---- */}
      {!loading && !error && !selected && (
        <>
          {templates.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border py-16 text-center">
              <p className="text-sm text-muted-foreground">
                No templates yet. Create one to reuse baseline rules across
                projects.
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {templates.map((t) => (
                <Card key={t.id} className="transition-colors hover:bg-accent/40">
                  <div className="flex items-start justify-between gap-3 p-4">
                    <button
                      onClick={() => setSelectedId(t.id)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-semibold">
                          {t.name}
                        </span>
                        <Badge variant="secondary">
                          {t.items.length} item
                          {t.items.length === 1 ? "" : "s"}
                        </Badge>
                      </div>
                      <p className="mt-0.5 truncate text-sm text-muted-foreground">
                        {t.description || "No description"}
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {formatRelative(t.created_at)}
                      </p>
                    </button>
                    <div className="flex shrink-0 gap-1.5">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setSelectedId(t.id)}
                      >
                        Open
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setEditTpl(t)}
                      >
                        Rename
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setDeleteTemplate(t)}
                      >
                        Delete
                      </Button>
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          )}
        </>
      )}

      {/* ---- template detail ---- */}
      {!loading && !error && selected && (
        <div className="space-y-3">
          {selected.description && (
            <p className="text-sm text-muted-foreground">
              {selected.description}
            </p>
          )}
          {selected.items.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border py-12 text-center">
              <p className="text-sm text-muted-foreground">
                No items yet. Add one to build out this template.
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {selected.items.map((item) => (
                <Card key={item.id}>
                  <div className="p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <CategoryBadge category={item.category} />
                          <span className="text-sm font-semibold">
                            {item.title}
                          </span>
                          <Badge variant="outline">
                            {priorityLabel(item.priority)}
                          </Badge>
                        </div>
                        <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                          {item.content}
                        </p>
                      </div>
                      <div className="flex shrink-0 gap-1.5">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            setItemEditor({ open: true, item })
                          }
                        >
                          Edit
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setDeleteItem(item)}
                        >
                          Delete
                        </Button>
                      </div>
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          )}
        </div>
      )}

      <NewTemplateDialog
        open={newOpen}
        onClose={() => setNewOpen(false)}
        saving={creating}
        onCreate={createTemplate}
      />

      <EditTemplateDialog
        open={Boolean(editTpl)}
        onClose={() => {
          setEditTpl(null);
          setEditTplError(null);
        }}
        saving={editTplSaving}
        template={editTpl}
        error={editTplError}
        onErrorClear={() => setEditTplError(null)}
        onSave={updateTemplate}
      />

      <TemplateItemDialog
        open={itemEditor.open}
        onClose={() => setItemEditor({ open: false })}
        item={itemEditor.item}
        categories={categories}
        saving={itemSaving}
        onSave={saveItem}
      />

      <ConfirmDialog
        open={Boolean(deleteTemplate)}
        title="Delete template"
        description={
          deleteTemplate
            ? `“${deleteTemplate.name}” and its ${deleteTemplate.items.length} item(s) will be deleted.`
            : undefined
        }
        confirmLabel="Delete"
        destructive
        busy={deleteBusy}
        onConfirm={confirmDeleteTemplate}
        onClose={() => setDeleteTemplate(null)}
      />

      <ConfirmDialog
        open={Boolean(deleteItem)}
        title="Delete item"
        description={
          deleteItem
            ? `“${deleteItem.title}” will be removed from this template.`
            : undefined
        }
        confirmLabel="Delete"
        destructive
        busy={deleteBusy}
        onConfirm={confirmDeleteItem}
        onClose={() => setDeleteItem(null)}
      />
    </div>
  );
}
