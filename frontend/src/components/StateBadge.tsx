import { SEVERITY_GLYPH } from "@/components/severity";
import { formatAge, formatTimestamp } from "@/lib/datetime";
import { isDonor } from "@/lib/donor";
import type { HealthSeverity, MaintenanceState } from "@/types/server";

/**
 * Health severity plus chips shown alongside rather than replacing it:
 * maintenance ("critical, and someone is on it" and "in maintenance,
 * otherwise fine" must not render the same), stale (a HEALTHY badge on
 * a server nothing has reached for 14 hours is a claim about the past, and
 * the chip says so — ADR-0029), and a name mismatch (OpenShift reports this
 * server under a hostname the vendor manager no longer uses — ADR-0036).
 * Every hue sits outside the severity set so none reads as an extra severity.
 */

interface SeverityStyle {
  label: string;
  glyph: string;
  className: string;
}

const SEVERITIES: Record<HealthSeverity, SeverityStyle> = {
  CRITICAL: {
    label: "Critical",
    glyph: SEVERITY_GLYPH.CRITICAL,
    className: "bg-[var(--tint-critical)] text-[var(--text-on-critical)]",
  },
  MAJOR: {
    label: "Major",
    glyph: SEVERITY_GLYPH.MAJOR,
    className: "bg-[var(--tint-major)] text-[var(--text-on-major)]",
  },
  WARNING: {
    label: "Warning",
    glyph: SEVERITY_GLYPH.WARNING,
    className: "bg-[var(--tint-warning)] text-[var(--text-on-warning)]",
  },
  HEALTHY: {
    label: "Healthy",
    glyph: SEVERITY_GLYPH.HEALTHY,
    className: "bg-[var(--tint-healthy)] text-[var(--text-on-healthy)]",
  },
  UNKNOWN: {
    label: "Unknown",
    glyph: SEVERITY_GLYPH.UNKNOWN,
    className: "bg-[var(--tint-unknown)] text-[var(--text-on-unknown)]",
  },
};

export function StateBadge({
  severity,
  maintenance,
  stale = false,
  lastSeenAt = null,
  reportedName = null,
}: {
  severity: HealthSeverity;
  maintenance: MaintenanceState;
  /** `ServerRow.stale`; renders the chip. */
  stale?: boolean;
  /** Gives the chip its age (`20h`); null means never collected. */
  lastSeenAt?: string | null;
  /** `ServerRow.openshift_reported_name`; non-null renders the chip (ADR-0036). */
  reportedName?: string | null;
}) {
  const style = SEVERITIES[severity];

  return (
    <span className="inline-flex flex-wrap items-center justify-center gap-1.5">
      <span
        className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ${style.className}`}
      >
        <span aria-hidden="true" className="text-[0.7em] leading-none">
          {style.glyph}
        </span>
        {style.label}
      </span>
      {maintenance.enabled && (
        <span
          title={maintenance.reason ?? undefined}
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ${
            isDonor(maintenance)
              ? "bg-[var(--tint-donor)] text-[var(--text-on-donor)]"
              : "bg-[var(--tint-maintenance)] text-[var(--text-on-maintenance)]"
          }`}
        >
          <span aria-hidden="true" className="text-[0.7em] leading-none">
            {isDonor(maintenance) ? "◇" : "⏸"}
          </span>
          {isDonor(maintenance) ? "Donor" : "Maint"}
        </span>
      )}
      {stale && (
        <span
          title={
            lastSeenAt
              ? `Last seen ${formatTimestamp(lastSeenAt)}`
              : "Never successfully collected"
          }
          className="inline-flex items-center rounded-full bg-[var(--tint-unknown)] px-2 py-0.5 text-xs font-medium whitespace-nowrap text-[var(--text-on-unknown)]"
        >
          Stale{lastSeenAt ? ` ${formatAge(lastSeenAt)}` : ""}
        </span>
      )}
      {reportedName && (
        <span
          title={`OpenShift reports this node as "${reportedName}"`}
          className="inline-flex items-center rounded-full bg-[var(--tint-warning)] px-2 py-0.5 text-xs font-medium whitespace-nowrap text-[var(--text-on-warning)]"
        >
          Name mismatch
        </span>
      )}
    </span>
  );
}
