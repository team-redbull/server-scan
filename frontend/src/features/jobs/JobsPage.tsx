import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { getJobs, getPodLogs, runJob } from "@/api/jobs";
import type { CronJob, JobRun, JobState } from "@/api/jobs";
import { ApiError } from "@/api/client";
import { queryKeys } from "@/api/queryKeys";
import { formatRelative, formatTimestamp } from "@/lib/datetime";

const TAIL = 500;
const LIST_POLL_MS = 5000;
const LOG_POLL_MS = 3000;

const STATE_TINT: Record<JobState, [tint: string, text: string]> = {
  active: ["--tint-info", "--text-on-info"],
  succeeded: ["--tint-healthy", "--text-on-healthy"],
  failed: ["--tint-critical", "--text-on-critical"],
  pending: ["--tint-unknown", "--text-on-unknown"],
};

const BUTTON =
  "rounded-full px-4 py-1.5 text-sm font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] disabled:cursor-not-allowed disabled:opacity-40";

interface Selected {
  cronjob: string;
  job: string;
  pod: string;
}

function message(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail;
  return error instanceof Error ? error.message : "That did not work.";
}

function StateChip({ state }: { state: JobState }) {
  const [tint, text] = STATE_TINT[state];
  return (
    <span
      className="rounded-full px-2 py-0.5 text-xs font-medium"
      style={{ backgroundColor: `var(${tint})`, color: `var(${text})` }}
    >
      {state}
    </span>
  );
}

