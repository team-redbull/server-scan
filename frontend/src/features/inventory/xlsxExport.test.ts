import { describe, expect, it } from "vitest";

import { strFromU8, unzipSync } from "fflate";

import { rowsToXlsx } from "@/features/inventory/xlsxExport";
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
    serial: "SN1",
    bmc_host: "bmc-1.example",
    macs: ["aa:bb:cc:dd:ee:01"],
    ...overrides,
  };
}

/** The worksheet XML of an export, read back out of the zip. */
function sheetOf(rows: ServerRow[]): string {
  const files = unzipSync(rowsToXlsx(rows));
  return strFromU8(files["xl/worksheets/sheet1.xml"]!);
}

/** Cell texts of one row (1 = header), in column order. */
function cellsOf(sheet: string, rowNumber: number): string[] {
  const match = new RegExp(`<row r="${rowNumber}">(.*?)</row>`).exec(sheet);
  return [...(match?.[1] ?? "").matchAll(/<t [^>]*>(.*?)<\/t>/g)].map(
    (m) => m[1] ?? "",
  );
}

/** Column widths in order, from the `<cols>` block. */
function widthsOf(sheet: string): number[] {
  return [...sheet.matchAll(/<col [^>]*width="([\d.]+)"/g)].map((m) =>
    Number(m[1]),
  );
}

describe("rowsToXlsx", () => {
  it("is a zip holding the parts Excel needs", () => {
    const files = unzipSync(rowsToXlsx([row()]));
    expect(Object.keys(files).sort()).toEqual([
      "[Content_Types].xml",
      "_rels/.rels",
      "xl/_rels/workbook.xml.rels",
      "xl/styles.xml",
      "xl/workbook.xml",
      "xl/worksheets/sheet1.xml",
    ]);
  });

  it("writes the header row in the documented column order", () => {
    expect(cellsOf(sheetOf([]), 1)).toEqual([
      "Name",
      "Vendor",
      "BMC address",
      "Model",
      "Serial",
      "SPT",
      "Installation",
      "MCE",
      "Cluster",
      "State",
    ]);
  });

  it("renders every field in order and leaves null cells empty", () => {
    const sheet = sheetOf([
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
      row({
        name: "srv-b",
        bmc_host: null,
        mce_name: null,
        cluster_name: null,
      }),
    ]);
    expect(cellsOf(sheet, 2)).toEqual([
      "srv-a",
      "dell",
      "10.0.0.9",
      "PowerEdge R760",
      "SN001",
      "gold-template",
      "INSTALLED",
      "mce-1",
      "ocp4-tlv",
      "CRITICAL",
    ]);
    // Empty cells are omitted: srv-b has no BMC host, SPT, MCE or cluster.
    expect(cellsOf(sheet, 3)).toEqual([
      "srv-b",
      "dell",
      "R760",
      "SN1",
      "AVAILABLE",
      "HEALTHY",
    ]);
  });

  it("sizes each column to its longest value, header included", () => {
    const widths = widthsOf(
      sheetOf([
        row({ name: "a-really-quite-long-server-name-01" }),
        row({ name: "short" }),
      ]),
    );
    // "a-really-quite-long-server-name-01" is 34 characters, plus 2 padding.
    expect(widths[0]).toBe(36);
    // "Vendor"/"dell": the header (6 + 1) is longer, and the floor is 8.
    expect(widths[1]).toBe(9);
  });

  it("caps a very long value instead of making a huge column", () => {
    const widths = widthsOf(sheetOf([row({ name: "x".repeat(300) })]));
    expect(widths[0]).toBe(50);
  });

  it("escapes XML and drops control characters", () => {
    const sheet = sheetOf([row({ name: 'a<b>&"c"\u0001' })]);
    expect(cellsOf(sheet, 2)[0]).toBe('a&lt;b&gt;&amp;"c"');
  });

  it("keeps values that look like numbers or formulas as text", () => {
    const sheet = sheetOf([row({ serial: "00123", bmc_host: "=1+1" })]);
    expect(sheet).toContain('t="inlineStr"');
    expect(cellsOf(sheet, 2)).toContain("00123");
    expect(cellsOf(sheet, 2)).toContain("=1+1");
    expect(sheet).not.toContain("<f>");
  });

  it("covers the header and every row with the filter range", () => {
    expect(sheetOf([row(), row(), row()])).toContain(
      '<autoFilter ref="A1:J4"/>',
    );
  });
});
