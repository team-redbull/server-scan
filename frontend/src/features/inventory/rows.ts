import { UNASSIGNED_SITE_ID } from "@/api/sites";
import type { SortableField } from "@/features/inventory/sorting";
import type { HealthSeverity, ServerRow, WarrantyStatus } from "@/types/server";
import { SEVERITY_ORDER } from "@/components/severity";
import { isDonor } from "@/lib/donor";

/**
 * Filtering, search, sort, facet counts and paging over the fleet's rows —
 * pure functions, so the page is `useMemo` glue (ADR-0033).
 */

export interface RowFilters {
  search?: string;
  /** Each of these eight takes several values (OR within, AND across); empty or unset is "any". */
  vendor?: string[];
  server_type?: string[];
  site_id?: string[];
  source_provider?: string[];
  installation_type?: string[];
  openshift_state?: string[];
  health?: string[];
  /** Fired policies by `policy_key`: a row matches when any of its health reasons is one of them. */
  reason?: string[];
  /** `WarrantyStatus` values (ADR-0070); never a health verdict, so maintenance does not affect it. */
  warranty?: string[];
  /** Unset shows servers in maintenance with the rest; "only" keeps just them, "exclude" hides them.
   * Healthy servers in maintenance are the exception: Healthy shows them only with "only". */
  maintenance?: "only" | "exclude";
  /** Only servers an install currently holds a lock on (ADR-0035). */
  reserved?: true;
  stale?: true;
  duplicate?: true;
  /** OpenShift membership contested between two clusters (`contested_with` set). */
  contested?: true;
  /** Installed in OpenShift under a hostname the vendor manager no longer
   * reports (ADR-0036) — `ServerRow.openshift_reported_name` is set. */
  nameMismatch?: true;
  /** Maintenance whose reason says "donor" (ADR-0038). */
  donor?: true;
  /** Selected MCEs (OR within); ANDed with every other filter. */
  mce?: string[];
  /** Selected clusters, hosted and UPI alike (OR within). */
  cluster?: string[];
}

/** One sidebar entry: a name and how many rows carry it under the current filters. */
export interface ClusterOption {
  name: string;
  count: number;
  /** The owning MCE for a hosted cluster; null for an MCE itself or a UPI cluster. */
  mce: string | null;
}

export interface ClusterFacets {
  mces: ClusterOption[];
  hosted: ClusterOption[];
  upi: ClusterOption[];
}

export interface FacetCounts {
  total: number;
  vendor: Record<string, number>;
  server_type: Record<string, number>;
  source_provider: Record<string, number>;
  installation_type: Record<string, number>;
  site_id: Record<string, number>;
  openshift_state: Record<string, number>;
  health: Record<string, number>;
  /** Rows per fired `policy_key`; a row counts once per policy. */
  reason: Record<string, number>;
  /** Rows per `WarrantyStatus`; a server with no status (a VM) is not counted. */
  warranty: Record<string, number>;
  maintenance: Record<string, number>;
  stale: Record<string, number>;
}

const COLLATOR = new Intl.Collator("en", {
  numeric: true,
  sensitivity: "base",
});

const HEX_LIKE = /^[0-9a-f:-]+$/i;

/**
 * How many rows in the given set share each name — the estate-side
 * naming collisions a name-derived duplicate can't tell apart from a
 * genuine platform bug (docs/adr/0016's duplicate-server update).
 *
 * Args:
 *   rows (ServerRow[]): The set to count within — the whole fleet, not
 *     an already-filtered subset, or a pair split by another filter
 *     would each look unique.
 *
 * Returns:
 *   Map<string, number>: Row count per `name`.
 */
function nameCounts(rows: ServerRow[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const row of rows) {
    counts.set(row.name, (counts.get(row.name) ?? 0) + 1);
  }
  return counts;
}

/**
 * Whether a row passes the Health and Reason dropdowns. Any ticked severity or reason also means
 * in service: a server in maintenance shows only when Maintenance is ticked too
 * (`maintenance: "only"`), whatever other filters (site, installation, ...) are set.
 *
 * Args:
 *   row (ServerRow): The row to test.
 *   filters (RowFilters): The active filters; only `health`, `reason` and `maintenance` are read.
 *
 * Returns:
 *   boolean: True when nothing is ticked, or the row's health is among the ticked severities
 *   (if any), it carries a ticked reason (if any), and it is in service (or Maintenance is ticked).
 */
