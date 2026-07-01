import { useState } from "react";
import { LogOut } from "lucide-react";
import type { Role } from "../../types";
import { api } from "../../lib/api";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";

export interface UserMenuProps {
  username: string;
  role: Role;
  /** Called after logout so the gate returns to the login screen. */
  onLoggedOut: () => void;
}

export function UserMenu({ username, role, onLoggedOut }: UserMenuProps) {
  const [busy, setBusy] = useState(false);

  const logout = async () => {
    setBusy(true);
    try {
      await api.logout();
    } finally {
      onLoggedOut();
    }
  };

  return (
    <div className="flex items-center gap-2">
      <span className="hidden text-sm text-muted-foreground sm:inline">
        {username}
      </span>
      <Badge variant={role === "admin" ? "default" : "secondary"}>{role}</Badge>
      <Button
        variant="ghost"
        size="icon"
        onClick={logout}
        disabled={busy}
        title="Log out"
        aria-label="Log out"
      >
        <LogOut className="size-4" />
      </Button>
    </div>
  );
}
