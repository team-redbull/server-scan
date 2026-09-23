import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMemoryRouter, RouterProvider } from "react-router";

import { InventoryPage } from "@/features/inventory/InventoryPage";
import type { ServerRow, ServerRowsResponse } from "@/types/server";

function makeServer(overrides: Partial<ServerRow> = {}): ServerRow {
  return {
    id: "srv_1",
    name: "ocp-dell-worker-001",
    vendor: "dell",
    model: "PowerEdge R760",
    site_id: "tlv",
    source_provider: "OPENMANAGE",
    installation_type: "UPI",
    health: "HEALTHY",
    maintenance: { enabled: false, reason: null },
    openshift_state: "INSTALLED",
    cluster_name: "ocp4-tlv",
    mce_name: null,
    profile_template_name: null,
    last_seen_at: "2026-08-12T10:00:00Z",
    stale: false,
    reachable: true,
    serial: "SN001",
    bmc_host: "bmc-001.example",
    macs: ["aa:bb:cc:dd:ee:01"],
    ...overrides,
  };
}

function rowsResponse(items: ServerRow[]): ServerRowsResponse {
  return { items, generated_at: "2026-08-12T10:00:00Z" };
}

/** The pair every facet test uses: one Dell, one Cisco. */
const TWO_VENDORS = [
  makeServer(),
  makeServer({
    id: "srv_2",
    name: "ucs-cisco-worker-002",
    vendor: "cisco",
    model: "UCS C240",
    source_provider: "INTERSIGHT",
    openshift_state: "AVAILABLE",
    cluster_name: null,
  }),
];

/** Every test has to answer `GET /api/v1/sites` for the site filter. */
const SITES_RESPONSE = {
  items: [
    {
      site_id: "tlv",
      name: "Tel Aviv",
      total: 1,
      by_vendor: [],
      by_health: { UNKNOWN: 0, HEALTHY: 1, WARNING: 0, MAJOR: 0, CRITICAL: 0 },
      in_maintenance: 0,
    },
  ],
};

function jsonResponse(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  });
}

function renderInventoryPage(initialEntry = "/") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createMemoryRouter([{ path: "/", element: <InventoryPage /> }], {
    initialEntries: [initialEntry],
  });

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );

  return { router };
}

function rowNames(): string[] {
  return screen.getAllByRole("row").slice(1).map((tr) => within(tr).getAllByRole("cell")[0]?.textContent ?? "");
}

