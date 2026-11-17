import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Role } from "@/api/auth";
import { RefreshButton } from "@/features/servers/RefreshButton";
import { showToast } from "@/lib/toast";
import type { ServerDetail } from "@/types/server";

vi.mock("@/lib/toast", () => ({ showToast: vi.fn() }));

const SERVER = {
  id: "srv_1",
  name: "ocp-dell-worker-000",
  source_provider: "OPENMANAGE",
  revision: 4,
} as unknown as ServerDetail;

function stubApi(role: Role, refresh: { status: number; body: unknown }) {
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    const path = new URL(input, "http://localhost").pathname;
    if (path === "/api/v1/auth/me") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({ login_required: true, authenticated: true, username: "u", role }),
      });
    }
    expect(path).toBe("/api/v1/servers/srv_1/refresh");
    expect(init?.method).toBe("POST");
    return Promise.resolve({
      ok: refresh.status < 400,
      status: refresh.status,
      json: () => Promise.resolve(refresh.body),
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderButton() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RefreshButton server={SERVER} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(showToast).mockClear();
});

describe("RefreshButton", () => {
  it("fetches for an admin and says when nothing changed", async () => {
    const show = vi.mocked(showToast);
    const fetchMock = stubApi("ADMIN", { status: 200, body: SERVER });
    renderButton();

    const button = await screen.findByRole("button", { name: "Fetch this server's data now" });
    await waitFor(() => {
      expect(button).not.toHaveAttribute("aria-disabled");
    });
    fireEvent.click(button);

    await waitFor(() => {
      expect(show).toHaveBeenCalledWith({
        title: "Fetched ocp-dell-worker-000",
        detail: "From OPENMANAGE — no changes",
        ok: true,
      });
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/servers/srv_1/refresh",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("says the data was updated when the revision moved", async () => {
    const show = vi.mocked(showToast);
    stubApi("ADMIN", { status: 200, body: { ...SERVER, revision: 5 } });
    renderButton();

    fireEvent.click(await screen.findByRole("button", { name: "Fetch this server's data now" }));

    await waitFor(() => {
      expect(show).toHaveBeenCalledWith(
        expect.objectContaining({ detail: "From OPENMANAGE — data updated", ok: true }),
      );
    });
  });

  it("shows the API's reason inline when the fetch fails", async () => {
    const show = vi.mocked(showToast);
    stubApi("ADMIN", {
      status: 409,
      body: {
        type: "/problems/server-refresh-unsupported",
        title: "Server Refresh Unsupported",
        status: 409,
        detail: "The API is not connected to the OPENMANAGE manager; run its collector job instead.",
        code: "SERVER_REFRESH_UNSUPPORTED",
      },
    });
    renderButton();

    fireEvent.click(await screen.findByRole("button", { name: "Fetch this server's data now" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("run its collector job instead");
    expect(show).toHaveBeenCalledWith(expect.objectContaining({ title: "Fetch failed", ok: false }));
  });

  it("is a no-op with an explanation for a viewer", async () => {
    const fetchMock = stubApi("VIEWER", { status: 200, body: SERVER });
    renderButton();

    const button = await screen.findByRole("button", {
      name: "Only admins can fetch a server's data",
    });
    expect(button).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(button);

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(
      fetchMock.mock.calls.some(([input]) => String(input).includes("/refresh")),
    ).toBe(false);
  });
});
