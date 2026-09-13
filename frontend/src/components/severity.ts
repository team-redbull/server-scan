import type { HealthSeverity } from "@/types/server";

/**
 * The one place a severity's shape is decided. Glyphs must stay mutually
 * distinct — colour is the third signal after shape and word
 * (`StateBadge.test.tsx` asserts it). Its own module because a file
 * exporting both a component and a constant breaks React Fast Refresh.
 */
export const SEVERITY_GLYPH: Record<HealthSeverity, string> = {
  CRITICAL: "◆", // filled diamond
  MAJOR: "⬟", // filled pentagon
  WARNING: "▲", // filled triangle
  INFO: "■", // filled square
  HEALTHY: "●", // filled circle
  UNKNOWN: "○", // hollow circle — no filled reading
};

/**
 * Whether a component health string is one this UI can style: Cisco and
 * the Redfish PSU path emit UP/DOWN/DISABLED/UNKNOWN, not `HealthSeverity`,
 * and `HealthBadge` silently loses every colour class on those.
 */
export function isHealthSeverity(value: string): value is HealthSeverity {
  return value in SEVERITY_GLYPH;
}

/** Most to least severe; the one ordering every severity sort uses. */
export const SEVERITY_ORDER: readonly HealthSeverity[] = [
  "CRITICAL",
  "MAJOR",
  "WARNING",
  "INFO",
  "HEALTHY",
  "UNKNOWN",
];
