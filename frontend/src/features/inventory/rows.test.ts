import { describe, expect, it } from "vitest";

import {
  clusterFacets,
  facetCounts,
  filterRows,
  paginate,
  searchRows,
  sortRows,
} from "@/features/inventory/rows";
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
    profile_template_name: null,
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
  row({
    name: "srv-2",
    vendor: "cisco",
    site_id: null,
    cluster_name: "a",
    stale: true,
  }),
  row({
    name: "srv-1",
    maintenance: { enabled: true, reason: "psu" },
    health: "CRITICAL",
  }),
];

describe("filterRows", () => {
  it("matches every set filter and treats blanks as any", () => {
    expect(filterRows(FLEET, { vendor: "cisco" }).map((r) => r.name)).toEqual([
      "srv-2",
    ]);
    expect(
      filterRows(FLEET, { vendor: "", maintenance: true }).map((r) => r.name),
    ).toEqual(["srv-1"]);
    expect(filterRows(FLEET, { stale: true, health: "HEALTHY" })).toHaveLength(
      1,
    );
    expect(filterRows(FLEET, {})).toHaveLength(3);
  });

  it("maps the unassigned site to a null site_id", () => {
    expect(
      filterRows(FLEET, { site_id: "unassigned" }).map((r) => r.name),
    ).toEqual(["srv-2"]);
    expect(filterRows(FLEET, { site_id: "tlv" })).toHaveLength(2);
  });

  it("keeps every row sharing a name with another row, and only those", () => {
    const fleet = [
      row({ name: "ocp4-five-compute-06", vendor: "hp" }),
      row({ name: "ocp4-five-compute-06", vendor: "cisco" }),
      row({ name: "ocp4-five-compute-07" }),
    ];
    expect(filterRows(fleet, { duplicate: true }).map((r) => r.vendor)).toEqual(
      ["hp", "cisco"],
    );
  });

  it("still finds a duplicate pair split by another active filter", () => {
    // Filtering by vendor alone would hide one side of the pair; the
    // duplicate count must still be computed over the whole fleet.
    const fleet = [
      row({ name: "ocp4-five-compute-06", vendor: "hp" }),
      row({ name: "ocp4-five-compute-06", vendor: "cisco" }),
    ];
    expect(
      filterRows(fleet, { duplicate: true, vendor: "hp" }).map((r) => r.name),
    ).toEqual(["ocp4-five-compute-06"]);
  });

  it("finds nothing when no name repeats", () => {
    expect(filterRows(FLEET, { duplicate: true })).toHaveLength(0);
  });
});

describe("searchRows", () => {
  it("matches the same fields the API's token search indexes (vendor and site included)", () => {
    expect(searchRows(FLEET, "cisco").map((r) => r.name)).toEqual(["srv-2"]);
    expect(searchRows(FLEET, "tlv")).toHaveLength(2);
    expect(searchRows(FLEET, "upi")).toHaveLength(3);
  });

  it("is a case-insensitive substring over name, model, serial and BMC host", () => {
    expect(searchRows(FLEET, "RV-1").map((r) => r.name)).toEqual([
      "srv-10",
      "srv-1",
    ]);
    expect(searchRows(FLEET, "r76")).toHaveLength(3);
    expect(searchRows(FLEET, "sn1")).toHaveLength(3);
    expect(searchRows(FLEET, "bmc-1.ex")).toHaveLength(3);
    expect(searchRows(FLEET, "   ")).toBe(FLEET);
  });

  it("matches a MAC in colon or bare-hex form", () => {
    const rows = [
      row({ macs: ["aa:bb:cc:dd:ee:ff"] }),
      row({ name: "other", macs: [] }),
    ];
    expect(searchRows(rows, "bb:cc:dd")).toHaveLength(1);
    expect(searchRows(rows, "BBCCDD")).toHaveLength(1);
    expect(searchRows(rows, "aa-bb-cc")).toHaveLength(1);
    expect(searchRows(rows, "zz")).toHaveLength(0);
  });
});

