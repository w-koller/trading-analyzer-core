/**
 * Shared number/date formatting.
 *
 * Centralised so a price renders identically in a table cell, a chart legend
 * and a card — and so "no data" is always an em dash rather than "null",
 * "NaN" or a silently-missing element.
 */

export const DASH = "—";

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

export function compactNum(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  return value.toFixed(0);
}

/**
 * Parse a backend timestamp to epoch ms, or NaN.
 *
 * The API emits two shapes: offset-qualified ("2026-08-23T16:46:41+00:00")
 * and, from SQLite's own defaults, bare local-looking strings. Appending "Z"
 * unconditionally corrupts the first kind into an invalid date, which fails
 * silently — a NaN comparison is simply false, so a whole list quietly
 * filters itself to empty rather than erroring. Hence one parser, used
 * everywhere a timestamp is read.
 */
export function parseIso(iso: string | null | undefined): number {
  if (!iso) return NaN;
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`).getTime();
}

/** "3m ago" / "2h ago" — absolute timestamps are noise on a live dashboard. */
export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const then = parseIso(iso);
  if (Number.isNaN(then)) return DASH;
  const secs = Math.floor((Date.now() - then) / 1000);
  if (secs < 0) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}

/**
 * "6 hours ago" — the long form.
 *
 * Separate from timeAgo rather than a flag: the dense mover rows want "6h
 * ago" and a news layout wants the words, and one function with a boolean
 * reads worse at both call sites.
 */
export function timeAgoLong(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const then = parseIso(iso);
  if (Number.isNaN(then)) return DASH;
  const secs = Math.floor((Date.now() - then) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? "" : "s"} ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days} day${days === 1 ? "" : "s"} ago`;
  return new Date(then).toLocaleDateString();
}

/** "in 45s" / "in 3m" — the forward-looking counterpart to timeAgo. */
export function timeUntil(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const then = parseIso(iso);
  if (Number.isNaN(then)) return DASH;
  const secs = Math.round((then - Date.now()) / 1000);
  if (secs <= 0) return "any moment";
  if (secs < 60) return `in ${secs}s`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `in ${hrs}h`;
  return `in ${Math.round(hrs / 24)}d`;
}

/** "US.PLTR" -> "PLTR", for when the market is already shown separately. */
export function bareTicker(code: string): string {
  const i = code.indexOf(".");
  return i === -1 ? code : code.slice(i + 1);
}

export function marketOf(code: string): string {
  const i = code.indexOf(".");
  return i === -1 ? "" : code.slice(0, i).toUpperCase();
}