function matchesHealth(row: ServerRow, filters: RowFilters): boolean {
  const severities = filters.health ?? [];
  const reasons = filters.reason ?? [];
  if (!severities.length && !reasons.length) return true;
  if (severities.length && !severities.includes(row.health)) return false;
  if (
    reasons.length &&
    !row.health_reasons.some((r) => reasons.includes(r.policy_key))
  )
    return false;
  return !row.maintenance.enabled || filters.maintenance === "only";
}

/**
 * Keep the rows every set filter matches; `search` is applied last.
 *
 * Args:
 *   rows (ServerRow[]): The fleet — always the whole thing, even when a
 *     `duplicate` filter is also set (see `nameCounts`).
 *   filters (RowFilters): Empty strings and unset keys are "any".
 *
 * Returns:
 *   ServerRow[]: The matching rows, in input order.
 */
export function filterRows(
  rows: ServerRow[],
  filters: RowFilters,
): ServerRow[] {
  const counts = filters.duplicate ? nameCounts(rows) : null;
  const kept = rows.filter(
    (row) =>
      (!filters.vendor?.length || filters.vendor.includes(row.vendor)) &&
      (!filters.server_type?.length ||
        filters.server_type.includes(row.server_type)) &&
      (!filters.site_id?.length ||
        filters.site_id.includes(row.site_id ?? UNASSIGNED_SITE_ID)) &&
      (!filters.source_provider?.length ||
        (row.source_provider !== null &&
          filters.source_provider.includes(row.source_provider))) &&
      (!filters.installation_type?.length ||
        filters.installation_type.includes(row.installation_type)) &&
      (!filters.openshift_state?.length ||
        filters.openshift_state.includes(row.openshift_state)) &&
      matchesHealth(row, filters) &&
      (!filters.warranty?.length ||
        (row.warranty_status != null &&
          filters.warranty.includes(row.warranty_status))) &&
      (filters.maintenance === undefined ||
        (filters.maintenance === "only") === row.maintenance.enabled) &&
      (!filters.reserved || row.reservation.held) &&
      (!filters.stale || row.stale) &&
      (!filters.nameMismatch || row.openshift_reported_name !== null) &&
      (!filters.contested || row.contested_with !== null) &&
      (!filters.donor || isDonor(row.maintenance)) &&
      (!filters.mce?.length ||
        (row.mce_name !== null && filters.mce.includes(row.mce_name))) &&
      (!filters.cluster?.length ||
        (row.cluster_name !== null &&
          filters.cluster.includes(row.cluster_name))) &&
      (!counts || (counts.get(row.name) ?? 0) > 1),
  );
  return searchRows(kept, filters.search ?? "");
}

/** Longest regex body the search box accepts, same cap as the API's search. */
export const MAX_REGEX_LENGTH = 64;

export interface SearchMatcher {
  /** True when the row matches; every row matches while `error` is set. */
  test: (row: ServerRow) => boolean;
  /** Why the query is unusable (a bad regex), shown under the box. */
  error: string | null;
}

/** The text fields a search looks at, lowercased; MACs are separate. */
function searchFields(row: ServerRow): string[] {
  const fields = [
    row.name,
    row.model,
    row.serial,
    row.vendor,
    row.site_id,
    row.installation_type,
    row.bmc_host,
    // The MCE an install is headed for, so searching that cluster finds the
    // machines going to it — not only the ones already installed there.
    row.reservation.mce_cluster,
  ];
  return fields.flatMap((f) => (f ? [f.toLowerCase()] : []));
}

/**
 * Turn the search box text into a matcher. Plain text is a
 * case-insensitive substring; `"text"` in quotes must equal a whole field;
 * `/pattern/` is a case-insensitive regular expression (ADR-0046).
 *
 * Args:
 *   query (string): The raw box text; blank matches everything.
 *
 * Returns:
 *   SearchMatcher: The row test, plus `error` for an unusable regex (in
 *   which case the test keeps every row rather than blanking the table).
 */
