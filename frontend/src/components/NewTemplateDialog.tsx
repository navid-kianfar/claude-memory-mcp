import { useEffect, useState } from "react";
import type { TemplateInput } from "../types";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";

export interface NewTemplateDialogProps {
  open: boolean;
  onClose: () => void;
  saving: boolean;
  onCreate: (input: TemplateInput) => void;
}

export function NewTemplateDialog({
  open,
  onClose,
  saving,
  onCreate,
}: NewTemplateDialogProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
    }
  }, [open]);

  const canSave = name.trim().length > 0;

  function handleCreate() {
    if (!canSave) return;
    onCreate({
      name: name.trim(),
      description: description.trim() || undefined,
    });
  }

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogHeader
        title="New template"
        description="Create a reusable set of rules and memories."
        onClose={onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="tpl-name">Name</Label>
          <Input
            id="tpl-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Baseline rules"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="tpl-desc">Description</Label>
          <Textarea
            id="tpl-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional description"
            className="min-h-[80px]"
          />
        </div>
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={saving}>
          Cancel
        </Button>
        <Button onClick={handleCreate} disabled={!canSave || saving}>
          {saving ? "Creating…" : "Create template"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