function ConfirmRun({
  cronjob,
  error,
  pending,
  onCancel,
  onConfirm,
}: {
  cronjob: CronJob;
  error: unknown;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <>
      <span
        className="fixed inset-0 z-20 bg-black/40"
        onClick={onCancel}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Run ${cronjob.name}`}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel();
        }}
        className="fixed top-1/3 left-1/2 z-30 w-[26rem] max-w-[calc(100vw-2rem)] -translate-x-1/2 rounded-[var(--radius-card)] border border-[var(--border-strong)] bg-[var(--surface-raised)] p-5 shadow-lg"
      >
        <h2 className="text-base font-semibold text-[var(--text-primary)]">
          Run this job now?
        </h2>
        <p className="mt-1 font-mono text-xs break-all text-[var(--text-muted)]">
          {cronjob.name}
        </p>
        <p className="mt-3 text-sm text-[var(--text-secondary)]">
          It starts a real Job from the CronJob&apos;s template and runs exactly
          as scheduled.
        </p>
        {cronjob.warning && (
          <p
            role="note"
            className="mt-3 rounded-md px-3 py-2 text-sm"
            style={{
              backgroundColor: "var(--tint-warning)",
              color: "var(--text-on-warning)",
            }}
          >
            {cronjob.warning}
          </p>
        )}
        {error != null && (
          <p
            role="alert"
            className="mt-3 text-sm text-[var(--text-on-critical)]"
          >
            {message(error)}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className={`${BUTTON} border border-[var(--border-strong)] text-[var(--text-secondary)]`}
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={pending}
            onClick={onConfirm}
            className={`${BUTTON} bg-[var(--color-status-info)] text-white hover:brightness-110`}
          >
            Run job
          </button>
        </div>
      </div>
    </>
  );
}

function LogPane({
  selected,
  active,
  onClose,
}: {
  selected: Selected;
  active: boolean;
  onClose: () => void;
}) {
  const { cronjob, job, pod } = selected;
  const card = useRef<HTMLDivElement>(null);
  useEffect(() => {
    card.current?.focus();
  }, []);
  const logs = useQuery({
    queryKey: queryKeys.jobs.logs(cronjob, job, pod),
    queryFn: () => getPodLogs(cronjob, job, pod, TAIL),
    refetchInterval: active ? LOG_POLL_MS : false,
  });
  return (
    <>
      <span
        className="fixed inset-0 z-20 bg-black/50"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={card}
        role="dialog"
        aria-modal="true"
        aria-label={`Logs of ${pod}`}
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
        className="fixed top-1/2 left-1/2 z-30 flex max-h-[85vh] w-[56rem] max-w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-[var(--radius-card)] border border-[var(--border-strong)] bg-[var(--surface-raised)] p-5 shadow-lg outline-none"
      >
        <div className="flex items-baseline justify-between gap-4">
          <div className="min-w-0">
            <h2 className="font-mono text-sm break-all text-[var(--text-primary)]">
              {pod}
            </h2>
            <p className="mt-0.5 font-mono text-xs break-all text-[var(--text-muted)]">
              {job}
            </p>
          </div>
          <div className="flex shrink-0 gap-4">
            <button
              type="button"
              onClick={() => {
                void logs.refetch();
              }}
              className="text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            >
              Refresh
            </button>
            <button
              type="button"
              onClick={onClose}
              className="text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            >
              Close
            </button>
          </div>
        </div>
        <p className="mt-1 text-xs text-[var(--text-muted)]">
          Last {TAIL} lines
          {active ? ", refreshing every 3 s while the job runs" : ""}.
        </p>
        {logs.isError ? (
          <p
            role="alert"
            className="mt-3 text-sm text-[var(--text-on-critical)]"
          >
            {message(logs.error)}
          </p>
        ) : (
          <pre className="mt-3 min-h-0 flex-1 overflow-auto rounded-md bg-[var(--surface-sunken)] p-3 font-mono text-xs whitespace-pre-wrap text-[var(--text-primary)]">
            {logs.data ? logs.data.text || "(no output yet)" : "Loading…"}
          </pre>
        )}
      </div>
    </>
  );
}

function JobRow({
  cronjob,
  job,
  selected,
  onSelect,
}: {
  cronjob: string;
  job: JobRun;
  selected: Selected | null;
  onSelect: (s: Selected) => void;
}) {
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5 text-sm">
      <StateChip state={job.state} />
      <span className="font-mono text-xs text-[var(--text-primary)]">
        {job.name}
      </span>
      {job.manual && (
        <span className="text-xs text-[var(--text-muted)]">
          by hand{job.triggered_by ? ` (${job.triggered_by})` : ""}
        </span>
      )}
      {job.created_at && (
        <span
          className="text-xs text-[var(--text-muted)]"
          title={formatTimestamp(job.created_at)}
        >
          {formatRelative(job.created_at)}
        </span>
      )}
      <span className="ml-auto flex flex-wrap gap-2">
        {job.pods.map((pod) => (
          <button
            key={pod.name}
            type="button"
            aria-pressed={selected?.pod === pod.name}
            onClick={() => {
              onSelect({ cronjob, job: job.name, pod: pod.name });
            }}
            className={`rounded-md border px-2 py-0.5 text-xs ${
              selected?.pod === pod.name
                ? "border-[var(--border-strong)] text-[var(--text-primary)]"
                : "border-[var(--border-subtle)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            }`}
          >
            Logs · {pod.name.slice(-5)} · {pod.phase}
          </button>
        ))}
      </span>
    </li>
  );
}

