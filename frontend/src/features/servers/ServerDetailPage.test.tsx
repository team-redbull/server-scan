import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Role } from "@/api/auth";
import { ServerDetailPage } from "@/features/servers/ServerDetailPage";

vi.mock("@/features/servers/OverviewTab", () => ({
  OverviewTab: () => <p>overview body</p>,
}));
vi.mock("@/features/servers/HardwareTab", () => ({
  HardwareTab: () => <p>hardware body</p>,
}));
vi.mock("@/features/servers/NetworkTab", () => ({
  NetworkTab: () => <p>network body</p>,
}));
vi.mock("@/features/servers/ConnectivityTab", () => ({
  ConnectivityTab: () => <p>connectivity body</p>,
}));
vi.mock("@/features/events/HistoryTab", () => ({
  HistoryTab: () => <p>history body</p>,
}));

const SERVER = {
  id: "srv_1",
  name: "ocp-dell-worker-000",
  model: "PowerEdge R6515",
  hardware: { gpus: [] },
  network: { bmc: { host: null } },
  connectivity: {},
};

function stubApi(role: Role) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string) => {
      const isMe =
        new URL(input, "http://localhost").pathname === "/api/v1/auth/me";
      const body = isMe
        ? { login_required: true, authenticated: true, username: "u", role }
        : SERVER;
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      });
    }),
  );
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/servers/srv_1"]}>
        <Routes>
          <Route path="/servers/:id" element={<ServerDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ServerDetailPage History tab", () => {
  it.each<[Role, boolean]>([
    ["ADMIN", true],
    ["AUDITOR", true],
    ["VIEWER", false],
  ])("%s sees the History tab: %s", async (role, visible) => {
    stubApi(role);
    renderPage();
    await screen.findByRole("heading", { name: SERVER.name });
    await screen.findByRole("button", { name: "Connectivity" });
    if (visible) {
      expect(
        await screen.findByRole("button", { name: "History" }),
      ).toBeInTheDocument();
    } else {
      expect(
        screen.queryByRole("button", { name: "History" }),
      ).not.toBeInTheDocument();
    }
    expect(
      screen.getByRole("button", { name: "Hardware" }),
    ).toBeInTheDocument();
  });
});
