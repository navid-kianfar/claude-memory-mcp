import { useEffect, useState } from "react";
import { CheckCircle2 } from "lucide-react";
import type { Project, ProjectInput } from "../types";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";
import { ImportRulesPanel } from "./ImportRulesPanel";

export interface NewProjectDialogProps {
  open: boolean;
  onClose: () => void;
  saving: boolean;
  /** all projects, used as import sources in the seeding step */
  projects: Project[];
  /**
   * Creates the project and resolves to its slug, or null on failure.
   * The dialog advances to the seeding step only on a non-null slug.
   */
  onCreate: (input: ProjectInput) => Promise<string | null>;
  /** surfaces a toast for seeding results */
  onSeeded: (summary: string) => void;
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
  projects,
  onCreate,
  onSeeded,
}: NewProjectDialogProps) {
  const [step, setStep] = useState<"details" | "seed">("details");
  const [displayName, setDisplayName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [createdSlug, setCreatedSlug] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setStep("details");
      setDisplayName("");
      setSlug("");
      setSlugTouched(false);
      setDescription("");
      setCreatedSlug(null);
    }
  }, [open]);

  const canSave = slug.trim().length > 0 && displayName.trim().length > 0;

  function handleName(value: string) {
    setDisplayName(value);
    if (!slugTouched) setSlug(slugify(value));
  }

  async function handleCreate() {
    if (!canSave) return;
    const resultSlug = await onCreate({
      slug: slug.trim(),
      display_name: displayName.trim(),
      description: description.trim() || undefined,
    });
    if (resultSlug) {
      setCreatedSlug(resultSlug);
      setStep("seed");
    }
  }

  if (step === "seed" && createdSlug) {
    return (
      <Dialog open={open} onClose={onClose} className="max-w-xl">
        <DialogHeader
          title="Seed project rules"
          description="Optionally import baseline rules. You can skip this and start with a bare project."
          onClose={onClose}
        />
        <DialogBody>
          <div className="flex items-center gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-500">
            <CheckCircle2 className="size-4 shrink-0" />
            <span>
              Project “{displayName}” created. Import rules below or skip.
            </span>
          </div>
          <ImportRulesPanel
            targetSlug={createdSlug}
            projects={projects}
            onDone={(summary) => {
              onSeeded(summary);
              onClose();
            }}
          />
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Skip — finish
          </Button>
        </DialogFooter>
      </Dialog>
    );
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
        <Button
          onClick={() => void handleCreate()}
          disabled={!canSave || saving}
        >
          {saving ? "Creating…" : "Create project"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
