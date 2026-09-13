/** Hand-written mirror of `/api/v1/events` (`events_schemas.py` is
 * authoritative). */

export type ActorType = "SYSTEM" | "USER" | "TOKEN";

export interface Actor {
  type: ActorType;
  id: string;
  display: string | null;
}

/** A UI-side subset: `event_type` on the wire is a free string with more
 * members than these. */
export type ClassificationEventType =
  | "CLASSIFICATION_RULE_CREATED"
  | "CLASSIFICATION_RULE_UPDATED"
  | "CLASSIFICATION_RULE_DELETED";

export type HealthPolicyEventType =
  | "HEALTH_POLICY_CREATED"
  | "HEALTH_POLICY_UPDATED"
  | "HEALTH_POLICY_DISABLED"
  | "HEALTH_POLICY_DELETED";

export const CLASSIFICATION_EVENT_TYPES: ClassificationEventType[] = [
  "CLASSIFICATION_RULE_CREATED",
  "CLASSIFICATION_RULE_UPDATED",
  "CLASSIFICATION_RULE_DELETED",
];

export const HEALTH_POLICY_EVENT_TYPES: HealthPolicyEventType[] = [
  "HEALTH_POLICY_CREATED",
  "HEALTH_POLICY_UPDATED",
  "HEALTH_POLICY_DISABLED",
  "HEALTH_POLICY_DELETED",
];

export interface AuditEventResponse {
  id: string;
  event_type: string;
  server_id: string | null;
  actor: Actor;
  request_id: string | null;
  created_at: string;
  data: Record<string, unknown>;
}

export interface EventPageInfo {
  next_cursor: string | null;
  has_more: boolean;
  page_size: number;
}

export interface AuditEventListResponse {
  items: AuditEventResponse[];
  page: EventPageInfo;
}
