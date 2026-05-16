import { useEffect, useState } from "react";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "./ui/Dialog";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Label } from "./ui/Label";
import { Switch } from "./ui/Switch";

export interface ImportDialogProps {
  open: boolean;
  onClose: () => void;
  saving: boolean;
  onImport: (input: { path: string; stub_rewrite: boolean }) => void;
}

export function ImportDialog({
  open,
  onClose,
  saving,
  onImport,
}: ImportDialogProps) {
  const [path, setPath] = useState("");
  const [stubRewrite, setStubRewrite] = useState(false);

  useEffect(() => {
    if (open) {
      setPath("");
      setStubRewrite(false);
    }
  }, [open]);

  const canSave = path.trim().length > 0;

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogHeader
        title="Import CLAUDE.md"
        description="Parse a CLAUDE.md file and store its sections as memories."
        onClose={onClose}
      />
      <DialogBody>
        <div className="space-y-1.5">
          <Label htmlFor="import-path">File path</Label>
          <Input
            id="import-path"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/path/to/CLAUDE.md"
            className="font-mono"
          />
        </div>
        <div className="flex items-start justify-between gap-4 rounded-md border border-border p-3">
          <div className="space-y-0.5">
            <Label htmlFor="import-stub">Rewrite CLAUDE.md as stub</Label>
            <p className="text-xs text-muted-foreground">
              Replace the file with a short pointer after importing.
            </p>
          </div>
          <Switch
            id="import-stub"
            checked={stubRewrite}
            onCheckedChange={setStubRewrite}
          />
        </div>
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={saving}>
          Cancel
        </Button>
        <Button
          onClick={() =>
            onImport({ path: path.trim(), stub_rewrite: stubRewrite })
          }
          disabled={!canSave || saving}
        >
          {saving ? "Importing…" : "Import"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
