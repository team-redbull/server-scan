import type { EventListParams } from "@/api/events";
import { parseIsraelInput } from "@/lib/datetime";

export const RANGE_PRESETS = [
  { value: "1h", label: "Last hour", ms: 3_600_000 },
  { value: "24h", label: "Last 24 hours", ms: 86_400_000 },
  { value: "7d", label: "Last 7 days", ms: 7 * 86_400_000 },
  { value: "30d", label: "Last 30 days", ms: 30 * 86_400_000 },
] as const;

/** Event types renamed since a URL was bookmarked; "Health changes" was a shortcut value. */
const LEGACY_EVENT_TYPES: Record<string, string> = {
  HEALTH_STATUS_CHANGED: "HEALTH_CHANGED",
  "Health changes": "HEALTH_CHANGED",
};

export function normalizeEventType(value: string): string {
  return LEGACY_EVENT_TYPES[value] ?? value;
}

export interface EventFilters {
  server: string;
  eventType: string;
  actorId: string;
  /** "" (any time), a preset value, or "custom". */
  range: string;
  /** Custom bounds, `DD/MM/YYYY [HH:MM]` in Israel time. */
  from: string;
  to: string;
}

/** Filter state -> `GET /events` query params; `now` only matters for a preset. */
export function filtersToParams(
  f: EventFilters,
  now: Date = new Date(),
): Omit<EventListParams, "cursor" | "page_size"> {
  const params: Omit<EventListParams, "cursor" | "page_size"> = {};
  if (f.server.trim()) params.server_name = f.server.trim();
  if (f.eventType) params.event_type = f.eventType;
  if (f.actorId) params.actor_id = f.actorId;
  const preset = RANGE_PRESETS.find((p) => p.value === f.range);
  if (preset) {
    const minute = Math.floor(now.getTime() / 60_000) * 60_000;
    params.since = new Date(minute - preset.ms).toISOString();
  } else if (f.range === "custom") {
    const since = parseIsraelInput(f.from);
    const until = parseIsraelInput(f.to);
    if (since) params.since = since;
    if (until) params.until = until;
  }
  return params;
}
