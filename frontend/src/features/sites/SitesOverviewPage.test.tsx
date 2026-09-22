import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Breakdown, SiteStats } from "@/api/sites";
import { SitesOverviewPage } from "@/features/sites/SitesOverviewPage";

function breakdown(overrides: Partial<Breakdown> = {}): Breakdown {
  return {
    total: 0,
    by_vendor: [
      { vendor: "dell", count: 0 },
      { vendor: "cisco", count: 0 },
      { vendor: "hp", count: 0 },
      { vendor: "standalone", count: 0 },
    ],
    by_health: { UNKNOWN: 0, HEALTHY: 0, WARNING: 0, MAJOR: 0, CRITICAL: 0 },
    in_maintenance: 0,
    ...overrides,
  };
}

function site(
  site_id: string,
  name: string,
  slices: {
    UPI: Partial<Breakdown>;
    HOSTED_CLUSTER: Partial<Breakdown>;
    AVAILABLE?: Partial<Breakdown>;
  },
): SiteStats {
  const upi = breakdown(slices.UPI);
  const hosted = breakdown(slices.HOSTED_CLUSTER);
  const available = breakdown(slices.AVAILABLE);
  return {
    site_id,
    name,
    ...breakdown({
      total: upi.total + hosted.total,
      by_health: {
        UNKNOWN: 0,
        HEALTHY: 0,
       
        WARNING: 0,
        MAJOR: upi.by_health.MAJOR + hosted.by_health.MAJOR,
        CRITICAL: upi.by_health.CRITICAL + hosted.by_health.CRITICAL,
      },
    }),
    by_installation_type: {
      UPI: upi,
      HOSTED_CLUSTER: hosted,
      MCE: breakdown(),
      UNCLASSIFIED: breakdown(),
    },
    by_openshift_state: {
      AVAILABLE: available,
      INSTALLED: breakdown({ total: upi.total + hosted.total - available.total }),
      INSTALLED_TO_INVENTORY: breakdown(),
    },
  };
}

/** Fixture-only mirror of the backend aggregation, so the mock response is
 * internally consistent; the page itself reads `fleet` as given. */
function sumBreakdowns(records: Breakdown[]): Breakdown {
  const result = breakdown();
  for (const record of records) {
    result.total += record.total;
    result.in_maintenance += record.in_maintenance;
    for (const entry of record.by_vendor) {
      const existing = result.by_vendor.find((v) => v.vendor === entry.vendor);
      if (existing) existing.count += entry.count;
    }
    for (const [severity, count] of Object.entries(record.by_health)) {
      result.by_health[severity as keyof typeof result.by_health] += count;
    }
  }
  return result;
}

const SITE_ITEMS = [
  site("tlv", "Tel Aviv", {
    UPI: { total: 30, by_health: { UNKNOWN: 0, HEALTHY: 28, WARNING: 0, MAJOR: 0, CRITICAL: 2 } },
    HOSTED_CLUSTER: { total: 12 },
    AVAILABLE: { total: 7 },
  }),
  site("nyc", "New York City", {
    UPI: { total: 5 },
    HOSTED_CLUSTER: { total: 3, by_health: { UNKNOWN: 0, HEALTHY: 2, WARNING: 0, MAJOR: 0, CRITICAL: 1 } },
    AVAILABLE: { total: 2 },
  }),
];

const SITES_RESPONSE = {
  items: SITE_ITEMS,
  fleet: {
    ...sumBreakdowns(SITE_ITEMS),
    by_installation_type: {
      UPI: sumBreakdowns(SITE_ITEMS.map((s) => s.by_installation_type.UPI)),
      HOSTED_CLUSTER: sumBreakdowns(SITE_ITEMS.map((s) => s.by_installation_type.HOSTED_CLUSTER)),
      MCE: sumBreakdowns(SITE_ITEMS.map((s) => s.by_installation_type.MCE)),
      UNCLASSIFIED: sumBreakdowns(SITE_ITEMS.map((s) => s.by_installation_type.UNCLASSIFIED)),
    },
    by_openshift_state: {
      AVAILABLE: sumBreakdowns(SITE_ITEMS.map((s) => s.by_openshift_state.AVAILABLE)),
      INSTALLED: sumBreakdowns(SITE_ITEMS.map((s) => s.by_openshift_state.INSTALLED)),
      INSTALLED_TO_INVENTORY: sumBreakdowns(
        SITE_ITEMS.map((s) => s.by_openshift_state.INSTALLED_TO_INVENTORY),
      ),
    },
  },
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createMemoryRouter([{ path: "/", element: <SitesOverviewPage /> }], {
    initialEntries: ["/"],
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

function card(name: string): HTMLAnchorElement {
  const heading = screen.getByRole("heading", { name });
  const link = heading.closest("a");
  expect(link).not.toBeNull();
  return link as HTMLAnchorElement;
}

const EMPTY_SLICES = { UPI: {}, HOSTED_CLUSTER: {} };

function stubSites(response: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(response) }),
    ),
  );
}

