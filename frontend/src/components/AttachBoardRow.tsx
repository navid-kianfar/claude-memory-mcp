import { useMemo, useState } from "react";
import { AlertTriangle, Link2 } from "lucide-react";
import type { BoardRef } from "../types";
import { parseMatchPathsLine } from "../lib/links";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Select } from "./ui/Select";

export interface AttachBoardInput {
  work_package_id: string;
  label?: string;
  is_default: boolean;
  match_paths?: string[];
}

export interface AttachBoardRowProps {
  boards: BoardRef[];
  /** Rendered before the picker; the project name in the unlinked list. */
  leading?: React.ReactNode;
  /** Work package ids already linked to this project — never offered twice. */
  alreadyLinked?: string[];
  /**
   * No link yet, so this board becomes the default. With a link already in
   * place the new board is attached as NON-default: attaching a board for
   * `frontend/**` must not quietly move where every unmatched task goes.
   */
  isFirstLink: boolean;
  busy: boolean;
  onAttach: (input: AttachBoardInput) => Promise<void>;
}

/**
 * Attach one board to one project, with the repo subpaths it owns.
 *
 * ONE component for both places it appears - the per-project "attach another
 * board" inside a linked card, and the list of projects with no board yet - so
 * the two cannot drift into offering different fields, which is how a monorepo
 * ended up with a single board in the first place.
 */
export function AttachBoardRow({
  boards,
  leading,
  alreadyLinked,
  isFirstLink,
  busy,
  onAttach,
}: AttachBoardRowProps) {
  const [boardId, setBoardId] = useState("");
  const [pathsDraft, setPathsDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const options = useMemo(() => {
    const taken = new Set(alreadyLinked ?? []);
    return boards
      .filter((b) => !taken.has(b.id))
      .map((b) => ({
        value: b.id,
        label: `${b.title}${b.project_title ? ` — ${b.project_title}` : ""}`,
      }));
  }, [boards, alreadyLinked]);

  const attach = async () => {
    const board = boards.find((b) => b.id === boardId);
    const paths = parseMatchPathsLine(pathsDraft);
    setError(null);
    try {
      await onAttach({
        work_package_id: boardId,
        label: board?.external_ref || board?.title,
        is_default: isFirstLink,
        ...(paths.length ? { match_paths: paths } : {}),
      });
      // Only on success: a failed attach keeps what was typed.
      setBoardId("");
      setPathsDraft("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        {leading}
        <Select
          className="min-w-[200px] flex-1"
          options={options}
          value={boardId}
          onValueChange={setBoardId}
          placeholder={options.length ? "choose a board…" : "every board is linked"}
          disabled={busy || options.length === 0}
        />
        <Input
          value={pathsDraft}
          spellCheck={false}
          placeholder="paths (optional): apps/api, libs/shared"
          onChange={(e) => setPathsDraft(e.target.value)}
          className="min-w-[200px] flex-1 font-mono text-xs"
        />
        <Button size="sm" disabled={!boardId || busy} onClick={() => void attach()}>
          <Link2 className="mr-1 size-3" /> Link
        </Button>
      </div>
      {error && (
        <p className="flex items-start gap-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 size-3 shrink-0" />
          <span className="break-words">{error}</span>
        </p>
      )}
    </div>
  );
}
