import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMemoryRouter, RouterProvider } from "react-router";

import { InventoryPage } from "@/features/inventory/InventoryPage";
import type { ServerListResponse, ServerSummary } from "@/types/server";

function makeServer(overrides: Partial<ServerSummary> = {}): ServerSummary {
  return {
    id: "srv_1",
    name: "ocp-dell-worker-001",
    vendor: "dell",
    model: "PowerEdge R760",
    site_id: "tlv",
    manager_id: "mgr_ome_tlv_01",
    source_provider: "UCS_CENTRAL",
    classification: { installation_type: "HOSTED_CLUSTER", matched_rule_id: null },
    health: {
      overall: "HEALTHY",
      cpu: "HEALTHY",
      memory: "HEALTHY",
      storage: "HEALTHY",
      network: "HEALTHY",
      connectivity: "HEALTHY",
      power: "HEALTHY",
      gpu: "UNKNOWN",
    },
    maintenance: { enabled: false, reason: null },
    openshift: {
      lifecycle_state: "INSTALLED",
      mce_name: null,
      cluster_name: "ocp4-tlv",
      last_reported_at: "2026-08-12T10:00:00Z",
      reported_by_agent_id: "ocp4-tlv",
    },
    connectivity: {
      facts: {
        fabric_paths_total: 2,
        fabric_paths_up: 2,
        fabric_paths_down: 0,
        fabrics_present: ["A", "B"],
      },
    },
    last_seen_at: "2026-08-12T10:00:00Z",
    reachable: true,
    unreachable_since: null,
    updated_at: "2026-08-12T10:00:00Z",
    ...overrides,
  };
}

function pageResponse(
  items: ServerSummary[],
  page: Partial<ServerListResponse["page"]> = {},
): ServerListResponse {
  return {
    items,
    page: {
      next_cursor: null,
      has_more: false,
      page_size: 50,
      count: null,
      count_capped: false,
      ...page,
    },
  };
}

