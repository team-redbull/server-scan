import type { ReactNode } from "react";

import { Badge } from "@/components/Badge";
import { HealthBadge } from "@/components/HealthBadge";
import { InstallationBadge } from "@/components/InstallationBadge";
import { ReservationBadge } from "@/components/ReservationBadge";
import { actorIdLabel } from "@/lib/actor";
import { formatRelative, formatTimestamp } from "@/lib/datetime";
import { inferredGpuModel } from "@/lib/gpuModel";
import type {
  HealthSummary,
  ServerDetail,
  UnreachableReason,
} from "@/types/server";

interface OverviewTabProps {
  server: ServerDetail;
}

/** Each vendor's own name for a profile template. `REDFISH_STANDALONE` is
 * absent on purpose: a bare BMC has no template concept, so no row renders. */
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
    <div className="grid grid-cols-1 gap-x-8 gap-y-4 sm:grid-cols-2">
      {/* Two explicit columns, not one auto-flowing grid: a field that only
          some vendors have (the template row below, "Collection" when
          unreachable) must never reflow every field after it into the
          other column — a standalone server was one field short and so
          showed a visibly different layout from every other vendor. */}
      <dl className="flex flex-col gap-4">
        <Field label="Name" value={server.name} />
        <Field label="Model" value={<ModelValue server={server} />} />
        <Field label="Serial" value={server.identity?.serial ?? "—"} />
        <Field label="Manager" value={server.manager_id ?? "—"} />
        <Field label="OpenShift" value={<OpenShiftValue server={server} />} />
        <Field
          label="Health breakdown"
          value={
            <HealthBreakdown
              health={server.health}
              sourceProvider={server.source_provider}
            />
          }
        />
        {/* Relative so staleness reads at a glance; the exact instant is the hover. */}
        <Field
          label="Last seen"
          value={
            server.last_seen_at ? (
              <span
                className="inline-flex items-center gap-2"
                title={formatTimestamp(server.last_seen_at)}
              >
                {formatRelative(server.last_seen_at)}
                {server.stale && <Badge tone="warning">Stale</Badge>}
              </span>
            ) : (
              <Badge tone="warning">Never collected</Badge>
            )
          }
        />
      </dl>
      <dl className="flex flex-col gap-4">
        <Field label="Vendor" value={server.identity?.vendor ?? "unknown"} />
        {profileTemplateLabel && (
          <Field
            label={profileTemplateLabel}
            value={server.profile_template.name ?? "—"}
          />
        )}
        <Field label="Site" value={server.site_id ?? "—"} />
        <Field
          label="Classification"
          value={<Badge>{server.classification.installation_type}</Badge>}
        />
        {server.classification.matched_rule_name && (
          <Field
            label="Matched rule"
            value={`${server.classification.matched_rule_name}${
              server.classification.matched_field &&
              server.classification.matched_pattern
                ? ` (${server.classification.matched_field} ~ ${server.classification.matched_pattern})`
                : ""
            }`}
          />
        )}
        <Field
          label="Overall health"
          value={<HealthBadge severity={server.health.overall} />}
        />
        {/* Read-only: maintenance is switched from the inventory list. */}
        <Field
          label="Maintenance"
          value={
            server.maintenance.enabled ? (
              <Badge tone="warning">
                {server.maintenance.reason ?? "Enabled"}
                {server.maintenance.created_by
                  ? ` - by ${actorIdLabel(server.maintenance.created_by)}`
                  : ""}
              </Badge>
            ) : (
              <span className="text-[var(--text-secondary)]">
                Not in maintenance
              </span>
            )
          }
        />
        {!server.reachable && (
          <Field
            label="Collection"
            value={
              <Badge tone="warning">
                {server.unreachable_reason
                  ? UNREACHABLE_LABELS[server.unreachable_reason]
                  : "Unreachable"}
                {server.unreachable_since
                  ? ` since ${formatTimestamp(server.unreachable_since)}`
                  : ""}
              </Badge>
            }
          />
        )}
        <Field label="Last changed" value={formatTimestamp(server.updated_at)} />
      </dl>
    </div>
  );
}

