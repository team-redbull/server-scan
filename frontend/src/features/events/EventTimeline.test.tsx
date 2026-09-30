import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { EventTimeline } from "@/features/events/EventTimeline";
import type { AuditEventResponse } from "@/types/events";

function event(id: string, data: Record<string, unknown>): AuditEventResponse {
  return {
    id,
    event_type: "MAINTENANCE_ENABLED",
    server_id: `srv_${id}`,
    server_name: `ocp-server-${id}`,
    actor: { type: "USER", id: "alice", display: null },
    request_id: null,
    created_at: "2026-09-30T21:34:17Z",
    data,
  };
}

describe("EventTimeline details", () => {
  it("opens the details in their own full-width row and leaves the other cells alone", () => {
    render(
      <MemoryRouter>
        <EventTimeline
          showServer
          events={[
            event("a", { reason: "x".repeat(300) }),
            event("b", { reason: "y" }),
          ]}
        />
      </MemoryRouter>,
    );
    const before = screen.getAllByRole("row").length;
    const serverCell = screen.getByText("ocp-server-a").closest("td");

    fireEvent.click(screen.getAllByRole("button", { name: /details/i })[0]!);

    expect(screen.getAllByRole("row")).toHaveLength(before + 1);
    expect(
      screen.getByText(/x{300}/, { selector: "pre" }).closest("td"),
    ).toHaveAttribute("colspan", "4");
    expect(screen.getByText("ocp-server-a").closest("td")).toBe(serverCell);
    expect(
      screen.getAllByRole("button", { name: /details/i })[0],
    ).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getAllByRole("button", { name: /details/i })[1],
    ).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(screen.getAllByRole("button", { name: /details/i })[0]!);
    expect(screen.getAllByRole("row")).toHaveLength(before);
  });
});

