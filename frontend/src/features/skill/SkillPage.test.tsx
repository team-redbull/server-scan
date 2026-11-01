import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SkillPage } from "@/features/skill/SkillPage";

const SKILL = {
  name: "server-scan-api",
  description: 'Answers fleet questions, e.g. "all Dell servers in tlv".',
  when_to_use: "",
  allowed_tools: "Bash(curl *) Bash(jq *)",
  version: "abc123def456",
  updated_at: "2026-11-01T10:00:00Z",
  files: [{ path: "SKILL.md", content: "# Server Scan API\n## Connection\nBASE_URL=x\n" }],
};

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SkillPage />
    </QueryClientProvider>,
  );
}

describe("SkillPage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => SKILL }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the headline, a prompt from the description and the download", async () => {
    renderPage();

    expect(
      await screen.findByRole("heading", { level: 1, name: "Ask Claude about Server Scan" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /All Dell servers in tlv/ })).toBeInTheDocument();
    const download = screen.getByRole("link", { name: /Download server-scan-api\.zip/ });
    expect(download).toHaveAttribute("href", expect.stringContaining("/api/v1/skill/download?base_url="));
  });

  it("lists the files of the skill as tabs", async () => {
    renderPage();

    expect(await screen.findByRole("tab", { name: "SKILL.md", selected: true })).toBeInTheDocument();
  });

  it("explains itself when the server carries no skill", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({ detail: "none", code: "NOT_FOUND", status: 404 }),
      }),
    );
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent(/no skill/);
  });
});
