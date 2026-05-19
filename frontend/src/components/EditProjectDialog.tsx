import { useEffect, useState } from "react";
import type { Project, ProjectUpdate } from "../types";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";

export interface EditProjectDialogProps {
  open: boolean;
  onClose: () => void;
  saving: boolean;
  project: Project | null;
  onSave: (slug: string, input: ProjectUpdate) => void;
}

/** Edits a project's display name and description. */
export function EditProjectDialog({
  open,
  onClose,
  saving,
  project,
  onSave,
}: EditProjectDialogProps) {
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");

  useEffect(() => {
    if (open && project) {
      setDisplayName(project.display_name);
      setDescription(project.description ?? "");
    }
  }, [open, project]);

  const canSave = displayName.trim().length > 0;

  function handleSave() {
    if (!canSave || !project) return;
    onSave(project.slug, {
      display_name: displayName.trim(),
      description: description.trim(),
    });
  }

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogHeader
        title="Edit project"
        description="Update this project's name and description."
        onClose={onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="edit-proj-name">Display name</Label>
          <Input
            id="edit-proj-name"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="My Project"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="edit-proj-desc">Description</Label>
          <Textarea
            id="edit-proj-desc"
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
        <Button onClick={handleSave} disabled={!canSave || saving}>
          {saving ? "Saving…" : "Save changes"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
