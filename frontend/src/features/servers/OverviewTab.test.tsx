import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OverviewTab } from "@/features/servers/OverviewTab";
import type { ServerDetail } from "@/types/server";

function makeServer(overrides: Partial<ServerDetail> = {}): ServerDetail {
  return {
    id: "srv_1",
    name: "ocp-dell-worker-000",
    model: "PowerEdge R6515",
    profile_template: { name: null, external_id: null },
    identity: { vendor: "dell", serial: "SN123", system_uuid: null, nic_macs: [] },
    hardware: {
      cpu: { sockets: 2, cores: 32, threads: 64, model: "Xeon Gold 6338" },
      memory: { total_bytes: 0, modules: [] },
      storage: { total_bytes: 0, drives: [] },
      gpus: [],
      power: { psus: [] },
    },
    network: {
      bmc: { address_raw: null, scheme: null, host: null, port: null, mac: null },
      interfaces: [],
    },
    connectivity: {
      attachments: [],
      facts: {
        fabric_paths_total: 0,
        fabric_paths_up: 0,
        fabric_paths_down: 0,
        fabrics_present: [],
      },
    },
    classification: { installation_type: "HOSTED_CLUSTER", matched_rule_id: null },
    health: {
      overall: "HEALTHY",
      cpu: "HEALTHY",
      memory: "HEALTHY",
      storage: "HEALTHY",
      network: "HEALTHY",
      connectivity: "HEALTHY",
      power: "HEALTHY",
      gpu: "UNKNOWN",
    },
    maintenance: { enabled: false, reason: null },
    unread_fields: [],
    nic_os_names: {},
    openshift: {
      lifecycle_state: "AVAILABLE",
      mce_name: null,
      cluster_name: null,
      last_reported_at: null,
      reported_by_agent_id: null,
    },
    tags: [],
    created_at: "2026-08-13T10:00:00Z",
    site_id: "tlv",
    manager_id: "mgr_1",
    source_provider: "UCS_CENTRAL",
    last_seen_at: "2026-08-13T10:00:00Z",
    stale: false,
    reachable: true,
    unreachable_since: null,
    updated_at: "2026-08-13T10:00:00Z",
    ...overrides,
  };
}

describe("OverviewTab model", () => {
  it("shows the chassis's own model when it read one", () => {
    render(<OverviewTab server={makeServer({ model: "PowerEdge R6515" })} />);
    expect(screen.getByText("PowerEdge R6515")).toBeInTheDocument();
    expect(screen.queryByText(/from GPU/)).not.toBeInTheDocument();
  });

  it("shows a labeled GPU-derived hint when the chassis never reported a model", () => {
    const server = makeServer({ model: null });
    server.hardware.gpus = [
      {
        vendor: "NVIDIA",
        model: "NVIDIA A100 80GB",
        serial: null,
        memory_bytes: null,
        health: null,
        health_detail: null,
        pci_address: null,
        firmware_version: null,
        memory_type: null,
        ecc_mode_enabled: null,
        correctable_error_count: null,
        uncorrectable_error_count: null,
        temperature_celsius: null,
        power_watts: null,
      },
    ];

    render(<OverviewTab server={server} />);

    expect(screen.getByText("A100")).toBeInTheDocument();
    expect(screen.getByText("(from GPU)")).toBeInTheDocument();
  });

  it("falls back to a dash when neither the chassis nor a GPU reported a model", () => {
    render(<OverviewTab server={makeServer({ model: null })} />);
    expect(screen.getByText("Model").nextElementSibling).toHaveTextContent("—");
  });
});

