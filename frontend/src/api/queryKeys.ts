import type { ClassificationRuleListParams } from "@/api/classificationRules";
import type { EventListParams } from "@/api/events";
import type { HealthPolicyListParams } from "@/api/healthPolicies";
import type { ServerListParams } from "@/api/servers";

/** Central TanStack Query key factory, so invalidation can target a whole
 * resource or one slice of it. */
export const queryKeys = {
  servers: {
    all: ["servers"] as const,
    lists: () => [...queryKeys.servers.all, "list"] as const,
    list: (params: ServerListParams) => [...queryKeys.servers.lists(), params] as const,
    // `facets` is a sibling of `lists()`, not a child: invalidating one
    // does not touch the other.
    facetsAll: () => [...queryKeys.servers.all, "facets"] as const,
    facets: (params: ServerListParams) => [...queryKeys.servers.facetsAll(), params] as const,
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
