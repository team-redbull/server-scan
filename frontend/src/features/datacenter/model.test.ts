import { describe, expect, it } from "vitest";

import { fiConnections, generateDemo, MODELS } from "@/features/datacenter/model";

describe("generateDemo", () => {
  const data = generateDemo(1000, 7);

  it("makes exactly the requested servers across every Cisco model", () => {
    expect(data.units).toHaveLength(1000);
    const models = new Set(data.units.map((u) => u.model));
    expect(models.size).toBe(14);
    expect([...models].every((m) => MODELS[m] !== undefined)).toBe(true);
  });

  it("is deterministic and keeps every server inside a 42U rack", () => {
    expect(generateDemo(1000, 7).units.map((u) => u.name)).toEqual(data.units.map((u) => u.name));
    for (const u of data.units) {
      expect(u.u + (MODELS[u.model]?.h ?? 1)).toBeLessThanOrEqual(42);
    }
  });

  it("puts every blade in a chassis and gives each row an A/B FI pair", () => {
    const blades = data.units.filter((u) => MODELS[u.model]?.kind === "blade");
    expect(blades.length).toBeGreaterThan(0);
    expect(blades.every((b) => b.links.length === 0)).toBe(true);
    expect(data.chassis.length).toBeGreaterThan(0);
    expect(data.fis).toHaveLength(data.rows * 2);
  });

  it("lists every cable of an FI by port, with the blades behind a chassis", () => {
    const fi = data.fis[0];
    expect(fi).toBeDefined();
    const conns = fiConnections(data, 0);
    const ports = conns.map((c) => Number(c.port.split("/")[1]));
    expect(ports).toEqual([...ports].sort((a, b) => a - b));
    expect(new Set(ports).size).toBe(ports.length);
    const cabled = [...data.units, ...data.chassis].filter((o) => o.row === fi?.row).flatMap((o) => o.links);
    expect(conns).toHaveLength(cabled.filter((l) => l.fabric === fi?.slot).length);
    const chassisConn = conns.find((c) => c.key.startsWith("chassis:"));
    expect(chassisConn?.blades.length).toBeGreaterThan(0);
  });
});
