import { useCallback, useEffect, useState } from "react";
import { Copy, KeyRound, Plus, UserX, Users as UsersIcon } from "lucide-react";
import type { User } from "../../types";
import { formatRelative } from "../../lib/utils";
import { api } from "../../lib/api";
import { useToast } from "../ui/Toast";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Badge } from "../ui/Badge";
import { ConfirmDialog } from "../ConfirmDialog";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "../ui/Dialog";
import { NewUserDialog } from "./NewUserDialog";

export function UsersView() {
  const { toast } = useToast();
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [deactivateTarget, setDeactivateTarget] = useState<User | null>(null);
  const [busy, setBusy] = useState(false);
  // A one-time token to reveal after create/rotate (never retrievable again).
  const [revealed, setRevealed] = useState<{ username: string; token: string } | null>(
    null
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listUsers();
      setUsers(res.users);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load users");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const rotate = async (user: User) => {
    try {
      const res = await api.rotateUserToken(user.id);
      setRevealed({ username: user.username, token: res.token });
      void load();
    } catch (err) {
      toast({
        title: "Failed to rotate token",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  const confirmDeactivate = async () => {
    if (!deactivateTarget) return;
    setBusy(true);
    try {
      await api.deactivateUser(deactivateTarget.id);
      toast({ title: "User deactivated", variant: "success" });
      setDeactivateTarget(null);
      void load();
    } catch (err) {
      toast({
        title: "Failed to deactivate user",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const copyToken = () => {
    if (revealed) void navigator.clipboard?.writeText(revealed.token);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold">
            <UsersIcon className="size-5 text-primary" />
            Users
          </h1>
          <p className="text-sm text-muted-foreground">
            Accounts that can access this server and their API tokens.
          </p>
        </div>
        <Button size="sm" onClick={() => setNewOpen(true)}>
          <Plus />
          New user
        </Button>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading && (
        <div className="space-y-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      )}

      {!loading &&
        users.map((u) => (
          <Card key={u.id} className="border-border">
            <div className="flex items-center justify-between gap-3 p-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-semibold">
                    {u.username}
                  </span>
                  <Badge variant={u.role === "admin" ? "default" : "secondary"}>
                    {u.role}
                  </Badge>
                  {!u.active && <Badge variant="destructive">inactive</Badge>}
                </div>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {u.last_login
                    ? `Last login ${formatRelative(u.last_login)}`
                    : "Never signed in"}
                </p>
              </div>
              <div className="flex shrink-0 gap-1.5">
                <Button size="sm" variant="ghost" onClick={() => void rotate(u)}>
                  <KeyRound className="size-3.5" />
                  Rotate
                </Button>
                {u.active && (
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive"
                    onClick={() => setDeactivateTarget(u)}
                  >
                    <UserX className="size-3.5" />
                    Deactivate
                  </Button>
                )}
              </div>
            </div>
          </Card>
        ))}

      <NewUserDialog
        open={newOpen}
        onClose={() => setNewOpen(false)}
        onCreated={(result) => {
          setNewOpen(false);
          setRevealed({ username: result.user.username, token: result.token });
          void load();
        }}
      />

      <ConfirmDialog
        open={deactivateTarget !== null}
        title="Deactivate user?"
        description={`${deactivateTarget?.username} will no longer be able to sign in or use their token.`}
        confirmLabel="Deactivate"
        destructive
        busy={busy}
        onConfirm={confirmDeactivate}
        onClose={() => setDeactivateTarget(null)}
      />

      <Dialog
        open={revealed !== null}
        onClose={() => setRevealed(null)}
        className="max-w-md"
      >
        <DialogHeader
          title="API token"
          onClose={() => setRevealed(null)}
        />
        <DialogBody>
          <p className="text-sm text-muted-foreground">
            Token for <span className="font-medium">{revealed?.username}</span>.
            Copy it now — it is shown only once.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <code className="flex-1 truncate rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs">
              {revealed?.token}
            </code>
            <Button size="icon" variant="outline" onClick={copyToken} title="Copy">
              <Copy className="size-4" />
            </Button>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button onClick={() => setRevealed(null)}>Done</Button>
        </DialogFooter>
      </Dialog>
    </div>
  );
}
