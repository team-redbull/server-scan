import type { ReactNode } from "react";

import { Badge } from "@/components/Badge";
import { HealthBadge } from "@/components/HealthBadge";
import { InstallationBadge } from "@/components/InstallationBadge";
import { formatTimestamp } from "@/lib/datetime";
import type { HealthSummary, ServerDetail } from "@/types/server";

interface OverviewTabProps {
  server: ServerDetail;
}

/** What each collector's own vendor calls this concept — Cisco UCS's
 * "Service Profile Template" is not HPE's or Cisco Intersight's "Server
 * Profile Template" is not Dell's "Deployment Template", even though
 * `ProfileTemplate` stores all three the same way. `REDFISH_STANDALONE`
 * is deliberately absent: a bare BMC has no template concept to name, so
 * that row simply does not render for a standalone server rather than
 * showing a label for a thing that was never possible to have.
 */
const PROFILE_TEMPLATE_LABELS: Record<string, string> = {
  UCS_CENTRAL: "Service profile template",
  INTERSIGHT: "Server profile template",
  ONEVIEW: "Server profile template",
  OPENMANAGE: "Deployment template",
};

export function OverviewTab({ server }: OverviewTabProps) {
  const profileTemplateLabel = server.source_provider
    ? PROFILE_TEMPLATE_LABELS[server.source_provider]
    : undefined;

  return (
    <dl className="grid grid-cols-1 gap-x-8 gap-y-4 sm:grid-cols-2">
      <Field label="Name" value={server.name} />
      <Field label="Vendor" value={server.identity?.vendor ?? "unknown"} />
      <Field label="Model" value={server.model ?? "—"} />
      {profileTemplateLabel && (
        <Field label={profileTemplateLabel} value={server.profile_template.name ?? "—"} />
      )}
      <Field label="Serial" value={server.identity?.serial ?? "—"} />
      <Field label="Site" value={server.site_id ?? "—"} />
      <Field label="Manager" value={server.manager_id ?? "—"} />
      <Field label="Classification" value={<Badge>{server.classification.installation_type}</Badge>} />
      <Field label="OpenShift" value={<OpenShiftValue server={server} />} />
      <Field label="Overall health" value={<HealthBadge severity={server.health.overall} />} />
      <Field label="Health breakdown" value={<HealthBreakdown health={server.health} />} />
      {/* Read-only on purpose: maintenance is switched from the inventory
          list, where the operator can see the whole fleet. This only says
          whether it is on, and why. */}
      <Field
        label="Maintenance"
        value={
          server.maintenance.enabled ? (
            <Badge tone="warning">{server.maintenance.reason ?? "Enabled"}</Badge>
          ) : (
            <span className="text-[var(--text-secondary)]">Not in maintenance</span>
          )
        }
      />
      {!server.reachable && (
        <Field
          label="Collection"
          value={
            <Badge tone="warning">
              Unreachable
              {server.unreachable_since ? ` since ${formatTimestamp(server.unreachable_since)}` : ""}
            </Badge>
          }
        />
      )}
      <Field
        label="Last seen"
        value={server.last_seen_at ? formatTimestamp(server.last_seen_at) : "—"}
      />
      <Field label="Updated" value={formatTimestamp(server.updated_at)} />
    </dl>
  );
}

/** Whether a server is in use, according to OpenShift.
 *
 * Deliberately shown next to Classification rather than merged with it.
 * Classification is a regex verdict on the hostname; this is a cluster or
 * an MCE reporting what it actually holds. When the two disagree the
 * server is misnamed or misplaced, and seeing both is the only way to
 * notice — so this never falls back to the classification. */
function OpenShiftValue({ server }: { server: ServerDetail }) {
  const { lifecycle_state, cluster_name, mce_name } = server.openshift;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <InstallationBadge state={lifecycle_state} full />
        {cluster_name && <span className="font-medium">{cluster_name}</span>}
      </div>
      <span className="text-xs text-[var(--text-secondary)]">
        {mce_name ? `MCE ${mce_name}` : "—"}
      </span>
    </div>
  );
}

/** The six categories behind "Overall health" — `overall` is shown on its
 * own field above and omitted here. Without this, the page opened to
 * answer "why is this unhealthy" only ever said the overall verdict, never
 * which subsystem earned it, even though the API sends all seven on every
 * request. */
const HEALTH_CATEGORIES: { key: keyof Omit<HealthSummary, "overall">; label: string }[] = [
  { key: "cpu", label: "CPU" },
  { key: "memory", label: "Memory" },
  { key: "storage", label: "Storage" },
  { key: "network", label: "Network" },
  { key: "connectivity", label: "Connectivity" },
  { key: "power", label: "Power" },
  { key: "gpu", label: "GPU" },
];

function HealthBreakdown({ health }: { health: HealthSummary }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1.5">
      {HEALTH_CATEGORIES.map(({ key, label }) => (
        <span key={key} className="inline-flex items-center gap-1.5 text-xs">
          <span className="text-gray-500">{label}</span>
          <HealthBadge severity={health[key]} />
        </span>
      ))}
    </div>
  );
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">{label}</dt>
      <dd className="mt-1 text-sm">{value}</dd>
    </div>
  );
}