describe("InventoryPage", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  /** Route the sites request to the fixed list, everything else to `handler`. */
  function mockRows(handler: (url: URL) => unknown) {
    fetchMock.mockImplementation((input: string) => {
      const url = new URL(input, "http://localhost");
      if (url.pathname === "/api/v1/sites") {
        return jsonResponse(SITES_RESPONSE);
      }
      return handler(url);
    });
  }

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders rows from the mocked API response", async () => {
    mockRows(() => jsonResponse(rowsResponse(TWO_VENDORS)));

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });
    expect(screen.getByText("ucs-cisco-worker-002")).toBeInTheDocument();
    expect(screen.getByText("UCS C240")).toBeInTheDocument();
    expect(screen.getAllByText("Healthy")).toHaveLength(2);
    expect(screen.getByText("2 servers")).toBeInTheDocument();
    expect(screen.getByText(/^Updated /)).toBeInTheDocument();
  });

  it("links each row's BMC host to its own console, and renders nothing without one", async () => {
    mockRows(() =>
      jsonResponse(
        rowsResponse([
          makeServer({ bmc_host: "10.2.3.4" }),
          makeServer({ id: "srv_2", name: "no-bmc-01", bmc_host: null }),
        ]),
      ),
    );

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });

    const bmcLink = screen.getByRole("link", { name: /open bmc console for ocp-dell-worker-001/i });
    expect(bmcLink).toHaveAttribute("href", "https://10.2.3.4");
    expect(bmcLink).toHaveAttribute("target", "_blank");
    expect(
      screen.queryByRole("link", { name: /open bmc console for no-bmc-01/i }),
    ).not.toBeInTheDocument();
  });

  it("shows the cluster as a column, and hides MCE until a row has one", async () => {
    mockRows(() => jsonResponse(rowsResponse([makeServer()])));

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByRole("columnheader", { name: /cluster/i })).toBeInTheDocument();
    });
    expect(screen.getAllByText("ocp4-tlv").length).toBeGreaterThan(0);
    expect(screen.queryByRole("columnheader", { name: /^mce$/i })).not.toBeInTheDocument();
  });

  it("shows the MCE column as soon as one row reports one", async () => {
    mockRows(() =>
      jsonResponse(
        rowsResponse([
          makeServer(),
          makeServer({
            id: "srv_mce",
            name: "ocp4-hypershift-tlv-01",
            mce_name: "mce-tlv",
            cluster_name: "hc-tlv-01",
          }),
        ]),
      ),
    );

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByRole("columnheader", { name: /^mce$/i })).toBeInTheDocument();
    });
    expect(within(screen.getByRole("table")).getByText("mce-tlv")).toBeInTheDocument();
  });

  it("sorts in place when the Installation header is clicked, and records it in the URL", async () => {
    mockRows(() => jsonResponse(rowsResponse(TWO_VENDORS)));

    const { router } = renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /installation/i })).toBeInTheDocument();
    });
    expect(rowNames()).toEqual(["ocp-dell-worker-001", "ucs-cisco-worker-002"]);

    fireEvent.click(screen.getByRole("button", { name: /installation/i }));

    await waitFor(() => {
      expect(router.state.location.search).toContain("sort=openshift_state");
    });
    // AVAILABLE sorts before INSTALLED.
    expect(rowNames()).toEqual(["ucs-cisco-worker-002", "ocp-dell-worker-001"]);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/rows"))).toHaveLength(1);
  });

  it("filters in the browser and updates the URL when a filter changes", async () => {
    mockRows(() => jsonResponse(rowsResponse(TWO_VENDORS)));

    const { router } = renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Vendor"), { target: { value: "cisco" } });

    await waitFor(() => {
      expect(router.state.location.search).toContain("vendor=cisco");
    });
    expect(screen.queryByText("ocp-dell-worker-001")).not.toBeInTheDocument();
    expect(screen.getByText("ucs-cisco-worker-002")).toBeInTheDocument();
    expect(screen.getByText("1 server")).toBeInTheDocument();
  });

  it("keeps only stale rows when Stale is ticked, and marks stale rows", async () => {
    mockRows(() =>
      jsonResponse(
        rowsResponse([
          makeServer({ id: "srv_fresh", name: "fresh-01" }),
          makeServer({
            id: "srv_stale",
            name: "stale-01",
            stale: true,
            last_seen_at: new Date(Date.now() - 20 * 3600 * 1000).toISOString(),
          }),
        ]),
      ),
    );

    const { router } = renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("stale-01")).toBeInTheDocument();
    });
    // The chip marks the stale row even with no filter applied.
    expect(screen.getByText("Stale 20h")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText(/^Stale/));

    await waitFor(() => {
      expect(router.state.location.search).toContain("stale=true");
    });
    expect(screen.queryByText("fresh-01")).not.toBeInTheDocument();
    expect(screen.getByText("stale-01")).toBeInTheDocument();
    // The toggles count only while ticked — no "(21)" on an option nobody chose.
    expect(screen.getByLabelText(/^Stale \(1\)/)).toBeChecked();
    expect(screen.getByLabelText(/^Maintenance$/)).not.toBeChecked();
  });

  it("keeps only rows sharing a name when Duplicate is ticked", async () => {
    mockRows(() =>
      jsonResponse(
        rowsResponse([
          makeServer({ id: "srv_hp", name: "ocp4-five-compute-06", vendor: "hp" }),
          makeServer({ id: "srv_cisco", name: "ocp4-five-compute-06", vendor: "cisco" }),
          makeServer({ id: "srv_unique", name: "ocp4-five-compute-07" }),
        ]),
      ),
    );

    const { router } = renderInventoryPage();

    await waitFor(() => {
      expect(screen.getAllByText("ocp4-five-compute-06")).toHaveLength(2);
    });

    fireEvent.click(screen.getByLabelText(/^Duplicate/));

    await waitFor(() => {
      expect(router.state.location.search).toContain("duplicate=true");
    });
    expect(screen.getAllByText("ocp4-five-compute-06")).toHaveLength(2);
    expect(screen.queryByText("ocp4-five-compute-07")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/^Duplicate \(2\)/)).toBeChecked();
  });

  it("searches by substring across name, serial, BMC host and MAC", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockRows(() => jsonResponse(rowsResponse(TWO_VENDORS)));

    renderInventoryPage();
    await waitFor(() => {
      expect(screen.getByText("ucs-cisco-worker-002")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByPlaceholderText("Name, serial, tag, BMC…"), {
      target: { value: "isco" },
    });
    await vi.advanceTimersByTimeAsync(300);

    await waitFor(() => {
      expect(screen.queryByText("ocp-dell-worker-001")).not.toBeInTheDocument();
    });
    expect(screen.getByText("ucs-cisco-worker-002")).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("pages through the sorted set and resets to page 1 when a filter changes", async () => {
    const fleet = Array.from({ length: 51 }, (_, i) =>
      makeServer({ id: `srv_${i}`, name: `srv-${String(i).padStart(3, "0")}` }),
    );
    fleet[50] = makeServer({ id: "srv_50", name: "srv-050", vendor: "cisco" });
    mockRows(() => jsonResponse(rowsResponse(fleet)));

    const { router } = renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("srv-000")).toBeInTheDocument();
    });
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.queryByText("srv-050")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => {
      expect(router.state.location.search).toContain("page=2");
    });
    expect(screen.getByText("srv-050")).toBeInTheDocument();
    expect(screen.getByText("Page 2 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Vendor"), { target: { value: "cisco" } });

    await waitFor(() => {
      expect(router.state.location.search).not.toContain("page=");
    });
    expect(screen.getByText("Page 1 of 1")).toBeInTheDocument();
    expect(screen.getByText("srv-050")).toBeInTheDocument();
  });

  it("clamps an out-of-range page in the URL", async () => {
    mockRows(() => jsonResponse(rowsResponse([makeServer()])));

    renderInventoryPage("/?page=7");

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });
    expect(screen.getByText("Page 1 of 1")).toBeInTheDocument();
  });

  it("shows the API error detail when the request fails", async () => {
    mockRows(() =>
      Promise.resolve({
        ok: false,
        status: 503,
        json: () =>
          Promise.resolve({
            type: "/problems/service-unavailable",
            title: "Service Unavailable",
            status: 503,
            detail: "MongoDB is unreachable",
            instance: "/api/v1/servers/rows",
            code: "SERVICE_UNAVAILABLE",
            request_id: "req_123",
            details: {},
          }),
      }),
    );

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("MongoDB is unreachable")).toBeInTheDocument();
    });
  });

  it("shows how many servers each filter option would match", async () => {
    mockRows(() => jsonResponse(rowsResponse(TWO_VENDORS)));

    renderInventoryPage();

    expect(await screen.findByRole("option", { name: "dell (1)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "OpenManage (Dell) (1)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "HEALTHY (2)" })).toBeInTheDocument();
  });

  it("shows (0) on an option matching nothing, and counts sites", async () => {
    mockRows(() => jsonResponse(rowsResponse(TWO_VENDORS)));

    renderInventoryPage();

    expect(await screen.findByRole("option", { name: "hp (0)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /^Tel Aviv \(\d+\)$/ })).toBeInTheDocument();
  });

  it("counts within the other filters, and stays silent on the filtered dimension", async () => {
    mockRows(() => jsonResponse(rowsResponse(TWO_VENDORS)));

    renderInventoryPage("/?vendor=cisco");

    expect(await screen.findByRole("option", { name: "Intersight (1)" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "OpenManage (Dell) (1)" })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "cisco" })).toBeInTheDocument();
  });

  it("asks why before putting a server into maintenance, and sends the reason", async () => {
    // Stateful like the real API: once PUT, the fleet reports the maintenance.
    let inMaintenance = false;
    mockRows((url) => {
      if (url.pathname === "/api/v1/servers/srv_1/maintenance") {
        inMaintenance = true;
      }
      const server = makeServer({
        maintenance: { enabled: inMaintenance, reason: inMaintenance ? "Replacing PSU 2" : null },
      });
      return jsonResponse(url.pathname.endsWith("/rows") ? rowsResponse([server]) : server);
    });

    const { router } = renderInventoryPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "Put ocp-dell-worker-001 into maintenance" }),
    );

    const card = screen.getByRole("dialog");
    expect(
      (fetchMock.mock.calls as [string, RequestInit | undefined][]).some(
        ([, init]) => init?.method === "PUT",
      ),
    ).toBe(false);

    fireEvent.change(within(card).getByLabelText(/why is it going into maintenance/i), {
      target: { value: "  Replacing PSU 2  " },
    });
    fireEvent.click(within(card).getByRole("button", { name: "Start maintenance" }));

    await waitFor(() => {
      const put = (fetchMock.mock.calls as [string, RequestInit | undefined][]).find(
        ([input, init]) =>
          init?.method === "PUT" && input.endsWith("/api/v1/servers/srv_1/maintenance"),
      );
      expect(put).toBeDefined();
      expect(JSON.parse(put?.[1]?.body as string)).toEqual({ reason: "Replacing PSU 2" });
    });
    // The row is patched from the response before any refetch lands.
    expect(
      await screen.findByRole("button", { name: "End maintenance on ocp-dell-worker-001" }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("cancels the maintenance card without writing anything", async () => {
    mockRows(() => jsonResponse(rowsResponse([makeServer()])));

    renderInventoryPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "Put ocp-dell-worker-001 into maintenance" }),
    );
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      (fetchMock.mock.calls as [string, RequestInit | undefined][]).some(
        ([, init]) => init?.method === "PUT",
      ),
    ).toBe(false);
  });

  it("ends maintenance in one click, with no card", async () => {
    mockRows((url) =>
      url.pathname === "/api/v1/servers/srv_1/maintenance"
        ? jsonResponse(makeServer())
        : jsonResponse(
            rowsResponse([makeServer({ maintenance: { enabled: true, reason: "disk swap" } })]),
          ),
    );

    renderInventoryPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "End maintenance on ocp-dell-worker-001" }),
    );

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => {
      const del = (fetchMock.mock.calls as [string, RequestInit | undefined][]).find(
        ([input, init]) =>
          init?.method === "DELETE" && input.endsWith("/api/v1/servers/srv_1/maintenance"),
      );
      expect(del).toBeDefined();
    });
  });
  it("filters by MCE and cluster from the sidebar, keeps them in the URL, and removes them from the chips", async () => {
    mockRows(() =>
      jsonResponse(
        rowsResponse([
          makeServer({ id: "a", name: "srv-a", mce_name: "mce-a", cluster_name: "hosted-1", installation_type: "HOSTED_CLUSTER" }),
          makeServer({ id: "b", name: "srv-b", mce_name: "mce-b", cluster_name: "hosted-2", installation_type: "HOSTED_CLUSTER" }),
          makeServer({ id: "c", name: "srv-c", mce_name: null, cluster_name: "upi-1" }),
        ]),
      ),
    );

    const { router } = renderInventoryPage();
    await waitFor(() => {
      expect(screen.getByText("srv-a")).toBeInTheDocument();
    });

    const sidebar = screen.getByRole("complementary", { name: "Cluster filters" });
    expect(within(sidebar).getByRole("region", { name: "MCE" })).toBeInTheDocument();
    expect(within(sidebar).getByRole("region", { name: "Hosted clusters" })).toBeInTheDocument();
    expect(within(sidebar).getByRole("region", { name: "UPI clusters" })).toBeInTheDocument();

    const mces = within(within(sidebar).getByRole("region", { name: "MCE" }));
    fireEvent.click(mces.getByRole("checkbox", { name: /mce-a/ }));
    await waitFor(() => {
      expect(rowNames()).toEqual(["srv-a"]);
    });
    expect(router.state.location.search).toBe("?mce=mce-a");

    // The page reads the live window.location, which a memory router never touches.
    window.history.replaceState(null, "", `/${router.state.location.search}`);
    fireEvent.click(mces.getByRole("checkbox", { name: /mce-b/ }));
    await waitFor(() => {
      expect(rowNames()).toEqual(["srv-a", "srv-b"]);
    });

    window.history.replaceState(null, "", `/${router.state.location.search}`);
    fireEvent.click(screen.getByRole("button", { name: /^MCE mce-a/ }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("?mce=mce-b");
    });
    window.history.replaceState(null, "", `/${router.state.location.search}`);

    fireEvent.click(within(sidebar).getByRole("checkbox", { name: /upi-1/ }));
    await waitFor(() => {
      expect(screen.getByText(/No servers match: MCE mce-b, Cluster upi-1/)).toBeInTheDocument();
    });
    window.history.replaceState(null, "", "/");
  });
});
