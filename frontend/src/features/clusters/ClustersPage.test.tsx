import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Role } from "@/api/auth";
import { ClustersPage } from "@/features/clusters/ClustersPage";
import { SilentClustersBanner } from "@/features/clusters/SilentClustersBanner";
import { showToast } from "@/lib/toast";

vi.mock("@/lib/toast", () => ({ showToast: vi.fn() }));

const DAY = 86400;
const JOBS = {
  silent_after_seconds: 7 * DAY,
  items: [
    { kind: "nodes", name: "ocp-tomer-bat-yam", held: 2, last_run_at: "2026-11-01T10:00:00Z", silent_seconds: 12 * DAY, silent: true },
    { kind: "agents", name: "mce-nyc", held: 9, last_run_at: "2026-11-19T10:00:00Z", silent_seconds: 600, silent: false },
  ],
};

function stubApi(role: Role) {
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    const path = new URL(input, "http://localhost").pathname;
    const body =
      path === "/api/v1/auth/me"
        ? { login_required: true, authenticated: true, username: "u", role }
        : init?.method === "POST"
          ? { kind: "nodes", name: "ocp-tomer-bat-yam", freed: 2 }
          : JOBS;
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderWith(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const posts = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST");

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(showToast).mockClear();
});

describe("ClustersPage", () => {
  it("marks a job silent for over seven days and leaves a live one reporting", async () => {
    stubApi("ADMIN");
    renderWith(<ClustersPage />);

    expect(await screen.findByText("Not reporting for 12 days")).toBeInTheDocument();
    expect(screen.getByText("Reporting")).toBeInTheDocument();
    expect(screen.getByText(/has not run for 7 days/)).toBeInTheDocument();
  });

  it("decommissions only after the name is typed, and says how many servers were freed", async () => {
    const fetchMock = stubApi("ADMIN");
    renderWith(<ClustersPage />);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Decommission ocp-tomer-bat-yam" })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "Decommission ocp-tomer-bat-yam" }));

    const confirm = screen.getByRole("button", { name: "Decommission" });
    expect(confirm).toBeDisabled();
    expect(screen.getByText(/2 servers it holds become Available/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Type the name/), { target: { value: "ocp-tomer-bat-yam" } });
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Decommissioned ocp-tomer-bat-yam", detail: "2 servers freed" }),
      );
    });
    expect(posts(fetchMock)[0]?.[0]).toBe("/api/v1/openshift/clusters/nodes/ocp-tomer-bat-yam/decommission");
  });
});

describe("SilentClustersBanner", () => {
  it("names the one silent cluster and links to the Clusters page", async () => {
    stubApi("ADMIN");
    renderWith(<SilentClustersBanner />);

    expect(await screen.findByText(/ocp-tomer-bat-yam has not reported for 12 days/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute("href", "/clusters");
  });

  it("asks the API nothing and shows nothing for a viewer", async () => {
    const fetchMock = stubApi("VIEWER");
    renderWith(<SilentClustersBanner />);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("status")).toBeNull();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/openshift/"))).toBe(false);
  });

  it("shows nothing for a server whose own cluster is reporting", async () => {
    const fetchMock = stubApi("ADMIN");
    renderWith(<SilentClustersBanner only={(job) => job.name === "mce-nyc"} />);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("status")).toBeNull();
  });
});
