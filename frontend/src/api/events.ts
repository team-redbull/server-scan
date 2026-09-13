import { apiFetch } from "@/api/client";
import type { AuditEventListResponse } from "@/types/events";

export interface EventListParams {
  server_id?: string;
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

/** `GET /api/v1/events`. Filters by `server_id`/`event_type`/`actor_id`
 * only — per-rule/policy history is filtered client-side (`HistoryPanel`). */
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
