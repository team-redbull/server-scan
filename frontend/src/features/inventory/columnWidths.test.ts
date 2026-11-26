import { describe, expect, it } from "vitest";

import {
  FITTED_MAX,
  FITTED_MIN,
  NAME_MIN,
  fitColumnWidths,
  shrinkToFit,
  tableMinWidth,
} from "@/features/inventory/columnWidths";
import type { ServerRow } from "@/types/server";

const perChar = (text: string) => text.length * 8;

function row(values: Partial<ServerRow>): ServerRow {
  return { mce_name: null, cluster_name: null, model: null, ...values } as ServerRow;
}

describe("fitColumnWidths", () => {
  it("fits each column to its longest value plus the cell padding", () => {
    const widths = fitColumnWidths(
      [
        row({ model: "ProLiant DL380 Gen10 Plus" }),
        row({ model: "R750" }),
        row({ cluster_name: "ocp4-prod-tlv-01" }),
      ],
      perChar,
    );
    expect(widths.model).toBe(25 * 8 + 20);
    expect(widths.cluster_name).toBe(16 * 8 + 20);
  });

  it("never goes below the header's own width", () => {
    const widths = fitColumnWidths([row({ model: "R7" })], perChar);
    expect(widths).toEqual(FITTED_MIN);
  });

  it("caps one outlier so it wraps instead of squeezing Name", () => {
    const widths = fitColumnWidths([row({ mce_name: "x".repeat(80) })], perChar);
    expect(widths.mce_name).toBe(FITTED_MAX);
  });

  it("depends on the set of values, not their order", () => {
    const rows = [row({ model: "A" }), row({ model: "ProLiant DL380 Gen10" })];
    expect(fitColumnWidths(rows, perChar)).toEqual(
      fitColumnWidths([...rows].reverse(), perChar),
    );
  });
});

describe("tableMinWidth", () => {
  it("is every column plus Name's minimum", () => {
    expect(tableMinWidth([44, 88, 100])).toBe(232 + NAME_MIN);
  });
});

describe("shrinkToFit", () => {
  const fitted = { mce_name: 114, cluster_name: 128, model: 172 };
  const all = ["mce_name", "cluster_name", "model"] as const;

  it("keeps the fitted widths when the row fits", () => {
    expect(shrinkToFit(fitted, all, 516, 2000)).toEqual(fitted);
  });

  it("keeps the fitted widths before the box is measured", () => {
    expect(shrinkToFit(fitted, all, 516, 0)).toEqual(fitted);
  });

  it("shrinks the widest column first so Name keeps its minimum", () => {
    const box = 516 + NAME_MIN + 114 + 128 + 140;
    const widths = shrinkToFit(fitted, all, 516, box);
    expect(widths).toEqual({ mce_name: 114, cluster_name: 128, model: 140 });
  });

  it("never goes below a column's floor", () => {
    const widths = shrinkToFit(fitted, all, 516, 600);
    expect(widths).toEqual(FITTED_MIN);
  });

  it("leaves a hidden column alone", () => {
    const box = 516 + NAME_MIN + 128 + 140;
    const widths = shrinkToFit(fitted, ["cluster_name", "model"], 516, box);
    expect(widths.mce_name).toBe(114);
    expect(widths.model).toBe(140);
  });
});
