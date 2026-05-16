import { useEffect, useState } from "react";
import { CheckCircle2, FilePlus2, FolderOpen } from "lucide-react";
import type {
  LoadFromFolderSource,
  Project,
  ProjectInput,
} from "../types";
import { api } from "../lib/api";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { Label } from "./ui/Label";
import { Tabs } from "./ui/Tabs";
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

type Mode = "blank" | "folder";

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function folderSourceNote(
  source: LoadFromFolderSource,
  claudeMdImported: number
): string {
  switch (source) {
    case "existing_memory_db":
      return "Attached the existing memory database from this folder.";
    case "claude_md":
      return `Imported ${claudeMdImported} entries from the folder's CLAUDE.md.`;
    case "new_empty":
      return "Created a new empty project from this folder.";
  }
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
  const [mode, setMode] = useState<Mode>("blank");

  // blank-project form
  const [displayName, setDisplayName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [projectPath, setProjectPath] = useState("");

  // folder-load form
  const [folderPath, setFolderPath] = useState("");
  const [loadingFolder, setLoadingFolder] = useState(false);
  const [folderError, setFolderError] = useState<string | null>(null);

  // result carried into the seed step
  const [createdSlug, setCreatedSlug] = useState<string | null>(null);
  const [createdName, setCreatedName] = useState("");
  const [folderNote, setFolderNote] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setStep("details");
      setMode("blank");
      setDisplayName("");
      setSlug("");
      setSlugTouched(false);
      setDescription("");
      setProjectPath("");
      setFolderPath("");
      setLoadingFolder(false);
      setFolderError(null);
      setCreatedSlug(null);
      setCreatedName("");
      setFolderNote(null);
    }
  }, [open]);

  const canSave = slug.trim().length > 0 && displayName.trim().length > 0;
  const canLoad = folderPath.trim().length > 0 && !loadingFolder;

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
      project_path: projectPath.trim() || undefined,
    });
    if (resultSlug) {
      setCreatedSlug(resultSlug);
      setCreatedName(displayName.trim());
      setFolderNote(null);
      setStep("seed");
    }
  }

  async function handleLoadFolder() {
    if (!canLoad) return;
    setLoadingFolder(true);
    setFolderError(null);
    try {
      const res = await api.loadProjectFromFolder(folderPath.trim());
      setCreatedSlug(res.project.slug);
      setCreatedName(res.project.display_name);
      setFolderNote(folderSourceNote(res.source, res.claude_md_imported));
      setStep("seed");
    } catch (err) {
      setFolderError(
        err instanceof Error ? err.message : "Failed to load folder"
      );
    } finally {
      setLoadingFolder(false);
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
              Project “{createdName}” created. Import rules below or skip.
            </span>
          </div>
          {folderNote && (
            <div className="rounded-md border border-border bg-muted px-3 py-2 text-sm text-muted-foreground">
              {folderNote}
            </div>
          )}
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
        <Tabs
          tabs={[
            {
              value: "blank",
              label: "Blank project",
              icon: <FilePlus2 />,
            },
            {
              value: "folder",
              label: "From folder",
              icon: <FolderOpen />,
            },
          ]}
          value={mode}
          onValueChange={(v) => setMode(v as Mode)}
        />

        {mode === "blank" ? (
          <>
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
            <div className="space-y-1.5">
              <Label htmlFor="proj-path">Project folder</Label>
              <Input
                id="proj-path"
                value={projectPath}
                onChange={(e) => setProjectPath(e.target.value)}
                placeholder="/absolute/path/to/folder"
                className="font-mono"
              />
              <p className="text-xs text-muted-foreground">
                Optional — bind this project to a folder so its rules sync
                via git.
              </p>
            </div>
          </>
        ) : (
          <>
            <div className="space-y-1.5">
              <Label htmlFor="proj-folder">Folder path</Label>
              <Input
                id="proj-folder"
                value={folderPath}
                onChange={(e) => {
                  setFolderPath(e.target.value);
                  if (folderError) setFolderError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void handleLoadFolder();
                  }
                }}
                placeholder="/absolute/path/to/project"
                className="font-mono"
              />
              <p className="text-xs text-muted-foreground">
                The project name is derived from the folder. An existing
                memory database or a CLAUDE.md is imported automatically.
              </p>
            </div>
            {folderError && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                {folderError}
              </div>
            )}
          </>
        )}
      </DialogBody>
      <DialogFooter>
        <Button
          variant="ghost"
          onClick={onClose}
          disabled={saving || loadingFolder}
        >
          Cancel
        </Button>
        {mode === "blank" ? (
          <Button
            onClick={() => void handleCreate()}
            disabled={!canSave || saving}
          >
            {saving ? "Creating…" : "Create project"}
          </Button>
        ) : (
          <Button
            onClick={() => void handleLoadFolder()}
            disabled={!canLoad}
          >
            {loadingFolder ? "Loading…" : "Load"}
          </Button>
        )}
      </DialogFooter>
    </Dialog>
  );
}
