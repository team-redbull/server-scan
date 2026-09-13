/**
 * Hand-written types mirroring the backend's `/api/v1/classification-rules`
 * JSON shapes (see `backend/app/api/v1/classification_schemas.py`, which is
 * authoritative). Same "decoupled from the backend" convention as
 * `types/server.ts` — this only carries what the UI actually consumes.
 */

import type { InstallationType, Vendor } from "@/types/server";

export type ManagerType =
  | "OPENMANAGE"
  | "UCS_MANAGER"
  | "UCS_CENTRAL"
  | "INTERSIGHT"
  | "ONEVIEW"
  | "REDFISH_STANDALONE";

/** Every `ManagerType`, as a person names it. Vendor-free on purpose, so a
 * label can sit beside its vendor without repeating it. */
export const MANAGER_TYPE_LABELS: Record<ManagerType, string> = {
  UCS_CENTRAL: "UCS Central",
  UCS_MANAGER: "UCS Manager",
  INTERSIGHT: "Intersight",
  OPENMANAGE: "OpenManage",
  ONEVIEW: "OneView",
  REDFISH_STANDALONE: "Redfish standalone",
};

export type RuleSource =
  | "SITE_CUSTOM"
  | "MANAGER_CUSTOM"
  | "VENDOR_CUSTOM"
  | "GLOBAL_CUSTOM"
  | "SYSTEM_DEFAULT";

/** The full set of valid sources. `SYSTEM_DEFAULT` rules can never be
 * created through the API (only seeded), so the create/edit form's source
 * picker uses `RULE_SOURCES_FOR_CREATE` instead of this. */
export const RULE_SOURCES: RuleSource[] = [
  "SITE_CUSTOM",
  "MANAGER_CUSTOM",
  "VENDOR_CUSTOM",
  "GLOBAL_CUSTOM",
  "SYSTEM_DEFAULT",
];

export const RULE_SOURCES_FOR_CREATE: RuleSource[] = [
  "SITE_CUSTOM",
  "MANAGER_CUSTOM",
  "VENDOR_CUSTOM",
  "GLOBAL_CUSTOM",
];

/** `priority` must fall in its `source`'s band (validated authoritatively
 * server-side; this is a client-side hint only). Mirrors
 * `app.domain.models.classification_rule.PRIORITY_BANDS` exactly. */
export const PRIORITY_BANDS: Record<RuleSource, { low: number; high: number }> = {
  SITE_CUSTOM: { low: 500, high: 599 },
  MANAGER_CUSTOM: { low: 400, high: 499 },
  VENDOR_CUSTOM: { low: 300, high: 399 },
  GLOBAL_CUSTOM: { low: 200, high: 299 },
  SYSTEM_DEFAULT: { low: 100, high: 199 },
};

/** Mirrors `app.domain.models.classification_rule.CLASSIFIABLE_FIELDS` —
 * there is no API to fetch this list, it is hardcoded there and here. */
export const CLASSIFIABLE_FIELDS = ["name", "hostname", "serial", "model", "site_id"] as const;
export type ClassifiableField = (typeof CLASSIFIABLE_FIELDS)[number];

export interface RuleScope {
  vendor: Vendor | null;
  manager_type: ManagerType | null;
  site_id: string | null;
}

export function emptyRuleScope(): RuleScope {
  return { vendor: null, manager_type: null, site_id: null };
}

export interface RuleFlags {
  ignore_case: boolean;
  multiline: boolean;
  dotall: boolean;
}

export function defaultRuleFlags(): RuleFlags {
  return { ignore_case: true, multiline: false, dotall: false };
}

export interface RuleStats {
  last_matched_at: string | null;
  timeout_count: number;
  quarantined: boolean;
}

export interface ClassificationRuleResponse {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  system: boolean;
  installation_type: InstallationType;
  scope: RuleScope;
  field: string;
  pattern: string;
  flags: RuleFlags;
  source: RuleSource;
  priority: number;
  order: number;
  stats: RuleStats;
  revision: number;
  created_at: string;
  updated_at: string;
  created_by: string | null;
  updated_by: string | null;
}

export interface ClassificationRuleListResponse {
  items: ClassificationRuleResponse[];
}

