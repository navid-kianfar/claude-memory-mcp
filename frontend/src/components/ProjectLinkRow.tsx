import { useEffect, useState } from "react";
import { AlertTriangle, FolderTree, Star, Trash2, X } from "lucide-react";
import type { ProjectLink, ProjectLinkUpdate } from "../types";
import { formatMatchPaths, parseMatchPaths, sameMatchPaths } from "../lib/links";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";
import { Textarea } from "./ui/Textarea";
import { Tooltip } from "./ui/Tooltip";
import { ConfirmDialog } from "./ConfirmDialog";

export interface ProjectLinkRowProps {
  link: ProjectLink;
  /** True while a call for THIS link is in flight. */
  busy: boolean;
  /**
   * Applies a change. Resolves with the server's version of the link - whose
   * `match_paths` is the normalised one, so the draft is replaced by what was
   * actually stored rather than by what was typed. Rejects with an Error whose
   * message is already presentable.
   */
  onPatch: (fields: ProjectLinkUpdate) => Promise<ProjectLink>;
  onDelete: () => Promise<void>;
}

/**
 * One linked board, as a ROW rather than a badge: the label, which board is the
 * default, the repo subpaths it owns, and the way to unlink it.
 *
 * It used to be a read-only badge, which is half of why a monorepo ended up
 * sending every task to one board - the binding that decides where a task goes
 * was not visible, let alone editable.
 *
 * The paths draft is LOCAL state and the link itself is the server's; a Save
 * replaces the draft with what came back, so a normalised `apps/api/**` shows
 * as `apps/api` immediately and nobody wonders which of the two was stored.
 */
export function ProjectLinkRow({ link, busy, onPatch, onDelete }: ProjectLinkRowProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => formatMatchPaths(link.match_paths));
  const [error, setError] = useState<string | null>(null);
  const [confirmUnlink, setConfirmUnlink] = useState(false);

  // The link can change underneath the row (a refresh, or another link being
  // made default). A draft being edited is the user's, so it is left alone.
  useEffect(() => {
    if (!editing) setDraft(formatMatchPaths(link.match_paths));
  }, [link.match_paths, editing]);

  const paths = link.match_paths ?? [];
  const name = link.label || link.remote_work_package_id.slice(0, 8);
  const dirty = !sameMatchPaths(parseMatchPaths(draft), paths);

  const save = async () => {
    const next = parseMatchPaths(draft);
    setError(null);
    try {
      // An empty editor means "no subpath binding", which the API spells null.
      const updated = await onPatch({ match_paths: next.length ? next : null });
      setDraft(formatMatchPaths(updated.match_paths));
      setEditing(false);
    } catch (err) {
      // The server owns pattern validation, so its message is the message.
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const makeDefault = async () => {
    setError(null);
    try {
      await onPatch({ is_default: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="rounded-md border border-border/60 bg-background/40 p-2.5">
      <div className="flex items-center gap-2">
        {link.is_default ? (
          <Tooltip content="The default board: where a task with no matching path goes">
            <span className="flex size-7 items-center justify-center text-amber-500">
              <Star className="size-3.5" fill="currentColor" />
            </span>
          </Tooltip>
        ) : (
          <Tooltip content="Make this the default board">
            <Button
              variant="ghost"
              size="icon"
              className="size-7 text-muted-foreground hover:text-amber-500"
              aria-label={`Make ${name} the default board`}
              disabled={busy}
              onClick={() => void makeDefault()}
            >
              <Star className="size-3.5" />
            </Button>
          </Tooltip>
        )}

        <span className="min-w-0 flex-1 truncate text-sm font-medium" title={name}>
          {name}
        </span>

        {paths.length === 0 ? (
          <Badge variant="outline" className="shrink-0 text-[10px]">
            no paths
          </Badge>
        ) : (
          <div className="flex min-w-0 shrink flex-wrap justify-end gap-1">
            {paths.map((path) => (
              <Badge key={path} variant="secondary" className="max-w-[180px] truncate font-mono text-[10px]">
                {path}
              </Badge>
            ))}
          </div>
        )}

        <Button
          variant="ghost"
          size="sm"
          className="shrink-0"
          disabled={busy}
          onClick={() => {
            setError(null);
            setDraft(formatMatchPaths(link.match_paths));
            setEditing((v) => !v);
          }}
        >
          {editing ? <X className="size-3" /> : <FolderTree className="size-3" />}
          {editing ? "Close" : "Paths"}
        </Button>

        <Tooltip content="Unlink this board">
          <Button
            variant="ghost"
            size="icon"
            className="size-7 shrink-0 text-muted-foreground hover:text-destructive"
            aria-label={`Unlink ${name}`}
            disabled={busy}
            onClick={() => setConfirmUnlink(true)}
          >
            <Trash2 className="size-3.5" />
          </Button>
        </Tooltip>
      </div>

      {editing && (
        <div className="mt-2 space-y-2 border-t border-border/60 pt-2">
          <Textarea
            autoFocus
            value={draft}
            spellCheck={false}
            placeholder={"apps/api\nlibs/shared"}
            onChange={(e) => setDraft(e.target.value)}
            className="min-h-[72px] font-mono text-xs"
          />
          <p className="text-xs text-muted-foreground">
            One repo-relative prefix per line — <span className="font-mono">apps/api</span>,{" "}
            <span className="font-mono">frontend</span>. A trailing{" "}
            <span className="font-mono">/**</span> or <span className="font-mono">/*</span> is
            allowed and the server drops it. The longest matching prefix wins; an empty list
            leaves this board reachable only as the default.
          </p>
          {error && (
            <p className="flex items-start gap-2 text-xs text-destructive">
              <AlertTriangle className="mt-0.5 size-3 shrink-0" />
              <span className="break-words">{error}</span>
            </p>
          )}
          <div className="flex gap-2">
            <Button size="sm" disabled={busy || !dirty} onClick={() => void save()}>
              Save paths
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => {
                setDraft(formatMatchPaths(link.match_paths));
                setError(null);
                setEditing(false);
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}

      {!editing && error && (
        <p className="mt-2 flex items-start gap-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 size-3 shrink-0" />
          <span className="break-words">{error}</span>
        </p>
      )}

      <ConfirmDialog
        open={confirmUnlink}
        title={`Unlink ${name}?`}
        description={
          "The board itself is untouched, and tasks already mirrored to it stay where they are — " +
          "they are not moved or deleted. New tasks stop going to it, and any path binding on this " +
          "link is dropped."
        }
        confirmLabel="Unlink"
        destructive
        busy={busy}
        onConfirm={() => {
          setError(null);
          void onDelete()
            .then(() => setConfirmUnlink(false))
            .catch((err: unknown) => {
              setConfirmUnlink(false);
              setError(err instanceof Error ? err.message : String(err));
            });
        }}
        onClose={() => setConfirmUnlink(false)}
      />
    </div>
  );
}
