import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { AsoodeSyncState, PauseSyncInput } from "../types";

/** How often the drain is polled while a resume is still sending. */
const DRAIN_POLL_INTERVAL_MS = 1500;

export interface SyncStateHandle {
  state: AsoodeSyncState | null;
  /** The last load's failure. Pause/resume failures are thrown to the caller. */
  error: string | null;
  isDraining: boolean;
  reload: () => Promise<void>;
  pause: (input: PauseSyncInput) => Promise<AsoodeSyncState>;
  resume: () => Promise<AsoodeSyncState>;
}

/**
 * A project's asoode sync state, kept fresh while a resume drains.
 *
 * Answers that arrive for a project the caller has since moved away from are
 * dropped, so switching projects mid-request never shows the wrong badge.
 */
export function useSyncState(slug: string): SyncStateHandle {
  const [state, setState] = useState<AsoodeSyncState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const currentSlug = useRef(slug);
  currentSlug.current = slug;

  const accept = useCallback(
    (forSlug: string, next: AsoodeSyncState) => {
      if (currentSlug.current !== forSlug) return;
      setState(next);
      setError(null);
    },
    []
  );

  const reload = useCallback(async () => {
    const forSlug = slug;
    try {
      const next = await api.getSyncState(forSlug);
      accept(forSlug, next);
    } catch (err) {
      if (currentSlug.current !== forSlug) return;
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [slug, accept]);

  useEffect(() => {
    setState(null);
    setError(null);
    void reload();
  }, [reload]);

  const isDraining = state?.drain?.state === "running";

  // One timer per answer: each fresh state schedules the next poll, and the
  // chain ends by itself once the drain reports finished.
  useEffect(() => {
    if (!isDraining) return;
    const timer = window.setTimeout(() => void reload(), DRAIN_POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [isDraining, state, reload]);

  const pause = useCallback(
    async (input: PauseSyncInput) => {
      const forSlug = slug;
      const next = await api.pauseSync(forSlug, input);
      accept(forSlug, next);
      return next;
    },
    [slug, accept]
  );

  const resume = useCallback(async () => {
    const forSlug = slug;
    const next = await api.resumeSync(forSlug);
    accept(forSlug, next);
    return next;
  }, [slug, accept]);

  return { state, error, isDraining, reload, pause, resume };
}
