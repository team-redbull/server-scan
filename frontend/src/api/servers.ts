import { apiFetch } from "@/api/client";
import type {
  ServerDetail,
  ServerRowsResponse,
  VmHostResponse,
} from "@/types/server";

export function listServerRows(): Promise<ServerRowsResponse> {
  return apiFetch<ServerRowsResponse>("/api/v1/servers/rows");
}

export function getServer(id: string): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(`/api/v1/servers/${encodeURIComponent(id)}`);
}

/** Resolved on every call, never cached; 404 for a server that is not a VM. */
export function getServerVmHost(id: string): Promise<VmHostResponse> {
  return apiFetch<VmHostResponse>(
    `/api/v1/servers/${encodeURIComponent(id)}/vm-host`,
  );
}

export interface MaintenanceEnableRequest {
  reason?: string;
  expected_end?: string;
}

// Both endpoints return the full `ServerDetail` (`maintenance_schemas.py`).
export function enableMaintenance(
  id: string,
  body: MaintenanceEnableRequest,
): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(
    `/api/v1/servers/${encodeURIComponent(id)}/maintenance`,
    {
      method: "PUT",
      body: JSON.stringify(body),
    },
  );
}

export function disableMaintenance(id: string): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(
    `/api/v1/servers/${encodeURIComponent(id)}/maintenance`,
    {
      method: "DELETE",
    },
  );
}

/** Reads the server from its vendor manager now (admin only, ADR-0064); `revision`
 * moves only when its content changed. */
export function refreshServer(id: string): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(
    `/api/v1/servers/${encodeURIComponent(id)}/refresh`,
    { method: "POST" },
  );
}

/** Removes the server from the database (admin only, ADR-0065). Without `force`, a server in use
 * is refused with a 409 whose `details.blockers` lists why. */
export function deleteServer(id: string, force: boolean): Promise<void> {
  return apiFetch<void>(
    `/api/v1/servers/${encodeURIComponent(id)}${force ? "?force=true" : ""}`,
    { method: "DELETE" },
  );
}
