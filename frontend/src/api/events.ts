import { apiFetch } from "@/api/client";
import type { AuditEventListResponse, EventActor } from "@/types/events";

export interface EventListParams {
  server_id?: string;
  /** Case-insensitive substring of the server name. */
  server_name?: string;
  /** ISO-8601, inclusive. */
  since?: string;
  /** ISO-8601, exclusive. */
  until?: string;
  event_type?: string;
  actor_id?: string;
  cursor?: string;
  page_size?: number;
}

function buildSearchParams<T extends object>(params: T): URLSearchParams {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") {
      continue;
    }
    searchParams.set(key, String(value as string | number));
  }
  return searchParams;
}

/** `GET /api/v1/events`. Per-rule/policy history is filtered client-side (`HistoryPanel`). */
export function listEvents(params: EventListParams = {}): Promise<AuditEventListResponse> {
  const query = buildSearchParams(params).toString();
  return apiFetch<AuditEventListResponse>(query ? `/api/v1/events?${query}` : "/api/v1/events");
}

export function listServerEvents(
  serverId: string,
  params: { cursor?: string; page_size?: number } = {},
): Promise<AuditEventListResponse> {
  const query = buildSearchParams(params).toString();
  const path = `/api/v1/servers/${encodeURIComponent(serverId)}/events`;
  return apiFetch<AuditEventListResponse>(query ? `${path}?${query}` : path);
}

/** `GET /api/v1/events/actors`: who has written events, for the User filter. */
export function listEventActors(): Promise<{ items: EventActor[] }> {
  return apiFetch<{ items: EventActor[] }>("/api/v1/events/actors");
}
