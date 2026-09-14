import { useState } from "react";
import { AlertTriangle, ArrowRight, Check } from "lucide-react";
import type { LinkProposal, LinkProposalStatus } from "../types";
import { Badge } from "./ui/Badge";
import type { BadgeVariant } from "./ui/Badge";
import { Button } from "./ui/Button";

const STATUS_VARIANT: Record<LinkProposalStatus, BadgeVariant> = {
  matches: "success",
  differs: "default",
  unlinked: "outline",
};

const STATUS_LABEL: Record<LinkProposalStatus, string> = {
  matches: "already bound",
  differs: "differs",
  unlinked: "not linked",
};

const APPLY_LABEL: Record<LinkProposalStatus, string> = {
  matches: "",
  differs: "Update paths",
  unlinked: "Link board",
};

export interface LinkProposalsCardProps {
  projectName: string;
  proposals: LinkProposal[];
  /** The key of the proposal currently being applied, from `proposalKey`. */
  busyKey: string | null;
  /** Rejects with an Error whose message is already presentable. */
  onApply: (proposal: LinkProposal) => Promise<void>;
}

/** Stable per-proposal key: a board appears at most once per project. */
export function proposalKey(proposal: LinkProposal): string {
  return proposal.remote_work_package_id;
}

function Paths({ paths }: { paths: string[] }) {
  if (paths.length === 0) {
    return <span className="text-xs text-muted-foreground">no paths</span>;
  }
  return (
    <span className="flex flex-wrap gap-1">
      {paths.map((path) => (
        <Badge key={path} variant="secondary" className="font-mono text-[10px]">
          {path}
        </Badge>
      ))}
    </span>
  );
}

/**
 * Bindings a committed manifest suggests for one project.
 *
 * Every row is applied BY HAND and one at a time - there is deliberately no
 * "apply all". Linking a board creates real mirroring of real tasks onto
 * somebody's board, so the manifest proposes and a person decides; an
 * auto-bind on read would be the same class of mistake as a default board
 * swallowing every task.
 */
export function LinkProposalsCard({
  projectName,
  proposals,
  busyKey,
  onApply,
}: LinkProposalsCardProps) {
  const [errors, setErrors] = useState<Record<string, string>>({});

  const apply = async (proposal: LinkProposal) => {
    const key = proposalKey(proposal);
    setErrors((prev) => {
      const { [key]: _dropped, ...rest } = prev;
      return rest;
    });
    try {
      await onApply(proposal);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setErrors((prev) => ({ ...prev, [key]: message }));
    }
  };

  return (
    <div className="rounded-md border border-border/60 p-3">
      <p className="mb-2 text-sm font-medium">{projectName}</p>
      <div className="space-y-2">
        {proposals.map((proposal) => {
          const key = proposalKey(proposal);
          const name = proposal.label || proposal.remote_work_package_id.slice(0, 8);
          const error = errors[key];
          return (
            <div key={key} className="rounded-md border border-border/50 bg-background/40 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={STATUS_VARIANT[proposal.status]} className="shrink-0 text-[10px]">
                  {STATUS_LABEL[proposal.status]}
                </Badge>
                <span className="min-w-0 flex-1 truncate text-sm" title={name}>
                  {name}
                  {proposal.is_default && (
                    <span className="ml-1 text-amber-500" title="Proposed as the default board">
                      ★
                    </span>
                  )}
                </span>
                {proposal.status === "matches" ? (
                  <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
                    <Check className="size-3" /> nothing to do
                  </span>
                ) : (
                  <Button
                    size="sm"
                    variant={proposal.status === "differs" ? "outline" : "default"}
                    disabled={busyKey === key}
                    onClick={() => void apply(proposal)}
                  >
                    {busyKey === key ? "Applying…" : APPLY_LABEL[proposal.status]}
                  </Button>
                )}
              </div>

              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                {proposal.status === "differs" ? (
                  <>
                    <span className="text-muted-foreground">now</span>
                    <Paths paths={proposal.current_match_paths ?? []} />
                    <ArrowRight className="size-3 shrink-0 text-muted-foreground" />
                    <span className="text-muted-foreground">proposed</span>
                    <Paths paths={proposal.match_paths} />
                  </>
                ) : (
                  <>
                    <span className="text-muted-foreground">paths</span>
                    <Paths paths={proposal.match_paths} />
                  </>
                )}
              </div>

              {error && (
                <p className="mt-1.5 flex items-start gap-2 text-xs text-destructive">
                  <AlertTriangle className="mt-0.5 size-3 shrink-0" />
                  <span className="break-words">{error}</span>
                </p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
