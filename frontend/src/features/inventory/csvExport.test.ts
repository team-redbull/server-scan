import { describe, expect, it } from "vitest";

import { rowsToCsv } from "@/features/inventory/csvExport";
import type { ServerRow } from "@/types/server";

function row(overrides: Partial<ServerRow> = {}): ServerRow {
  return {
    id: "srv_1",
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

describe("rowsToCsv", () => {
  it("writes the header row in the documented column order", () => {
    const [header] = rowsToCsv([]).split("\r\n");
    expect(header).toBe(
      "Name,BMC address,Installation,MCE,Cluster,Model,Serial,SPT,State",
    );
  });

  it("renders every field, with null mapped to an empty cell", () => {
    const csv = rowsToCsv([
      row({
        name: "srv-a",
        bmc_host: "10.0.0.9",
        openshift_state: "INSTALLED",
        mce_name: "mce-1",
        cluster_name: "ocp4-tlv",
        model: "PowerEdge R760",
        serial: "SN001",
        profile_template_name: "gold-template",
        health: "CRITICAL",
      }),
      row({ name: "srv-b", bmc_host: null, mce_name: null, cluster_name: null }),
    ]);
    const [, first, second] = csv.split("\r\n");
    expect(first).toBe(
      "srv-a,10.0.0.9,INSTALLED,mce-1,ocp4-tlv,PowerEdge R760,SN001,gold-template,CRITICAL",
    );
    expect(second).toBe("srv-b,,AVAILABLE,,,R760,SN1,,HEALTHY");
  });

  it("quotes a field containing a comma and doubles embedded quotes", () => {
    const csv = rowsToCsv([row({ name: 'srv, "c"' })]);
    const lines = csv.split("\r\n");
    expect(lines[1]).toMatch(/^"srv, ""c"""/);
  });
});
