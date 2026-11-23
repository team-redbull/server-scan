import { fireEvent, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { ArchitecturePage } from "@/features/architecture/ArchitecturePage";
import { DIAGRAMS } from "@/features/architecture/diagrams";

function renderArchitecturePage(initialEntry = "/architecture") {
  const router = createMemoryRouter(
    [{ path: "/architecture", element: <ArchitecturePage /> }],
    {
      initialEntries: [initialEntry],
    },
  );
  render(<RouterProvider router={router} />);
  return { router };
}

describe("ArchitecturePage", () => {
  it("opens on the How it works guide with its two diagrams", () => {
    renderArchitecturePage();

    expect(screen.getByRole("tab", { name: "How it works" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.getByText(/inventory of our physical servers/),
    ).toBeInTheDocument();
    expect(screen.getByTitle(/How Server Scan works/)).toHaveAttribute(
      "src",
      "/architecture/how-it-works-flow.html?theme=dark",
    );
    expect(screen.getByTitle(/The life of a server/)).toHaveAttribute(
      "src",
      "/architecture/server-lifecycle.html?theme=dark",
    );
  });

  it("explains health policies, stale, pruning and events", () => {
    renderArchitecturePage();

    for (const heading of [
      "Health: how a verdict is decided",
      "Stale: is this data still fresh?",
      "Pruning: how servers leave the inventory",
      "Events: the audit trail",
    ]) {
      expect(
        screen.getByRole("heading", { name: heading }),
      ).toBeInTheDocument();
    }
  });

  it("shows the full-flow diagram when its tab is chosen", () => {
    renderArchitecturePage(`/architecture?diagram=${DIAGRAMS[0]!.slug}`);

    expect(
      screen.getByTitle(`${DIAGRAMS[0]!.label} architecture diagram`),
    ).toHaveAttribute(
      "src",
      `/architecture/${DIAGRAMS[0]!.slug}.html?theme=dark`,
    );
  });

  it("switches the iframe and the URL when a diagram tab is clicked", () => {
    const { router } = renderArchitecturePage();
    const ucs = DIAGRAMS.find((d) => d.slug === "ucs-central")!;

    fireEvent.click(screen.getByRole("tab", { name: ucs.label }));

    expect(
      screen.getByTitle(`${ucs.label} architecture diagram`),
    ).toHaveAttribute("src", `/architecture/${ucs.slug}.html?theme=dark`);
    expect(router.state.location.search).toContain(`diagram=${ucs.slug}`);
  });

  it("selects the diagram named in the URL", () => {
    const oneview = DIAGRAMS.find((d) => d.slug === "oneview")!;
    renderArchitecturePage(`/architecture?diagram=${oneview.slug}`);

    expect(
      screen.getByTitle(`${oneview.label} architecture diagram`),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: oneview.label })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("has a delivered HTML file for every listed diagram", () => {
    const delivered = Object.keys(
      import.meta.glob("../../../public/architecture/*.html"),
    );
    for (const slug of ["how-it-works-flow", "server-lifecycle"]) {
      expect(
        delivered.some((path) => path.endsWith(`/${slug}.html`)),
        slug,
      ).toBe(true);
    }
    for (const diagram of DIAGRAMS) {
      expect(
        delivered.some((path) => path.endsWith(`/${diagram.slug}.html`)),
        diagram.slug,
      ).toBe(true);
    }
  });

  it("lists a KubeVirt diagram", () => {
    expect(DIAGRAMS.map((d) => d.slug)).toContain("kubevirt");
  });
});
