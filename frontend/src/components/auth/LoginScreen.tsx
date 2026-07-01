import { useState } from "react";
import { KeyRound, ShieldCheck } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Label } from "../ui/Label";

export interface LoginScreenProps {
  /** Called after a successful login so the gate can re-check identity. */
  onLoggedIn: () => void;
}

export function LoginScreen({ onLoggedIn }: LoginScreenProps) {
  const [username, setUsername] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!username.trim() || !token.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.login({ username: username.trim(), token: token.trim() });
      onLoggedIn();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Login failed. Check your token."
      );
      setBusy(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") submit();
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <div className="w-full max-w-sm rounded-lg border border-border bg-card p-6 text-card-foreground shadow-xl">
        <div className="mb-5 flex items-center gap-2">
          <ShieldCheck className="size-5 text-primary" />
          <h1 className="text-lg font-semibold">Memory MCP</h1>
        </div>
        <p className="mb-5 text-sm text-muted-foreground">
          Sign in with your username and API token to continue.
        </p>

        {error && (
          <div className="mb-4 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}

        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="login-username">Username</Label>
            <Input
              id="login-username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              onKeyDown={onKeyDown}
              autoFocus
              placeholder="your-username"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="login-token">API token</Label>
            <Input
              id="login-token"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="mmcp_…"
            />
          </div>
        </div>

        <Button
          className="mt-5 w-full"
          onClick={submit}
          disabled={busy || !username.trim() || !token.trim()}
        >
          <KeyRound className="size-4" />
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </div>
    </div>
  );
}
