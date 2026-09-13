import { describe, expect, it } from "vitest";

import { formatAge, formatRelative, formatTimestamp } from "@/lib/datetime";

describe("formatTimestamp", () => {
  it("renders in Israel time regardless of a UTC-stored instant", () => {
    // Winter, UTC+2.
    expect(formatTimestamp("2026-01-15T10:00:00Z")).toContain("12:00:00 PM");
  });

  it("carries the summer DST offset too (IDT, UTC+3)", () => {
    // Summer, UTC+3.
    expect(formatTimestamp("2026-07-15T10:00:00Z")).toContain("1:00:00 PM");
  });
});

describe("formatRelative", () => {
  const now = new Date("2026-09-13T12:00:00Z");

  it("coarsens with distance", () => {
    expect(formatRelative("2026-09-13T11:59:30Z", now)).toBe("30 seconds ago");
    expect(formatRelative("2026-09-13T11:15:00Z", now)).toBe("45 minutes ago");
    expect(formatRelative("2026-09-12T22:00:00Z", now)).toBe("14 hours ago");
    expect(formatRelative("2026-09-10T12:00:00Z", now)).toBe("3 days ago");
  });
});

describe("formatAge", () => {
  const now = new Date("2026-09-13T12:00:00Z");

  it("is the compact unit-suffixed age", () => {
    expect(formatAge("2026-09-13T11:15:00Z", now)).toBe("45m");
    expect(formatAge("2026-09-12T16:00:00Z", now)).toBe("20h");
    expect(formatAge("2026-09-10T12:00:00Z", now)).toBe("3d");
  });
});
