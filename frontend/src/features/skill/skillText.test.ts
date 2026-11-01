import { describe, expect, it } from "vitest";

import { extractPrompts, outline } from "@/features/skill/skillText";

describe("extractPrompts", () => {
  it("lifts the quoted examples out of the description", () => {
    const prompts = extractPrompts('Use for ("all Dell servers in tlv", "which servers are available").');

    expect(prompts).toEqual(["All Dell servers in tlv", "Which servers are available"]);
  });

  it("ignores short quotes and repeats", () => {
    expect(extractPrompts('say "ok" and "all Dell servers in tlv"', '"all Dell servers in tlv"')).toEqual([
      "All Dell servers in tlv",
    ]);
  });
});

describe("outline", () => {
  it("lists ## and ### headings, skipping fenced code", () => {
    const headings = outline("# Title\n## One\n```\n## not a heading\n```\n### Two\n");

    expect(headings.map((h) => [h.level, h.text, h.line])).toEqual([
      [2, "One", 1],
      [3, "Two", 5],
    ]);
  });
});
