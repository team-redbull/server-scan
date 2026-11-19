import { apiFetch } from "@/api/client";

export type MembershipKind = "nodes" | "agents";

export interface ClusterJob {
  kind: MembershipKind;
  name: string;
  held: number;
  last_run_at: string;
  silent_seconds: number;
  silent: boolean;
}

export interface ClusterJobsResponse {
  items: ClusterJob[];
  silent_after_seconds: number;
}

/** Every membership job with the servers it holds and how long since it last ran (ADR-0067). */
export function listClusterJobs(): Promise<ClusterJobsResponse> {
  return apiFetch<ClusterJobsResponse>("/api/v1/openshift/clusters");
}

/** Frees what a destroyed cluster still holds and drops its run record (admin only). */
export function decommissionCluster(
  kind: MembershipKind,
  name: string,
): Promise<{ kind: MembershipKind; name: string; freed: number }> {
  return apiFetch(
    `/api/v1/openshift/clusters/${kind}/${encodeURIComponent(name)}/decommission`,
    { method: "POST" },
  );
}
