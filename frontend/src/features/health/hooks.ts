import { useQuery } from "@tanstack/react-query";

import type { HealthPolicyListParams } from "@/api/healthPolicies";
import { listHealthPolicies } from "@/api/healthPolicies";
import { queryKeys } from "@/api/queryKeys";

/** Read-only by design; policies ship with the platform. */
export function useHealthPoliciesQuery(params: HealthPolicyListParams = {}) {
  return useQuery({
    queryKey: queryKeys.healthPolicies.list(params),
    queryFn: () => listHealthPolicies(params),
  });
}
