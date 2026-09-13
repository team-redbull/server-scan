import { apiFetch } from "@/api/client";
import type { ServerDetail, ServerFacets, ServerListResponse } from "@/types/server";

/** Query params for `GET /api/v1/servers`; an omitted key is left out of
 * the request, never sent as an empty string. */
export interface ServerListParams {
  search?: string;
  site_id?: string;
  vendor?: string;
  manager_id?: string;
  source_provider?: string;
  installation_type?: string;
  /** `openshift.lifecycle_state`. */
  openshift_state?: string;
  cluster_name?: string;
  health_overall?: string;
  maintenance?: boolean;
  stale?: boolean;
  sort?:
    | "name"
    | "serial"
    | "model"
    | "updated_at"
    | "last_seen_at"
    | "openshift_state"
    | "cluster_name"
    | "mce_name";
  sort_desc?: boolean;
  cursor?: string;
  page_size?: number;
  with_count?: boolean;
}

function buildSearchParams(params: ServerListParams): URLSearchParams {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    searchParams.set(key, String(value));
  }
  return searchParams;
}

export function listServers(params: ServerListParams = {}): Promise<ServerListResponse> {
  const query = buildSearchParams(params).toString();
  const path = query ? `/api/v1/servers?${query}` : "/api/v1/servers";
  return apiFetch<ServerListResponse>(path);
}

/** Per-option match counts under the filters already applied. Pagination
 * and ordering params are dropped: they would split the cache per page. */
export function getServerFacets(params: ServerListParams = {}): Promise<ServerFacets> {
  const { cursor, page_size, sort, sort_desc, with_count, ...filters } = params;
  void cursor;
  void page_size;
  void sort;
  void sort_desc;
  void with_count;
  const query = buildSearchParams(filters).toString();
  const path = query ? `/api/v1/servers/facets?${query}` : "/api/v1/servers/facets";
  return apiFetch<ServerFacets>(path);
}

export function getServer(id: string): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(`/api/v1/servers/${encodeURIComponent(id)}`);
}

export interface MaintenanceEnableRequest {
  reason?: string;
  ticket?: string;
  expected_end?: string;
}

// Both endpoints return the full `ServerDetail` (`maintenance_schemas.py`).
export function enableMaintenance(
  id: string,
  body: MaintenanceEnableRequest,
): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(`/api/v1/servers/${encodeURIComponent(id)}/maintenance`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function disableMaintenance(id: string): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(`/api/v1/servers/${encodeURIComponent(id)}/maintenance`, {
    method: "DELETE",
  });
}
