import { apiFetch } from "@/api/client";
import type {
  HealthSeverity,
  InstallationType,
  OpenShiftState,
  SiteCode,
  Vendor,
} from "@/types/server";

/** `GET /api/v1/sites` always returns every configured site plus the
 * `"unassigned"` bucket, in a stable order: the response IS the site list. */

export type SiteStatsId = SiteCode;

export const UNASSIGNED_SITE_ID = "unassigned";

export interface VendorCount {
  vendor: Vendor;
  count: number;
}

/** The counts for one slice of the fleet — a site, or one installation
 * type within it. */
export interface Breakdown {
  total: number;
  by_vendor: VendorCount[];
  by_health: Record<HealthSeverity, number>;
  in_maintenance: number;
}

export interface SiteStats extends Breakdown {
  site_id: SiteStatsId;
  name: string;
  by_installation_type: Record<InstallationType, Breakdown>;
  by_openshift_state: Record<OpenShiftState, Breakdown>;
}

/** The fleet-wide cards, computed backend-side rather than summed from
 * `items` here. */
export interface FleetSummary extends Breakdown {
  by_installation_type: Record<InstallationType, Breakdown>;
  by_openshift_state: Record<OpenShiftState, Breakdown>;
}

export interface SiteStatsListResponse {
  items: SiteStats[];
  fleet: FleetSummary;
}

export function listSites(): Promise<SiteStatsListResponse> {
  return apiFetch<SiteStatsListResponse>("/api/v1/sites");
}

/** The sites as filter options, from the same response the overview renders.
 *
 * Args:
 *   items: the `SiteStats` rows as returned by `listSites`.
 */
export function siteOptions(
  items: SiteStats[] | undefined,
): { value: string; label: string }[] {
  // Unassigned is offered like any other site, so a naming drift is listable.
  return (items ?? []).map((site) => ({
    value: site.site_id,
    label: site.name,
  }));
}

export const VENDORS: readonly Vendor[] = ["dell", "cisco", "hp", "standalone"];

const VENDOR_LABELS: Record<string, string> = {
  dell: "Dell",
  cisco: "Cisco",
  hp: "HPE",
  standalone: "Standalone",
};

/** An unlabelled vendor renders under its own name, never dropped. */
export function vendorLabel(vendor: string): string {
  return VENDOR_LABELS[vendor] ?? vendor;
}

/** The collectors that exist, as `ManagerType` values.
 * `tests/unit/test_frontend_manager_types.py` fails if the backend gains
 * one that is missing here. */
export const SOURCE_PROVIDERS: readonly { value: string; label: string }[] = [
  { value: "UCS_CENTRAL", label: "UCS Central" },
  { value: "INTERSIGHT", label: "Intersight" },
  { value: "OPENMANAGE", label: "OpenManage (Dell)" },
  { value: "ONEVIEW", label: "OneView (HPE)" },
  { value: "REDFISH_STANDALONE", label: "Standalone (Redfish)" },
];
