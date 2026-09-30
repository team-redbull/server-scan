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
    description: "Every collector through Mongo, the prune and nodes-status jobs, Redis, the API, Prometheus, AD login and the UI, in one diagram.",
  },
  {
    slug: "ucs-central",
    label: "Cisco UCS Central",
    description: "Central login, per-domain UCS Manager sessions, the managed-object queries, then IngestService. Cisco BMCs are behind the fabric interconnects and are not probed.",
  },
  {
    slug: "intersight",
    label: "Cisco Intersight",
    description: "API-key request signing, the owner-relation resource walk, then IngestService.",
  },
  {
    slug: "openmanage",
    label: "Dell OpenManage",
    description: "OME for identity, each iDRAC's Redfish for hardware, joined before IngestService; a failed iDRAC becomes a stub with a reason.",
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
    slug: "nodes-status",
    label: "Nodes Status",
    description: "The nodes-status jobs that write Server.openshift directly, bypassing IngestService.",
  },
];
