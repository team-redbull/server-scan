import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMemoryRouter, RouterProvider } from "react-router";

import { EventsPage } from "@/features/events/EventsPage";
import { HistoryTab } from "@/features/events/HistoryTab";
import type { AuditEventResponse } from "@/types/events";

function ev(
  id: string,
  over: Partial<AuditEventResponse> = {},
): AuditEventResponse {
  return {
    id,
    event_type: "HEALTH_CHANGED",
    server_id: "srv_1",
    server_name: "ocp-worker-001",
    actor: { type: "SYSTEM", id: "sys", display: null },
    request_id: null,
    created_at: "2026-08-12T10:00:00Z",
    data: { from: "HEALTHY", to: "CRITICAL" },
    ...over,
  };
}

function json(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: new Headers(),
    json: () => Promise.resolve(body),
  });
}

function page(items: AuditEventResponse[], next: string | null = null) {
  return {
    items,
    page: { next_cursor: next, has_more: next !== null, page_size: 50 },
  };
}

function renderAt(ui: React.ReactElement, path = "/") {
  const router = createMemoryRouter([{ path: "*", element: ui }], {
    initialEntries: [path],
  });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

function urlsOf(m: ReturnType<typeof vi.fn>): string[] {
  return m.mock.calls.map((c) => String(c[0]));
}

describe("events UI", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("History tab lists events and loads the next page by cursor", async () => {
    fetchMock.mockImplementation((url: string) =>
      url.includes("cursor=c1")
        ? json(page([ev("e2", { event_type: "SERVER_CREATED", data: {} })]))
        : json(page([ev("e1")], "c1")),
    );
    renderAt(<HistoryTab serverId="srv_1" />);

    expect(
      await screen.findByRole("group", { name: "HEALTHY → CRITICAL" }),
    ).toBeInTheDocument();
    expect(fetchMock.mock.calls[0]?.[0]).toContain(
      "/api/v1/servers/srv_1/events",
    );
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("SERVER_CREATED")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
  });

  it("History tab shows the empty state", async () => {
    fetchMock.mockImplementation(() => json(page([])));
    renderAt(<HistoryTab serverId="srv_1" />);
    expect(await screen.findByText("No events recorded.")).toBeInTheDocument();
  });

  it("History tab shows an error", async () => {
    fetchMock.mockImplementation(() => Promise.reject(new Error("boom")));
    renderAt(<HistoryTab serverId="srv_1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });

  it("Events page shows the server name column and sends filters from the URL", async () => {
    fetchMock.mockImplementation((url: string) =>
      url.includes("/events/actors")
        ? json({ items: [] })
        : json(page([ev("e1")])),
    );
    renderAt(
      <EventsPage />,
      "/events?event_type=HEALTH_CHANGED&server_name=ocp-dell&actor_id=sys&range=custom&from=01/10/2026 09:00&to=02/10/2026",
    );

    expect(
      await screen.findByRole("link", { name: "ocp-worker-001" }),
    ).toHaveAttribute("href", "/servers/srv_1");
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]));
      expect(
        urls.some(
          (u) =>
            u.includes("/api/v1/events?") &&
            u.includes("event_type=HEALTH_CHANGED") &&
            u.includes("server_name=ocp-dell") &&
            u.includes("actor_id=sys") &&
            u.includes("since=2026-10-01T06%3A00%3A00.000Z") &&
            u.includes("until=2026-10-01T21%3A00%3A00.000Z"),
        ),
      ).toBe(true);
    });
    expect(urlsOf(fetchMock).some((u) => u.includes("server_id="))).toBe(false);
  });

  const ACTORS = {
    items: [
      { id: "alice", type: "USER", display: "Alice A", event_count: 3 },
      { id: "api-token-admin", type: "TOKEN", display: null, event_count: 2 },
      { id: "api-token-viewer", type: "TOKEN", display: null, event_count: 1 },
      { id: "ingest", type: "SYSTEM", display: null, event_count: 9 },
    ],
  };

  function renderWithActors(path = "/events") {
    fetchMock.mockImplementation((url: string) =>
      url.includes("/events/actors") ? json(ACTORS) : json(page([ev("e1")])),
    );
    renderAt(<EventsPage />, path);
  }

  async function openUserList() {
    const input = await screen.findByRole("combobox", { name: "User" });
    fireEvent.focus(input);
    const list = await screen.findByRole("listbox", { name: "Users" });
    await within(list).findByRole("option", { name: /Alice A/ });
    return { input, list };
  }

  function labelsIn(list: HTMLElement) {
    return within(list)
      .getAllByRole("option")
      .map((o) => o.textContent);
  }

  it("the User field is a combobox listing All first, then actors with labels and counts", async () => {
    renderWithActors();
    const { list } = await openUserList();
    expect(labelsIn(list)).toEqual([
      "All",
      "Alice A3",
      "API token (admin)2",
      "API token (viewer)1",
      "System (ingest)9",
    ]);
  });

  it("typing narrows the list by label or id, and picking one filters by its id", async () => {
    renderWithActors();
    const { input, list } = await openUserList();
    fireEvent.change(input, { target: { value: "TOK" } });
    expect(labelsIn(list)).toEqual(["API token (admin)2", "API token (viewer)1"]);

    fireEvent.change(input, { target: { value: "ingest" } });
    expect(labelsIn(list)).toEqual(["System (ingest)9"]);

    fireEvent.change(input, { target: { value: "api-token-adm" } });
    fireEvent.click(
      within(list).getByRole("option", { name: /API token \(admin\)/ }),
    );
    await waitFor(() => {
      expect(
        urlsOf(fetchMock).some((u) => u.includes("actor_id=api-token-admin")),
      ).toBe(true);
    });
    expect(input).toHaveValue("API token (admin)");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("says so when nothing matches, and Enter picks the highlighted match", async () => {
    renderWithActors();
    const { input } = await openUserList();
    fireEvent.change(input, { target: { value: "zzz" } });
    expect(screen.getByText("No matching user")).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "alice" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(urlsOf(fetchMock).some((u) => u.includes("actor_id=alice"))).toBe(
        true,
      );
    });
    expect(input).toHaveValue("Alice A");
  });

  it("shows the selected user's label, and All clears the filter", async () => {
    renderWithActors("/events?actor_id=alice");
    const { input, list } = await openUserList();
    expect(input).toHaveValue("Alice A");

    fireEvent.click(within(list).getByRole("option", { name: "All" }));
    await waitFor(() => {
      expect(input).toHaveValue("");
    });
    const eventUrls = urlsOf(fetchMock).filter((u) =>
      u.includes("/api/v1/events?"),
    );
    expect(eventUrls[eventUrls.length - 1]).not.toContain("actor_id");
  });

  it("changing the type select updates the URL-driven filter", async () => {
    fetchMock.mockImplementation((url: string) =>
      url.includes("/events/actors")
        ? json({ items: [] })
        : json(page([ev("e1")])),
    );
    renderAt(<EventsPage />, "/events");
    await screen.findByRole("group", { name: "HEALTHY → CRITICAL" });
    fireEvent.change(screen.getByLabelText("Event type"), {
      target: { value: "SERVER_PRUNED" },
    });
    await waitFor(() => {
      expect(
        urlsOf(fetchMock).some((u) => u.includes("event_type=SERVER_PRUNED")),
      ).toBe(true);
    });
  });

  it("orders the filters: Event type, Server, User, Time range", async () => {
    fetchMock.mockImplementation(() => json(page([ev("e1")])));
    renderAt(<EventsPage />, "/events");
    await screen.findByRole("group", { name: "HEALTHY → CRITICAL" });
    const labels = Array.from(document.querySelectorAll("label")).map(
      (l) => l.textContent,
    );
    expect(labels).toEqual(["Event type", "Server", "User", "Time range"]);
  });

  it("lays out Server, What happened, When, User and never shows a raw id as the server", async () => {
    fetchMock.mockImplementation((url: string) =>
      url.includes("/events/actors")
        ? json({ items: [] })
        : json(
            page([
              ev("e1"),
              ev("e2", { server_id: "srv_gone", server_name: null }),
            ]),
          ),
    );
    renderAt(<EventsPage />, "/events");
    await screen.findByText("Unknown server");
    const heads = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(heads).toEqual(["Server", "What happened", "When", "User"]);
    expect(screen.getByText("Unknown server")).toHaveAttribute(
      "title",
      "srv_gone",
    );
    expect(screen.queryByText("srv_gone")).not.toBeInTheDocument();
    expect(screen.queryByText("srv_1")).not.toBeInTheDocument();
  });

  it("History tab has no Server column", async () => {
    fetchMock.mockImplementation(() => json(page([ev("e1")])));
    renderAt(<HistoryTab serverId="srv_1" />);
    await screen.findByRole("group", { name: "HEALTHY → CRITICAL" });
    expect(
      screen.getAllByRole("columnheader").map((h) => h.textContent),
    ).toEqual(["What happened", "When", "User"]);
  });
});
