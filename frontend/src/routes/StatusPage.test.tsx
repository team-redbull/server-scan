import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StatusPage } from "@/routes/StatusPage";

describe("StatusPage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          status: "ok",
          dependencies: { mongo: "ok", redis: "ok" },
        }),
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders backend readiness once the query resolves", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <StatusPage />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Checking…")).toBeInTheDocument();

    // "ok" appears three times (status/mongo/redis).
    await waitFor(() => {
      expect(screen.getAllByText("ok")).toHaveLength(3);
    });
  });
});
