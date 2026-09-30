import { describe, expect, it } from "vitest";

import {
  describeEvent,
  healthSections,
  WRITTEN_EVENT_TYPES,
} from "@/features/events/describeEvent";
import type { AuditEventResponse } from "@/types/events";

function ev(
  event_type: string,
  data: Record<string, unknown> = {},
): AuditEventResponse {
  return {
    id: "e",
    event_type,
    server_id: "srv_1",
    server_name: "ocp-worker-001",
    actor: { type: "SYSTEM", id: "sys", display: null },
    request_id: null,
    created_at: "2026-08-12T10:00:00Z",
    data,
  };
}

describe("describeEvent", () => {
  it.each([
    ["SERVER_CREATED", { vendor: "dell", name: "x" }, "Server discovered"],
    [
      "SERVER_PRUNED",
      { reason: "r" },
      "Pruned: no longer listed by its manager",
    ],
    [
      "HEALTH_CHANGED",
      { from: "HEALTHY", to: "CRITICAL" },
      "HEALTHY → CRITICAL",
    ],
    [
      "CLASSIFICATION_CHANGED",
      { from: "UPI", to: "HOSTED" },
      "Classification UPI -> HOSTED",
    ],
    [
      "OPENSHIFT_STATE_CHANGED",
      { from: "AVAILABLE", to: "INSTALLED", cluster_name: "ocp4-tlv" },
      "OpenShift state AVAILABLE -> INSTALLED (ocp4-tlv)",
    ],
    [
      "MAINTENANCE_ENABLED",
      { reason: "fan swap" },
      "Put into maintenance by System (sys): fan swap",
    ],
    [
      "MAINTENANCE_ENABLED",
      { reason: "Donor for x" },
      "Marked as parts donor by System (sys): Donor for x",
    ],
    [
      "MAINTENANCE_ENABLED",
      { reason: null },
      "Put into maintenance by System (sys)",
    ],
    [
      "MAINTENANCE_ENABLED",
      { reason: "legacy", ticket: "T-1" },
      "Put into maintenance by System (sys): legacy",
    ],
    [
      "MAINTENANCE_UPDATED",
      { reason: "longer" },
      "Updated maintenance by System (sys): longer",
    ],
    ["MAINTENANCE_DISABLED", {}, "Maintenance ended"],
    [
      "SERVER_RESERVED",
      {
        holder: "wf",
        mce_cluster: "mce1",
        infra_env: "i",
        workflow_id: "w",
        expires_at: "x",
      },
      "Reserved by wf for mce1",
    ],
    [
      "SERVER_RESERVATION_REFUSED",
      { requested_by: "a", held_by: "b", held_for_mce: "m" },
      "Reservation by a refused: held by b",
    ],
    [
      "SERVER_RESERVATION_REFUSED",
      { requested_by: "a", reason: "lost the revision race" },
      "Reservation by a refused: lost the revision race",
    ],
    [
      "SERVER_RELEASED",
      { holder: "wf", mce_cluster: "mce1", workflow_id: "w" },
      "Released from wf (mce1)",
    ],
    ["SOMETHING_NEW", {}, "SOMETHING_NEW"],
  ])("%s %j", (type, data, expected) => {
    expect(describeEvent(ev(type, data))).toBe(expected);
  });

  it("puts the actor into maintenance sentences", () => {
    const by = (actor: AuditEventResponse["actor"], reason: string) =>
      describeEvent({
        ...ev("MAINTENANCE_ENABLED", { reason }),
        actor,
      });
    expect(by({ type: "USER", id: "alice", display: null }, "fan swap")).toBe(
      "Put into maintenance by alice: fan swap",
    );
    expect(
      by({ type: "TOKEN", id: "api-token-admin", display: null }, "fan swap"),
    ).toBe("Put into maintenance by API token (admin): fan swap");
    expect(by({ type: "USER", id: "alice", display: "Alice" }, "donor")).toBe(
      "Marked as parts donor by Alice: donor",
    );
  });

  it("covers every written type", () => {
    for (const t of WRITTEN_EVENT_TYPES) {
      expect(describeEvent(ev(t))).not.toBe(t);
    }
  });

  describe("HEALTH_CHANGED reasons", () => {
    const base = { from: "MAJOR", to: "CRITICAL" };
    const reason = (name: string, message: string) => ({
      policy_key: name,
      policy_name: name,
      category: "storage",
      severity: "CRITICAL",
      message,
    });

    it("describes only the transition; reasons are separate sections", () => {
      const d = { ...base, to_reasons: [reason("A", "x"), reason("B", "y")] };
      expect(describeEvent(ev("HEALTH_CHANGED", d))).toBe(
        "MAJOR → CRITICAL",
      );
      expect(healthSections(d).to).toHaveLength(2);
      expect(healthSections(d).from).toBeNull();
    });

    it("reads both sides, falls back to legacy reasons, and has none otherwise", () => {
      const both = healthSections({
        from_reasons: [reason("A", "x")],
        to_reasons: [],
      });
      expect(both.from).toHaveLength(1);
      expect(both.to).toEqual([]);
      expect(healthSections({ reasons: [reason("A", "x")] }).to).toHaveLength(
        1,
      );
      expect(healthSections(base)).toEqual({ from: null, to: null });
    });
  });
});
