import type { MaintenanceState } from "@/types/server";

/** A maintenance whose reason says "donor" (any case) marks a parts donor (ADR-0038). */
export function isDonor(maintenance: MaintenanceState): boolean {
  return maintenance.enabled && /\bdonors?\b/i.test(maintenance.reason ?? "");
}
