/**
 * The one place a timestamp is formatted: pinned to Asia/Jerusalem (Israel's
 * single IANA zone) rather than the viewer's machine, and to the European
 * day/month/year order with a 24-hour clock (`en-GB`), whatever the browser's
 * own locale is. Stored instants stay UTC ISO strings.
 */
const DISPLAY_TIME_ZONE = "Asia/Jerusalem";
const DISPLAY_LOCALE = "en-GB";

export function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(DISPLAY_LOCALE, {
    timeZone: DISPLAY_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
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

/** Asia/Jerusalem's UTC offset (ms) at the UTC instant `utcMs`. */
function jerusalemOffsetMs(utcMs: number): number {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: DISPLAY_TIME_ZONE,
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    second: "numeric",
    hourCycle: "h23",
  }).formatToParts(new Date(utcMs));
  const n = (t: string) => Number(parts.find((p) => p.type === t)?.value);
  const asUtc = Date.UTC(
    n("year"),
    n("month") - 1,
    n("day"),
    n("hour"),
    n("minute"),
    n("second"),
  );
  return asUtc - Math.floor(utcMs / 1000) * 1000;
}

const ISRAEL_INPUT =
  /^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:[ T](\d{1,2}):(\d{2}))?$/;

/** Parses `DD/MM/YYYY` or `DD/MM/YYYY HH:MM` typed as Israel time into a UTC ISO
 * string, or null when it is not a real date. A bare date means 00:00. */
export function parseIsraelInput(text: string): string | null {
  const m = ISRAEL_INPUT.exec(text.trim());
  if (!m) return null;
  const [day, month, year, hour, minute] = [
    m[1],
    m[2],
    m[3],
    m[4] ?? "0",
    m[5] ?? "0",
  ].map(Number) as [number, number, number, number, number];
  const wall = Date.UTC(year, month - 1, day, hour, minute);
  const check = new Date(wall);
  if (
    check.getUTCMonth() !== month - 1 ||
    check.getUTCDate() !== day ||
    hour > 23 ||
    minute > 59
  )
    return null;
  let utc = wall - jerusalemOffsetMs(wall);
  utc = wall - jerusalemOffsetMs(utc);
  return new Date(utc).toISOString();
}
