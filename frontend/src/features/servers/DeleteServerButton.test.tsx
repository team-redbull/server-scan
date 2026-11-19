import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Role } from "@/api/auth";
import { DeleteServerButton } from "@/features/servers/DeleteServerButton";
import { showToast } from "@/lib/toast";
import type { ServerDetail } from "@/types/server";

vi.mock("@/lib/toast", () => ({ showToast: vi.fn() }));

const SERVER = { id: "srv_1", name: "ocp-dell-worker-000" } as unknown as ServerDetail;
const BLOCKED = {
  type: "/problems/server-delete-blocked",
  title: "Server Delete Blocked",
  status: 409,
  detail: "not deleted",
  code: "SERVER_DELETE_BLOCKED",
  details: { blockers: ["it is in maintenance", "it is in use by OpenShift (INSTALLED in hc-nyc-02)"] },
};

/** `deletes` answers each DELETE in turn: a status and body. */
function stubApi(role: Role, deletes: { status: number; body?: unknown }[]) {
  const queue = [...deletes];
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    const path = new URL(input, "http://localhost").pathname;
    if (path === "/api/v1/auth/me") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ login_required: true, authenticated: true, username: "u", role }),
      });
    }
    expect(init?.method).toBe("DELETE");
    const next = queue.shift() ?? { status: 204 };
    return Promise.resolve({
      ok: next.status < 400,
      status: next.status,
      json: () => Promise.resolve(next.body),
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderButton() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/servers/srv_1"]}>
        <Routes>
          <Route path="/servers/srv_1" element={<DeleteServerButton server={SERVER} />} />
          <Route path="/servers" element={<p>the inventory</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const deleteCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "DELETE");

async function openDialog() {
  const button = await screen.findByRole("button", { name: "Remove this server" });
  await waitFor(() => {
    expect(button).not.toHaveAttribute("aria-disabled");
  });
  fireEvent.click(button);
  return screen.findByRole("dialog");
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(showToast).mockClear();
});

describe("DeleteServerButton", () => {
  it("stays disabled until the server's name is typed, then deletes and returns to the inventory", async () => {
    const fetchMock = stubApi("ADMIN", [{ status: 204 }]);
    renderButton();
    await openDialog();

    const confirm = screen.getByRole("button", { name: "Remove server" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Type the server/), { target: { value: "ocp-dell-worker-00" } });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Type the server/), { target: { value: SERVER.name } });
    fireEvent.click(confirm);

    expect(await screen.findByText("the inventory")).toBeInTheDocument();
    expect(deleteCalls(fetchMock)[0]?.[0]).toBe("/api/v1/servers/srv_1");
    expect(showToast).toHaveBeenCalledWith(expect.objectContaining({ title: `Removed ${SERVER.name}`, ok: true }));
  });

  it("lists why a server in use is refused and deletes it only after Remove anyway, with force", async () => {
    const fetchMock = stubApi("ADMIN", [{ status: 409, body: BLOCKED }, { status: 204 }]);
    renderButton();
    await openDialog();
    fireEvent.change(screen.getByLabelText(/Type the server/), { target: { value: SERVER.name } });
    fireEvent.click(screen.getByRole("button", { name: "Remove server" }));

    expect(await screen.findByText("it is in maintenance")).toBeInTheDocument();
    expect(screen.getByText(/INSTALLED in hc-nyc-02/)).toBeInTheDocument();
    const anyway = screen.getByRole("button", { name: "Remove anyway" });
    expect(anyway).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "Remove anyway" }));
    fireEvent.click(anyway);

    expect(await screen.findByText("the inventory")).toBeInTheDocument();
    expect(deleteCalls(fetchMock).map(([url]) => url)).toEqual([
      "/api/v1/servers/srv_1",
      "/api/v1/servers/srv_1?force=true",
    ]);
  });

  it("does nothing for a viewer", async () => {
    const fetchMock = stubApi("VIEWER", []);
    renderButton();

    const button = await screen.findByRole("button", { name: "Only admins can remove a server" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(button);

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(deleteCalls(fetchMock)).toHaveLength(0);
  });

  it("closes on Cancel without deleting", async () => {
    const fetchMock = stubApi("ADMIN", []);
    renderButton();
    await openDialog();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(deleteCalls(fetchMock)).toHaveLength(0);
  });
});
