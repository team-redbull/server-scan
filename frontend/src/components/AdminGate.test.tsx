import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Role } from "@/api/auth";
import { AdminGate } from "@/components/AdminGate";
import { AppNav } from "@/components/AppNav";

function stubMe(role: Role) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({ login_required: true, authenticated: true, username: "u", role }),
    }),
  );
}

function renderWithClient(ui: React.ReactElement) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("admin-only Jobs", () => {
  it.each<[Role, boolean]>([
    ["ADMIN", true],
    ["AUDITOR", false],
    ["VIEWER", false],
  ])("%s sees the Jobs link: %s", async (role, visible) => {
    stubMe(role);
    renderWithClient(<AppNav />);
    await screen.findByText(role);
    expect(screen.queryByRole("link", { name: "Jobs" }) !== null).toBe(visible);
  });

  it("keeps a non-admin off the page", async () => {
    stubMe("AUDITOR");
    renderWithClient(
      <AdminGate>
        <p>secret</p>
      </AdminGate>,
    );
    expect(await screen.findByText(/admins only/)).toBeInTheDocument();
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });
});
