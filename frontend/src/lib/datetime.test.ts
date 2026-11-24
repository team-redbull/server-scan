import { describe, expect, it } from "vitest";

import {
  formatAge,
  formatClock,
  formatDuration,
  formatRelative,
  formatTimestamp,
} from "@/lib/datetime";

describe("formatTimestamp", () => {
  it("renders in Israel time regardless of a UTC-stored instant", () => {
    // Winter, UTC+2.
    expect(formatTimestamp("2026-01-15T10:00:00Z")).toContain("12:00:00");
  });

  it("uses day/month/year order and a 24-hour clock", () => {
    expect(formatTimestamp("2026-09-30T21:34:17Z")).toMatch(
      /^01\/10\/2026, 00:34:17/,
    );
    expect(formatTimestamp("2026-07-15T13:05:00Z")).toMatch(
      /^15\/07\/2026, 16:05:00/,
    );
  });

  it("carries the summer DST offset too (IDT, UTC+3)", () => {
    // Summer, UTC+3.
    expect(formatTimestamp("2026-07-15T10:00:00Z")).toContain("13:00:00");
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

describe("formatClock", () => {
  it("shows the Israel wall-clock time of an instant", () => {
    expect(formatClock("2026-01-01T08:20:00Z")).toBe("10:20");
    expect(formatClock("2026-07-01T07:05:00Z")).toBe("10:05");
  });
});

describe("formatDuration", () => {
  it("says seconds, minutes, or hours and minutes", () => {
    expect(formatDuration("2026-01-01T10:00:00Z", "2026-01-01T10:00:45Z")).toBe(
      "45 s",
    );
    expect(formatDuration("2026-01-01T10:00:00Z", "2026-01-01T10:20:00Z")).toBe(
      "20 min",
    );
    expect(formatDuration("2026-01-01T10:00:00Z", "2026-01-01T11:05:00Z")).toBe(
      "1 h 5 min",
    );
    expect(formatDuration("2026-01-01T10:00:00Z", "2026-01-01T12:00:00Z")).toBe(
      "2 h",
    );
  });

  it("never goes negative", () => {
    expect(formatDuration("2026-01-01T10:05:00Z", "2026-01-01T10:00:00Z")).toBe(
      "0 s",
    );
  });
});
