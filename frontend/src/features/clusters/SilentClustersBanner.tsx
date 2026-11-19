import { Link } from "react-router";

import type { ClusterJob } from "@/api/openshift";
import { silentFor } from "@/features/clusters/format";
import { useClusterJobsQuery } from "@/features/clusters/hooks";

/**
 * "A cluster is not reporting" notice, with a link to `/clusters` (ADR-0067).
 *
 * Args:
 *   only: keeps the jobs that matter here (a server's own cluster); every silent job when absent.
 *
 * Returns:
 *   The banner, or nothing while every matching job is reporting.
 */
export function SilentClustersBanner({ only }: { only?: (job: ClusterJob) => boolean }) {
  const { data } = useClusterJobsQuery();
  const silent = (data?.items ?? []).filter((job) => job.silent && (only ? only(job) : true));
  if (silent.length === 0) return null;
  const first = silent[0];
  return (
    <p
      role="status"
      className="mb-4 rounded-[var(--radius-card)] px-4 py-3 text-sm"
      style={{ backgroundColor: "var(--tint-warning)", color: "var(--text-on-warning)" }}
    >
      {silent.length === 1 && first
        ? `${first.name} has not reported for ${silentFor(first.silent_seconds)}; its servers stay Installed until it is decommissioned. `
        : `${silent.length} clusters have not reported for over ${silentFor(data?.silent_after_seconds ?? 0)}; their servers stay Installed until they are decommissioned. `}
      <Link to="/clusters" className="font-medium underline">
        Review
      </Link>
    </p>
  );
}
