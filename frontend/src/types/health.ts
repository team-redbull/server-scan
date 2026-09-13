/**
 * Hand-written types mirroring the backend's `/api/v1/health-policies` and
 * `/api/v1/health-metrics` JSON shapes (see
 * `backend/app/api/v1/health_policy_schemas.py`, authoritative).
 *
 * `Condition` is intentionally a single flat interface with every field
 * optional/nullable rather than a discriminated union keyed on which field
 * is present: the backend's Pydantic model dumps *every* field on every
 * node (`all_of`, `any_of`, `not`, `metric`, `operator`, `value`, `equals`),
 * with the unused ones set to `null` rather than omitted (confirmed against
 * a live `GET /health-policies` response) — so "does the key exist" is not
 * a valid discriminator over wire data, only "is it non-null". The
 * `isLeaf`/`isAllOf`/`isAnyOf`/`isNot` guards below check nullness, not
 * key presence, for exactly that reason.
 */

import type { ManagerType, RuleSource } from "@/types/classification";
import type { HealthSeverity } from "@/types/server";

export type MetricType = "INT" | "FLOAT" | "STRING" | "BOOL" | "ENUM" | "LIST_STRING" | "LIST_INT";

export interface HealthMetricResponse {
  name: string;
  type: MetricType;
  category: string;
  description: string;
  enum_values: string[] | null;
  provider: string;
}

export interface HealthMetricListResponse {
  items: HealthMetricResponse[];
}

export type Operator =
  | "EQ"
  | "NE"
  | "GT"
  | "GTE"
  | "LT"
  | "LTE"
  | "IN"
  | "NOT_IN"
  | "EXISTS"
  | "NOT_EXISTS"
  | "ANY"
  | "ALL"
  | "COUNT_EQ"
  | "COUNT_GT"
  | "COUNT_GTE"
  | "COUNT_LT"
  | "COUNT_LTE";

/** Mirrors `app.domain.services.health.metrics.OPERATOR_ALLOWED_TYPES`
 * exactly — there is no API to fetch this table, it's hardcoded there and
 * here. Used to filter the operator `<select>` to whatever is valid for
 * the currently-selected metric's type. */
export const OPERATOR_ALLOWED_TYPES: Record<Operator, MetricType[]> = {
  EQ: ["INT", "FLOAT", "STRING", "BOOL", "ENUM"],
  NE: ["INT", "FLOAT", "STRING", "BOOL", "ENUM"],
  GT: ["INT", "FLOAT"],
  GTE: ["INT", "FLOAT"],
  LT: ["INT", "FLOAT"],
  LTE: ["INT", "FLOAT"],
  IN: ["INT", "FLOAT", "STRING", "ENUM"],
  NOT_IN: ["INT", "FLOAT", "STRING", "ENUM"],
  EXISTS: ["INT", "FLOAT", "STRING", "BOOL", "ENUM", "LIST_STRING", "LIST_INT"],
  NOT_EXISTS: ["INT", "FLOAT", "STRING", "BOOL", "ENUM", "LIST_STRING", "LIST_INT"],
  ANY: ["LIST_STRING", "LIST_INT"],
  ALL: ["LIST_STRING", "LIST_INT"],
  COUNT_EQ: ["LIST_STRING", "LIST_INT"],
  COUNT_GT: ["LIST_STRING", "LIST_INT"],
  COUNT_GTE: ["LIST_STRING", "LIST_INT"],
  COUNT_LT: ["LIST_STRING", "LIST_INT"],
  COUNT_LTE: ["LIST_STRING", "LIST_INT"],
};

export const ALL_OPERATORS = Object.keys(OPERATOR_ALLOWED_TYPES) as Operator[];

export function operatorsForMetricType(type: MetricType): Operator[] {
  return ALL_OPERATORS.filter((op) => OPERATOR_ALLOWED_TYPES[op].includes(type));
}

export const EXISTENCE_OPERATORS = new Set<Operator>(["EXISTS", "NOT_EXISTS"]);
export const COUNT_OPERATORS = new Set<Operator>([
  "COUNT_EQ",
  "COUNT_GT",
  "COUNT_GTE",
  "COUNT_LT",
  "COUNT_LTE",
]);
export const SET_OPERATORS = new Set<Operator>(["IN", "NOT_IN"]);
export const LIST_ELEMENT_OPERATORS = new Set<Operator>(["ANY", "ALL"]);

/** A single condition tree node. See module docstring for why every field
 * is optional/nullable rather than a discriminated union. */
export interface Condition {
  all_of?: Condition[] | null;
  any_of?: Condition[] | null;
  not?: Condition | null;
  metric?: string | null;
  operator?: Operator | null;
  value?: unknown;
  equals?: unknown;
}

export function isLeaf(c: Condition): boolean {
  return c.metric != null;
}
export function isAllOf(c: Condition): c is Condition & { all_of: Condition[] } {
  return c.all_of != null;
}
export function isAnyOf(c: Condition): c is Condition & { any_of: Condition[] } {
  return c.any_of != null;
}
export function isNot(c: Condition): c is Condition & { not: Condition } {
  return c.not != null;
}

export interface EvidenceField {
  key: string;
  metric: string;
}

export interface PolicyScope {
  site_id: string | null;
  vendor: string | null;
  manager_types: ManagerType[];
}

export function emptyPolicyScope(): PolicyScope {
  return { site_id: null, vendor: null, manager_types: [] };
}

export interface PolicyStats {
  fire_count: number;
  last_fired_at: string | null;
  error_count: number;
  quarantined: boolean;
}

export type PolicyMode = "EVALUATE" | "SUPPRESS";

/** Closed set per the domain model (see the task brief / domain services) —
 * there's no API to fetch this list either. */
export const POLICY_CATEGORIES = [
  "cpu",
  "memory",
  "storage",
  "network",
  "connectivity",
  "power",
  "gpu",
] as const;
export type PolicyCategory = (typeof POLICY_CATEGORIES)[number];

export interface HealthPolicyResponse {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  system: boolean;
  policy_key: string;
  mode: string;
  category: string;
  severity: HealthSeverity;
  condition: Condition;
  evidence: EvidenceField[];
  message_template: string;
  scope: PolicyScope;
  source: RuleSource;
  priority: number;
  order: number;
  stats: PolicyStats;
  revision: number;
  created_at: string;
  updated_at: string;
  created_by: string | null;
  updated_by: string | null;
}

export interface HealthPolicyListResponse {
  items: HealthPolicyResponse[];
}

export interface HealthPolicyPreviewSample {
  id: string;
  name: string;
  would_be_severity: HealthSeverity;
}

