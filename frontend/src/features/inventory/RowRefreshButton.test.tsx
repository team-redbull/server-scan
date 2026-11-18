import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Role } from "@/api/auth";
import { RowRefreshButton } from "@/features/inventory/RowRefreshButton";
import { showToast } from "@/lib/toast";
import type { ServerRow } from "@/types/server";

vi.mock("@/lib/toast", () => ({ showToast: vi.fn() }));

const ROW = { id: "srv_1", name: "ocp-dell-worker-000" } as unknown as ServerRow;

function stubApi(role: Role) {
  const fetchMock = vi.fn().mockImplementation((input: string) => {
    const path = new URL(input, "http://localhost").pathname;
    const body =
      path === "/api/v1/auth/me"
        ? { login_required: true, authenticated: true, username: "u", role }
        : { id: "srv_1", name: ROW.name, source_provider: "OPENMANAGE", revision: 2 };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderButton(onRowClick = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <div onClick={onRowClick}>
        <RowRefreshButton row={ROW} />
      </div>
    </QueryClientProvider>,
  );
  return onRowClick;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(showToast).mockClear();
});

describe("RowRefreshButton", () => {
  it("fetches the one server for an admin without opening the row", async () => {
    const fetchMock = stubApi("ADMIN");
    const rowClick = renderButton();

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    fireEvent.click(screen.getByRole("button", { name: "Fetch data of ocp-dell-worker-000 now" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/servers/srv_1/refresh",
        expect.objectContaining({ method: "POST" }),
      );
    });
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Fetched ocp-dell-worker-000", ok: true }),
      );
    });
    expect(rowClick).not.toHaveBeenCalled();
  });

  it("does nothing for a viewer", async () => {
    const fetchMock = stubApi("VIEWER");
    renderButton();

    const button = await screen.findByRole("button", {
      name: "Only admins can fetch a server's data",
    });
    fireEvent.click(button);

    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/refresh"))).toBe(false);
  });
});