describe("SitesOverviewPage", () => {
  beforeEach(() => {
    stubSites(SITES_RESPONSE);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the fleet-wide cards from the backend's own fleet summary", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Across all sites" })).toBeInTheDocument();
    });

    expect(within(card("Across all sites")).getByText("50")).toBeInTheDocument();
    expect(within(card("UPI")).getByText("35")).toBeInTheDocument();
    expect(within(card("Hosted cluster")).getByText("15")).toBeInTheDocument();
    // The fixture has no MCE servers; the card must still render at "0".
    expect(within(card("MCE")).getByText("0")).toBeInTheDocument();
  });

  it("links each fleet-wide card to the matching pre-filtered server list", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "UPI" })).toBeInTheDocument();
    });

    expect(card("Across all sites")).toHaveAttribute("href", "/servers");
    expect(card("UPI")).toHaveAttribute("href", "/servers?installation_type=UPI");
    expect(card("Hosted cluster")).toHaveAttribute(
      "href",
      "/servers?installation_type=HOSTED_CLUSTER",
    );
    expect(card("MCE")).toHaveAttribute("href", "/servers?installation_type=MCE");
    expect(card("Available")).toHaveAttribute(
      "href",
      "/servers?openshift_state=AVAILABLE",
    );
    expect(card("Installed")).toHaveAttribute(
      "href",
      "/servers?openshift_state=INSTALLED",
    );
  });

  it("orders the fleet-wide cards so the grid's two rows read as intended", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Available" })).toBeInTheDocument();
    });

    const names = ["Across all sites", "UPI", "Hosted cluster", "MCE", "Available", "Installed"];
    const positions = names.map((name) =>
      Array.prototype.indexOf.call(document.querySelectorAll("a"), card(name)),
    );
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  it("counts the OpenShift cards off the backend's own slice", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Available" })).toBeInTheDocument();
    });

    // 7 in Tel Aviv + 2 in New York.
    expect(within(card("Available")).getByText("9")).toBeInTheDocument();
  });

  it("trusts the backend's fleet field rather than recomputing it from items", async () => {
    // Deliberately inconsistent with SITE_ITEMS (which sum to 50).
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              items: SITE_ITEMS,
              fleet: {
                ...breakdown({ total: 999 }),
                by_installation_type: {
                  UPI: breakdown(),
                  HOSTED_CLUSTER: breakdown(),
                  MCE: breakdown(),
                  UNCLASSIFIED: breakdown(),
                },
                by_openshift_state: {
                  AVAILABLE: breakdown(),
                  INSTALLED: breakdown(),
                  INSTALLED_TO_INVENTORY: breakdown(),
                },
              },
            }),
        }),
      ),
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Across all sites" })).toBeInTheDocument();
    });

    expect(within(card("Across all sites")).getByText("999")).toBeInTheDocument();
  });

  it("carries health into the installation-type cards", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "UPI" })).toBeInTheDocument();
    });

    expect(within(card("UPI")).getByText("2")).toBeInTheDocument();
    expect(within(card("UPI")).getByText(/critical/)).toBeInTheDocument();
    expect(within(card("Hosted cluster")).getByText("1")).toBeInTheDocument();
  });

  it("still renders one card per site, linked by site_id", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Tel Aviv" })).toBeInTheDocument();
    });

    expect(card("Tel Aviv")).toHaveAttribute("href", "/servers?site_id=tlv");
    expect(card("New York City")).toHaveAttribute("href", "/servers?site_id=nyc");
  });

  it("hides the Unassigned card when no server landed in it", async () => {
    stubSites({ ...SITES_RESPONSE, items: [...SITE_ITEMS, site("unassigned", "Unassigned", EMPTY_SLICES)] });
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Tel Aviv" })).toBeInTheDocument();
    });

    expect(screen.queryByRole("heading", { name: "Unassigned" })).not.toBeInTheDocument();
  });

  it("shows installed/available counts on each site card, from its own slice", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Tel Aviv" })).toBeInTheDocument();
    });

    // Tel Aviv: 42 total, 7 available -> 35 installed.
    expect(within(card("Tel Aviv")).getByText("35")).toBeInTheDocument();
    expect(within(card("Tel Aviv")).getByText(/installed/)).toBeInTheDocument();
    expect(within(card("Tel Aviv")).getByText("7")).toBeInTheDocument();
    expect(within(card("Tel Aviv")).getByText(/available/)).toBeInTheDocument();

    // New York: 8 total, 2 available -> 6 installed.
    expect(within(card("New York City")).getByText("6")).toBeInTheDocument();
    expect(within(card("New York City")).getByText("2")).toBeInTheDocument();
  });

  it("links a site card's installed/available counts to a pre-filtered server list", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Tel Aviv" })).toBeInTheDocument();
    });

    const installedLink = within(card("Tel Aviv")).getByText(/installed/).closest('[role="link"]');
    const availableLink = within(card("Tel Aviv")).getByText(/available/).closest('[role="link"]');
    expect(installedLink).toHaveAttribute("tabindex", "0");
    expect(availableLink).toHaveAttribute("tabindex", "0");
  });

  it("does not show installed/available counts on the fleet-wide cards", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Across all sites" })).toBeInTheDocument();
    });

    // "Across all sites" already has its own dedicated Installed/Available
    // fleet cards elsewhere on the page; it must not duplicate them inline.
    expect(within(card("Across all sites")).queryByText(/installed/)).not.toBeInTheDocument();
  });

  it("shows the Unassigned card as soon as a hostname fails to parse", async () => {
    stubSites({
      ...SITES_RESPONSE,
      items: [
        ...SITE_ITEMS,
        site("unassigned", "Unassigned", { UPI: { total: 4 }, HOSTED_CLUSTER: {} }),
      ],
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Unassigned" })).toBeInTheDocument();
    });

    expect(card("Unassigned")).toHaveAttribute("href", "/servers?site_id=unassigned");
  });
});
