import { describe, expect, it } from "vitest";

import { normalizeEventType, filtersToParams } from "@/features/events/filters";
import { parseIsraelInput } from "@/lib/datetime";

const BASE = {
  server: "",
  eventType: "",
  actorId: "",
  range: "",
  from: "",
  to: "",
};

describe("filtersToParams", () => {
  it("maps server, type and user to their API params", () => {
    expect(
      filtersToParams({
        ...BASE,
        server: " ocp-dell ",
        eventType: "SERVER_PRUNED",
        actorId: "alice",
      }),
    ).toEqual({
      server_name: "ocp-dell",
      event_type: "SERVER_PRUNED",
      actor_id: "alice",
    });
  });
  it("sends nothing for empty filters", () => {
    expect(filtersToParams(BASE)).toEqual({});
  });
  it("turns a preset into since, relative to now", () => {
    const now = new Date("2026-10-01T12:00:30Z");
    expect(filtersToParams({ ...BASE, range: "24h" }, now).since).toBe(
      "2026-09-30T12:00:00.000Z",
    );
  });
  it("reads custom bounds as Israel time and sends UTC", () => {
    expect(
      filtersToParams({
        ...BASE,
        range: "custom",
        from: "01/10/2026 09:00",
        to: "02/10/2026",
      }),
    ).toEqual({
      since: "2026-10-01T06:00:00.000Z",
      until: "2026-10-01T21:00:00.000Z",
    });
  });
  it("ignores an unparseable custom bound", () => {
    expect(
      filtersToParams({
        ...BASE,
        range: "custom",
        from: "garbage",
        to: "31/02/2026",
      }),
    ).toEqual({});
  });
});

describe("parseIsraelInput", () => {
  it("uses winter time (UTC+2) and summer time (UTC+3)", () => {
    expect(parseIsraelInput("15/01/2026 10:00")).toBe(
      "2026-01-15T08:00:00.000Z",
    );
    expect(parseIsraelInput("15/07/2026 10:00")).toBe(
      "2026-07-15T07:00:00.000Z",
    );
  });
});

describe("normalizeEventType", () => {
  it("maps the old name and the old shortcut label to HEALTH_CHANGED", () => {
    expect(normalizeEventType("HEALTH_STATUS_CHANGED")).toBe("HEALTH_CHANGED");
    expect(normalizeEventType("Health changes")).toBe("HEALTH_CHANGED");
    expect(normalizeEventType("SERVER_CREATED")).toBe("SERVER_CREATED");
  });
});
