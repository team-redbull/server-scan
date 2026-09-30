import { SEVERITY_ORDER } from "@/components/severity";
import { actorLabel } from "@/lib/actor";
import { isDonor } from "@/lib/donor";
import type { AuditEventResponse } from "@/types/events";

/** The `event_type`s the backend actually writes, in filter-menu order. */
export const WRITTEN_EVENT_TYPES = [
  "SERVER_CREATED",
  "SERVER_PRUNED",
  "HEALTH_CHANGED",
  "CLASSIFICATION_CHANGED",
  "OPENSHIFT_STATE_CHANGED",
  "MAINTENANCE_ENABLED",
  "MAINTENANCE_UPDATED",
  "MAINTENANCE_DISABLED",
  "SERVER_RESERVED",
  "SERVER_RESERVATION_REFUSED",
  "SERVER_RELEASED",
] as const;

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** " (x)" when `value` is a non-empty string, else "". */
function paren(value: unknown): string {
  const s = str(value);
  return s ? ` (${s})` : "";
}

function change(label: string, data: Record<string, unknown>): string {
  return `${label} ${str(data.from) ?? "?"} -> ${str(data.to) ?? "?"}`;
}

export interface HealthReason {
  policy_key: string | null;
  policy_name: string | null;
  category: string | null;
  severity: string | null;
  message: string | null;
}

/** `data[key]` as reasons, or null when it is not an array (an absent side). */
function reasonsAt(
  data: Record<string, unknown>,
  key: string,
): HealthReason[] | null {
  const v = data[key];
  if (!Array.isArray(v)) return null;
  return v
    .filter(
      (x): x is Record<string, unknown> => typeof x === "object" && x !== null,
    )
    .map((r) => ({
      policy_key: str(r.policy_key),
      policy_name: str(r.policy_name),
      category: str(r.category),
      severity: str(r.severity),
      message: str(r.message),
    }));
}

/** The reasons behind each side of a HEALTH_CHANGED event; a side the
 * event did not record is null. Older events carried only `reasons` (the TO side). */
export function healthSections(data: Record<string, unknown>): {
  from: HealthReason[] | null;
  to: HealthReason[] | null;
} {
  return {
    from: reasonsAt(data, "from_reasons"),
    to: reasonsAt(data, "to_reasons") ?? reasonsAt(data, "reasons"),
  };
}

/** The single most severe reason; on a tie, the first one recorded. `null` when empty. */
export function topReason(reasons: HealthReason[]): HealthReason | null {
  const rank = (r: HealthReason): number => {
    const i = SEVERITY_ORDER.findIndex((s) => s === r.severity);
    return i === -1 ? SEVERITY_ORDER.length : i;
  };
  let best: HealthReason | null = null;
  for (const r of reasons) {
    if (best === null || rank(r) < rank(best)) best = r;
  }
  return best;
}

/** "FROM → TO" for a status-change event; "?" for a missing side. */
export function transition(d: Record<string, unknown>): string {
  return `${str(d.from) ?? "?"} → ${str(d.to) ?? "?"}`;
}

/** One human sentence for an audit event, from the `data` keys its writer
 * records (and, for maintenance, who did it); an unknown `event_type` falls back to the raw type name. */
export function describeEvent(event: AuditEventResponse): string {
  const d = event.data;
  switch (event.event_type) {
    case "SERVER_CREATED":
      return "Server discovered";
    case "SERVER_PRUNED":
      return "Pruned: no longer listed by its manager";
    case "HEALTH_CHANGED":
      return transition(d);
    case "CLASSIFICATION_CHANGED":
      return change("Classification", d);
    case "OPENSHIFT_STATE_CHANGED":
      return `${change("OpenShift state", d)}${paren(d.cluster_name)}`;
    case "MAINTENANCE_ENABLED":
    case "MAINTENANCE_UPDATED": {
      const reason = str(d.reason);
      const prefix =
        event.event_type === "MAINTENANCE_ENABLED" ? "Put into" : "Updated";
      const by = ` by ${actorLabel(event.actor).label}`;
      if (isDonor({ enabled: true, reason })) {
        return `Marked as parts donor${by}: ${reason ?? ""}`;
      }
      return `${prefix} maintenance${by}${reason ? `: ${reason}` : ""}`;
    }
    case "MAINTENANCE_DISABLED":
      return "Maintenance ended";
    case "SERVER_RESERVED":
      return `Reserved by ${str(d.holder) ?? "?"}${str(d.mce_cluster) ? ` for ${str(d.mce_cluster)}` : ""}`;
    case "SERVER_RESERVATION_REFUSED": {
      const why =
        str(d.reason) ?? (str(d.held_by) ? `held by ${str(d.held_by)}` : null);
      return `Reservation by ${str(d.requested_by) ?? "?"} refused${why ? `: ${why}` : ""}`;
    }
    case "SERVER_RELEASED":
      return `Released from ${str(d.holder) ?? "?"}${paren(d.mce_cluster)}`;
    default:
      return event.event_type;
  }
}

const LEGACY_DATA_KEYS = ["ticket", "matched_rule_id"];

/** The event's data minus legacy keys (removed ticket, internal rule id); the stored event is untouched. */
export function visibleData(
  event: AuditEventResponse,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(event.data).filter(([k]) => !LEGACY_DATA_KEYS.includes(k)),
  );
}

/** The readable rule of a CLASSIFICATION_CHANGED event; null for a legacy or unmatched one. */
export function matchedRule(
  data: Record<string, unknown>,
): { name: string; on: string | null } | null {
  const name = str(data.matched_rule);
  if (name === null) return null;
  const field = str(data.matched_field);
  const pattern = str(data.matched_pattern);
  return { name, on: field && pattern ? `${field} ~ ${pattern}` : null };
}
