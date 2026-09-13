/**
 * The one place a timestamp's displayed timezone is decided: every
 * timestamp is pinned to Asia/Jerusalem (Israel's single IANA zone) rather
 * than the viewer's machine. The locale stays `undefined`, so only the
 * wall-clock time is forced, not the number/date formatting.
 */
const DISPLAY_TIME_ZONE = "Asia/Jerusalem";

export function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    timeZone: DISPLAY_TIME_ZONE,
    timeZoneName: "short",
  });
}

const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

/**
 * "3 hours ago" / "2 days ago" for an ISO instant, coarsening with distance.
 * Pair it with `formatTimestamp` in a `title` so the exact moment is a hover
 * away; the relative form alone is what a scan of the table needs.
 */
export function formatRelative(iso: string, now: Date = new Date()): string {
  const seconds = Math.round((new Date(iso).getTime() - now.getTime()) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 60) return RELATIVE.format(seconds, "second");
  if (abs < 3600) return RELATIVE.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return RELATIVE.format(Math.round(seconds / 3600), "hour");
  return RELATIVE.format(Math.round(seconds / 86400), "day");
}

/** `45m` / `20h` / `3d` — the age of an instant, for a chip with no room for words. */
export function formatAge(iso: string, now: Date = new Date()): string {
  const seconds = Math.max(0, Math.round((now.getTime() - new Date(iso).getTime()) / 1000));
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}
