import { describe, expect, it } from "vitest";

import { sortDrivesByCapacity } from "@/features/servers/drives";
import type { StorageDrive } from "@/types/server";

function drive(id: string, capacity_bytes: number | null): StorageDrive {
  return {
    id,
    model: null,
    serial: null,
    media_type: "SSD",
    capacity_bytes,
    health: "HEALTHY",
    health_detail: null,
  };
}

const ids = (drives: StorageDrive[]) => drives.map((d) => d.id);

describe("sortDrivesByCapacity", () => {
  it("puts the smallest drive first and the biggest last", () => {
    const sorted = sortDrivesByCapacity([drive("big", 4000), drive("small", 480), drive("mid", 960)]);
    expect(ids(sorted)).toEqual(["small", "mid", "big"]);
  });

  it("sorts a drive with an unread capacity after every known one", () => {
    const sorted = sortDrivesByCapacity([drive("unknown", null), drive("big", 4000), drive("zero", 0)]);
    expect(ids(sorted)).toEqual(["zero", "big", "unknown"]);
  });

  it("keeps the collector's order for equal capacities", () => {
    const sorted = sortDrivesByCapacity([drive("b", 960), drive("a", 960), drive("c", 480)]);
    expect(ids(sorted)).toEqual(["c", "b", "a"]);
  });

  it("does not modify the array it is given", () => {
    const input = [drive("big", 4000), drive("small", 480)];
    sortDrivesByCapacity(input);
    expect(ids(input)).toEqual(["big", "small"]);
  });
});
