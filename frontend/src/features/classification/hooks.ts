import { useQuery } from "@tanstack/react-query";

import type { ClassificationRuleListParams } from "@/api/classificationRules";
import { listClassificationRules } from "@/api/classificationRules";
import { queryKeys } from "@/api/queryKeys";

/** Read-only by design; rules ship with the platform. */
export function useClassificationRulesQuery(params: ClassificationRuleListParams = {}) {
  return useQuery({
    queryKey: queryKeys.classificationRules.list(params),
    queryFn: () => listClassificationRules(params),
  });
}
