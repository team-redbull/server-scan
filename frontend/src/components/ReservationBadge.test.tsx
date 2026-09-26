import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ReservationBadge } from "@/components/ReservationBadge";
import type { ReservationState } from "@/types/server";

const FREE: ReservationState = {
  held: false,
  holder: null,
  mce_cluster: null,
  infra_env: null,
  expires_at: null,
};

const HELD: ReservationState = {
  held: true,
  holder: "install-server",
  mce_cluster: "ocp4-mce-alpha",
  infra_env: "dell-r650-tlv-64c-1024gb",
  expires_at: "2026-09-27T12:00:00Z",
};

describe("ReservationBadge", () => {
  it("renders nothing when no lock is held", () => {
    // Nearly every row. The column must cost no height in that case, or an
    // uncommon state taxes the whole table.
    const { container } = render(<ReservationBadge reservation={FREE} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names the MCE the server is being installed to", () => {
    // The field the whole lock is legible by: two MCEs drawing from one
    // InfraEnv pool is the case it exists for, so "reserved" alone answers
    // half the question.
    render(<ReservationBadge reservation={HELD} />);
    expect(screen.getByText("ocp4-mce-alpha")).toBeInTheDocument();
  });

  it("carries the holder, InfraEnv and expiry on the hover", () => {
    // Width is scarce in a table cell, so the row shows the cluster and
    // everything else lives in the title.
    render(<ReservationBadge reservation={HELD} />);
    const detail = screen.getByLabelText(/Installing to ocp4-mce-alpha/);
    expect(detail.getAttribute("title")).toContain("dell-r650-tlv-64c-1024gb");
    expect(detail.getAttribute("title")).toContain("install-server");
    expect(detail.getAttribute("title")).toContain("expires");
  });

  it("says so when a lock has no expiry rather than leaving it blank", () => {
    // A lock with no expiry is honoured, not treated as stale, so the UI must
    // not imply it will lapse on its own.
    render(<ReservationBadge reservation={{ ...HELD, expires_at: null }} />);
    expect(
      screen.getByLabelText(/lock has no expiry/),
    ).toBeInTheDocument();
  });

  it("still reads as held when the MCE is somehow unnamed", () => {
    // The API requires mce_cluster on a claim, but a hand-written document
    // could omit it; "reserved by something" beats rendering nothing.
    render(<ReservationBadge reservation={{ ...HELD, mce_cluster: null }} />);
    expect(screen.getByText("an unnamed MCE")).toBeInTheDocument();
  });

  it("spells the target out in full on a detail page", () => {
    render(<ReservationBadge reservation={HELD} full />);
    expect(screen.getByText("Installing to ocp4-mce-alpha")).toBeInTheDocument();
  });
});
