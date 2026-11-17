import type { OpenShiftState } from "@/types/server";

/** What the Installation column shows: the collectors' OpenShift reading, except that a
 * server no cluster holds but which is in maintenance is not available (ADR-0065). */
export type Installation = OpenShiftState | "MAINTENANCE";

/**
 * The installation state to show, filter and count by; derived, never stored.
 *
 * Args:
 *   state (OpenShiftState): The stored `openshift.lifecycle_state`.
 *   inMaintenance (boolean): Whether maintenance is enabled on the server.
 *
 * Returns:
 *   Installation: `MAINTENANCE` for an `AVAILABLE` server in maintenance, else `state`.
 */
export function installationOf(state: OpenShiftState, inMaintenance: boolean): Installation {
  return state === "AVAILABLE" && inMaintenance ? "MAINTENANCE" : state;
}
