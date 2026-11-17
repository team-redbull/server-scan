import { describe, expect, it } from "vitest";

import { installationOf } from "@/lib/installation";

describe("installationOf", () => {
  it("is MAINTENANCE for a server no cluster holds that is in maintenance", () => {
    expect(installationOf("AVAILABLE", true)).toBe("MAINTENANCE");
  });

  it("keeps AVAILABLE when not in maintenance", () => {
    expect(installationOf("AVAILABLE", false)).toBe("AVAILABLE");
  });

  it("keeps an installed server's state, maintenance or not", () => {
    expect(installationOf("INSTALLED", true)).toBe("INSTALLED");
    expect(installationOf("INSTALLED_TO_INVENTORY", true)).toBe("INSTALLED_TO_INVENTORY");
  });
});
