import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MaintenanceToggle } from "@/features/inventory/MaintenanceToggle";
import type { ServerRow } from "@/types/server";

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
    last_seen_at: "2026-08-12T10:00:00Z",
    stale: false,
    reachable: true,
    serial: "SN001",
    bmc_host: "bmc-001.example",
    macs: ["aa:bb:cc:dd:ee:01"],
    ...overrides,
  };
}

function jsonResponse(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  });
}

function renderToggle(role: "ADMIN" | "VIEWER", server: ServerRow = makeServer()) {
  const fetchMock = vi.fn().mockImplementation((input: string) => {
    const url = new URL(input, "http://localhost");
    if (url.pathname === "/api/v1/auth/me") {
      return jsonResponse({ login_required: true, authenticated: true, username: "u", role });
    }
    return jsonResponse(server);
  });
  vi.stubGlobal("fetch", fetchMock);

  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MaintenanceToggle server={server} />
    </QueryClientProvider>,
  );
  return fetchMock;
}

describe("MaintenanceToggle", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("is a live enabled button for an admin", async () => {
    renderToggle("ADMIN");
    const button = await screen.findByRole("button", { name: "Put ocp-dell-worker-001 into maintenance" });
    await waitFor(() => {
      expect(button).not.toHaveAttribute("aria-disabled");
    });
  });

  it("is shown greyed out with an admin-only tooltip for a viewer, and a click is a no-op", async () => {
    const fetchMock = renderToggle("VIEWER");
    const button = await screen.findByRole("button", { name: "Only admins can change maintenance" });

    await waitFor(() => {
      expect(button).toHaveAttribute("aria-disabled", "true");
    });
    expect(button).toHaveAttribute("title", "Only admins can change maintenance");

    const callsBeforeClick = fetchMock.mock.calls.length;
    button.click();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.length).toBe(callsBeforeClick);
  });
});
