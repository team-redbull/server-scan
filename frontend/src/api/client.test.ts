import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "@/api/client";
import { queryKeys } from "@/api/queryKeys";
import { queryClient } from "@/lib/query-client";

function problemResponse(status: number) {
  return Promise.resolve({
    ok: false,
    status,
    json: () => Promise.resolve({ status, detail: "x", code: "C" }),
  });
}

describe("apiFetch session expiry", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("re-checks /auth/me on a 401 from a data endpoint", async () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(problemResponse(401)));
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    await expect(apiFetch("/api/v1/servers/rows")).rejects.toThrow();

    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.auth.me() });
  });

  it("leaves a wrong-password 401 on /auth/login alone", async () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(problemResponse(401)));
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    await expect(apiFetch("/api/v1/auth/login", { method: "POST" })).rejects.toThrow();

    expect(spy).not.toHaveBeenCalled();
  });

  it("ignores a non-401 error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(problemResponse(500)));
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    await expect(apiFetch("/api/v1/servers/rows")).rejects.toThrow();

    expect(spy).not.toHaveBeenCalled();
  });
});
