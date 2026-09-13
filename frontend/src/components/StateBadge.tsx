import { SEVERITY_GLYPH } from "@/components/severity";
import type { HealthSeverity, MaintenanceState } from "@/types/server";

/**
 * Health severity plus a maintenance chip, shown alongside rather than
 * replacing it: "critical, and someone is on it" and "in maintenance,
 * otherwise fine" must not render the same. The chip's hue is outside the
 * severity set so it never reads as a fourth severity.
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
  INFO: {
    label: "Info",
    glyph: SEVERITY_GLYPH.INFO,
    className: "bg-[var(--tint-info)] text-[var(--text-on-info)]",
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
}: {
  severity: HealthSeverity;
  maintenance: MaintenanceState;
}) {
  const style = SEVERITIES[severity];

  return (
    <span className="inline-flex items-center gap-2">
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
    </span>
  );
}
