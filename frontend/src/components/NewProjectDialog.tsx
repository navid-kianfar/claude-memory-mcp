import { useEffect, useState } from "react";
import type { ProjectInput } from "../types";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";

export interface NewProjectDialogProps {
  open: boolean;
  onClose: () => void;
  saving: boolean;
  onCreate: (input: ProjectInput) => void;
}

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function NewProjectDialog({
  open,
  onClose,
  saving,
  onCreate,
}: NewProjectDialogProps) {
  const [displayName, setDisplayName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");

  useEffect(() => {
    if (open) {
      setDisplayName("");
      setSlug("");
      setSlugTouched(false);
      setDescription("");
    }
  }, [open]);

  const canSave = slug.trim().length > 0 && displayName.trim().length > 0;

  function handleName(value: string) {
    setDisplayName(value);
    if (!slugTouched) setSlug(slugify(value));
  }

  function handleCreate() {
    if (!canSave) return;
    onCreate({
      slug: slug.trim(),
      display_name: displayName.trim(),
      description: description.trim() || undefined,
    });
  }

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogHeader
        title="New project"
        description="Create a new memory database project."
        onClose={onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="proj-name">Display name</Label>
          <Input
            id="proj-name"
            value={displayName}
            onChange={(e) => handleName(e.target.value)}
            placeholder="My Project"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="proj-slug">Slug</Label>
          <Input
            id="proj-slug"
            value={slug}
            onChange={(e) => {
              setSlugTouched(true);
              setSlug(slugify(e.target.value));
            }}
            placeholder="my-project"
            className="font-mono"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="proj-desc">Description</Label>
          <Textarea
            id="proj-desc"
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
          {saving ? "Creating…" : "Create project"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
