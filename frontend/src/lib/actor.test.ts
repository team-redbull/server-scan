import { describe, expect, it } from "vitest";

import { actorIdLabel, actorLabel } from "@/lib/actor";

describe("actorLabel", () => {
  it("shows a person by display name, falling back to the id", () => {
    expect(actorLabel({ type: "USER", id: "u1", display: "Alice A" })).toEqual({
      label: "Alice A",
      kind: "user",
    });
    expect(actorLabel({ type: "USER", id: "alice", display: null })).toEqual({
      label: "alice",
      kind: "user",
    });
  });
  it("names the static API tokens", () => {
    expect(
      actorLabel({ type: "TOKEN", id: "api-token-admin", display: null }),
    ).toEqual({ label: "API token (admin)", kind: "token" });
    expect(
      actorLabel({ type: "TOKEN", id: "api-token-viewer", display: null })
        .label,
    ).toBe("API token (viewer)");
  });
  it("marks the system actor", () => {
    expect(actorLabel({ type: "SYSTEM", id: "ingest", display: null })).toEqual(
      { label: "System (ingest)", kind: "system" },
    );
  });
  it("maps a bare actor id", () => {
    expect(actorIdLabel("api-token-admin")).toBe("API token (admin)");
    expect(actorIdLabel("bob")).toBe("bob");
  });
});

describe("a personal token", () => {
  it("reads as its owner, so an audited action is attributable", () => {
    expect(actorIdLabel("token:dana")).toBe("dana (personal token)");
    expect(actorLabel({ type: "TOKEN", id: "token:dana", display: "dana" })).toEqual({
      label: "dana (personal token)",
      kind: "token",
    });
  });
});
