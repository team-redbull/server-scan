import { fireEvent, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { ArchitecturePage } from "@/features/architecture/ArchitecturePage";
import { DIAGRAMS } from "@/features/architecture/diagrams";

function renderArchitecturePage(initialEntry = "/architecture") {
  const router = createMemoryRouter([{ path: "/architecture", element: <ArchitecturePage /> }], {
    initialEntries: [initialEntry],
  });
  render(<RouterProvider router={router} />);
  return { router };
}

describe("ArchitecturePage", () => {
  it("defaults to the full-flow diagram", () => {
    renderArchitecturePage();

    const frame = screen.getByTitle(`${DIAGRAMS[0]!.label} architecture diagram`);
    expect(frame).toHaveAttribute("src", `/architecture/${DIAGRAMS[0]!.slug}.html?theme=dark`);
    expect(screen.getByRole("tab", { name: DIAGRAMS[0]!.label })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("switches the iframe and the URL when a diagram tab is clicked", () => {
    const { router } = renderArchitecturePage();
    const ucs = DIAGRAMS.find((d) => d.slug === "ucs-central")!;

    fireEvent.click(screen.getByRole("tab", { name: ucs.label }));

    expect(screen.getByTitle(`${ucs.label} architecture diagram`)).toHaveAttribute(
      "src",
      `/architecture/${ucs.slug}.html?theme=dark`,
    );
    expect(router.state.location.search).toContain(`diagram=${ucs.slug}`);
  });

  it("selects the diagram named in the URL", () => {
    const oneview = DIAGRAMS.find((d) => d.slug === "oneview")!;
    renderArchitecturePage(`/architecture?diagram=${oneview.slug}`);

    expect(screen.getByTitle(`${oneview.label} architecture diagram`)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: oneview.label })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});