export function parseSearch(query: string): SearchMatcher {
  const raw = query.trim();
  if (!raw) return { test: () => true, error: null };

  const regexBody = /^\/(.+)\/$/.exec(raw)?.[1];
  if (regexBody !== undefined) {
    if (regexBody.length > MAX_REGEX_LENGTH) {
      return {
        test: () => true,
        error: `Regex is longer than ${MAX_REGEX_LENGTH} characters`,
      };
    }
    let pattern: RegExp;
    try {
      pattern = new RegExp(regexBody, "i");
    } catch (e) {
      return {
        test: () => true,
        error: `Invalid regex: ${e instanceof Error ? e.message : String(e)}`,
      };
    }
    return {
      test: (row) =>
        searchFields(row).some((f) => pattern.test(f)) ||
        row.macs.some((mac) => pattern.test(mac)),
      error: null,
    };
  }

  const exactBody = /^"(.+)"$/.exec(raw)?.[1];
  const exact = exactBody !== undefined;
  const q = (exact ? exactBody : raw).toLowerCase();
  const bare = HEX_LIKE.test(q) ? q.replace(/[:-]/g, "") : null;
  const matches = (field: string, needle: string) =>
    exact ? field === needle : field.includes(needle);
  return {
    test: (row) =>
      searchFields(row).some((f) => matches(f, q)) ||
      row.macs.some(
        (mac) =>
          matches(mac, q) ||
          (bare !== null && matches(mac.replace(/:/g, ""), bare)),
      ),
    error: null,
  };
}

/**
 * Keep the rows the search box text matches (see `parseSearch` for the
 * three syntaxes) over name, model, serial, vendor, site, installation
 * type, BMC host, MACs and a reservation's MCE; a hex-looking query also
 * matches MACs bare.
 *
 * Args:
 *   rows (ServerRow[]): The rows to search.
 *   query (string): Blank means "keep everything".
 *
 * Returns:
 *   ServerRow[]: The rows with a match, in input order.
 */
export function searchRows(rows: ServerRow[], query: string): ServerRow[] {
  if (!query.trim()) return rows;
  return rows.filter(parseSearch(query).test);
}

/**
 * Sort a copy of the rows by one column, naturally (`srv-2` before
 * `srv-10`) and with nulls last whichever the direction; `health` sorts
 * by severity (ascending = healthiest first), not alphabetically.
 *
 * Args:
 *   rows (ServerRow[]): The rows; not mutated.
 *   field (SortableField): The column.
 *   desc (boolean): Descending when true.
 *
 * Returns:
 *   ServerRow[]: A sorted copy. Ties keep input order.
 */
export function sortRows(
  rows: ServerRow[],
  field: SortableField,
  desc: boolean,
): ServerRow[] {
  const sign = desc ? -1 : 1;
  if (field === "health") {
    const rank = (row: ServerRow) =>
      SEVERITY_ORDER.length - SEVERITY_ORDER.indexOf(row.health);
    return [...rows].sort((a, b) => sign * (rank(a) - rank(b)));
  }
  return [...rows].sort((a, b) => {
    const av = a[field];
    const bv = b[field];
    if (av === null) return bv === null ? 0 : 1;
    if (bv === null) return -1;
    return sign * COLLATOR.compare(av, bv);
  });
}

function count(counts: Record<string, number>, key: string): void {
  counts[key] = (counts[key] ?? 0) + 1;
}

/**
 * Per-option match counts over the rows given — call it with the filtered
 * set, so each count describes what the operator is looking at.
 *
 * Args:
 *   rows (ServerRow[]): The rows to count.
 *
 * Returns:
 *   FacetCounts: Per-value counts; a value no row has is absent (the page
 *   renders it as 0).
 */
export function facetCounts(rows: ServerRow[]): FacetCounts {
  const facets: FacetCounts = {
    total: rows.length,
    vendor: {},
    server_type: {},
    source_provider: {},
    installation_type: {},
    site_id: {},
    openshift_state: {},
    health: {},
    reason: {},
    warranty: {},
    maintenance: {},
    stale: {},
  };
  for (const row of rows) {
    count(facets.vendor, row.vendor);
    count(facets.server_type, row.server_type);
    if (row.source_provider) count(facets.source_provider, row.source_provider);
    count(facets.installation_type, row.installation_type);
    count(facets.site_id, row.site_id ?? UNASSIGNED_SITE_ID);
    count(facets.openshift_state, row.openshift_state);
    count(facets.health, row.health);
    for (const key of new Set(row.health_reasons.map((r) => r.policy_key)))
      if (key) count(facets.reason, key);
    if (row.warranty_status) count(facets.warranty, row.warranty_status);
    count(facets.maintenance, String(row.maintenance.enabled));
    count(facets.stale, String(row.stale));
  }
  return facets;
}

/** The filter keys the multi-select dropdowns own, with the facet each one counts. */
const DIMENSIONS = [
  ["vendor", "vendor"],
  ["server_type", "server_type"],
  ["source_provider", "source_provider"],
  ["site_id", "site_id"],
  ["installation_type", "installation_type"],
  ["openshift_state", "openshift_state"],
  ["health", "health"],
  ["reason", "reason"],
  ["warranty", "warranty"],
] as const;

