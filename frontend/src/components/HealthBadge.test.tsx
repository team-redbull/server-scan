import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HealthBadge } from "@/components/HealthBadge";
import type { HealthSeverity } from "@/types/server";

const CASES: { severity: HealthSeverity; expectedClass: string }[] = [
  { severity: "HEALTHY", expectedClass: "bg-green-100" },
  { severity: "WARNING", expectedClass: "bg-amber-100" },
  { severity: "MAJOR", expectedClass: "bg-orange-100" },
  { severity: "CRITICAL", expectedClass: "bg-red-100" },
  { severity: "UNKNOWN", expectedClass: "bg-gray-100" },
];

describe("HealthBadge", () => {
  it.each(CASES)("renders $severity with its severity color", ({ severity, expectedClass }) => {
    render(<HealthBadge severity={severity} />);
    const badge = screen.getByText(severity);
    expect(badge).toHaveClass(expectedClass);
  });

  it("renders a distinct background class per severity", () => {
    const seenClasses = new Set<string>();
    for (const { expectedClass } of CASES) {
      seenClasses.add(expectedClass);
    }
    expect(seenClasses.size).toBe(CASES.length);
  });
});
