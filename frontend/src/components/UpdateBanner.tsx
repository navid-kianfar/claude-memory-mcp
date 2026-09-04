import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowUpCircle,
  CheckCircle2,
  ExternalLink,
  RefreshCw,
} from "lucide-react";
import { api } from "../lib/api";
import type { UpdateStatus } from "../types";
import { Button } from "./ui/Button";
import { Badge } from "./ui/Badge";
import { formatRelative } from "../lib/utils";

/**
 * Machine-wide update notice, rendered above whatever view is open.
 *
 * THE HONEST BIT, which is the whole point of the component: pressing Update
 * does NOT install anything. It records approval; the Stop hook applies it when
 * the current turn ends, because installing reloads the launchd daemon and would
 * drop the live MCP connection mid-answer. So the copy says so before the click,
 * and the banner flips to the approved state immediately after it - a button
 * that looks inert for a minute gets pressed again.
 *
 * Machine-wide state, like Integrations: one daemon, one installation, no
 * project scoping. It borrows that screen's section idiom rather than inventing
 * a banner style.
 *
 * THREE STATES, and the third is the one that is easy to get wrong:
 *   - an update is available          -> the banner, with Update / approved
 *   - no update, no error             -> nothing at all
 *   - no update, but `last_error` set -> the last CHECK failed. That is not
 *     "you are up to date"; saying so would make an update checker actively
 *     misleading, so it renders as a quiet warning instead.
 */

/** Only ever put an http(s) URL in an href. `release_url` comes from GitHub. */
function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

/**
 * `last_checked_at` is unix epoch SECONDS as a string, not ISO, so it cannot go
 * straight into formatRelative. Accept either, in case the shape ever changes.
 */
function checkedAgo(value: string | null): string | null {
  if (!value) return null;
  const epoch = Number(value);
  const iso = Number.isFinite(epoch)
    ? new Date(epoch * 1000).toISOString()
    : value;
  const out = formatRelative(iso);
  return out === "—" ? null : out;
}

const SOURCE_LABELS: Record<string, string> = {
  github_releases: "GitHub releases",
  github_commits: "GitHub commits",
  git: "local git",
};

/** The poller runs every 10 minutes; a minute is plenty to notice its answer. */
const REFRESH_MS = 60_000;

