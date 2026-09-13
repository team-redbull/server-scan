import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RulesPage } from "@/features/rules/RulesPage";

const RULES_RESPONSE = {
  items: [
    {
      id: "rule_1",
      name: "hypershift hostname",
      installation_type: "HOSTED_CLUSTER",
      source: "SYSTEM_DEFAULT",
      priority: 100,
      enabled: true,
      system: true,
      field: "name",
      pattern: "^ocp4-hypershift-",
      flags: { ignore_case: true, multiline: false, dotall: false },
      scope: { vendor: null, manager_type: null, site_id: "tlv" },
    },
    {
      id: "rule_2",
      name: "upi hostname",
      installation_type: "UPI",
      source: "SYSTEM_DEFAULT",
      priority: 50,
      enabled: true,
      system: true,
      field: "name",
      pattern: "^ocp4-(prod|dev)-",
      flags: { ignore_case: true, multiline: false, dotall: false },
      scope: { vendor: null, manager_type: null, site_id: null },
    },
  ],
};

function policy(
  id: string,
  name: string,
  severity: string,
  scope: { vendor: string | null; manager_types: string[]; site_id: string | null },
) {
  return {
    id,
    name,
    category: "storage",
    severity,
    policy_key: `storage.${id}`,
    mode: "THRESHOLD",
    enabled: true,
    system: true,
    condition: {
      metric: "storage.failed_drive_count",
      operator: "GTE",
      value: 1,
      all_of: null,
      any_of: null,
      not: null,
      equals: null,
    },
    scope,
  };
}

// Deliberately out of section and severity order.
const POLICIES_RESPONSE = {
  items: [
    policy("policy_1", "failed drive", "CRITICAL", {
      vendor: "dell",
      manager_types: [],
      site_id: null,
    }),
    policy("policy_2", "general warning", "WARNING", {
      vendor: null,
      manager_types: [],
      site_id: null,
    }),
    policy("policy_3", "general critical", "CRITICAL", {
      vendor: null,
      manager_types: [],
      site_id: null,
    }),
    policy("policy_4", "general major", "MAJOR", {
      vendor: null,
      manager_types: [],
      site_id: null,
    }),
    policy("policy_5", "cisco vnic down", "MAJOR", {
      vendor: "cisco",
      manager_types: ["UCS_CENTRAL", "INTERSIGHT"],
      site_id: null,
    }),
  ],
};

function jsonResponse(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

function renderRulesPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RulesPage />
    </QueryClientProvider>,
  );
}

describe("RulesPage", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn((input: string) => {
      const url = new URL(input, "http://localhost");
      if (url.pathname === "/api/v1/classification-rules") {
        return jsonResponse(RULES_RESPONSE);
      }
      if (url.pathname === "/api/v1/health-policies") {
        return jsonResponse(POLICIES_RESPONSE);
      }
      throw new Error(`unexpected request to ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows classification rules and health policies on one page", async () => {
    renderRulesPage();

    expect(await screen.findByText("hypershift hostname")).toBeInTheDocument();
    expect(await screen.findByText("failed drive")).toBeInTheDocument();
  });

  it("offers no way to create, edit, enable or delete anything", async () => {
    renderRulesPage();
    await screen.findByText("hypershift hostname");

    // By role, not by text: "enabled" is legitimate page content.
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByText(/new rule/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/new policy/i)).not.toBeInTheDocument();
  });

  it("asks the API for enabled entries only, and shows no status column", async () => {
    renderRulesPage();
    await screen.findByText("hypershift hostname");

    for (const call of fetchMock.mock.calls as [string][]) {
      const url = new URL(call[0], "http://localhost");
      expect(url.searchParams.get("enabled")).toBe("true");
    }
    expect(screen.queryByText("enabled")).not.toBeInTheDocument();
    expect(screen.queryByText("disabled")).not.toBeInTheDocument();
  });

  it("shows each rule's field and regex", async () => {
    renderRulesPage();

    expect(await screen.findByText(/\^ocp4-hypershift-/)).toBeInTheDocument();
    expect(screen.getByText(/\^ocp4-\(prod\|dev\)-/)).toBeInTheDocument();
  });

  it("renders a health policy's condition as readable text", async () => {
    renderRulesPage();

    const conditions = await screen.findAllByText("storage.failed_drive_count GTE 1");
    expect(conditions).toHaveLength(POLICIES_RESPONSE.items.length);
  });

  it("renders an unscoped rule as unscoped rather than blank", async () => {
    renderRulesPage();

    expect(await screen.findByText("(unscoped)")).toBeInTheDocument();
    expect(screen.getByText("site=tlv")).toBeInTheDocument();
  });

  it("groups health policies by scope, General first", async () => {
    renderRulesPage();

    const general = await screen.findByRole("heading", { name: "General" });
    const dell = screen.getByRole("heading", { name: "Dell" });
    const cisco = screen.getByRole("heading", { name: "Cisco — UCS Central, Intersight" });
    expect(precedes(general, dell)).toBe(true);
    expect(precedes(general, cisco)).toBe(true);
    expect(precedes(dell, cisco)).toBe(true);
    expect(precedes(screen.getByText("general major"), dell)).toBe(true);
    expect(precedes(dell, screen.getByText("cisco vnic down"))).toBe(true);
    expect(precedes(dell, screen.getByText("failed drive"))).toBe(true);
    expect(screen.queryByText("vendor=dell")).not.toBeInTheDocument();
  });

  it("sorts each section by severity: CRITICAL, MAJOR, WARNING", async () => {
    renderRulesPage();

    const critical = await screen.findByText("general critical");
    const major = screen.getByText("general major");
    const warning = screen.getByText("general warning");
    expect(precedes(critical, major)).toBe(true);
    expect(precedes(major, warning)).toBe(true);
  });
});

function precedes(a: HTMLElement, b: HTMLElement): boolean {
  return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}
