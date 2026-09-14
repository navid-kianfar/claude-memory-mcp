import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, RefreshCw, Send } from "lucide-react";
import { api } from "../lib/api";
import type {
  AsoodeStatus,
  BoardRef,
  LinkProposal,
  Project,
  ProjectLink,
  ProjectLinkUpdate,
} from "../types";
import { describeLinkError } from "../lib/links";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Badge } from "./ui/Badge";
import { AttachBoardRow } from "./AttachBoardRow";
import type { AttachBoardInput } from "./AttachBoardRow";
import { LinkProposalsCard, proposalKey } from "./LinkProposalsCard";
import { ProjectLinkRow } from "./ProjectLinkRow";

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
  // Bindings a committed manifest suggests, per project. Absent from an older
  // daemon's answer, in which case there is simply no Proposals card.
  const [proposals, setProposals] = useState<Record<string, LinkProposal[]>>({});

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
      setProposals((prev) => ({ ...prev, [slug]: r.proposals ?? [] }));
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

  /**
   * One link changed. The answer carries the server's NORMALISED link, which is
   * handed back so the row can replace its draft with what was actually stored;
   * the list is reloaded as well because making one board the default unmakes
   * another, and that is the server's decision to report, not ours to guess.
   *
   * Rejects with a presentable Error - the row shows it next to the field that
   * caused it, which a banner at the top of a ten-link list cannot do.
   */
  const patchLink = async (
    slug: string,
    linkId: number,
    fields: ProjectLinkUpdate
  ): Promise<ProjectLink> => {
    setBusy(`link:${linkId}`);
    try {
      const res = await api.updateProjectLink(slug, linkId, fields);
      await loadLinks(slug);
      return res.link;
    } catch (err) {
      throw new Error(describeLinkError(err, "Could not change this link"));
    } finally {
      setBusy(null);
    }
  };

  const deleteLink = async (slug: string, linkId: number): Promise<void> => {
    setBusy(`link:${linkId}`);
    setNotice(null);
    try {
      await api.deleteProjectLink(slug, linkId);
      await loadLinks(slug);
      setNotice("Board unlinked. Tasks already mirrored to it were left alone.");
    } catch (err) {
      throw new Error(describeLinkError(err, "Could not unlink this board"));
    } finally {
      setBusy(null);
    }
  };

  /**
   * Attach a board. Throws on failure INSTEAD of raising the banner at the top
   * of the page: the row keeps the board and the paths that were typed, and
   * says what went wrong where the eye already is. A cleared form after a
   * failed attach is how a ten-project list loses a careful bit of typing.
   */
  const attachBoard = async (slug: string, input: AttachBoardInput) => {
    const name = projects.find((p) => p.slug === slug)?.display_name ?? slug;
    setBusy(`attach:${slug}`);
    setNotice(null);
    try {
      await api.attachBoard(slug, input);
      await loadLinks(slug);
      setNotice(`${name} linked. Existing tasks are not sent until you mirror.`);
    } catch (err) {
      throw new Error(describeLinkError(err, "Could not attach this board"));
    } finally {
      setBusy(null);
    }
  };

  /**
   * Apply ONE proposal. `unlinked` attaches the board with the proposed paths;
   * `differs` only moves the paths of the link that is already there. There is
   * no bulk apply on purpose - see LinkProposalsCard.
   */
  const applyProposal = async (slug: string, proposal: LinkProposal) => {
    const key = `prop:${slug}:${proposalKey(proposal)}`;
    setBusy(key);
    setNotice(null);
    try {
      if (proposal.status === "unlinked") {
        await api.attachBoard(slug, {
          work_package_id: proposal.remote_work_package_id,
          label: proposal.label ?? undefined,
          is_default: proposal.is_default,
          match_paths: proposal.match_paths,
        });
      } else if (proposal.link_id !== null) {
        await api.updateProjectLink(slug, proposal.link_id, {
          match_paths: proposal.match_paths.length ? proposal.match_paths : null,
        });
      } else {
        throw new Error(
          "This proposal says a link already exists but carries no link_id, so there is nothing to update."
        );
      }
      await loadLinks(slug);
      setNotice("Proposal applied.");
    } catch (err) {
      throw new Error(describeLinkError(err, "Could not apply this proposal"));
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
  const proposed = useMemo(
    () => projects.filter((p) => (proposals[p.slug] || []).length > 0),
    [projects, proposals]
  );

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
          <Button
            variant="ghost"
            size="sm"
            aria-label="Reload boards and links"
            onClick={() => {
              // The links are what this section is about, so refresh has to
              // reload them too - it used to fetch only the status and the
              // board list, which left the rows below it stale.
              void load();
              projects.forEach((p) => void loadLinks(p.slug));
            }}
          >
            <RefreshCw className="size-3.5" />
          </Button>
        </div>
        <p className="mb-3 text-xs text-muted-foreground">
          A project links to MANY boards — one per app in a monorepo. Give a board the
          repo subpaths it owns and a task about a file under one of them goes there;
          the board marked ★ is where everything else goes.
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
              <div className="space-y-1.5">
                {(links[p.slug] || []).map((l) => (
                  <ProjectLinkRow
                    key={l.id}
                    link={l}
                    busy={busy === `link:${l.id}`}
                    onPatch={(fields) => patchLink(p.slug, l.id, fields)}
                    onDelete={() => deleteLink(p.slug, l.id)}
                  />
                ))}
              </div>
              {status?.pat_configured && (
                <div className="mt-2.5 border-t border-border/60 pt-2.5">
                  <h4 className="mb-1.5 text-xs font-semibold text-muted-foreground">
                    Attach another board
                  </h4>
                  <AttachBoardRow
                    boards={boards}
                    alreadyLinked={(links[p.slug] || []).map(
                      (l) => l.remote_work_package_id
                    )}
                    isFirstLink={false}
                    busy={busy === `attach:${p.slug}`}
                    onAttach={(input) => attachBoard(p.slug, input)}
                  />
                </div>
              )}
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
                <AttachBoardRow
                  key={p.slug}
                  boards={boards}
                  leading={
                    <span className="w-40 shrink-0 truncate text-sm">{p.display_name}</span>
                  }
                  isFirstLink
                  busy={busy === `attach:${p.slug}`}
                  onAttach={(input) => attachBoard(p.slug, input)}
                />
              ))}
            </div>
          </div>
        )}
      </section>

      {/* ---- manifest proposals ---- */}
      {proposed.length > 0 && (
        <section className="rounded-lg border border-border bg-card p-4">
          <h2 className="mb-1 text-sm font-semibold">Proposed by the manifest</h2>
          <p className="mb-3 text-xs text-muted-foreground">
            Bindings a committed manifest suggests for this machine. Nothing here is
            applied for you — each row is a board that would start receiving real tasks,
            so linking stays an explicit act, one row at a time.
          </p>
          <div className="space-y-3">
            {proposed.map((p) => (
              <LinkProposalsCard
                key={p.slug}
                projectName={p.display_name}
                proposals={proposals[p.slug] || []}
                busyKey={
                  busy?.startsWith(`prop:${p.slug}:`)
                    ? busy.slice(`prop:${p.slug}:`.length)
                    : null
                }
                onApply={(proposal) => applyProposal(p.slug, proposal)}
              />
            ))}
          </div>
        </section>
      )}

    </div>
  );
}
