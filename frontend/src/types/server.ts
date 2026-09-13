/**
 * Hand-written types mirroring the backend's `/api/v1/servers` JSON shapes.
 * Deliberately decoupled from any generated OpenAPI client: the UI only
 * needs a subset of the persisted document, and hand-written types let us
 * keep that subset honest instead of importing the backend's full internal
 * model.
 *
 * **The one rule that matters here**: FastAPI serialises Python `None` as
 * JSON `null` and never omits the key (no `response_model_exclude_none`
 * anywhere in `backend/app`), so a Pydantic field typed `X | None` is
 * `X | null` on the wire — not `X | undefined`. Declaring one of those
 * optional-only makes `x !== undefined` look like a sufficient guard, and
 * `null.toFixed()` then unmounts the page (commit 1a896af). Every field
 * below is therefore typed from its backend counterpart:
 *
 * - `X | null` — the model field is `X | None`. Always sent, may be null.
 * - `X` — the model field has a non-null default. Always sent, never null.
 * - `?:` — reserved for keys the API genuinely may not send at all.
 */

/** The three vendors this platform ingests from. There is no "unknown":
 * every server arrives through a vendor-specific collector, so the
 * vendor is known by construction. */
export type Vendor = "dell" | "cisco" | "hp" | "standalone";

/** A site code. A server's site is parsed from its name
 * (`ocp4-prod-tlv-infra-01` -> "tlv"); `null` means the name carries no
 * site token and is surfaced as "Unassigned".
 *
 * Deliberately not a union of the current codes: the closed set is the
 * backend's `INVENTORY_SITES` catalog, and `GET /api/v1/sites` is what
 * tells the UI which codes exist and what each is called. A union here
 * would be a second copy of that list, free to disagree with it — as it
 * did when the sites were renamed and the filter dropdown kept offering
 * the old ones. */
export type SiteCode = string;

export type HealthSeverity =
  "UNKNOWN" | "HEALTHY" | "INFO" | "WARNING" | "MAJOR" | "CRITICAL";

/** Link/operational state as reported for a physical/logical network link. */
export type LinkState = "UP" | "DOWN" | "UNKNOWN" | "DISABLED";

/** Administrative enable state of a fabric attachment port. */
export type AdminState = "ENABLED" | "DISABLED" | "UNKNOWN";

export type InstallationType = "HOSTED_CLUSTER" | "MCE" | "UPI" | "UNCLASSIFIED";

export interface Classification {
  installation_type: InstallationType;
  matched_rule_id: string | null;
}

/** The reusable deployment template this server's own profile was
 * provisioned from — a UCS Manager/Central Service Profile Template, an
 * Intersight or HPE OneView Server Profile Template, or a Dell OpenManage
 * Deployment Template. Both fields are null for a standalone server (a
 * bare BMC has no template concept at all) or for a vendor whose
 * collector could not read it this run. */
export interface ProfileTemplate {
  name: string | null;
  external_id: string | null;
}

/** Every category is written on every evaluation (`Health` defaults each
 * to `UNKNOWN`), so none of these is ever absent or null — `UNKNOWN` is
 * the "no policy has said anything yet" value. */
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

export interface ConnectivitySummary {
  facts: ConnectivityFacts;
}

/** Row shape returned by `GET /api/v1/servers` (list). */
export interface ServerSummary {
  id: string;
  name: string;
  vendor: Vendor;
  /** `Server.model` is `str | None` — a BMC that did not report a model
   * leaves this null rather than empty. */
  model: string | null;
  site_id: SiteCode | null;
  manager_id: string | null;
  /** Which collector produced this record — `REDFISH_STANDALONE`
   * means the machine has no manager and is reached at its own BMC. */
  source_provider: string | null;
  classification: Classification;
  health: HealthSummary;
  maintenance: MaintenanceState;
  /** Whether a cluster is using this server, from the OpenShift jobs.
   * Independent of `classification`, which is what its *name* claims. */
  openshift: OpenShiftLifecycle;
  connectivity: ConnectivitySummary;
  last_seen_at: string | null;
  /** False when the collector knew this server's identity but could not
   * reach it on the most recent run — hardware fields keep their
   * last-known values, they are not blanked. */
  reachable: boolean;
  unreachable_since: string | null;
  updated_at: string;
}

export interface PageMeta {
  next_cursor: string | null;
  has_more: boolean;
  page_size: number;
  count: number | null;
  count_capped: boolean;
}

export interface ServerListResponse {
  items: ServerSummary[];
  page: PageMeta;
}

// ---------------------------------------------------------------------------
// Detail shape — GET /api/v1/servers/{id}
// ---------------------------------------------------------------------------

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
  /** `MemoryModule.speed_mhz` on the backend. This was declared
   * `speed_mts` here and matched no field the API has ever sent. */
  speed_mhz: number | null;
  health: ComponentHealth;
  /** The raw vendor string `health` was reduced from — e.g. "self-test-failed"
   * for a drive read as CRITICAL, or "inoperable" for a PSU read as DOWN.
   * Never read by the health policy engine; diagnosis only. */
  health_detail: string | null;
}

