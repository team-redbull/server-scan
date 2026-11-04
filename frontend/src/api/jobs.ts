import { apiFetch } from "@/api/client";

export type JobState = "active" | "succeeded" | "failed" | "pending";

export interface JobPod {
  name: string;
  phase: string;
  started_at: string | null;
}

export interface JobRun {
  name: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  active: number;
  succeeded: number;
  failed: number;
  state: JobState;
  manual: boolean;
  triggered_by: string | null;
  pods: JobPod[];
}

export interface CronJob {
  name: string;
  schedule: string;
  suspend: boolean;
  last_schedule_time: string | null;
  last_successful_time: string | null;
  warning: string | null;
  active: boolean;
  enabled: boolean;
  deployed: boolean;
  jobs: JobRun[];
}

export interface JobsOverview {
  enabled: boolean;
  backend: "kubernetes" | "fake" | "off";
  namespace: string | null;
  cronjobs: CronJob[];
}

export interface PodLogs {
  pod: string;
  tail: number;
  text: string;
}

export function getJobs(): Promise<JobsOverview> {
  return apiFetch<JobsOverview>("/api/v1/jobs");
}

export function runJob(cronjob: string): Promise<JobRun> {
  return apiFetch<JobRun>(`/api/v1/jobs/${encodeURIComponent(cronjob)}/run`, {
    method: "POST",
  });
}

export function getPodLogs(
  cronjob: string,
  job: string,
  pod: string,
  tail: number,
): Promise<PodLogs> {
  const path = [cronjob, "runs", job, "pods", pod, "logs"]
    .map(encodeURIComponent)
    .join("/");
  return apiFetch<PodLogs>(`/api/v1/jobs/${path}?tail=${tail}`);
}
