import { useEffect, useState } from "react";
import type { Template, TemplateUpdate } from "../types";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";

export interface EditTemplateDialogProps {
  open: boolean;
  onClose: () => void;
  saving: boolean;
  template: Template | null;
  /** inline error to surface (e.g. a 400 "name taken"); cleared on edit */
  error: string | null;
  onErrorClear: () => void;
  onSave: (id: number, input: TemplateUpdate) => void;
}

/** Edits a template's name and description. */
export function EditTemplateDialog({
  open,
  onClose,
  saving,
  template,
  error,
  onErrorClear,
  onSave,
}: EditTemplateDialogProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  useEffect(() => {
    if (open && template) {
      setName(template.name);
      setDescription(template.description ?? "");
    }
  }, [open, template]);

  const canSave = name.trim().length > 0;

  function handleSave() {
    if (!canSave || !template) return;
    onSave(template.id, {
      name: name.trim(),
      description: description.trim(),
    });
  }

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogHeader
        title="Edit template"
        description="Update this template's name and description."
        onClose={onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="edit-tpl-name">Name</Label>
          <Input
            id="edit-tpl-name"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (error) onErrorClear();
            }}
            placeholder="Baseline rules"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="edit-tpl-desc">Description</Label>
          <Textarea
            id="edit-tpl-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional description"
            className="min-h-[80px]"
          />
        </div>
        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={saving}>
          Cancel
        </Button>
        <Button onClick={handleSave} disabled={!canSave || saving}>
          {saving ? "Saving…" : "Save changes"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