/** Every test has to answer `GET /api/v1/sites` for the site filter. */
const SITES_RESPONSE = {
  items: [
    {
      site_id: "tlv",
      name: "Tel Aviv",
      total: 1,
      by_vendor: [],
      by_health: { UNKNOWN: 0, HEALTHY: 1, INFO: 0, WARNING: 0, CRITICAL: 0 },
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

function renderInventoryPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createMemoryRouter(
    [{ path: "/", element: <InventoryPage /> }],
    {
      initialEntries: ["/"],
    },
  );

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );

  return { router };
}

/** The most recent request for the server *list* — facets and sites
 * requests are excluded, since facets drops the pagination params. */
function lastRequestUrl(fetchMock: ReturnType<typeof vi.fn>): URL {
  const urls = (fetchMock.mock.calls as [string, RequestInit][])
    .map(([input]) => new URL(input, "http://localhost"))
    .filter((url) => url.pathname === "/api/v1/servers");
  const last = urls.at(-1);
  if (!last) {
    throw new Error("fetch was never called for the server list");
  }
  return last;
}

const FACETS_RESPONSE = {
  total: 2,
  vendor: { dell: 1, cisco: 1 },
  source_provider: { OPENMANAGE: 1, INTERSIGHT: 1 },
  installation_type: { UPI: 2 },
  health_overall: { HEALTHY: 2 },
  maintenance: { false: 2 },
  openshift_state: { INSTALLED: 1, AVAILABLE: 1 },
};

describe("InventoryPage", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  /** Route the sites request to the fixed list, everything else to `handler`. */
  function mockServerList(handler: (url: URL) => unknown) {
    fetchMock.mockImplementation((input: string) => {
      const url = new URL(input, "http://localhost");
      if (url.pathname === "/api/v1/sites") {
        return jsonResponse(SITES_RESPONSE);
      }
      if (url.pathname === "/api/v1/servers/facets") {
        return jsonResponse(FACETS_RESPONSE);
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
    mockServerList(() =>
      jsonResponse(
        pageResponse([
          makeServer(),
          makeServer({
            id: "srv_2",
            name: "ucs-cisco-worker-002",
            vendor: "cisco",
            model: "UCS C240",
          }),
        ]),
      ),
    );

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });
    expect(screen.getByText("ucs-cisco-worker-002")).toBeInTheDocument();
    expect(screen.getByText("UCS C240")).toBeInTheDocument();
    // Fabric, vendor, site and classification are not columns any more.
    expect(screen.getAllByText("Healthy")).toHaveLength(2);
    expect(screen.queryByText("2/2 up")).not.toBeInTheDocument();
  });

  it("shows the cluster as a column, and hides MCE until a row has one", async () => {
    mockServerList(() => jsonResponse(pageResponse([makeServer()])));

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByRole("columnheader", { name: /cluster/i })).toBeInTheDocument();
    });
    expect(screen.getAllByText("ocp4-tlv").length).toBeGreaterThan(0);
    expect(screen.queryByRole("columnheader", { name: /^mce$/i })).not.toBeInTheDocument();
  });

  it("shows the MCE column as soon as one row reports one", async () => {
    mockServerList(() =>
      jsonResponse(
        pageResponse([
          makeServer(),
          makeServer({
            id: "srv_mce",
            name: "ocp4-hypershift-tlv-01",
            openshift: {
              lifecycle_state: "INSTALLED",
              mce_name: "mce-tlv",
              cluster_name: "hc-tlv-01",
              last_reported_at: "2026-08-12T10:00:00Z",
              reported_by_agent_id: "mce-tlv",
            },
          }),
        ]),
      ),
    );

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByRole("columnheader", { name: /^mce$/i })).toBeInTheDocument();
    });
    expect(screen.getByText("mce-tlv")).toBeInTheDocument();
  });

  it("sorts by installation when the Installation header is clicked", async () => {
    const requests: string[] = [];
    mockServerList((url) => {
      requests.push(url.search);
      return jsonResponse(pageResponse([makeServer()]));
    });

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /installation/i })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /installation/i }));

    await waitFor(() => {
      expect(requests.some((url) => url.includes("sort=openshift_state"))).toBe(true);
    });
  });

  it("updates the URL search params when a filter changes", async () => {
    mockServerList(() => jsonResponse(pageResponse([makeServer()])));

    const { router } = renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });

    const vendorSelect = screen.getByLabelText("Vendor");
    fireEvent.change(vendorSelect, { target: { value: "cisco" } });

    await waitFor(() => {
      expect(router.state.location.search).toContain("vendor=cisco");
    });

    await waitFor(() => {
      expect(lastRequestUrl(fetchMock).searchParams.get("vendor")).toBe(
        "cisco",
      );
    });
  });

  it("resets the cursor in the URL when a filter changes", async () => {
    mockServerList((url) => {
      if (url.searchParams.get("cursor") === "cursor-1") {
        return jsonResponse(
          pageResponse([makeServer({ id: "srv_2", name: "page-two-server" })]),
        );
      }
      return jsonResponse(
        pageResponse([makeServer()], {
          has_more: true,
          next_cursor: "cursor-1",
        }),
      );
    });

    const { router } = renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => {
      expect(router.state.location.search).toContain("cursor=cursor-1");
    });

    const vendorSelect = screen.getByLabelText("Vendor");
    fireEvent.change(vendorSelect, { target: { value: "dell" } });

    await waitFor(() => {
      expect(router.state.location.search).not.toContain("cursor=");
    });
  });

  it("paginates with the next_cursor and disables Next once has_more is false", async () => {
    mockServerList((url) => {
      if (url.searchParams.get("cursor") === "cursor-1") {
        return jsonResponse(
          pageResponse([makeServer({ id: "srv_2", name: "page-two-server" })], {
            has_more: false,
            next_cursor: null,
          }),
        );
      }
      return jsonResponse(
        pageResponse([makeServer()], {
          has_more: true,
          next_cursor: "cursor-1",
        }),
      );
    });

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });

    const nextButton = screen.getByRole("button", { name: "Next" });
    expect(nextButton).not.toBeDisabled();

    fireEvent.click(nextButton);

    await waitFor(() => {
      expect(screen.getByText("page-two-server")).toBeInTheDocument();
    });

    expect(lastRequestUrl(fetchMock).searchParams.get("cursor")).toBe(
      "cursor-1",
    );
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("shows the API error detail when the request fails", async () => {
    mockServerList(() =>
      Promise.resolve({
        ok: false,
        status: 422,
        json: () =>
          Promise.resolve({
            type: "/problems/page-size-too-large",
            title: "Unprocessable Entity",
            status: 422,
            detail: "page_size must be <= 200",
            instance: "/api/v1/servers",
            code: "PAGE_SIZE_TOO_LARGE",
            request_id: "req_123",
            details: {},
          }),
      }),
    );

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("page_size must be <= 200")).toBeInTheDocument();
    });
  });

  it("shows how many servers each filter option would match", async () => {
    mockServerList(() =>
      jsonResponse({ items: [], page: { next_cursor: null, has_more: false } }),
    );

    renderInventoryPage();

    expect(
      await screen.findByRole("option", { name: "dell (1)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "OpenManage (Dell) (1)" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "HEALTHY (2)" })).toBeInTheDocument();
  });

  it("leaves an option matching nothing unannotated rather than showing (0)", async () => {
    mockServerList(() =>
      jsonResponse({ items: [], page: { next_cursor: null, has_more: false } }),
    );

    renderInventoryPage();

    // hp is absent from FACETS_RESPONSE.vendor.
    expect(await screen.findByRole("option", { name: "hp" })).toBeInTheDocument();
  });

  it("drops pagination params from the facets request", async () => {
    mockServerList(() =>
      jsonResponse({ items: [], page: { next_cursor: null, has_more: false } }),
    );

    renderInventoryPage();
    await screen.findByRole("option", { name: "dell (1)" });

    const facetUrls = (fetchMock.mock.calls as [string, RequestInit][])
      .map(([input]) => new URL(input, "http://localhost"))
      .filter((url) => url.pathname === "/api/v1/servers/facets");

    expect(facetUrls.length).toBeGreaterThan(0);
    for (const url of facetUrls) {
      expect(url.searchParams.get("cursor")).toBeNull();
      expect(url.searchParams.get("page_size")).toBeNull();
      expect(url.searchParams.get("sort")).toBeNull();
    }
  });

  it("asks why before putting a server into maintenance, and sends the reason", async () => {
    mockServerList((url) =>
      url.pathname === "/api/v1/servers/srv_1/maintenance"
        ? jsonResponse(makeServer({ maintenance: { enabled: true, reason: "Replacing PSU 2" } }))
        : jsonResponse(pageResponse([makeServer()])),
    );

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
    expect(router.state.location.pathname).toBe("/");
  });

  it("cancels the maintenance card without writing anything", async () => {
    mockServerList(() => jsonResponse(pageResponse([makeServer()])));

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
    mockServerList((url) =>
      url.pathname === "/api/v1/servers/srv_1/maintenance"
        ? jsonResponse(makeServer())
        : jsonResponse(
            pageResponse([makeServer({ maintenance: { enabled: true, reason: "disk swap" } })]),
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

});
