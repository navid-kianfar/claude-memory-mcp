import { useEffect, useState } from "react";
import type { TemplateItem, TemplateItemInput } from "../types";
import { titleCase } from "../lib/utils";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";
import { Select } from "./ui/Select";

export interface TemplateItemDialogProps {
  open: boolean;
  onClose: () => void;
  /** existing item when editing, undefined when creating */
  item?: TemplateItem;
  categories: string[];
  saving: boolean;
  onSave: (value: TemplateItemInput) => void;
}

const PRIORITY_OPTIONS = [
  { value: "0", label: "0 — Low" },
  { value: "1", label: "1 — Normal" },
  { value: "2", label: "2 — High" },
  { value: "3", label: "3 — Critical" },
];

/** rule category preferred as the default for new template items */
const DEFAULT_CATEGORY = "mandatory_rules";

export function TemplateItemDialog({
  open,
  onClose,
  item,
  categories,
  saving,
  onSave,
}: TemplateItemDialogProps) {
  const editing = Boolean(item);
  const [category, setCategory] = useState(DEFAULT_CATEGORY);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [priority, setPriority] = useState("1");

  useEffect(() => {
    if (!open) return;
    if (item) {
      setCategory(item.category);
      setTitle(item.title);
      setContent(item.content);
      setPriority(String(item.priority ?? 1));
    } else {
      const fallback = categories.includes(DEFAULT_CATEGORY)
        ? DEFAULT_CATEGORY
        : categories[0] ?? DEFAULT_CATEGORY;
      setCategory(fallback);
      setTitle("");
      setContent("");
      setPriority("1");
    }
  }, [open, item, categories]);

  const categoryOptions = categories.map((c) => ({
    value: c,
    label: titleCase(c),
  }));

  const canSave = title.trim().length > 0 && content.trim().length > 0;

  function handleSave() {
    if (!canSave) return;
    onSave({
      category,
      title: title.trim(),
      content: content.trim(),
      priority: Number(priority),
    });
  }

  return (
    <Dialog open={open} onClose={onClose} className="max-w-xl">
      <DialogHeader
        title={editing ? "Edit template item" : "New template item"}
        description={
          editing
            ? "Update this reusable item."
            : "Add a reusable item to this template."
        }
        onClose={onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="ti-category">Category</Label>
          <Select
            id="ti-category"
            options={categoryOptions}
            value={category}
            onValueChange={setCategory}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ti-title">Title</Label>
          <Input
            id="ti-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Short, descriptive title"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ti-content">Content</Label>
          <Textarea
            id="ti-content"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="The item body…"
            className="min-h-[140px]"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ti-priority">Priority</Label>
          <Select
            id="ti-priority"
            options={PRIORITY_OPTIONS}
            value={priority}
            onValueChange={setPriority}
          />
        </div>
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={saving}>
          Cancel
        </Button>
        <Button onClick={handleSave} disabled={!canSave || saving}>
          {saving ? "Saving…" : editing ? "Save changes" : "Add item"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