/**
 * Facet counts for the dropdown options: each dimension's counts are taken over the rows every
 * *other* filter leaves, so an option's number is what ticking it would add (the sidebar's rule).
 * Without this a ticked option would hide every other option's count, or read zero.
 *
 * Args:
 *   rows (ServerRow[]): The whole fleet.
 *   filters (RowFilters): The active filters, the dropdowns' own included.
 *
 * Returns:
 *   FacetCounts: `total` and the toggles' counts over the fully filtered rows (as `facetCounts`);
 *   the dropdown dimensions replaced by their own-filter-excluded counts. `maintenance` is
 *   counted without the maintenance filter, so its option always shows a number; every other
 *   count respects it, and Health's and Reason's count in-service rows only (`matchesHealth`).
 */
export function dropdownFacetCounts(
  rows: ServerRow[],
  filters: RowFilters,
): FacetCounts {
  const facets = facetCounts(filterRows(rows, filters));
  for (const [filterKey, facetKey] of DIMENSIONS) {
    facets[facetKey] = facetCounts(
      filterRows(rows, without(filters, filterKey)),
    )[facetKey];
  }
  const healthOnly: RowFilters = {};
  if (filters.health) healthOnly.health = filters.health;
  if (filters.reason) healthOnly.reason = filters.reason;
  healthOnly.maintenance = "only";
  const ignoringHealth = filterRows(
    rows,
    without(without(without(filters, "maintenance"), "health"), "reason"),
  );
  facets.maintenance = facetCounts(
    ignoringHealth.filter((row) => matchesHealth(row, healthOnly)),
  ).maintenance;
  const inService = (row: ServerRow) =>
    filters.maintenance === "only" || !row.maintenance.enabled;
  facets.health = facetCounts(
    filterRows(rows, without(filters, "health")).filter(inService),
  ).health;
  facets.reason = facetCounts(
    filterRows(rows, without(filters, "reason")).filter(inService),
  ).reason;
  return facets;
}

/** One fired policy as a Reason option: its key, the name it shows, and its worst severity. */
export interface ReasonOption {
  key: string;
  name: string;
  severity: HealthSeverity;
}

/**
 * Every policy fired anywhere in the fleet, read off the rows like the cluster sidebar, so a new
 * or renamed policy appears with the next poll. A key fired under several names (a site
 * override, ADR-0005) shows its most common one.
 *
 * Args:
 *   rows (ServerRow[]): The whole fleet.
 *
 * Returns:
 *   ReasonOption[]: Worst severity first, then by name.
 */
export function reasonOptions(rows: ServerRow[]): ReasonOption[] {
  const byKey = new Map<
    string,
    { names: Map<string, number>; severity: HealthSeverity }
  >();
  for (const row of rows) {
    for (const r of row.health_reasons) {
      if (!r.policy_key) continue;
      const entry = byKey.get(r.policy_key) ?? {
        names: new Map<string, number>(),
        severity: r.severity,
      };
      const name = r.policy_name || r.policy_key;
      entry.names.set(name, (entry.names.get(name) ?? 0) + 1);
      if (
        SEVERITY_ORDER.indexOf(r.severity) <
        SEVERITY_ORDER.indexOf(entry.severity)
      )
        entry.severity = r.severity;
      byKey.set(r.policy_key, entry);
    }
  }
  const options = [...byKey].map(([key, { names, severity }]) => ({
    key,
    severity,
    name: [...names].sort((a, b) => b[1] - a[1])[0]?.[0] ?? key,
  }));
  return options.sort(
    (a, b) =>
      SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity) ||
      COLLATOR.compare(a.name, b.name),
  );
}

/**
 * One page of rows, clamping an out-of-range page number.
 *
 * Args:
 *   rows (ServerRow[]): The full ordered set.
 *   page (number): 1-based; anything below 1 or past the end clamps.
 *   pageSize (number): Rows per page.
 *
 * Returns:
 *   The page's rows, the page actually shown, and the page count (never 0).
 */
export function paginate(
  rows: ServerRow[],
  page: number,
  pageSize: number,
): { items: ServerRow[]; page: number; pageCount: number } {
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const current = Math.min(Math.max(1, Math.trunc(page) || 1), pageCount);
  const start = (current - 1) * pageSize;
  return {
    items: rows.slice(start, start + pageSize),
    page: current,
    pageCount,
  };
}

