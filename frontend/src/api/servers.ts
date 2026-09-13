import { apiFetch } from "@/api/client";
import type { ServerDetail, ServerRowsResponse } from "@/types/server";

export function listServerRows(): Promise<ServerRowsResponse> {
  return apiFetch<ServerRowsResponse>("/api/v1/servers/rows");
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
