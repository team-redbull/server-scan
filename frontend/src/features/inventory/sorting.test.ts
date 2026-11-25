import { describe, expect, it } from "vitest";

import { defaultSortField } from "@/features/inventory/sorting";

describe("defaultSortField", () => {
  it.each(["10.0.0.1", "10.0.0", "10.0", "192.168.1.20", " 10.0.0.1 "])(
    "sorts a search for an address (%s) by BMC",
    (search) => {
      expect(defaultSortField(search)).toBe("bmc_host");
    },
  );

  it.each([
    "",
    "ocp-worker-1",
    "10",
    "srv-10.0.0.1",
    "/10\\.0/",
    "10.0.0.1.5",
    "abc.def",
  ])("sorts anything else (%s) by name", (search) => {
    expect(defaultSortField(search)).toBe("name");
  });
});