export function UpdateBanner() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  const load = useCallback(async () => {
    try {
      const s = await api.getUpdateStatus();
      if (alive.current) setStatus(s);
    } catch {
      // The daemon being unreachable is not this component's story to tell -
      // the app's own boot error covers it. Staying silent beats a second
      // red bar saying the same thing.
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    // The hook applies the update while the user is away in the terminal, and
    // clears `approved` when it does. Refresh on return so the banner does not
    // sit there claiming work that is already finished.
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [load]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      // Always re-read: on success it confirms the new state, and on the
      // "nothing to approve" refusal it retires a banner that had gone stale.
      await load();
      if (alive.current) setBusy(false);
    }
  };

  if (!status) return null;

  const checked = checkedAgo(status.last_checked_at);
  const sourceLabel = status.source ? SOURCE_LABELS[status.source] ?? status.source : null;

  if (!status.update_available) {
    // The banner was stale: the poll moved on between render and click, so the
    // server refused. Say so, rather than letting the banner vanish under the
    // cursor - silently disappearing after a click reads as "it worked".
    if (error) {
      return (
        <div
          role="status"
          className="mb-4 flex items-start gap-3 rounded-lg border border-border bg-card p-3 text-xs"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-500" />
          <div className="min-w-0 flex-1 space-y-1">
            <p className="font-medium">
              Nothing was approved — that update is no longer on offer.
            </p>
            <p className="break-words text-muted-foreground">{error}</p>
            <p className="text-muted-foreground">
              The banner had gone stale; the check has been re-read. Nothing has
              been installed.
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={() => setError(null)}>
            Dismiss
          </Button>
        </div>
      );
    }

    // ---- the check itself failed: report that, never "up to date" ----
    if (!status.last_error) return null;
    return (
      <div
        role="status"
        className="mb-4 flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-600 dark:text-amber-400"
      >
        <AlertTriangle className="mt-0.5 size-4 shrink-0" />
        <div className="min-w-0 space-y-1">
          <p className="font-medium">
            Could not check for updates — this is not a confirmation that you are
            up to date.
          </p>
          <p className="break-words opacity-80">{status.last_error}</p>
          {checked && (
            <p className="opacity-70">
              Last attempt {checked}. The daemon retries every 10 minutes.
            </p>
          )}
        </div>
      </div>
    );
  }

  // ---- an update is available ----
  const changesUrl = safeHttpUrl(status.release_url);

  return (
    <div
      role="status"
      aria-live="polite"
      className="mb-4 rounded-lg border border-border bg-card p-4"
    >
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          {status.approved ? (
            <CheckCircle2 className="size-4 text-emerald-500" />
          ) : (
            <ArrowUpCircle className="size-4 text-primary" />
          )}
          {status.approved ? "Update approved" : "Update available"}
        </h2>
        <div className="flex flex-wrap items-center gap-1.5">
          {status.current_version && status.latest_version && (
            <Badge variant="outline" className="font-mono text-[11px]">
              {status.current_version} → {status.latest_version}
            </Badge>
          )}
          {status.commits_behind ? (
            <Badge variant="secondary" className="text-[11px]">
              {status.commits_behind} commit
              {status.commits_behind === 1 ? "" : "s"} behind
            </Badge>
          ) : null}
          {status.approved && <Badge variant="success">queued</Badge>}
        </div>
      </div>

      <p className="mb-3 max-w-2xl text-xs text-muted-foreground">
        {status.approved ? (
          <>
            Nothing has been installed yet. It applies at the end of the current
            turn — installing restarts the daemon, so doing it mid-turn would
            drop the live MCP connection. Claude reconnects on its own
            afterwards; there is nothing else to do here.
          </>
        ) : (
          <>
            Update does not install straight away. It records your approval, and
            the update is applied when the current turn ends — installing
            restarts the daemon, which would otherwise drop the live MCP
            connection mid-answer. Claude reconnects afterwards.
          </>
        )}
      </p>

      {status.release_notes && (
        <div className="mb-3 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-border/60 bg-background p-3 text-xs text-muted-foreground scrollbar-thin">
          {status.release_notes}
        </div>
      )}

      {error && (
        <p className="mb-3 flex items-start gap-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span className="break-words">{error}</span>
        </p>
      )}

      {/* A failed check does not erase what the last good one found - it just
          means this could have moved on since. Say which, rather than letting
          "Checked 4m ago" imply the answer is fresh. */}
      {status.last_error && (
        <p className="mb-3 flex items-start gap-2 text-xs text-amber-600 dark:text-amber-400">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span className="break-words">
            The most recent check failed, so this may be out of date:{" "}
            {status.last_error}
          </span>
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {status.approved ? (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => void run(() => api.cancelUpdateApproval())}
          >
            {busy && <RefreshCw className="animate-spin" />}
            Cancel update
          </Button>
        ) : (
          <Button
            size="sm"
            disabled={busy}
            onClick={() => void run(() => api.approveUpdate())}
          >
            {busy ? <RefreshCw className="animate-spin" /> : <ArrowUpCircle />}
            Update
          </Button>
        )}
        {changesUrl && (
          <a
            href={changesUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ExternalLink className="size-3.5" />
            View changes
          </a>
        )}
        {(checked || sourceLabel) && (
          <span className="ml-auto text-[11px] text-muted-foreground">
            {checked && <>Checked {checked}</>}
            {checked && sourceLabel && " · "}
            {sourceLabel && <>via {sourceLabel}</>}
          </span>
        )}
      </div>
    </div>
  );
}
