import type { ClassificationRuleListParams } from "@/api/classificationRules";
import type { EventListParams } from "@/api/events";
import type { HealthPolicyListParams } from "@/api/healthPolicies";

/** Central TanStack Query key factory, so invalidation can target a whole
 * resource or one slice of it. */
export const queryKeys = {
  auth: {
    all: ["auth"] as const,
    me: () => [...queryKeys.auth.all, "me"] as const,
  },
  servers: {
    all: ["servers"] as const,
    rows: () => [...queryKeys.servers.all, "rows"] as const,
    details: () => [...queryKeys.servers.all, "detail"] as const,
    detail: (id: string) => [...queryKeys.servers.details(), id] as const,
  },
  classificationRules: {
    all: ["classificationRules"] as const,
    lists: () => [...queryKeys.classificationRules.all, "list"] as const,
    list: (params: ClassificationRuleListParams) =>
      [...queryKeys.classificationRules.lists(), params] as const,
  },
  healthPolicies: {
    all: ["healthPolicies"] as const,
    lists: () => [...queryKeys.healthPolicies.all, "list"] as const,
    list: (params: HealthPolicyListParams) => [...queryKeys.healthPolicies.lists(), params] as const,
  },
  healthMetrics: {
    all: ["healthMetrics"] as const,
    list: () => [...queryKeys.healthMetrics.all, "list"] as const,
  },
  sites: {
    all: ["sites"] as const,
    list: () => [...queryKeys.sites.all, "list"] as const,
  },
  events: {
    all: ["events"] as const,
    lists: () => [...queryKeys.events.all, "list"] as const,
    list: (params: EventListParams) => [...queryKeys.events.lists(), params] as const,
  },
};
