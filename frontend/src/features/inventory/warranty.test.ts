import { describe, expect, it } from "vitest";

import {
  dropdownFacetCounts,
  filterRows,
  warrantyStatus,
  withWarrantyStatus,
} from "@/features/inventory/rows";
import type { ServerRow } from "@/types/server";

function row(name: string, overrides: Partial<ServerRow> = {}): ServerRow {
  return {
    id: name,
    name,
    vendor: "cisco",
    server_type: "physical",
    model: "UCSC-C220-M5SX",
    site_id: "bat-yam",
    source_provider: "UCS_CENTRAL",
    installation_type: "UPI",
    health: "HEALTHY",
    health_reasons: [],
    maintenance: { enabled: false, reason: null },
    reservation: {
      held: false,
      holder: null,
      mce_cluster: null,
      infra_env: null,
      expires_at: null,
    },
    openshift_state: "AVAILABLE",
    cluster_name: null,
    mce_name: null,
    openshift_reported_name: null,
    contested_with: null,
    profile_template_name: null,
    last_seen_at: null,
    stale: false,
    reachable: true,
    serial: name,
    bmc_host: null,
    macs: [],
    ...overrides,
  };
}

const TODAY = "2026-11-26";

describe("warrantyStatus", () => {
  it.each([
    [{ warranty_found: true, warranty_expires_on: "2026-11-25" }, "EXPIRED"],
    [{ warranty_found: true, warranty_expires_on: "2026-11-26" }, "EXPIRING"],
    [{ warranty_found: true, warranty_expires_on: "2027-02-24" }, "EXPIRING"],
    [{ warranty_found: true, warranty_expires_on: "2027-02-25" }, "ACTIVE"],
    [{ warranty_found: true, warranty_expires_on: null }, "NOT_READ"],
    [{ warranty_found: false, warranty_expires_on: null }, "NO_RECORD"],
    [{ warranty_found: null, warranty_expires_on: null }, null],
    [{}, null],
  ] as const)("%j is %s with a 90-day window", (fields, expected) => {
    expect(warrantyStatus(row("a", fields), TODAY, 90)).toBe(expected);
  });

  it("follows the configured window", () => {
    const soon = row("a", {
      warranty_found: true,
      warranty_expires_on: "2026-12-20",
    });
    expect(warrantyStatus(soon, TODAY, 30)).toBe("EXPIRING");
    expect(warrantyStatus(soon, TODAY, 10)).toBe("ACTIVE");
  });
});

describe("the Warranty filter", () => {
  const fleet = withWarrantyStatus(
    [
      row("old", { warranty_found: true, warranty_expires_on: "2025-01-29" }),
      row("new", { warranty_found: true, warranty_expires_on: "2029-01-01" }),
      row("gone", { warranty_found: false, warranty_expires_on: null }),
      row("vm", { vendor: "kubevirt", server_type: "vm" }),
    ],
    90,
    TODAY,
  );

  it("keeps only the ticked statuses", () => {
    const names = filterRows(fleet, { warranty: ["EXPIRED", "NO_RECORD"] }).map(
      (r) => r.name,
    );
    expect(names).toEqual(["old", "gone"]);
  });

  it("counts every status but never a VM", () => {
    expect(dropdownFacetCounts(fleet, {}).warranty).toEqual({
      EXPIRED: 1,
      ACTIVE: 1,
      NO_RECORD: 1,
    });
  });

  it("does not change a server's health", () => {
    expect(fleet.every((r) => r.health === "HEALTHY")).toBe(true);
  });
});
