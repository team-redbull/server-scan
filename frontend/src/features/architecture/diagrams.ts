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
    description: "Every collector through Mongo, the OpenShift membership jobs, Redis, the API and the UI, in one diagram.",
  },
  {
    slug: "ucs-central",
    label: "Cisco UCS Central",
    description: "Central login, per-domain UCS Manager sessions, the managed-object queries, then IngestService.",
  },
  {
    slug: "intersight",
    label: "Cisco Intersight",
    description: "API-key request signing, the owner-relation resource walk, then IngestService.",
  },
  {
    slug: "openmanage",
    label: "Dell OpenManage",
    description: "OME for identity, each iDRAC's Redfish for hardware, joined before IngestService.",
  },
  {
    slug: "oneview",
    label: "HPE OneView",
    description: "The one HPE source regardless of iLO generation, then IngestService.",
  },
  {
    slug: "redfish-standalone",
    label: "Redfish Standalone",
    description: "No manager: a fleet from an inventory file, one BMC at a time, then IngestService.",
  },
  {
    slug: "openshift-membership",
    label: "OpenShift Membership",
    description: "The nodes and agents jobs that write Server.openshift directly, bypassing IngestService.",
  },
];
