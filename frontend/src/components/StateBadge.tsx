import { SEVERITY_GLYPH } from "@/components/severity";
import { formatAge, formatTimestamp } from "@/lib/datetime";
import type { HealthSeverity, MaintenanceState } from "@/types/server";

/**
 * Health severity plus two chips shown alongside rather than replacing it:
 * maintenance ("critical, and someone is on it" and "in maintenance,
 * otherwise fine" must not render the same) and stale (a HEALTHY badge on
 * a server nothing has reached for 14 hours is a claim about the past, and
 * the chip says so — ADR-0029). Both hues sit outside the severity set so
 * neither reads as an extra severity.
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
}: {
  severity: HealthSeverity;
  maintenance: MaintenanceState;
  /** `ServerSummary.stale`; renders the chip. */
  stale?: boolean;
  /** Gives the chip its age (`20h`); null means never collected. */
  lastSeenAt?: string | null;
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
          className="inline-flex items-center gap-1 rounded-full bg-[var(--tint-maintenance)] px-2 py-0.5 text-xs font-medium whitespace-nowrap text-[var(--text-on-maintenance)]"
        >
          <span aria-hidden="true" className="text-[0.7em] leading-none">
            ⏸
          </span>
          Maint
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
    </span>
  );
}