describe("OverviewTab maintenance", () => {
  it("says a server is not in maintenance, and offers no way to change that", () => {
    render(<OverviewTab server={makeServer()} />);
    expect(screen.getByText("Not in maintenance")).toBeInTheDocument();
    // Maintenance is switched from the inventory list only.
    expect(screen.queryByRole("button", { name: /maintenance/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("shows the reason when in maintenance, still with no control", () => {
    render(
      <OverviewTab
        server={makeServer({ maintenance: { enabled: true, reason: "disk replacement" } })}
      />,
    );
    expect(screen.getByText("disk replacement")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /maintenance/i })).not.toBeInTheDocument();
  });
});

describe("OverviewTab OpenShift membership", () => {
  it("shows a server no cluster holds as available, not as a gap in the data", () => {
    // AVAILABLE is the default; it must never borrow the classification.
    render(<OverviewTab server={makeServer()} />);

    expect(screen.getByText("Available")).toBeInTheDocument();
  });

  it("names the hosted cluster and the MCE that reported it", () => {
    const server = makeServer();
    server.openshift = {
      ...server.openshift,
      lifecycle_state: "INSTALLED",
      mce_name: "mce-tlv",
      cluster_name: "hc-tlv-02",
    };

    render(<OverviewTab server={server} />);

    expect(screen.getByText("Installed")).toBeInTheDocument();
    expect(screen.getByText("hc-tlv-02")).toBeInTheDocument();
    expect(screen.getByText(/mce-tlv/)).toBeInTheDocument();
  });

  it("names a plain cluster without an MCE, which does not manage one", () => {
    const server = makeServer();
    server.openshift = {
      ...server.openshift,
      lifecycle_state: "INSTALLED",
      cluster_name: "upi-tlv",
    };

    render(<OverviewTab server={server} />);

    expect(screen.getByText("Installed")).toBeInTheDocument();
    expect(screen.getByText("upi-tlv")).toBeInTheDocument();
    expect(screen.queryByText(/MCE /)).not.toBeInTheDocument();
  });

  it("shows an unbound agent as held by its MCE, with no cluster name", () => {
    const server = makeServer();
    server.openshift = {
      ...server.openshift,
      lifecycle_state: "INSTALLED_TO_INVENTORY",
      mce_name: "mce-nyc",
    };

    render(<OverviewTab server={server} />);

    expect(screen.getByText("Installed to inventory")).toBeInTheDocument();
    expect(screen.getByText(/mce-nyc/)).toBeInTheDocument();
  });

  it("shows a disagreement with the classification rather than hiding it", () => {
    // A misnamed server is only noticeable when both values are visible.
    const server = makeServer();
    server.classification.installation_type = "UNCLASSIFIED";
    server.openshift = {
      ...server.openshift,
      lifecycle_state: "INSTALLED",
      cluster_name: "hc-nyc-01",
      mce_name: "mce-nyc",
    };

    render(<OverviewTab server={server} />);

    expect(screen.getByText("UNCLASSIFIED")).toBeInTheDocument();
    expect(screen.getByText("Installed")).toBeInTheDocument();
  });
});

describe("OverviewTab profile template", () => {
  it("labels it in the collector's own vendor terminology", () => {
    const cases: [string, string][] = [
      ["UCS_CENTRAL", "Service profile template"],
      ["INTERSIGHT", "Server profile template"],
      ["ONEVIEW", "Server profile template"],
      ["OPENMANAGE", "Deployment template"],
    ];
    for (const [sourceProvider, label] of cases) {
      const server = makeServer({
        source_provider: sourceProvider,
        profile_template: { name: "worker-profile-tmpl", external_id: "tmpl-001" },
      });

      const { unmount } = render(<OverviewTab server={server} />);

      expect(screen.getByText(label)).toBeInTheDocument();
      expect(screen.getByText("worker-profile-tmpl")).toBeInTheDocument();
      unmount();
    }
  });

  it("shows a dash rather than hiding the row when the vendor supports templates but none was read", () => {
    const server = makeServer({
      source_provider: "OPENMANAGE",
      profile_template: { name: null, external_id: null },
    });

    render(<OverviewTab server={server} />);

    expect(screen.getByText("Deployment template")).toBeInTheDocument();
  });

  it("omits the row entirely for a standalone server — a bare BMC has no template concept", () => {
    const server = makeServer({
      source_provider: "REDFISH_STANDALONE",
      profile_template: { name: null, external_id: null },
    });

    render(<OverviewTab server={server} />);

    expect(screen.queryByText(/profile template|deployment template/i)).not.toBeInTheDocument();
  });

  it("omits the row when the server has no source provider at all", () => {
    const server = makeServer({ source_provider: null });

    render(<OverviewTab server={server} />);

    expect(screen.queryByText(/profile template|deployment template/i)).not.toBeInTheDocument();
  });
});

describe("OverviewTab collection status", () => {
  it("shows nothing extra for a reachable server", () => {
    const server = makeServer({ reachable: true, unreachable_since: null });

    render(<OverviewTab server={server} />);

    expect(screen.queryByText("Unreachable", { exact: false })).not.toBeInTheDocument();
  });

  it("flags an unreachable server with how long it has been down", () => {
    const server = makeServer({
      reachable: false,
      unreachable_since: "2026-09-09T10:00:00Z",
    });

    render(<OverviewTab server={server} />);

    expect(screen.getByText(/Unreachable since/)).toBeInTheDocument();
  });

  it("still flags an unreachable server with no known start time", () => {
    const server = makeServer({ reachable: false, unreachable_since: null });

    render(<OverviewTab server={server} />);

    expect(screen.getByText("Unreachable")).toBeInTheDocument();
  });
});