function CronJobCard({
  cronjob,
  selected,
  onSelect,
  onRun,
}: {
  cronjob: CronJob;
  selected: Selected | null;
  onSelect: (s: Selected) => void;
  onRun: () => void;
}) {
  return (
    <section
      aria-label={cronjob.name}
      className={`rounded-2xl border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-5 ${cronjob.enabled ? "" : "opacity-70"}`}
    >
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-mono text-sm font-semibold text-[var(--text-primary)]">
          {cronjob.name}
        </h2>
        <span className="font-mono text-xs text-[var(--text-muted)]">
          {cronjob.schedule}
        </span>
        {cronjob.suspend && (
          <span className="text-xs text-[var(--text-muted)]">suspended</span>
        )}
        <span
          className="rounded-full px-2 py-0.5 text-xs font-semibold"
          style={{
            backgroundColor: `var(${cronjob.enabled ? "--tint-healthy" : "--tint-unknown"})`,
            color: `var(${cronjob.enabled ? "--text-on-healthy" : "--text-on-unknown"})`,
          }}
          title="The `enabled` flag for this job in the deployment's values.yaml"
        >
          enabled: {String(cronjob.enabled)}
        </span>
        <button
          type="button"
          disabled={cronjob.active || !cronjob.deployed}
          title={
            !cronjob.deployed
              ? "Not deployed: turn it on in values.yaml first"
              : cronjob.active
                ? "A Job of this CronJob is already running"
                : undefined
          }
          onClick={onRun}
          className={`${BUTTON} ml-auto bg-[var(--color-status-info)] text-white hover:brightness-110`}
        >
          Run job
        </button>
      </div>
      {cronjob.jobs.length === 0 ? (
        <p className="mt-3 text-sm text-[var(--text-secondary)]">
          {cronjob.deployed
            ? "No Jobs yet."
            : "Not deployed: it is switched off in values.yaml."}
        </p>
      ) : (
        <ul className="mt-3 divide-y divide-[var(--border-subtle)]">
          {cronjob.jobs.map((job) => (
            <JobRow
              key={job.name}
              cronjob={cronjob.name}
              job={job}
              selected={selected}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

/** Whether the selected pod's Job is still running, read from the latest overview. */
function isUnfinished(
  cronjobs: CronJob[] | undefined,
  selected: Selected,
): boolean {
  const job = cronjobs
    ?.find((c) => c.name === selected.cronjob)
    ?.jobs.find((j) => j.name === selected.job);
  return (
    job !== undefined && (job.state === "active" || job.state === "pending")
  );
}

/** `/jobs`: run a collector or prune CronJob by hand and read its pods' logs (ADR-0055). */
export function JobsPage() {
  const queryClient = useQueryClient();
  const [asking, setAsking] = useState<CronJob | null>(null);
  const [selected, setSelected] = useState<Selected | null>(null);

  const jobs = useQuery({
    queryKey: queryKeys.jobs.all,
    queryFn: getJobs,
    refetchInterval: (query) =>
      query.state.data?.cronjobs.some((c) => c.active)
        ? LIST_POLL_MS
        : LIST_POLL_MS * 6,
  });
  const run = useMutation({
    mutationFn: (name: string) => runJob(name),
    onSuccess: (job, name) => {
      setAsking(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.jobs.all });
      const pod = job.pods[0];
      if (pod) setSelected({ cronjob: name, job: job.name, pod: pod.name });
    },
  });

  return (
    <main className="mx-auto max-w-5xl p-8">
      <h1 className="text-3xl font-semibold tracking-[-0.02em] text-[var(--text-primary)]">
        Jobs
      </h1>
      <p className="mt-2 max-w-2xl text-sm text-[var(--text-secondary)]">
        Start a collector or prune CronJob now, like{" "}
        <span className="font-mono">oc create job --from</span>, and read its
        logs. Every run is recorded in the audit trail.
        {jobs.data?.backend === "fake" &&
          " This deployment uses simulated jobs."}
      </p>

      {jobs.isError && (
        <p role="alert" className="mt-6 text-sm text-[var(--text-on-critical)]">
          {message(jobs.error)}
        </p>
      )}
      {jobs.data && !jobs.data.enabled && (
        <p className="mt-6 text-sm text-[var(--text-secondary)]">
          Running jobs from the UI is not enabled on this deployment.
        </p>
      )}
      {jobs.data?.enabled && (
        <div className="mt-6 flex flex-col gap-4">
          {jobs.data.cronjobs.length === 0 && (
            <p className="text-sm text-[var(--text-secondary)]">
              No triggerable CronJobs found.
            </p>
          )}
          {jobs.data.cronjobs.map((cronjob) => (
            <CronJobCard
              key={cronjob.name}
              cronjob={cronjob}
              selected={selected}
              onSelect={setSelected}
              onRun={() => {
                run.reset();
                setAsking(cronjob);
              }}
            />
          ))}
        </div>
      )}

      {selected && (
        <LogPane
          key={selected.pod}
          selected={selected}
          active={isUnfinished(jobs.data?.cronjobs, selected)}
          onClose={() => {
            setSelected(null);
          }}
        />
      )}

      {asking && (
        <ConfirmRun
          cronjob={asking}
          error={run.error}
          pending={run.isPending}
          onCancel={() => {
            setAsking(null);
          }}
          onConfirm={() => {
            run.mutate(asking.name);
          }}
        />
      )}
    </main>
  );
}
