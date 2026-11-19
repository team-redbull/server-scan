import { useState } from "react";

import { ApiError } from "@/api/client";
import type { ClusterJob } from "@/api/openshift";
import { silentFor } from "@/features/clusters/format";
import { useClusterJobsQuery, useDecommissionMutation } from "@/features/clusters/hooks";
import { formatTimestamp } from "@/lib/datetime";
import { showToast } from "@/lib/toast";

const BUTTON =
  "rounded-md px-3 py-1.5 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] disabled:cursor-not-allowed disabled:opacity-40";

const KIND_LABEL = { nodes: "Cluster nodes", agents: "MCE agents" } as const;

function DecommissionDialog({ job, onClose }: { job: ClusterJob; onClose: () => void }) {
  const [typed, setTyped] = useState("");
  const decommission = useDecommissionMutation();

  function close() {
    if (!decommission.isPending) onClose();
  }

  return (
    <>
      <span className="fixed inset-0 z-20 bg-black/40" onClick={close} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Decommission ${job.name}`}
        onKeyDown={(event) => {
          if (event.key === "Escape") close();
        }}
        className="fixed top-1/3 left-1/2 z-30 w-[28rem] max-w-[calc(100vw-2rem)] -translate-x-1/2 rounded-[var(--radius-card)] border border-[var(--border-strong)] bg-[var(--surface-raised)] p-5 shadow-lg"
      >
        <h2 className="text-base font-semibold text-[var(--text-primary)]">
          Decommission this cluster?
        </h2>
        <p className="mt-1 font-mono text-xs break-all text-[var(--text-muted)]">{job.name}</p>
        <p className="mt-3 text-sm text-[var(--text-secondary)]">
          {job.held === 0
            ? "It holds no servers; only its record and metrics are removed."
            : `${job.held} server${job.held === 1 ? "" : "s"} it holds become Available again, so /servers/available can hand ${job.held === 1 ? "it" : "them"} out.`}{" "}
          Its last run was {silentFor(job.silent_seconds)} ago. If the cluster is in fact alive, its next
          job run claims its servers back.
        </p>
        {decommission.isError && (
          <p role="alert" className="mt-3 text-sm text-[var(--text-on-critical)]">
            {decommission.error instanceof ApiError
              ? decommission.error.problem.detail
              : "Failed to decommission."}
          </p>
        )}
        <label className="mt-4 block text-xs font-medium text-[var(--text-secondary)]">
          Type the name to confirm
          <input
            type="text"
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
            }}
            className="mt-1 w-full rounded-md border border-[var(--border-subtle)] bg-[var(--surface-sunken)] px-2.5 py-1.5 font-mono text-sm text-[var(--text-primary)]"
          />
        </label>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            disabled={decommission.isPending}
            onClick={close}
            className={`${BUTTON} border border-[var(--border-strong)] text-[var(--text-secondary)]`}
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={typed !== job.name || decommission.isPending}
            onClick={() => {
              decommission.mutate(
                { kind: job.kind, name: job.name },
                {
                  onSuccess: (result) => {
                    showToast({
                      title: `Decommissioned ${job.name}`,
                      detail: `${result.freed} server${result.freed === 1 ? "" : "s"} freed`,
                      ok: true,
                    });
                    onClose();
                  },
                },
              );
            }}
            className={`${BUTTON} bg-[var(--color-status-critical)] text-white hover:brightness-110`}
          >
            {decommission.isPending ? "Decommissioning…" : "Decommission"}
          </button>
        </div>
      </div>
    </>
  );
}

/** `/clusters`: every membership job by recency, and the way to retire a destroyed cluster's (ADR-0067). */
export function ClustersPage() {
  const { data, isPending, isError } = useClusterJobsQuery();
  const [target, setTarget] = useState<ClusterJob | null>(null);
  const days = data ? Math.round(data.silent_after_seconds / 86400) : 7;

  return (
    <main className="mx-auto max-w-5xl px-8 py-8">
      <h1 className="text-xl font-semibold tracking-tight text-[var(--text-primary)]">Clusters</h1>
      <p className="mt-1 text-sm text-[var(--text-secondary)]">
        Each OpenShift cluster or MCE reports its servers with a CronJob that runs inside it. One that
        has not run for {days} days is presumed destroyed; its servers stay Installed until you
        decommission it here.
      </p>

      {isPending && <p className="mt-6 text-[var(--text-muted)]">Loading…</p>}
      {isError && (
        <p role="alert" className="mt-6 text-[var(--text-on-critical)]">
          Failed to load the clusters.
        </p>
      )}
      {data?.items.length === 0 && (
        <p className="mt-6 text-[var(--text-muted)]">No membership job has reported yet.</p>
      )}
      {data && data.items.length > 0 && (
        <div className="mt-6 overflow-x-auto rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--surface-raised)]">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-[var(--text-secondary)] uppercase">
              <tr>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Job</th>
                <th className="px-3 py-2 font-medium">Servers held</th>
                <th className="px-3 py-2 font-medium">Last run</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {data.items.map((job) => (
                <tr
                  key={`${job.kind}:${job.name}`}
                  className="border-t border-[var(--border-subtle)]"
                >
                  <td className="px-3 py-2 font-mono text-[var(--text-primary)]">{job.name}</td>
                  <td className="px-3 py-2 text-[var(--text-secondary)]">{KIND_LABEL[job.kind]}</td>
                  <td className="px-3 py-2 tabular">{job.held}</td>
                  <td className="px-3 py-2 text-xs text-[var(--text-muted)]">
                    {formatTimestamp(job.last_run_at)}
                  </td>
                  <td className="px-3 py-2">
                    {job.silent ? (
                      <span
                        className="rounded-full px-2 py-0.5 text-xs font-medium"
                        style={{
                          backgroundColor: "var(--tint-warning)",
                          color: "var(--text-on-warning)",
                        }}
                      >
                        Not reporting for {silentFor(job.silent_seconds)}
                      </span>
                    ) : (
                      <span
                        className="rounded-full px-2 py-0.5 text-xs font-medium"
                        style={{
                          backgroundColor: "var(--tint-healthy)",
                          color: "var(--text-on-healthy)",
                        }}
                      >
                        Reporting
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      aria-label={`Decommission ${job.name}`}
                      title="Free its servers and remove its record"
                      onClick={() => {
                        setTarget(job);
                      }}
                      className="h-7 rounded-md border border-[var(--border-subtle)] px-2.5 text-xs font-medium text-[var(--text-muted)] hover:border-[var(--color-status-critical)] hover:text-[var(--text-on-critical)]"
                    >
                      Decommission
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {target && (
        <DecommissionDialog
          job={target}
          onClose={() => {
            setTarget(null);
          }}
        />
      )}
    </main>
  );
}