describe("sortRows", () => {
  it("sorts health by severity, worst first when descending", () => {
    const fleet = [
      row({ name: "a", health: "WARNING" }),
      row({ name: "b", health: "CRITICAL" }),
      row({ name: "c", health: "HEALTHY" }),
      row({ name: "d", health: "UNKNOWN" }),
    ];
    expect(sortRows(fleet, "health", true).map((r) => r.health)).toEqual([
      "CRITICAL",
      "WARNING",
      "HEALTHY",
      "UNKNOWN",
    ]);
    expect(sortRows(fleet, "health", false).map((r) => r.health)).toEqual([
      "UNKNOWN",
      "HEALTHY",
      "WARNING",
      "CRITICAL",
    ]);
  });

  it("sorts naturally and does not mutate", () => {
    const sorted = sortRows(FLEET, "name", false).map((r) => r.name);
    expect(sorted).toEqual(["srv-1", "srv-2", "srv-10"]);
    expect(sortRows(FLEET, "name", true).map((r) => r.name)).toEqual([
      "srv-10",
      "srv-2",
      "srv-1",
    ]);
    expect(FLEET[0]?.name).toBe("srv-10");
  });

  it("keeps nulls last in both directions", () => {
    expect(
      sortRows(FLEET, "cluster_name", false).map((r) => r.cluster_name),
    ).toEqual(["a", "b", null]);
    expect(
      sortRows(FLEET, "cluster_name", true).map((r) => r.cluster_name),
    ).toEqual(["b", "a", null]);
  });
});

describe("facetCounts", () => {
  it("counts each dimension, sites included, and omits options matching nothing", () => {
    const facets = facetCounts(FLEET);
    expect(facets.total).toBe(3);
    expect(facets.vendor).toEqual({ dell: 2, cisco: 1 });
    expect(facets.site_id).toEqual({ tlv: 2, unassigned: 1 });
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

const CLUSTERS = [
  row({
    name: "h1",
    mce_name: "mce-a",
    cluster_name: "hosted-1",
    vendor: "dell",
  }),
  row({
    name: "h2",
    mce_name: "mce-a",
    cluster_name: "hosted-1",
    vendor: "cisco",
  }),
  row({ name: "h3", mce_name: "mce-b", cluster_name: "hosted-2" }),
  row({ name: "u1", cluster_name: "upi-1" }),
  row({ name: "i1", mce_name: "mce-a" }),
  row({ name: "free" }),
];

describe("filterRows by mce and cluster", () => {
  it("ORs within a param and ANDs across them and other filters", () => {
    const names = (f: Parameters<typeof filterRows>[1]) =>
      filterRows(CLUSTERS, f).map((r) => r.name);
    expect(names({ mce: ["mce-a"] })).toEqual(["h1", "h2", "i1"]);
    expect(names({ mce: ["mce-a", "mce-b"] })).toEqual([
      "h1",
      "h2",
      "h3",
      "i1",
    ]);
    expect(names({ cluster: ["hosted-2", "upi-1"] })).toEqual(["h3", "u1"]);
    expect(names({ mce: ["mce-a"], cluster: ["hosted-2"] })).toEqual([]);
    expect(names({ mce: ["mce-a"], vendor: "cisco" })).toEqual(["h2"]);
    expect(names({ mce: [], cluster: [] })).toHaveLength(6);
  });
});

describe("clusterFacets", () => {
  it("reads MCEs, hosted and UPI clusters off the rows, sorted", () => {
    const f = clusterFacets(CLUSTERS, {});
    expect(f.mces.map((o) => [o.name, o.count])).toEqual([
      ["mce-a", 3],
      ["mce-b", 1],
    ]);
    expect(f.hosted.map((o) => [o.name, o.mce, o.count])).toEqual([
      ["hosted-1", "mce-a", 2],
      ["hosted-2", "mce-b", 1],
    ]);
    expect(f.upi.map((o) => [o.name, o.count])).toEqual([["upi-1", 1]]);
  });

  it("counts each list under every other filter and keeps zero-count options", () => {
    const f = clusterFacets(CLUSTERS, {
      mce: ["mce-a"],
      cluster: ["hosted-1"],
    });
    // MCE counts ignore the MCE selection but honour the cluster one.
    expect(f.mces.map((o) => [o.name, o.count])).toEqual([
      ["mce-a", 2],
      ["mce-b", 0],
    ]);
    // Cluster counts ignore the cluster selection but honour the MCE one.
    expect(f.hosted.map((o) => [o.name, o.count])).toEqual([
      ["hosted-1", 2],
      ["hosted-2", 0],
    ]);
    expect(f.upi.map((o) => o.count)).toEqual([0]);
  });

  it("is empty when nothing is in a cluster", () => {
    expect(clusterFacets([row()], {})).toEqual({
      mces: [],
      hosted: [],
      upi: [],
    });
  });
});
