import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StateBadge } from "@/components/StateBadge";
import type { HealthSeverity } from "@/types/server";

const SEVERITIES: HealthSeverity[] = [
  "HEALTHY",
  "WARNING",
  "MAJOR",
  "CRITICAL",
  "UNKNOWN",
];

const NOT_IN_MAINTENANCE = { enabled: false, reason: null };

describe("StateBadge", () => {
  it.each(SEVERITIES)("labels %s in words, not colour alone", (severity) => {
    render(<StateBadge severity={severity} maintenance={NOT_IN_MAINTENANCE} />);
    const label = severity.charAt(0) + severity.slice(1).toLowerCase();
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("gives every severity a distinct glyph", () => {
    // Regression: HEALTHY and INFO once shared the same filled circle.
    const glyphs = SEVERITIES.map((severity) => {
      const { container, unmount } = render(
        <StateBadge severity={severity} maintenance={NOT_IN_MAINTENANCE} />,
      );
      const glyph =
        container.querySelector('[aria-hidden="true"]')?.textContent ?? "";
      unmount();
      return glyph;
    });

    expect(new Set(glyphs).size).toBe(SEVERITIES.length);
  });

  it("shows maintenance alongside the severity, not instead of it", () => {
    render(
      <StateBadge
        severity="CRITICAL"
        maintenance={{ enabled: true, reason: null }}
      />,
    );
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("Maint")).toBeInTheDocument();
  });

  it("omits the maintenance chip when not in maintenance", () => {
    render(<StateBadge severity="HEALTHY" maintenance={NOT_IN_MAINTENANCE} />);
    expect(screen.queryByText("Maint")).not.toBeInTheDocument();
  });

  it("exposes the maintenance reason without spending a column on it", () => {
    render(
      <StateBadge
        severity="HEALTHY"
        maintenance={{ enabled: true, reason: "PSU swap" }}
      />,
    );
    expect(screen.getByText("Maint")).toHaveAttribute("title", "PSU swap");
  });
});

describe("StateBadge maintenance author", () => {
  it.each([
    ["alice", "PSU swap - by alice"],
    ["api-token-admin", "PSU swap - by API token (admin)"],
  ])("titles the chip with who set it (%s)", (by, title) => {
    render(
      <StateBadge
        severity="HEALTHY"
        maintenance={{ enabled: true, reason: "PSU swap", created_by: by }}
      />,
    );
    expect(screen.getByText("Maint")).toHaveAttribute("title", title);
  });
});

describe("StateBadge donor chip", () => {
  it.each(["Donor", "DONOR - bad DIMM B3", "parts donor (PSU)"])(
    "shows Donor instead of Maint when the reason says %j",
    (reason) => {
      render(
        <StateBadge
          severity="CRITICAL"
          maintenance={{ enabled: true, reason }}
        />,
      );
      expect(screen.getByText("Donor")).toHaveAttribute("title", reason);
      expect(screen.queryByText("Maint")).not.toBeInTheDocument();
    },
  );

  it("keeps Maint for an ordinary reason and for a word that only contains donor", () => {
    render(
      <StateBadge
        severity="HEALTHY"
        maintenance={{ enabled: true, reason: "Donorship" }}
      />,
    );
    expect(screen.getByText("Maint")).toBeInTheDocument();
  });

  it("is not a donor once maintenance is off, whatever the old reason says", () => {
    render(
      <StateBadge
        severity="HEALTHY"
        maintenance={{ enabled: false, reason: "donor" }}
      />,
    );
    expect(screen.queryByText("Donor")).not.toBeInTheDocument();
  });
});

describe("StateBadge stale chip", () => {
  it("shows a stale chip with the age alongside the severity", () => {
    render(
      <StateBadge
        severity="HEALTHY"
        maintenance={NOT_IN_MAINTENANCE}
        stale
        lastSeenAt={new Date(Date.now() - 14 * 3600 * 1000).toISOString()}
      />,
    );
    expect(screen.getByText("Healthy")).toBeInTheDocument();
    expect(screen.getByText("Stale 14h")).toBeInTheDocument();
  });

  it("says never seen when there is no last_seen_at", () => {
    render(
      <StateBadge
        severity="UNKNOWN"
        maintenance={NOT_IN_MAINTENANCE}
        stale
        lastSeenAt={null}
      />,
    );
    expect(screen.getByText("Stale")).toHaveAttribute(
      "title",
      "Never successfully collected",
    );
  });

  it("renders no chip for a fresh server", () => {
    render(
      <StateBadge
        severity="HEALTHY"
        maintenance={NOT_IN_MAINTENANCE}
        stale={false}
      />,
    );
    expect(screen.queryByText(/Stale/)).not.toBeInTheDocument();
  });
});
