import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { JobsPage } from "@/features/jobs/JobsPage";

const POD = {
  name: "r-collector-oneview-123-abcde",
  phase: "Succeeded",
  started_at: null,
};
const JOB = {
  name: "r-collector-oneview-123",
  created_at: "2026-11-01T06:00:00Z",
  started_at: null,
  finished_at: null,
  active: 0,
  succeeded: 1,
  failed: 0,
  state: "succeeded",
  manual: false,
  triggered_by: null,
  pods: [POD],
};

function cronjob(name: string, over: object = {}) {
  return {
    name,
    schedule: "0 6 * * *",
    suspend: false,
    last_schedule_time: null,
    last_successful_time: null,
    warning: null,
    active: false,
    enabled: true,
    deployed: true,
    jobs: [JOB],
    ...over,
  };
}

function overview(cronjobs: object[]) {
  return { enabled: true, backend: "kubernetes", namespace: "ns", cronjobs };
}

function stubFetch(cronjobs: object[]) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const ok = (body: unknown, status = 200) =>
      Promise.resolve({ ok: true, status, json: () => Promise.resolve(body) });
    if (init?.method === "POST")
      return ok(
        { ...JOB, name: "new-job", state: "active", active: 1, pods: [] },
        201,
      );
    if (url.includes("/logs"))
      return ok({ pod: POD.name, tail: 500, text: "collector.done\n" });
    return ok(overview(cronjobs));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPage() {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <JobsPage />
    </QueryClientProvider>,
  );
}

describe("JobsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("disables Run job for a CronJob that already has a running Job", async () => {
    stubFetch([cronjob("idle"), cronjob("busy", { active: true })]);
    renderPage();

    const idle = await screen.findByRole("region", { name: "idle" });
    const busy = screen.getByRole("region", { name: "busy" });
    expect(within(idle).getByRole("button", { name: "Run job" })).toBeEnabled();
    expect(
      within(busy).getByRole("button", { name: "Run job" }),
    ).toBeDisabled();
  });

  it("confirms with the warning, then POSTs the run", async () => {
    const fetchMock = stubFetch([
      cronjob("r-prune-events", {
        warning: "This run can permanently delete audit events.",
      }),
    ]);
    renderPage();

    const card = await screen.findByRole("region", { name: "r-prune-events" });
    fireEvent.click(within(card).getByRole("button", { name: "Run job" }));
    const dialog = screen.getByRole("dialog", { name: "Run r-prune-events" });
    expect(within(dialog).getByRole("note")).toHaveTextContent(
      "permanently delete audit events",
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Run job" }));

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/jobs/r-prune-events/run",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("shows a pod's logs", async () => {
    stubFetch([cronjob("r-collector-oneview")]);
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /Logs · abcde · Succeeded/ }),
    );

    const dialog = await screen.findByRole("dialog", { name: /^Logs of / });
    expect(
      await within(dialog).findByText(/collector\.done/),
    ).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(
      screen.queryByRole("dialog", { name: /^Logs of / }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "r-collector-oneview" }),
    ).toBeInTheDocument();
  });

  it("closes the logs card with Escape", async () => {
    stubFetch([cronjob("r-collector-oneview")]);
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /Logs · abcde · Succeeded/ }),
    );
    const dialog = await screen.findByRole("dialog", { name: /^Logs of / });
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(
      screen.queryByRole("dialog", { name: /^Logs of / }),
    ).not.toBeInTheDocument();
  });

  it("shows each job's values.yaml flag and cannot run one that is not deployed", async () => {
    stubFetch([
      cronjob("on"),
      cronjob("off", {
        enabled: false,
        deployed: false,
        jobs: [],
        schedule: "",
      }),
    ]);
    renderPage();

    const on = await screen.findByRole("region", { name: "on" });
    const off = screen.getByRole("region", { name: "off" });
    expect(within(on).getByText("enabled: true")).toBeInTheDocument();
    expect(within(off).getByText("enabled: false")).toBeInTheDocument();
    expect(within(off).getByRole("button", { name: "Run job" })).toBeDisabled();
    expect(
      within(off).getByText(/switched off in values\.yaml/),
    ).toBeInTheDocument();
  });

  it("says so when the feature is off", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            enabled: false,
            backend: "off",
            namespace: null,
            cronjobs: [],
          }),
      }),
    );
    renderPage();

    expect(
      await screen.findByText(/not enabled on this deployment/),
    ).toBeInTheDocument();
  });
});
