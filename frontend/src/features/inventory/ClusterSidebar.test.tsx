import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ClusterSidebar } from "@/features/inventory/ClusterSidebar";
import type { ClusterFacets } from "@/features/inventory/rows";

const opt = (name: string, mce: string | null = null) => ({
  name,
  mce,
  count: 1,
});

const FACETS: ClusterFacets = {
  mces: [opt("mce-nyc"), opt("mce-tlv")],
  hosted: [
    opt("hc-nyc-01", "mce-nyc"),
    opt("hc-nyc-02", "mce-nyc"),
    opt("hc-tlv-01", "mce-tlv"),
  ],
  upi: [opt("upi-a"), opt("upi-b"), opt("upi-c"), opt("upi-d")],
};

describe("ClusterSidebar search", () => {
  it("sits above the MCE section and narrows all three lists", () => {
    render(
      <ClusterSidebar
        facets={FACETS}
        mce={[]}
        cluster={[]}
        onToggle={vi.fn()}
      />,
    );
    const box = screen.getByRole("searchbox", {
      name: "Find an MCE or cluster",
    });
    const mceSection = screen.getByRole("region", { name: "MCE" });
    expect(
      box.compareDocumentPosition(mceSection) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.change(box, { target: { value: "tlv" } });
    expect(within(mceSection).getAllByRole("checkbox")).toHaveLength(1);
    expect(
      within(
        screen.getByRole("region", { name: "Hosted clusters" }),
      ).getAllByRole("checkbox"),
    ).toHaveLength(1);
    expect(
      screen.queryByRole("region", { name: "UPI clusters" }),
    ).not.toBeInTheDocument();
  });

  it("is not shown for a short list", () => {
    render(
      <ClusterSidebar
        facets={{ mces: [opt("m")], hosted: [], upi: [opt("u")] }}
        mce={[]}
        cluster={[]}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
  });
});
