import { useEffect, useState } from "react";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Label } from "./ui/Label";

export interface LinkFolderDialogProps {
  open: boolean;
  onClose: () => void;
  /** display name of the project being linked */
  projectName: string;
  /** links the folder; rejects on failure */
  onLink: (path: string) => Promise<void>;
}

export function LinkFolderDialog({
  open,
  onClose,
  projectName,
  onLink,
}: LinkFolderDialogProps) {
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setPath("");
      setBusy(false);
      setError(null);
    }
  }, [open]);

  const canLink = path.trim().length > 0 && !busy;

  async function handleLink() {
    if (!canLink) return;
    setBusy(true);
    setError(null);
    try {
      await onLink(path.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to link folder");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} className="max-w-md">
      <DialogHeader
        title="Link folder"
        description={`Bind “${projectName}” to a source folder so its rules sync via git.`}
        onClose={onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="link-folder-path">Folder path</Label>
          <Input
            id="link-folder-path"
            value={path}
            onChange={(e) => {
              setPath(e.target.value);
              if (error) setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void handleLink();
              }
            }}
            placeholder="/absolute/path/to/folder"
            className="font-mono"
          />
          <p className="text-xs text-muted-foreground">
            A <code>.claude-memory/</code> snapshot is written into this
            folder for git syncing.
          </p>
        </div>
        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button onClick={() => void handleLink()} disabled={!canLink}>
          {busy ? "Linking…" : "Link folder"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
