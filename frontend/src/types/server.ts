/**
 * Hand-written mirror of the `/api/v1/servers` JSON shapes.
 *
 * FastAPI sends a Pydantic `X | None` as `X | null`, never an absent key, so
 * `X | null` here means "always sent, may be null" and `?:` is reserved for
 * keys the API genuinely may omit. Typing one as optional-only let
 * `null.toFixed()` unmount the page once (commit 1a896af).
 */

export type Vendor = "dell" | "cisco" | "hp" | "standalone";

/** Not a union: the closed set is `INVENTORY_SITES`, read via
 * `GET /api/v1/sites` — a copy here drifted once when sites were renamed. */
export type SiteCode = string;

export type HealthSeverity = "UNKNOWN" | "HEALTHY" | "WARNING" | "MAJOR" | "CRITICAL";

export type LinkState = "UP" | "DOWN" | "UNKNOWN" | "DISABLED";

export type AdminState = "ENABLED" | "DISABLED" | "UNKNOWN";

export type InstallationType = "HOSTED_CLUSTER" | "MCE" | "UPI" | "UNCLASSIFIED";

export interface Classification {
  installation_type: InstallationType;
  matched_rule_id: string | null;
}

/** Null for a standalone server (a bare BMC has no template concept) or
 * when the collector could not read it this run. */
export interface ProfileTemplate {
  name: string | null;
  external_id: string | null;
}

/** Never absent or null: `UNKNOWN` is "no policy has said anything yet". */
export interface HealthSummary {
  overall: HealthSeverity;
  cpu: HealthSeverity;
  memory: HealthSeverity;
  storage: HealthSeverity;
  network: HealthSeverity;
  connectivity: HealthSeverity;
  power: HealthSeverity;
  gpu: HealthSeverity;
}

export interface MaintenanceState {
  enabled: boolean;
  reason: string | null;
}

export interface ConnectivityFacts {
  fabric_paths_total: number;
  fabric_paths_up: number;
  fabric_paths_down: number;
  fabrics_present: string[];
}

/** One inventory row — `GET /api/v1/servers/rows`, the whole fleet in one
 * response, filtered and sorted in the browser (ADR-0033). `serial`,
 * `bmc_host` and `macs` exist for search parity only and are never rendered. */
export interface ServerRow {
  id: string;
  name: string;
  vendor: Vendor;
  model: string | null;
  site_id: SiteCode | null;
  source_provider: string | null;
  installation_type: InstallationType;
  health: HealthSeverity;
  maintenance: MaintenanceState;
  openshift_state: OpenShiftState;
  cluster_name: string | null;
  mce_name: string | null;
  last_seen_at: string | null;
  /** Unseen for longer than `INVENTORY_STALE_AFTER_SECONDS`, or never (ADR-0029). */
  stale: boolean;
  reachable: boolean;
  serial: string | null;
  bmc_host: string | null;
  macs: string[];
}

export interface ServerRowsResponse {
  items: ServerRow[];
  generated_at: string;
}

// Detail shape — GET /api/v1/servers/{id}

export interface ServerIdentity {
  vendor: Vendor;
  serial: string | null;
  system_uuid: string | null;
  nic_macs: string[];
}

export interface CpuInfo {
  sockets: number;
  cores: number;
  threads: number;
  model: string | null;
}

export interface MemoryModule {
  slot: string | null;
  size_bytes: number | null;
  speed_mhz: number | null;
  health: ComponentHealth;
  /** The raw vendor string `health` was reduced from; diagnosis only. */
  health_detail: string | null;
}

export interface MemoryInfo {
  total_bytes: number;
  modules: MemoryModule[];
}

/** Not narrowed to `HealthSeverity`: Cisco and the Redfish PSU path report
 * UP/DOWN/DISABLED/UNKNOWN instead. Render through `isHealthSeverity`. */
export type ComponentHealth = string | null;

export interface StorageDrive {
  id: string;
  model: string | null;
  serial: string | null;
  media_type: string;
  capacity_bytes: number | null;
  health: ComponentHealth;
  health_detail: string | null;
}

export interface StorageInfo {
  total_bytes: number;
  drives: StorageDrive[];
}

