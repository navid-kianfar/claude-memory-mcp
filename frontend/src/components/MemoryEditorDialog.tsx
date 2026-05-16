import { useEffect, useState } from "react";
import type { Memory } from "../types";
import { titleCase } from "../lib/utils";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";
import { Select } from "./ui/Select";

export interface MemoryEditorValue {
  category: string;
  title: string;
  content: string;
  tags: string[];
  priority: number;
  status: string;
}

export interface MemoryEditorDialogProps {
  open: boolean;
  onClose: () => void;
  /** existing memory when editing, undefined when creating */
  memory?: Memory;
  /** preset category for creation; locks the select when lockCategory is set */
  presetCategory?: string;
  lockCategory?: boolean;
  categories: string[];
  saving: boolean;
  onSave: (value: MemoryEditorValue) => void;
}

const PRIORITY_OPTIONS = [
  { value: "0", label: "0 — Low" },
  { value: "1", label: "1 — Normal" },
  { value: "2", label: "2 — High" },
  { value: "3", label: "3 — Critical" },
];

const STATUS_OPTIONS = [
  { value: "active", label: "Active" },
  { value: "archived", label: "Archived" },
];

export function MemoryEditorDialog({
  open,
  onClose,
  memory,
  presetCategory,
  lockCategory,
  categories,
  saving,
  onSave,
}: MemoryEditorDialogProps) {
  const editing = Boolean(memory);
  const [category, setCategory] = useState("decision");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [priority, setPriority] = useState("1");
  const [status, setStatus] = useState("active");

  useEffect(() => {
    if (!open) return;
    if (memory) {
      setCategory(memory.category);
      setTitle(memory.title);
      setContent(memory.content);
      setTags(memory.tags.join(", "));
      setPriority(String(memory.priority ?? 1));
      setStatus(memory.status || "active");
    } else {
      setCategory(presetCategory ?? categories[0] ?? "decision");
      setTitle("");
      setContent("");
      setTags("");
      setPriority("1");
      setStatus("active");
    }
  }, [open, memory, presetCategory, categories]);

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
      tags: tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      priority: Number(priority),
      status,
    });
  }

  return (
    <Dialog open={open} onClose={onClose} className="max-w-xl">
      <DialogHeader
        title={editing ? "Edit memory" : "New memory"}
        description={
          editing
            ? "Update the stored memory."
            : "Store a new memory in this project."
        }
        onClose={onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="mem-category">Category</Label>
          <Select
            id="mem-category"
            options={categoryOptions}
            value={category}
            onValueChange={setCategory}
            disabled={editing || lockCategory}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="mem-title">Title</Label>
          <Input
            id="mem-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Short, descriptive title"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="mem-content">Content</Label>
          <Textarea
            id="mem-content"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="The memory body…"
            className="min-h-[140px]"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="mem-tags">Tags</Label>
          <Input
            id="mem-tags"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            placeholder="comma, separated, tags"
          />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label htmlFor="mem-priority">Priority</Label>
            <Select
              id="mem-priority"
              options={PRIORITY_OPTIONS}
              value={priority}
              onValueChange={setPriority}
            />
          </div>
          {editing && (
            <div className="space-y-1.5">
              <Label htmlFor="mem-status">Status</Label>
              <Select
                id="mem-status"
                options={STATUS_OPTIONS}
                value={status}
                onValueChange={setStatus}
              />
            </div>
          )}
        </div>
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={saving}>
          Cancel
        </Button>
        <Button onClick={handleSave} disabled={!canSave || saving}>
          {saving ? "Saving…" : editing ? "Save changes" : "Create memory"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
