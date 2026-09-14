import { ApiError } from "./api";

/**
 * Helpers shared by the board-link surfaces in the Integrations screen.
 *
 * The validation of a `match_paths` entry lives ON THE SERVER and nowhere else:
 * it is the thing that routes a task, and a second opinion in the browser would
 * either reject what the server accepts or accept what it rejects. So the draft
 * is only split into lines here - a rejected pattern comes back as a 400 whose
 * message is shown verbatim.
 */

/** Textarea draft -> the array the API takes. One prefix per line. */
export function parseMatchPaths(draft: string): string[] {
  return draft
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

/**
 * One-line draft -> the array the API takes. Comma-separated, because the
 * attach form is a single field: `apps/api, libs/shared`. Splitting on spaces
 * too would quietly cut a path that contains one.
 */
export function parseMatchPathsLine(draft: string): string[] {
  return draft
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

/** The API's array -> the textarea draft. */
export function formatMatchPaths(paths: string[] | null | undefined): string {
  return (paths ?? []).join("\n");
}

/** Same paths, same order — used to keep a Save button honest. */
export function sameMatchPaths(
  a: string[] | null | undefined,
  b: string[] | null | undefined
): boolean {
  const left = a ?? [];
  const right = b ?? [];
  return left.length === right.length && left.every((v, i) => v === right[i]);
}

/**
 * A failure message for one of the link routes.
 *
 * A 404 here is not "not found" in the usual sense - `PATCH`/`DELETE` on a link
 * only exist in a daemon that carries the path-routing half of this change, and
 * an older one answers 404 with a plain-text body. Saying so is the difference
 * between a surface that looks broken and one that says what is missing.
 */
export function describeLinkError(err: unknown, what: string): string {
  if (err instanceof ApiError && err.status === 404) {
    return `${what}: this daemon has no route for it yet (404). The path-routing endpoints arrive with the server half of this change.`;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
