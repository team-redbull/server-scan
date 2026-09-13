import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HistoryPanel } from "@/features/events/HistoryPanel";
import type { AuditEventListResponse, AuditEventResponse } from "@/types/events";

function makeEvent(overrides: Partial<AuditEventResponse> = {}): AuditEventResponse {
  return {
    id: "evt_1",
    event_type: "CLASSIFICATION_RULE_CREATED",
    server_id: null,
    actor: { type: "USER", id: "user_1", display: "baruch" },
    request_id: "req_1",
    created_at: "2026-08-12T10:00:00Z",
    data: { rule_id: "crul_1", name: "dell-vendor-hosted-cluster" },
    ...overrides,
  };
}

function pageResponse(items: AuditEventResponse[]): AuditEventListResponse {
  return { items, page: { next_cursor: null, has_more: false, page_size: 200 } };
}

function jsonResponse(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <HistoryPanel
        eventTypes={["CLASSIFICATION_RULE_CREATED", "CLASSIFICATION_RULE_UPDATED", "CLASSIFICATION_RULE_DELETED"]}
        idField="rule_id"
        entityId="crul_1"
      />
    </QueryClientProvider>,
  );
}

describe("HistoryPanel", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows events for the current entity id and excludes events for a different id", async () => {
    fetchMock.mockImplementation((input: string) => {
      const url = new URL(input, "http://localhost");
      const eventType = url.searchParams.get("event_type");
      if (eventType === "CLASSIFICATION_RULE_CREATED") {
        return jsonResponse(
          pageResponse([
            makeEvent({ id: "evt_1", data: { rule_id: "crul_1", name: "dell-vendor-hosted-cluster" } }),
            makeEvent({ id: "evt_2", data: { rule_id: "crul_2", name: "some-other-rule" } }),
          ]),
        );
      }
      return jsonResponse(pageResponse([]));
    });

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("CLASSIFICATION_RULE_CREATED")).toBeInTheDocument();
    });

    expect(screen.getByText("dell-vendor-hosted-cluster")).toBeInTheDocument();
    expect(screen.queryByText("some-other-rule")).not.toBeInTheDocument();
  });

  it("shows an empty state when nothing matches", async () => {
    fetchMock.mockImplementation(() => jsonResponse(pageResponse([])));

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("No history recorded yet.")).toBeInTheDocument();
    });
  });
});