export interface MemoryInfo {
  total_bytes: number;
  modules: MemoryModule[];
}

/** A drive/GPU/PSU's own reported condition, as `str | None` on the
 * backend and deliberately not narrowed to `HealthSeverity` here: the
 * vocabulary differs by collector. Redfish and OneView normalise onto
 * `HealthSeverity` (`health_of`, `_PSU_STATE_HEALTH`), while Cisco and
 * the Redfish PSU path normalise onto UP/DOWN/DISABLED/UNKNOWN
 * (`ucs_common.normalize_oper_state`, `redfish.mapping._psu_health`).
 * Render it through `isHealthSeverity` — a badge keyed on the severity
 * table alone silently loses its styling for every Cisco value. */
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
  /** `controller/port/partition` on Dell (`1/1/1`), the BMC's own raw
   *  identifier elsewhere. Null when the BMC places the NIC by nothing. */
  location: string | null;
}

export interface NetworkInfo {
  bmc: BmcInfo;
  interfaces: NetworkInterface[];
}

/**
 * One physical/logical link between a server and a fabric interconnect (or
 * other connectivity provider). `fabric` is a free-form label (commonly "A"
 * / "B" for dual-fabric UCS setups, but the UI must not assume exactly two —
 * `fabric: null` means "ungrouped" and should render under an "Other"
 * section rather than being dropped or crashing).
 */
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
  /**
   * "PHYSICAL" for a cabled uplink, "VNIC" for the OS-facing logical
   * carve-out UCS Manager virtualizes on top of it — the two can report
   * the identical `fabric`, so this is what tells a real cable apart
   * from the vNICs riding on it. Collected for every attachment; the UI
   * only ever shows "PHYSICAL" ones (see ConnectivityTab).
   */
  interface_kind: string;
}

export interface ConnectivityDetail {
  attachments: ConnectivityAttachment[];
  facts: ConnectivityFacts;
}

/** Full document returned by `GET /api/v1/servers/{id}`. */
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
  /** Dotted paths into this same response that the most recent collection
   * could not read (`hardware.storage.drives`). The stored value at such
   * a path is either carried over from an earlier run or the model's zero
   * — never a reading from this run, which is why the UI must not present
   * a `0`/`[]` there as fact. */
  /** What OpenShift observed about this server, as opposed to what its
   * name suggests. Written by the UPI and MCE jobs, never by a hardware
   * collector, and deliberately not reconciled with
   * `classification.installation_type`: that is a regex verdict on a
   * hostname, and the two disagreeing is the signal, not a bug. */
  openshift: OpenShiftLifecycle;
  unread_fields: string[];
  /** A hardware interface name (`NIC.Slot.8-1-1`) against the name the
   * host's OS gives it (`ens8f0np0`), for the interfaces a mapping is
   * configured for. Derived from `INVENTORY_NIC_OS_NAMES`, never
   * collected: no management API reports an OS-level name, because it
   * does not exist until the host boots. Empty when nothing is
   * configured, which the UI must show as absence rather than filling in
   * a plausible-looking guess. */
  nic_os_names: Record<string, string>;
  tags: string[];
  last_seen_at: string | null;
  reachable: boolean;
  unreachable_since: string | null;
  updated_at: string;
  created_at: string;
}

/**
 * How many servers each filter option would match, for one view.
 *
 * Every count is *within the filters already applied*, so after picking a
 * site the vendor counts describe that site rather than the estate.
 *
 * A value matching nothing is absent rather than zero, which is what lets
 * the UI show an option as unavailable instead of silently selectable.
 */
export interface ServerFacets {
  total: number;
  vendor: Record<string, number>;
  source_provider: Record<string, number>;
  installation_type: Record<string, number>;
  health_overall: Record<string, number>;
  /** Keyed `"true"`/`"false"` — JSON object keys cannot be booleans. */
  maintenance: Record<string, number>;
  openshift_state: Record<string, number>;
}

/** Whether a server is in use, as OpenShift reports it.
 *
 * Three states, and *what kind* of node it is comes from
 * `InstallationType` instead. `AVAILABLE` is the default — a server no
 * cluster claims — so there is no "nothing reported yet" state. */
export type OpenShiftState =
  | "AVAILABLE"
  | "INSTALLED"
  | "INSTALLED_TO_INVENTORY";

/**
 * One server's observed OpenShift membership.
 *
 * Read `lifecycle_state` before trusting anything else: `cluster_name` is
 * set only when a cluster claims the server, and `mce_name` only by the
 * MCE job.
 */
export interface OpenShiftLifecycle {
  lifecycle_state: OpenShiftState;
  cluster_name: string | null;
  mce_name: string | null;
  /** Nothing reports a *removal*, so a membership nobody has confirmed in
   * weeks is indistinguishable from a live one except by this. */
  last_reported_at: string | null;
  reported_by_agent_id: string | null;
}
