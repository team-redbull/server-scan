import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { NetworkTab } from "@/features/servers/NetworkTab";
import type { NetworkInfo } from "@/types/server";

/** A BMC that answered, and one interface it could not read a MAC or speed for. */
function partialNetwork(): NetworkInfo {
  return {
    bmc: { address_raw: null, scheme: null, host: null, port: null, mac: null },
    interfaces: [
      { name: "NIC.Slot.1-1", mac: null, speed_mbps: null, link_state: "UNKNOWN", location: null },
    ],
  };
}

describe("NetworkTab unread fields", () => {
  it("dashes a BMC address and a NIC MAC the collector could not read", () => {
    render(<NetworkTab network={partialNetwork()} />);

    expect(screen.getByText("NIC.Slot.1-1")).toBeInTheDocument();
    expect(screen.getAllByText("—")).toHaveLength(4);
  });

  it("prefers the BMC host over its raw address when both were read", () => {
    const network = partialNetwork();
    network.bmc = {
      address_raw: "https://10.0.0.5:443/redfish/v1",
      scheme: "https",
      host: "10.0.0.5",
      port: 443,
      mac: "aa:bb:cc:dd:ee:ff",
    };

    render(<NetworkTab network={network} />);

    expect(screen.getByText("10.0.0.5")).toBeInTheDocument();
    expect(screen.getByText("aa:bb:cc:dd:ee:ff")).toBeInTheDocument();
  });
});

describe("NetworkTab OS names", () => {
  /** A Dell with its onboard LOM and a card in slot 8. */
  function dellNetwork(): NetworkInfo {
    return {
      bmc: { address_raw: null, scheme: null, host: "10.0.0.5", port: null, mac: null },
      interfaces: [
        {
          name: "NIC.Integrated.1-1-1",
          mac: "aa:00:00:00:00:01",
          speed_mbps: 25000,
          link_state: "UP",
          location: "1/1/1",
        },
        {
          name: "NIC.Slot.8-1-1",
          mac: "aa:00:00:00:00:03",
          speed_mbps: 25000,
          link_state: "UP",
          location: "8/1/1",
        },
      ],
    };
  }

  it("reads an FQDD's location in words, distinguishing onboard from a slot", () => {
    render(<NetworkTab network={dellNetwork()} />);

    expect(screen.getByText("Onboard · port 1")).toBeInTheDocument();
    expect(screen.getByText("Slot 8 · port 1")).toBeInTheDocument();
  });

  it("numbers each MAC by discovery order, which is what tooling selects on", () => {
    render(<NetworkTab network={dellNetwork()} />);

    expect(screen.getByText("MAC #1")).toBeInTheDocument();
    expect(screen.getByText("MAC #2")).toBeInTheDocument();
  });

  it("labels a configured OS name as derived, never as something collected", () => {
    render(
      <NetworkTab
        network={dellNetwork()}
        osNames={{ "NIC.Slot.8-1-1": "ens8f0np0" }}
      />,
    );

    expect(screen.getByText("ens8f0np0")).toBeInTheDocument();
    expect(screen.getByText(/OS name \(derived\)/)).toBeInTheDocument();
  });

  it("shows nothing at all for an interface with no configured name", () => {
    // A guessed name yields a boot configuration that silently does not come up.
    render(<NetworkTab network={dellNetwork()} osNames={{}} />);

    expect(screen.queryByText(/OS name \(derived\)/)).not.toBeInTheDocument();
  });
});
