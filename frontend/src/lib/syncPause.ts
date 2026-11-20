/** Mirrors the server's MAX_REASON_LENGTH in services/sync_pause.py. */
export const MAX_PAUSE_REASON_LENGTH = 500;

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Today as YYYY-MM-DD in the viewer's own timezone, for a date input's `min`. */
export function todayIsoDate(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

/**
 * "2026-12-04" -> "Dec 4, 2026". Parsed as a LOCAL date on purpose:
 * `new Date("2026-12-04")` is UTC midnight, which shows as the day before
 * anywhere west of Greenwich.
 */
export function formatPauseDate(value: string): string {
  const match = ISO_DATE.exec(value);
  if (!match) return value;
  const [, year, month, day] = match;
  const date = new Date(Number(year), Number(month) - 1, Number(day));
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function pluralize(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}
