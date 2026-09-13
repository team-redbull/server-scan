import { apiFetch } from "@/api/client";
import type { ClassificationRuleListResponse } from "@/types/classification";

const BASE = "/api/v1/classification-rules";

export interface ClassificationRuleListParams {
  enabled?: boolean;
}

/** List the classification rules. Read-only on purpose: rules ship with
 * the platform (docs/architecture.md, "Slice 5"). */
export function listClassificationRules(
  params: ClassificationRuleListParams = {},
): Promise<ClassificationRuleListResponse> {
  const query = new URLSearchParams();
  if (params.enabled !== undefined) {
    query.set("enabled", String(params.enabled));
  }
  const qs = query.toString();
  return apiFetch<ClassificationRuleListResponse>(qs ? `${BASE}?${qs}` : BASE);
}
