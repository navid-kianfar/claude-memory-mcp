import { useState } from "react";
import type { CreateUserResult, Role } from "../../types";
import { api } from "../../lib/api";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "../ui/Dialog";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Label } from "../ui/Label";
import { Select } from "../ui/Select";

export interface NewUserDialogProps {
  open: boolean;
  onClose: () => void;
  onCreated: (result: CreateUserResult) => void;
}

export function NewUserDialog({ open, onClose, onCreated }: NewUserDialogProps) {
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setUsername("");
    setDisplayName("");
    setRole("member");
    setError(null);
  };

  const submit = async () => {
    if (!username.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.createUser({
        username: username.trim(),
        role,
        display_name: displayName.trim() || undefined,
      });
      reset();
      onCreated(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create user");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} className="max-w-md">
      <DialogHeader title="New user" onClose={onClose} />
      <DialogBody>
        {error && (
          <div className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="new-user-username">Username</Label>
            <Input
              id="new-user-username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              placeholder="jane"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="new-user-display">Display name (optional)</Label>
            <Input
              id="new-user-display"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Jane Doe"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="new-user-role">Role</Label>
            <Select
              id="new-user-role"
              value={role}
              onValueChange={(v) => setRole(v as Role)}
              options={[
                { value: "member", label: "Member" },
                { value: "admin", label: "Admin" },
              ]}
            />
          </div>
        </div>
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={busy || !username.trim()}>
          {busy ? "Creating…" : "Create user"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
