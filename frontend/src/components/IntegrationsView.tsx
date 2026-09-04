import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Link2, RefreshCw, Send } from "lucide-react";
import { api } from "../lib/api";
import type { AsoodeStatus, BoardRef, Project, ProjectLink } from "../types";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Badge } from "./ui/Badge";

/**
 * asoode integration: where the server is, the machine-wide credential, and
 * which board each project mirrors to.
 *
 * A GLOBAL view rather than a project tab, because the endpoints and the token
 * are per machine - putting them on a project would imply they are per project,
 * which is exactly the confusion the credential design exists to avoid.
 *
 * The token is write-only here: it is sent when set and never rendered, because
 * the API only ever returns a prefix+last4 fingerprint.
 */
export function IntegrationsView({ projects }: { projects: Project[] }) {
  const [status, setStatus] = useState<AsoodeStatus | null>(null);
  const [boards, setBoards] = useState<BoardRef[]>([]);
  const [links, setLinks] = useState<Record<string, ProjectLink[]>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [urls, setUrls] = useState({ api_url: "", app_url: "", socket_url: "" });
  const [attachTo, setAttachTo] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      const s = await api.getAsoodeStatus();
      setStatus(s);
      setUrls({
        api_url: s.endpoints.api_url,
        app_url: s.endpoints.app_url,
        socket_url: s.endpoints.socket_url,
      });
      if (s.pat_configured) {
        const b = await api.listBoards();
        setBoards(b.boards);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const loadLinks = useCallback(async (slug: string) => {
    try {
      const r = await api.getProjectLinks(slug);
      setLinks((prev) => ({ ...prev, [slug]: r.links }));
    } catch {
      /* a project with no links is not an error */
    }
  }, []);

  useEffect(() => {
    projects.forEach((p) => void loadLinks(p.slug));
  }, [projects, loadLinks]);

  const run = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(ok);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const linked = useMemo(
    () => projects.filter((p) => (links[p.slug] || []).length > 0),
    [projects, links]
  );
  const unlinked = useMemo(
    () => projects.filter((p) => (links[p.slug] || []).length === 0),
    [projects, links]
  );

  const boardLabel = (b: BoardRef) =>
    `${b.title}${b.project_title ? ` — ${b.project_title}` : ""}`;

  return (
    <div className="space-y-6 p-6">
      {error && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {notice && (
        <div className="flex items-start gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-500">
          <Check className="mt-0.5 size-4 shrink-0" />
          <span>{notice}</span>
        </div>
      )}

      {/* ---- credential ---- */}
      <section className="rounded-lg border border-border bg-card p-4">
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Access token</h2>
          {status?.pat_configured ? (
            <Badge variant="secondary">
              {status.pat?.prefix}…{status.pat?.last4}
            </Badge>
          ) : (
            <Badge variant="destructive">not set</Badge>
          )}
        </div>
        <p className="mb-3 text-xs text-muted-foreground">
          Stored once for this machine and shared by every project — never per
          project. It is never displayed again after saving; only the fingerprint
          above is readable.
        </p>
        <div className="flex gap-2">
          <Input
            type="password"
            value={token}
            placeholder="asoode_pat_…"
            onChange={(e) => setToken(e.target.value)}
          />
          <Button
            disabled={!token.trim() || busy === "pat"}
            onClick={() =>
              run("pat", async () => {
                await api.setAsoodePat(token.trim());
                setToken("");
              }, "Token stored.")
            }
          >
            Save
          </Button>
          {status?.pat_configured && (
            <Button
              variant="outline"
              disabled={busy === "clear"}
              onClick={() => run("clear", () => api.clearAsoodePat(), "Token cleared.")}
            >
              Clear
            </Button>
          )}
        </div>
      </section>

      {/* ---- endpoints ---- */}
      <section className="rounded-lg border border-border bg-card p-4">
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Endpoints</h2>
          {status?.endpoints.is_default ? (
            <Badge variant="secondary">hosted defaults</Badge>
          ) : (
            <Badge>overridden</Badge>
          )}
        </div>
        <p className="mb-3 text-xs text-muted-foreground">
          Only change these for an on-premise asoode. A value set in the daemon's
          environment wins over one saved here.
        </p>
        <div className="space-y-2">
          {(["api_url", "app_url", "socket_url"] as const).map((field) => (
            <div key={field} className="flex items-center gap-2">
              <label className="w-24 shrink-0 text-xs text-muted-foreground">
                {field.replace("_url", "")}
              </label>
              <Input
                value={urls[field]}
                onChange={(e) => setUrls({ ...urls, [field]: e.target.value })}
              />
              <Badge variant="outline" className="shrink-0 text-[10px]">
                {status?.endpoints.sources[field] ?? "—"}
              </Badge>
            </div>
          ))}
        </div>
        <div className="mt-3 flex gap-2">
          <Button
            disabled={busy === "urls"}
            onClick={() => run("urls", () => api.setAsoodeUrls(urls), "Endpoints saved.")}
          >
            Save
          </Button>
          <Button
            variant="outline"
            disabled={busy === "reset"}
            onClick={() =>
              run("reset", () => api.setAsoodeUrls({ reset: true }), "Back to the hosted defaults.")
            }
          >
            Reset to defaults
          </Button>
        </div>
        {status?.warnings.map((w) => (
          <p key={w} className="mt-2 flex items-start gap-2 text-xs text-amber-500">
            <AlertTriangle className="mt-0.5 size-3 shrink-0" />
            {w}
          </p>
        ))}
      </section>

      {/* ---- links ---- */}
      <section className="rounded-lg border border-border bg-card p-4">
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Linked projects</h2>
          <Button variant="ghost" size="sm" onClick={() => void load()}>
            <RefreshCw className="size-3.5" />
          </Button>
        </div>
        <p className="mb-3 text-xs text-muted-foreground">
          A project links to MANY boards — one per app in a monorepo. The board
          marked default is where a task with no explicit target goes.
        </p>

        {linked.length === 0 && (
          <p className="text-sm text-muted-foreground">No project is linked yet.</p>
        )}
        <div className="space-y-3">
          {linked.map((p) => (
            <div key={p.slug} className="rounded-md border border-border/60 p-3">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-sm font-medium">{p.display_name}</span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy === `push:${p.slug}`}
                  onClick={() =>
                    run(`push:${p.slug}`, () => api.pushProject(p.slug), `Mirrored ${p.display_name}.`)
                  }
                >
                  <Send className="mr-1 size-3" /> Mirror now
                </Button>
              </div>
              <div className="flex flex-wrap gap-1">
                {(links[p.slug] || []).map((l) => (
                  <Badge key={l.id} variant={l.is_default ? "default" : "outline"}>
                    {l.label || l.remote_work_package_id.slice(0, 8)}
                    {l.is_default && " ★"}
                  </Badge>
                ))}
              </div>
            </div>
          ))}
        </div>

        {status?.pat_configured && unlinked.length > 0 && (
          <div className="mt-4 border-t border-border/60 pt-3">
            <h3 className="mb-2 text-xs font-semibold text-muted-foreground">
              Attach a board
            </h3>
            <div className="space-y-2">
              {unlinked.slice(0, 8).map((p) => (
                <div key={p.slug} className="flex items-center gap-2">
                  <span className="w-40 shrink-0 truncate text-sm">{p.display_name}</span>
                  <select
                    className="h-9 flex-1 rounded-md border border-input bg-background px-2 text-sm"
                    value={attachTo[p.slug] || ""}
                    onChange={(e) => setAttachTo({ ...attachTo, [p.slug]: e.target.value })}
                  >
                    <option value="">choose a board…</option>
                    {boards.map((b) => (
                      <option key={b.id} value={b.id}>
                        {boardLabel(b)}
                      </option>
                    ))}
                  </select>
                  <Button
                    size="sm"
                    disabled={!attachTo[p.slug] || busy === `link:${p.slug}`}
                    onClick={() =>
                      run(
                        `link:${p.slug}`,
                        async () => {
                          const board = boards.find((b) => b.id === attachTo[p.slug]);
                          await api.attachBoard(p.slug, {
                            work_package_id: attachTo[p.slug],
                            label: board?.external_ref || board?.title,
                          });
                          await loadLinks(p.slug);
                        },
                        `${p.display_name} linked. Existing tasks are not sent until you mirror.`
                      )
                    }
                  >
                    <Link2 className="mr-1 size-3" /> Link
                  </Button>
                </div>
              ))}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
