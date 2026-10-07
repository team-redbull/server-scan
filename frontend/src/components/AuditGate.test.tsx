import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppNav } from "@/components/AppNav";
import { AuditGate } from "@/components/AuditGate";
import type { Role } from "@/api/auth";

function stubMe(role: Role | null, loginRequired = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          login_required: loginRequired,
          authenticated: true,
          username: "u",
          role,
        }),
    }),
  );
}

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("audit trail visibility", () => {
  it.each<[Role, boolean]>([
    ["ADMIN", true],
    ["AUDITOR", true],
    ["VIEWER", false],
  ])("%s sees the Events link: %s", async (role, visible) => {
    stubMe(role);
    renderWithClient(<AppNav />);
    await screen.findByText("Servers");
    await screen.findByText(role);
    if (visible) {
      expect(screen.getByRole("link", { name: "Events" })).toBeInTheDocument();
    } else {
      expect(
        screen.queryByRole("link", { name: "Events" }),
      ).not.toBeInTheDocument();
    }
    expect(
      screen.getByRole("link", { name: "Rules & Policies" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Architecture" }),
    ).toBeInTheDocument();
  });

  it("shows Events with auth disabled, where the caller is the dev admin", async () => {
    stubMe("ADMIN", false);
    renderWithClient(<AppNav />);
    expect(
      await screen.findByRole("link", { name: "Events" }),
    ).toBeInTheDocument();
  });

  it("AuditGate shows a viewer a message instead of the page", async () => {
    stubMe("VIEWER");
    renderWithClient(
      <AuditGate>
        <p>the events list</p>
      </AuditGate>,
    );
    expect(
      await screen.findByText(/admins and auditors only/),
    ).toBeInTheDocument();
    expect(screen.queryByText("the events list")).not.toBeInTheDocument();
  });

  it("AuditGate renders the page for an auditor", async () => {
    stubMe("AUDITOR");
    renderWithClient(
      <AuditGate>
        <p>the events list</p>
      </AuditGate>,
    );
    expect(await screen.findByText("the events list")).toBeInTheDocument();
  });
});