export interface GpuInfo {
  vendor: string | null;
  model: string | null;
  serial: string | null;
  memory_bytes: number | null;
  health: ComponentHealth;
  health_detail: string | null;
  pci_address: string | null;
  firmware_version: string | null;
  memory_type: string | null;
  ecc_mode_enabled: boolean | null;
  correctable_error_count: number | null;
  uncorrectable_error_count: number | null;
  temperature_celsius: number | null;
  power_watts: number | null;
}

export interface PsuInfo {
  id: string;
  model: string | null;
  serial: string | null;
  health: ComponentHealth;
  health_detail: string | null;
  capacity_watts: number | null;
  power_watts: number | null;
}

export interface PowerInfo {
  psus: PsuInfo[];
}

export interface HardwareInfo {
  cpu: CpuInfo;
  memory: MemoryInfo;
  storage: StorageInfo;
  gpus: GpuInfo[];
  power: PowerInfo;
}

export interface BmcInfo {
  address_raw: string | null;
  scheme: string | null;
  host: string | null;
  port: number | null;
  mac: string | null;
}

export interface NetworkInterface {
  name: string;
  mac: string | null;
  speed_mbps: number | null;
  link_state: LinkState;
  /** `controller/port/partition` on Dell (`1/1/1`), the BMC's raw id elsewhere. */
  location: string | null;
}

export interface NetworkInfo {
  bmc: BmcInfo;
  interfaces: NetworkInterface[];
}

/** `fabric` is a free-form label; do not assume exactly two, and `null`
 * renders under "Other" rather than being dropped. */
export interface ConnectivityAttachment {
  type: string;
  provider: string | null;
  fabric: string | null;
  fabric_name: string | null;
  fabric_id: string | null;
  fabric_model: string | null;
  fabric_serial: string | null;
  server_interface: string | null;
  server_port: string | null;
  fabric_port: string | null;
  admin_state: AdminState;
  oper_state: LinkState;
  speed_mbps: number | null;
  last_seen: string | null;
  /** "PHYSICAL" for a cabled uplink, "VNIC" for a carve-out on top of it —
   * both can report the same `fabric` (docs/cisco-collectors.md). */
  interface_kind: string;
}

export interface ConnectivityDetail {
  attachments: ConnectivityAttachment[];
  facts: ConnectivityFacts;
}

export interface ServerDetail {
  id: string;
  name: string;
  model: string | null;
  profile_template: ProfileTemplate;
  identity: ServerIdentity;
  hardware: HardwareInfo;
  network: NetworkInfo;
  connectivity: ConnectivityDetail;
  classification: Classification;
  health: HealthSummary;
  maintenance: MaintenanceState;
  site_id: SiteCode | null;
  manager_id: string | null;
  source_provider: string | null;
  /** Never reconciled with `classification.installation_type`: the two
   * disagreeing is the signal, not a bug (ADR-0024). */
  openshift: OpenShiftLifecycle;
  /** Dotted paths the most recent collection could not read; the stored
   * value there is carried forward or the model's zero, never a reading. */
  unread_fields: string[];
  /** FQDD -> OS interface name, from `INVENTORY_NIC_OS_NAMES` (never
   * collected). Empty must render as absence, not a guess. */
  nic_os_names: Record<string, string>;
  tags: string[];
  last_seen_at: string | null;
  /** Unseen for longer than `INVENTORY_STALE_AFTER_SECONDS`, or never (ADR-0029). */
  stale: boolean;
  reachable: boolean;
  unreachable_since: string | null;
  updated_at: string;
  created_at: string;
}

/** `AVAILABLE` is the default; there is no "nothing reported yet" state. */
export type OpenShiftState =
  | "AVAILABLE"
  | "INSTALLED"
  | "INSTALLED_TO_INVENTORY";

/** `cluster_name` is set only when a cluster claims the server, `mce_name`
 * only by the MCE job. */
export interface OpenShiftLifecycle {
  lifecycle_state: OpenShiftState;
  cluster_name: string | null;
  mce_name: string | null;
  /** Nothing reports a removal; a stale membership is only visible here. */
  last_reported_at: string | null;
  reported_by_agent_id: string | null;
}