describe("EventTimeline rows", () => {
  it("shows only the event type in the row; the sentence is in the details", () => {
    render(
      <MemoryRouter>
        <EventTimeline
          events={[
            {
              ...event("m", { reason: "Replacing PSU 2" }),
              event_type: "MAINTENANCE_ENABLED",
            },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("MAINTENANCE_ENABLED")).toBeInTheDocument();
    expect(screen.queryByText(/Replacing PSU 2/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /details/i }));
    expect(
      screen.getByText("Put into maintenance by alice: Replacing PSU 2"),
    ).toBeInTheDocument();
  });

  it("keeps a FROM → TO under the type for a status-change event", () => {
    render(
      <MemoryRouter>
        <EventTimeline
          events={[
            {
              ...event("c", { from: "UPI", to: "HOSTED" }),
              event_type: "CLASSIFICATION_CHANGED",
            },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("UPI → HOSTED")).toBeInTheDocument();
    expect(screen.queryByText(/Classification UPI/)).not.toBeInTheDocument();
  });
});

describe("EventTimeline health changes", () => {
  const reason = (
    key: string,
    name: string,
    severity: string,
    message: string,
  ) => ({
    policy_key: key,
    policy_name: name,
    category: "storage",
    severity,
    message,
  });
  const renderHealth = (data: Record<string, unknown>) => {
    render(
      <MemoryRouter>
        <EventTimeline
          events={[{ ...event("h", data), event_type: "HEALTH_CHANGED" }]}
        />
      </MemoryRouter>,
    );
  };

  it("shows only the transition in the row; FROM and TO reasons are in the details", () => {
    renderHealth({
      from: "MAJOR",
      to: "CRITICAL",
      from_reasons: [reason("m.key", "Memory degraded", "MAJOR", "1 DIMM bad")],
      to_reasons: [
        reason("s.key", "OS disk failed", "CRITICAL", "2 of 8 drives failed"),
      ],
    });

    expect(
      screen.getByRole("group", { name: "MAJOR → CRITICAL" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("2 of 8 drives failed")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /details/i }));
    const from = within(screen.getByRole("region", { name: "FROM" }));
    const to = within(screen.getByRole("region", { name: "TO" }));
    for (const label of [
      "Policy key",
      "Policy name",
      "Category",
      "Severity",
      "Message",
    ]) {
      expect(from.getByText(label)).toBeInTheDocument();
      expect(to.getByText(label)).toBeInTheDocument();
    }
    for (const v of ["m.key", "Memory degraded", "MAJOR", "1 DIMM bad"]) {
      expect(from.getAllByText(v).length).toBeGreaterThan(0);
    }
    for (const v of [
      "s.key",
      "OS disk failed",
      "CRITICAL",
      "2 of 8 drives failed",
    ]) {
      expect(to.getAllByText(v).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("storage")).toHaveLength(2);
  });

  it("shows only the most severe reason per side, and the first one on a tie", () => {
    renderHealth({
      from: "MAJOR",
      to: "CRITICAL",
      from_reasons: [
        reason(
          "psu.major",
          "PSU redundancy lost",
          "MAJOR",
          "1 of 2 PSUs failed",
        ),
        reason("disk.major", "OS disk bad", "MAJOR", "1 OS disk bad"),
        reason("disk.warn", "Data disk degraded", "WARNING", "1 data disk bad"),
      ],
      to_reasons: [
        reason(
          "disk.warn2",
          "Data disk degraded",
          "WARNING",
          "1 data disk bad",
        ),
        reason("psu.crit", "PSU failed", "CRITICAL", "2 of 2 PSUs failed"),
        reason("disk.major2", "OS disk bad", "MAJOR", "1 OS disk bad"),
      ],
    });
    fireEvent.click(screen.getByRole("button", { name: /details/i }));
    const from = within(screen.getByRole("region", { name: "FROM" }));
    const to = within(screen.getByRole("region", { name: "TO" }));

    expect(from.getByText("psu.major")).toBeInTheDocument();
    expect(from.queryByText("disk.major")).not.toBeInTheDocument();
    expect(from.queryByText("disk.warn")).not.toBeInTheDocument();
    expect(to.getByText("psu.crit")).toBeInTheDocument();
    expect(to.queryByText("disk.major2")).not.toBeInTheDocument();
    expect(to.queryByText("disk.warn2")).not.toBeInTheDocument();
  });

  it("says nothing failing for an empty side and dashes a missing value", () => {
    renderHealth({
      from: "CRITICAL",
      to: "HEALTHY",
      from_reasons: [
        {
          policy_key: "a",
          policy_name: "A",
          category: null,
          severity: null,
          message: null,
        },
      ],
      to_reasons: [],
    });
    fireEvent.click(screen.getByRole("button", { name: /details/i }));
    expect(screen.getByText("nothing failing")).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "FROM" })).getAllByText("—"),
    ).toHaveLength(3);
  });

  it("falls back to a TO section for a legacy event and none without reasons", () => {
    renderHealth({
      from: "HEALTHY",
      to: "CRITICAL",
      reasons: [reason("k", "Old", "CRITICAL", "legacy msg")],
      resolved: [],
    });
    fireEvent.click(screen.getByRole("button", { name: /details/i }));
    expect(
      screen.queryByRole("region", { name: "FROM" }),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "TO" })).getByText(
        "legacy msg",
      ),
    ).toBeInTheDocument();
  });

  it("shows only the raw JSON for an event with neither", () => {
    renderHealth({ from: "HEALTHY", to: "CRITICAL" });
    fireEvent.click(screen.getByRole("button", { name: /details/i }));
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
  });
});

describe("EventTimeline classification details", () => {
  function open(data: Record<string, unknown>) {
    render(
      <MemoryRouter>
        <EventTimeline
          events={[
            { ...event("c", data), event_type: "CLASSIFICATION_CHANGED" },
          ]}
        />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: /details/i }));
  }

  it("shows the rule name and what it matched on, and hides legacy keys", () => {
    open({
      from: "UNCLASSIFIED",
      to: "HOSTED_CLUSTER",
      matched_rule: "Hypershift",
      matched_field: "name",
      matched_pattern: "^ocp4-hypershift",
      matched_rule_id: "r1",
      ticket: "T-1",
    });
    expect(screen.getByText("Matched rule")).toBeInTheDocument();
    expect(screen.getByText("Hypershift")).toBeInTheDocument();
    expect(screen.getByText("name ~ ^ocp4-hypershift")).toBeInTheDocument();
    const json = screen.getByText(/"from"/, { selector: "pre" });
    expect(json.textContent).not.toMatch(/matched_rule_id|ticket/);
  });

  it("shows nothing extra for a legacy event with only a rule id", () => {
    open({ from: "UNCLASSIFIED", to: "UPI", matched_rule_id: "r1" });
    expect(screen.queryByText("Matched rule")).not.toBeInTheDocument();
  });
});
