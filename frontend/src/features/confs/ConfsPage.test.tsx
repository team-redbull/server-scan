import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Conf } from "@/api/confs";
import { ConfsPage } from "@/features/confs/ConfsPage";
import { formatKb, formatTb, groupByModel, visibleConfs } from "@/features/confs/format";

function conf(overrides: Partial<Conf>): Conf {
  return {
    conf_id: "CONF200",
    name: "Compute Std R650",
    part_number: "210-AZKL",
    model: "PowerEdge R650",
    cpu: {
      model: "Intel Xeon Gold 6338",
      vendor: "Intel",
      sockets: 2,
      cores_per_cpu: 32,
      total_cores_ht: 128,
      clock_mhz: 2000,
      max_clock_mhz: 3200,
      cache_kb: { l1: 2560, l2: 40960, l3: 49152 },
    },
    ram: { total_gib: 512, dimms: 16, per_dimm_gb: 32, type: ["DDR4"] },
    disks: [{ count: 2, model: "MTFDDAV480TDS", capacity_tb: 0.48, type: "SSD", protocol: "SATA" }],
    gpus: [],
    gpu_count: 0,
    adapters: [{ count: 1, model: "MLNX 100GbE 2P ConnectX6 Adpt" }],
    adapter_count: 1,
    ...overrides,
  };
}

const GPU_CONF = conf({
  conf_id: "CONF202",
  name: "GPU H200 R750",
  gpus: Array.from({ length: 8 }, () => ({ count: 1, model: "NVIDIA H200" })),
  gpu_count: 8,
});

const UNMAPPED_CONF = conf({
  conf_id: "CONF-21",
  name: null,
  part_number: null,
  ram: { total_gib: 512, dimms: 16, per_dimm_gb: 32, type: [] },
  disks: [{ count: 2, model: "unknown", capacity_tb: 0.96, type: "unknown", protocol: "unknown" }],
});

const RESPONSE = { items: [conf({}), GPU_CONF, UNMAPPED_CONF], unreadable: ["CONF-99"] };

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ConfsPage />
    </QueryClientProvider>,
  );
}

describe("ConfsPage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string) => {
        const url = new URL(input, "http://localhost");
        if (url.pathname === "/api/v1/confs") {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(RESPONSE) });
        }
        throw new Error(`unexpected request to ${url.pathname}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists every conf and the count", async () => {
    renderPage();

    expect(await screen.findByText("CONF200")).toBeInTheDocument();
    expect(screen.getByText("CONF202")).toBeInTheDocument();
    expect(screen.getByText("3 confs")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Servers" })).not.toBeInTheDocument();
  });

  it("groups GPUs listed one per unit", async () => {
    renderPage();

    const row = (await screen.findByText("CONF202")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText(/8×/)).toBeInTheDocument();
  });

  it("marks a conf with no catalog name as unmapped and names the unreadable ones", async () => {
    renderPage();

    expect(await screen.findByText("Unmapped")).toBeInTheDocument();
    expect(screen.getByText("CONF-99")).toBeInTheDocument();
  });

  it("filters by any component model", async () => {
    renderPage();
    await screen.findByText("CONF200");

    fireEvent.change(screen.getByLabelText("Search"), { target: { value: "h200" } });

    expect(screen.getByText("CONF202")).toBeInTheDocument();
    expect(screen.queryByText("CONF200")).not.toBeInTheDocument();
    expect(screen.getByText("1 of 3 confs", { exact: false })).toBeInTheDocument();
  });

  it("expands a row into its full spec", async () => {
    renderPage();
    await screen.findByText("CONF-21");

    fireEvent.click(screen.getByRole("button", { name: "Show details of CONF-21" }));

    expect(screen.getByText("not read (DIMM read failed)")).toBeInTheDocument();
    expect(screen.getByText(/L3 48 MB/)).toBeInTheDocument();
  });
});

describe("conf formatting", () => {
  it("sums entries per model in first-seen order", () => {
    expect(
      groupByModel([
        { count: 1, model: "B" },
        { count: 1, model: "A" },
        { count: 1, model: "B" },
      ]),
    ).toEqual([
      { model: "B", count: 2 },
      { model: "A", count: 1 },
    ]);
  });

  it("formats capacities and cache sizes without trailing zeros", () => {
    expect(formatTb(1.92)).toBe("1.92 TB");
    expect(formatTb(8)).toBe("8 TB");
    expect(formatKb(640)).toBe("640 KB");
    expect(formatKb(2560)).toBe("2.5 MB");
  });

  it("keeps the API's order and matches every search term", () => {
    const items = RESPONSE.items;
    expect(visibleConfs(items, "").map((c) => c.conf_id)).toEqual(["CONF200", "CONF202", "CONF-21"]);
    expect(visibleConfs(items, "r650 h200").map((c) => c.conf_id)).toEqual(["CONF202"]);
  });
});
