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
