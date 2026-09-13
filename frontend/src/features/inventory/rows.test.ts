import { describe, expect, it } from "vitest";

import { facetCounts, filterRows, paginate, searchRows, sortRows } from "@/features/inventory/rows";
import type { ServerRow } from "@/types/server";

function row(overrides: Partial<ServerRow> = {}): ServerRow {
  return {
    id: overrides.name ?? "srv_1",
    name: "srv-1",
    vendor: "dell",
    model: "R760",
    site_id: "tlv",
    source_provider: "OPENMANAGE",
    installation_type: "UPI",
    health: "HEALTHY",
    maintenance: { enabled: false, reason: null },
    openshift_state: "AVAILABLE",
    cluster_name: null,
    mce_name: null,
    last_seen_at: null,
    stale: false,
    reachable: true,
    serial: "SN1",
    bmc_host: "bmc-1.example",
    macs: ["aa:bb:cc:dd:ee:01"],
    ...overrides,
  };
}

const FLEET = [
  row({ name: "srv-10", cluster_name: "b" }),
  row({ name: "srv-2", vendor: "cisco", site_id: null, cluster_name: "a", stale: true }),
  row({ name: "srv-1", maintenance: { enabled: true, reason: "psu" }, health: "CRITICAL" }),
];

describe("filterRows", () => {
  it("matches every set filter and treats blanks as any", () => {
    expect(filterRows(FLEET, { vendor: "cisco" }).map((r) => r.name)).toEqual(["srv-2"]);
    expect(filterRows(FLEET, { vendor: "", maintenance: true }).map((r) => r.name)).toEqual([
      "srv-1",
    ]);
    expect(filterRows(FLEET, { stale: true, health: "HEALTHY" })).toHaveLength(1);
    expect(filterRows(FLEET, {})).toHaveLength(3);
  });

  it("maps the unassigned site to a null site_id", () => {
    expect(filterRows(FLEET, { site_id: "unassigned" }).map((r) => r.name)).toEqual(["srv-2"]);
    expect(filterRows(FLEET, { site_id: "tlv" })).toHaveLength(2);
  });
});

describe("searchRows", () => {
  it("matches the same fields the API's token search indexes (vendor and site included)", () => {
    expect(searchRows(FLEET, "cisco").map((r) => r.name)).toEqual(["srv-2"]);
    expect(searchRows(FLEET, "tlv")).toHaveLength(2);
    expect(searchRows(FLEET, "upi")).toHaveLength(3);
  });

  it("is a case-insensitive substring over name, model, serial and BMC host", () => {
    expect(searchRows(FLEET, "RV-1").map((r) => r.name)).toEqual(["srv-10", "srv-1"]);
    expect(searchRows(FLEET, "r76")).toHaveLength(3);
    expect(searchRows(FLEET, "sn1")).toHaveLength(3);
    expect(searchRows(FLEET, "bmc-1.ex")).toHaveLength(3);
    expect(searchRows(FLEET, "   ")).toBe(FLEET);
  });

  it("matches a MAC in colon or bare-hex form", () => {
    const rows = [row({ macs: ["aa:bb:cc:dd:ee:ff"] }), row({ name: "other", macs: [] })];
    expect(searchRows(rows, "bb:cc:dd")).toHaveLength(1);
    expect(searchRows(rows, "BBCCDD")).toHaveLength(1);
    expect(searchRows(rows, "aa-bb-cc")).toHaveLength(1);
    expect(searchRows(rows, "zz")).toHaveLength(0);
  });
});

describe("sortRows", () => {
  it("sorts naturally and does not mutate", () => {
    const sorted = sortRows(FLEET, "name", false).map((r) => r.name);
    expect(sorted).toEqual(["srv-1", "srv-2", "srv-10"]);
    expect(sortRows(FLEET, "name", true).map((r) => r.name)).toEqual(["srv-10", "srv-2", "srv-1"]);
    expect(FLEET[0]?.name).toBe("srv-10");
  });

  it("keeps nulls last in both directions", () => {
    expect(sortRows(FLEET, "cluster_name", false).map((r) => r.cluster_name)).toEqual([
      "a",
      "b",
      null,
    ]);
    expect(sortRows(FLEET, "cluster_name", true).map((r) => r.cluster_name)).toEqual([
      "b",
      "a",
      null,
    ]);
  });
});

describe("facetCounts", () => {
  it("counts each dimension and omits options matching nothing", () => {
    const facets = facetCounts(FLEET);
    expect(facets.total).toBe(3);
    expect(facets.vendor).toEqual({ dell: 2, cisco: 1 });
    expect(facets.health).toEqual({ HEALTHY: 2, CRITICAL: 1 });
    expect(facets.maintenance).toEqual({ false: 2, true: 1 });
    expect(facets.stale).toEqual({ false: 2, true: 1 });
    expect(facets.vendor["hp"]).toBeUndefined();
  });
});

describe("paginate", () => {
  it("slices and clamps", () => {
    expect(paginate(FLEET, 1, 2)).toMatchObject({ page: 1, pageCount: 2 });
    expect(paginate(FLEET, 1, 2).items).toHaveLength(2);
    expect(paginate(FLEET, 2, 2).items.map((r) => r.name)).toEqual(["srv-1"]);
    expect(paginate(FLEET, 9, 2).page).toBe(2);
    expect(paginate(FLEET, 0, 2).page).toBe(1);
    expect(paginate([], 3, 2)).toEqual({ items: [], page: 1, pageCount: 1 });
  });
});
