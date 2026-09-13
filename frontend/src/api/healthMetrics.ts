import { apiFetch } from "@/api/client";
import type { HealthMetricListResponse } from "@/types/health";

/** The metric registry; a small static-per-deploy list. */
export function listHealthMetrics(): Promise<HealthMetricListResponse> {
  return apiFetch<HealthMetricListResponse>("/api/v1/health-metrics");
}