/** Falls back to a short GPU-derived hint when the chassis itself never
 * reported a Model — never written back to `server.model` itself. */
function ModelValue({ server }: { server: ServerDetail }) {
  if (server.model) return <>{server.model}</>;

  const inferred = inferredGpuModel(server.hardware.gpus);
  if (!inferred) return <>—</>;

  return (
    <span className="inline-flex items-center gap-1.5">
      {inferred}
      <span className="text-xs text-[var(--text-secondary)]">(from GPU)</span>
    </span>
  );
}

/** OpenShift's own report, shown beside Classification and never falling
 * back to it: the two disagreeing is how a misnamed server is noticed. Carries
 * the install lock too, when one is held — ADR-0035. */
function OpenShiftValue({ server }: { server: ServerDetail }) {
  const { lifecycle_state, cluster_name, mce_name, reported_name } =
    server.openshift;
  const { contested_with, contested_name } = server.openshift;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <InstallationBadge state={lifecycle_state} full />
        {cluster_name && <span className="font-medium">{cluster_name}</span>}
        {/* Beside the state, not instead of it: a mid-install server is still
            AVAILABLE to the collectors. Renders nothing unless a lock is held. */}
        <ReservationBadge reservation={server.reservation} full />
      </div>
      <span className="text-xs text-[var(--text-secondary)]">
        {mce_name ? `MCE ${mce_name}` : "—"}
      </span>
      {/* A vendor-side rename after install (ADR-0036): the hardware is the
          same server, but the name it was installed under has since changed. */}
      {reported_name && (
        <span className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
          <Badge tone="warning">Name mismatch</Badge> Installed as{" "}
          <span className="font-medium">{reported_name}</span>
        </span>
      )}
      {/* Two jobs keep claiming this server (ADR-0041): name the other one,
          and the hostname it knows the server by when that differs. */}
      {contested_with && (
        <span className="flex flex-wrap items-center gap-2 text-xs text-[var(--text-secondary)]">
          <Badge tone="warning">Duplicate server</Badge> Also claimed by{" "}
          <span className="font-medium">{contested_with}</span>
          {contested_name && (
            <>
              {" "}as <span className="font-medium">{contested_name}</span>
            </>
          )}
        </span>
      )}
    </div>
  );
}

const UNREACHABLE_LABELS: Record<UnreachableReason, string> = {
  network_unreachable: "Unreachable",
  auth_rejected: "Wrong credentials",
  tls_error: "TLS error",
  timeout: "Timed out",
  protocol_error: "Protocol error",
};

/** The categories behind "Overall health"; `overall` has its own field. */
const HEALTH_CATEGORIES: {
  key: keyof Omit<HealthSummary, "overall">;
  label: string;
}[] = [
  { key: "memory", label: "Memory" },
  { key: "storage", label: "Storage" },
  { key: "network", label: "Network" },
  { key: "connectivity", label: "Connectivity" },
  { key: "power", label: "Power" },
  { key: "gpu", label: "GPU" },
  { key: "bmc", label: "BMC" },
];

/** Cisco servers sit behind the fabric interconnects and are never probed, so
 * their BMC row could only ever say "Unknown". Dell, HP and standalone keep it:
 * there an Unknown means the check ran and got no answer (ADR-0037). */
const BMC_UNCHECKED_PROVIDERS = new Set([
  "UCS_CENTRAL",
  "UCS_MANAGER",
  "INTERSIGHT",
]);

function HealthBreakdown({
  health,
  sourceProvider,
}: {
  health: HealthSummary;
  sourceProvider: string | null;
}) {
  const categories = HEALTH_CATEGORIES.filter(
    ({ key }) =>
      key !== "bmc" || !BMC_UNCHECKED_PROVIDERS.has(sourceProvider ?? ""),
  );
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1.5">
      {categories.map(({ key, label }) => (
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
      <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">
        {label}
      </dt>
      <dd className="mt-1 text-sm">{value}</dd>
    </div>
  );
}