function without(filters: RowFilters, key: keyof RowFilters): RowFilters {
  const next = { ...filters };
  delete next[key];
  return next;
}

/**
 * The MCE / hosted-cluster / UPI-cluster sidebar options, read off the
 * fleet itself — nothing is configured, so a new cluster appears with the
 * next poll and vanishes with its last server.
 *
 * Options come from the whole fleet (they never disappear as you filter);
 * each count is over the rows every *other* filter leaves, so it says what
 * ticking that option would show. A cluster with an MCE on any row is
 * hosted; one with none is UPI.
 *
 * Args:
 *   rows (ServerRow[]): The whole fleet.
 *   filters (RowFilters): The active filters, the sidebar's own included.
 *
 * Returns:
 *   ClusterFacets: Each list sorted by server count, largest first, then naturally by name.
 */
export function clusterFacets(
  rows: ServerRow[],
  filters: RowFilters,
): ClusterFacets {
  const mceCounts: Record<string, number> = {};
  for (const row of filterRows(rows, without(filters, "mce"))) {
    if (row.mce_name) count(mceCounts, row.mce_name);
  }
  const clusterCounts: Record<string, number> = {};
  for (const row of filterRows(rows, without(filters, "cluster"))) {
    if (row.cluster_name) count(clusterCounts, row.cluster_name);
  }

  const clusterMce = new Map<string, string | null>();
  const mces = new Set<string>();
  for (const row of rows) {
    if (row.mce_name) mces.add(row.mce_name);
    if (row.cluster_name) {
      clusterMce.set(
        row.cluster_name,
        row.mce_name ?? clusterMce.get(row.cluster_name) ?? null,
      );
    }
  }

  const byCount = (a: ClusterOption, b: ClusterOption) =>
    b.count - a.count || COLLATOR.compare(a.name, b.name);
  const hosted: ClusterOption[] = [];
  const upi: ClusterOption[] = [];
  for (const [name, mce] of clusterMce) {
    (mce ? hosted : upi).push({ name, mce, count: clusterCounts[name] ?? 0 });
  }
  return {
    mces: [...mces]
      .map((name) => ({ name, mce: null, count: mceCounts[name] ?? 0 }))
      .sort(byCount),
    hosted: hosted.sort(byCount),
    upi: upi.sort(byCount),
  };
}

/** The Warranty dropdown's labels, in the order they are explained. */
export const WARRANTY_LABELS: Record<WarrantyStatus, string> = {
  EXPIRED: "Expired",
  EXPIRING: "Expiring soon",
  ACTIVE: "Active",
  NOT_READ: "Date not read",
  NO_RECORD: "No warranty record",
};

/**
 * A row's warranty status on `today`, from its end date (ADR-0070).
 *
 * Args:
 *   row (ServerRow): One inventory row.
 *   today (string): Today as `YYYY-MM-DD`, in the viewer's time zone.
 *   expiringDays (number): How many days ahead counts as "expiring soon".
 *
 * Returns:
 *   WarrantyStatus | null: null for a server never looked up (a VM, no serial).
 */
export function warrantyStatus(
  row: Pick<ServerRow, "warranty_found" | "warranty_expires_on">,
  today: string,
  expiringDays: number,
): WarrantyStatus | null {
  if (row.warranty_found == null) return null;
  if (!row.warranty_found) return "NO_RECORD";
  const expires = row.warranty_expires_on;
  if (!expires) return "NOT_READ";
  if (expires < today) return "EXPIRED";
  const soon = new Date(`${today}T00:00:00Z`);
  soon.setUTCDate(soon.getUTCDate() + expiringDays);
  return expires <= soon.toISOString().slice(0, 10) ? "EXPIRING" : "ACTIVE";
}

/**
 * Today as `YYYY-MM-DD` in the viewer's time zone.
 *
 * Returns:
 *   string: The local date.
 */
export function localToday(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

/**
 * The rows with `warranty_status` set, so filtering and counting read one field.
 *
 * Args:
 *   rows (ServerRow[]): The fleet as the API sent it.
 *   expiringDays (number): The "expiring soon" window.
 *   today (string): Today as `YYYY-MM-DD`.
 *
 * Returns:
 *   ServerRow[]: New row objects carrying their status.
 */
export function withWarrantyStatus(
  rows: ServerRow[],
  expiringDays: number,
  today: string = localToday(),
): ServerRow[] {
  return rows.map((row) => ({
    ...row,
    warranty_status: warrantyStatus(row, today, expiringDays),
  }));
}
