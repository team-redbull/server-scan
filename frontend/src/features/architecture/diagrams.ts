/** The archify diagrams under `public/architecture/`, one per collector plus
 * the full-flow overview. A static list: these are pre-built HTML files, not
 * API-fetched data. */
export interface DiagramSpec {
  slug: string;
  label: string;
  description: string;
}

export const DIAGRAMS: DiagramSpec[] = [
  {
    slug: "runtime-architecture",
    label: "Full flow",
    description: "The six collectors through Mongo, the prune and nodes-status jobs, Redis, the API, Prometheus, AD login and the UI, in one diagram. Only the OpenManage run ever writes to a BMC.",
  },
  {
    slug: "ucs-central",
    label: "Cisco UCS Central",
    description: "Central login, per-domain UCS Manager sessions, the 17 managed-object queries (storage and virtual drives included), per-server failure containment, then IngestService. Cisco BMCs are not probed.",
  },
  {
    slug: "intersight",
    label: "Cisco Intersight",
    description: "API-key request signing, the owner-relation resource walk with partial-read semantics and a run budget, then IngestService.",
  },
  {
    slug: "openmanage",
    label: "Dell OpenManage",
    description: "OME for identity, each iDRAC's Redfish for hardware, joined before IngestService; a failed iDRAC becomes a stub with a reason. Includes the report-only iDRAC reset.",
  },
  {
    slug: "oneview",
    label: "HPE OneView",
    description: "The one HPE source regardless of iLO generation, plus an unauthenticated iLO reachability probe, then IngestService.",
  },
  {
    slug: "redfish-standalone",
    label: "Redfish Standalone",
    description: "No manager: a fleet from an inventory file, one BMC at a time; every failed host becomes a stub, then IngestService.",
  },
  {
    slug: "kubevirt",
    label: "KubeVirt",
    description: "One collector per KubeVirt cluster reading VMs through the kubevirt-redfish facade as Redfish Systems, then IngestService.",
  },
  {
    slug: "nodes-status",
    label: "Nodes Status",
    description: "The nodes-status jobs that write Server.openshift directly, bypassing IngestService.",
  },
];
