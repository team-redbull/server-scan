import { useMemo } from "react";
import { useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { SORTABLE_FIELDS } from "@/features/inventory/sorting";
import type { SortableField } from "@/features/inventory/sorting";
import { ClusterSidebar } from "@/features/inventory/ClusterSidebar";
import { downloadCsv, rowsToCsv } from "@/features/inventory/csvExport";
import { InventoryTable } from "@/features/inventory/InventoryTable";
import { MultiSelect } from "@/features/inventory/MultiSelect";
import { siteOptions, SOURCE_PROVIDERS, VENDORS } from "@/api/sites";
import { useServerRowsQuery } from "@/features/inventory/hooks";
import {
  clusterFacets,
  dropdownFacetCounts,
  filterRows,
  paginate,
  parseSearch,
  sortRows,
} from "@/features/inventory/rows";
import type { RowFilters } from "@/features/inventory/rows";
import { useSitesQuery } from "@/features/sites/hooks";
import { formatTimestamp } from "@/lib/datetime";
import { useDebouncedValue } from "@/lib/useDebouncedValue";

const INSTALLATION_TYPES = [
  "HOSTED_CLUSTER",
  "MCE",
  "UPI",
  "UNCLASSIFIED",
] as const;
const OPENSHIFT_STATES = [
  { value: "INSTALLED", label: "Installed" },
  { value: "INSTALLED_TO_INVENTORY", label: "In inventory" },
  { value: "AVAILABLE", label: "Available" },
] as const;
const HEALTH_SEVERITIES = [
  "UNKNOWN",
  "HEALTHY",
  "WARNING",
  "MAJOR",
  "CRITICAL",
] as const;
type MaintenanceMode = "include" | "exclude" | "only";
/** `maintenance=true` is "only": the Sites cards and older links already use it. */
const MAINTENANCE_PARAM: Record<MaintenanceMode, string | null> = {
  include: null,
  exclude: "exclude",
  only: "true",
};
const FIELD_CLASS =
  "mt-1 w-full min-w-0 rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-2.5 py-1.5 text-sm text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)]";

const DEFAULT_SORT: SortableField = "name";
const PAGE_SIZE = 50;

function isSortableField(value: string): value is SortableField {
  return (SORTABLE_FIELDS as readonly string[]).includes(value);
}

/** One labelled filter in the field row; shrinks to fit so a wide count never wraps. */
function FilterField({
  id,
  label,
  children,
}: {
  id: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-1 flex-col text-xs font-medium text-[var(--text-secondary)]">
      <label htmlFor={id}>{label}</label>
      {children}
    </div>
  );
}

/** The inventory table. All filter and pagination state lives in the URL,
 * so refresh and back both land where the user was — a project requirement.
 * The fleet arrives whole and is filtered, sorted and paged here (ADR-0033). */
export function InventoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();

  const sites = siteOptions(useSitesQuery().data?.items);

  const searchInput = searchParams.get("search") ?? "";
  const debouncedSearch = useDebouncedValue(searchInput, 300);

  // Repeated params (`?vendor=dell&vendor=hp`); a single `?vendor=dell` link is one value.
  const vendors = useMemo(() => searchParams.getAll("vendor"), [searchParams]);
  const siteIds = useMemo(() => searchParams.getAll("site_id"), [searchParams]);
  const installationTypes = useMemo(
    () => searchParams.getAll("installation_type"),
    [searchParams],
  );
  const openshiftStates = useMemo(
    () => searchParams.getAll("openshift_state"),
    [searchParams],
  );
  const sourceProviders = useMemo(
    () => searchParams.getAll("source_provider"),
    [searchParams],
  );
  const maintenanceParam = searchParams.get("maintenance");
  const maintenanceMode: MaintenanceMode =
    maintenanceParam === "true" || maintenanceParam === "only"
      ? "only"
      : maintenanceParam === "exclude"
        ? "exclude"
        : "include";
  const healthSeverities = useMemo(
    () => searchParams.getAll("health_overall"),
    [searchParams],
  );
  const staleOnly = searchParams.get("stale") === "true";
  const duplicateOnly = searchParams.get("duplicate") === "true";
  const contestedOnly = searchParams.get("contested") === "true";
  const nameMismatchOnly = searchParams.get("name_mismatch") === "true";
  const donorOnly = searchParams.get("donor") === "true";
  const sortParam = searchParams.get("sort") ?? "";
  const sortField: SortableField = isSortableField(sortParam)
    ? sortParam
    : DEFAULT_SORT;
  const sortDesc = searchParams.get("sort_desc") === "true";
  const pageParam = Number(searchParams.get("page") ?? "1");
  const mceSel = useMemo(() => searchParams.getAll("mce"), [searchParams]);
  const clusterSel = useMemo(
    () => searchParams.getAll("cluster"),
    [searchParams],
  );

  const filters: RowFilters = useMemo(() => {
    // Built incrementally: `exactOptionalPropertyTypes` forbids assigning
    // `undefined` to an optional property outright.
    const next: RowFilters = {};
    if (debouncedSearch) next.search = debouncedSearch;
    if (vendors.length) next.vendor = vendors;
    if (siteIds.length) next.site_id = siteIds;
    if (sourceProviders.length) next.source_provider = sourceProviders;
    if (installationTypes.length) next.installation_type = installationTypes;
    if (openshiftStates.length) next.openshift_state = openshiftStates;
    if (healthSeverities.length) next.health = healthSeverities;
    if (maintenanceMode !== "include") next.maintenance = maintenanceMode;
    if (staleOnly) next.stale = true;
    if (duplicateOnly) next.duplicate = true;
    if (contestedOnly) next.contested = true;
    if (nameMismatchOnly) next.nameMismatch = true;
    if (donorOnly) next.donor = true;
    if (mceSel.length) next.mce = mceSel;
    if (clusterSel.length) next.cluster = clusterSel;
    return next;
  }, [
    debouncedSearch,
    vendors,
    siteIds,
    sourceProviders,
    installationTypes,
    openshiftStates,
    healthSeverities,
    maintenanceMode,
    staleOnly,
    duplicateOnly,
    contestedOnly,
    nameMismatchOnly,
    donorOnly,
    mceSel,
    clusterSel,
  ]);

  const searchError = useMemo(
    () => parseSearch(searchInput).error,
    [searchInput],
  );

  const { data, isPending, isError, error } = useServerRowsQuery();
  const matched = useMemo(
    () => sortRows(filterRows(data?.items ?? [], filters), sortField, sortDesc),
    [data, filters, sortField, sortDesc],
  );
  const facets = useMemo(
    () => (data ? dropdownFacetCounts(data.items, filters) : undefined),
    [data, filters],
  );
  const clusterOptions = useMemo(
    () => (data ? clusterFacets(data.items, filters) : undefined),
    [data, filters],
  );
  const {
    items: servers,
    page,
    pageCount,
  } = paginate(matched, pageParam, PAGE_SIZE);

  const inMaint = facets?.maintenance["true"];
  const notInMaint = facets?.maintenance["false"];

  /** The dropdowns' text while nothing is ticked: "All (2,504)" once the fleet has loaded. */
  function allLabel(word = "All"): string {
    return facets ? `${word} (${facets.total})` : word;
  }

  /** Apply a filter patch to the URL and go back to page 1. Built from the
   * live location, not the hook's `prev`: that is the last *render's* URL,
   * so two changes under ~100 ms apart would both start from it and the
   * second silently drop the first. `replace: true` keeps keystrokes out of
   * browser history; `flushSync` keeps the navigation out of a transition,
   * or a controlled checkbox snaps back until the deferred render lands. */
  function updateFilters(patch: Record<string, string | string[] | null>) {
    setSearchParams(
      () => {
        const next = new URLSearchParams(window.location.search);
        for (const [key, value] of Object.entries(patch)) {
          next.delete(key);
          for (const v of Array.isArray(value) ? value : [value]) {
            if (v !== null && v !== "") next.append(key, v);
          }
        }
        next.delete("page");
        return next;
      },
      { replace: true, flushSync: true },
    );
  }

  /** Tick or untick one value of a repeated param (`?mce=a&mce=b`, `?vendor=dell&vendor=hp`). */
  function toggleMulti(key: string, value: string) {
    const current = new URLSearchParams(window.location.search).getAll(key);
    const next = current.includes(value)
      ? current.filter((v) => v !== value)
      : [...current, value];
    updateFilters({ [key]: next });
  }

  /** Show servers in maintenance with the rest, hide them, or keep only them. */
  function setMaintenance(mode: MaintenanceMode) {
    updateFilters({ maintenance: MAINTENANCE_PARAM[mode] });
  }

  function handleSortChange(field: SortableField, desc: boolean) {
    updateFilters({ sort: field, sort_desc: desc ? "true" : null });
  }

  /** Exports every row matching the current filters — not just the current
   * page — in the same order shown, so the file reflects what's on screen. */
  function handleExportCsv() {
    downloadCsv(
      `inventory-${new Date().toISOString().slice(0, 10)}.csv`,
      rowsToCsv(matched),
    );
  }

  function goToPage(target: number) {
    setSearchParams(
      () => {
        const next = new URLSearchParams(window.location.search);
        if (target <= 1) {
          next.delete("page");
        } else {
          next.set("page", String(target));
        }
        return next;
      },
      { replace: true, flushSync: true },
    );
  }

  // Named for the empty state: "no servers match" rather than "no servers".
  const activeFilters: { key: string; label: string; value?: string }[] = [];
  if (debouncedSearch)
    activeFilters.push({ key: "search", label: `Search "${debouncedSearch}"` });
  for (const v of vendors)
    activeFilters.push({ key: "vendor", label: `Vendor ${v}`, value: v });
  for (const id of siteIds)
    activeFilters.push({
      key: "site_id",
      label: `Site ${sites.find((s) => s.value === id)?.label ?? id}`,
      value: id,
    });
  for (const p of sourceProviders)
    activeFilters.push({
      key: "source_provider",
      label: `Source ${SOURCE_PROVIDERS.find((s) => s.value === p)?.label ?? p}`,
      value: p,
    });
  for (const t of installationTypes)
    activeFilters.push({
      key: "installation_type",
      label: `Classification ${t}`,
      value: t,
    });
  for (const st of openshiftStates)
    activeFilters.push({
      key: "openshift_state",
      label: `Installation ${OPENSHIFT_STATES.find((s) => s.value === st)?.label ?? st}`,
      value: st,
    });
  for (const h of healthSeverities)
    activeFilters.push({
      key: "health_overall",
      label: `Health ${h}`,
      value: h,
    });
  if (maintenanceMode !== "include")
    activeFilters.push({
      key: "maintenance",
      label: maintenanceMode === "only" ? "Maintenance only" : "Maintenance hidden",
    });
  for (const name of mceSel)
    activeFilters.push({ key: "mce", label: `MCE ${name}`, value: name });
  for (const name of clusterSel)
    activeFilters.push({
      key: "cluster",
      label: `Cluster ${name}`,
      value: name,
    });
  if (staleOnly) activeFilters.push({ key: "stale", label: "Stale" });
  if (duplicateOnly)
    activeFilters.push({ key: "duplicate", label: "Duplicate Name" });
  if (contestedOnly)
    activeFilters.push({ key: "contested", label: "Duplicate Server" });
  if (nameMismatchOnly)
    activeFilters.push({ key: "name_mismatch", label: "Name mismatch" });
  if (donorOnly) activeFilters.push({ key: "donor", label: "Donor" });

  function clearFilters() {
    updateFilters({
      search: null,
      vendor: null,
      site_id: null,
      source_provider: null,
      installation_type: null,
      openshift_state: null,
      health_overall: null,
      maintenance: null,
      stale: null,
      duplicate: null,
      contested: null,
      name_mismatch: null,
      donor: null,
      mce: null,
      cluster: null,
    });
  }

  return (
    <main className="mx-auto max-w-[1600px] px-8 py-8">
      <h1 className="text-xl font-semibold tracking-tight text-[var(--text-primary)]">
        Servers
      </h1>
      <p className="mt-1 text-sm text-[var(--text-secondary)]">
        {facets
          ? `${facets.total} server${facets.total === 1 ? "" : "s"}`
          : "Browse and filter discovered servers."}
        {data && (
          <span className="ml-2 text-xs text-[var(--text-muted)]">
            Updated {formatTimestamp(data.generated_at)}
          </span>
        )}
      </p>

      <div className="mt-6 flex items-start gap-4">
        {clusterOptions && (
          <ClusterSidebar
            facets={clusterOptions}
            mce={mceSel}
            cluster={clusterSel}
            onToggle={toggleMulti}
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="mb-4 space-y-3">
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault();
              }}
            >
              {/* Search and the selects share one row and shrink to fit, so a wide
            count can never wrap one under another; the toggles own row two. */}
              <div className="flex items-end gap-3">
                <label className="flex min-w-0 flex-[1.4] flex-col text-xs font-medium text-[var(--text-secondary)]">
                  Search
                  <input
                    type="text"
                    value={searchInput}
                    onChange={(e) => {
                      updateFilters({ search: e.target.value });
                    }}
                    placeholder="Name, serial, BMC…"
                    title={
                      'Plain text matches anywhere. "10.0.0.1" in quotes must equal the whole value. /10\\.0\\.0\\.(1|13)$/ between slashes is a regular expression.'
                    }
                    aria-invalid={searchError !== null}
                    className={FIELD_CLASS}
                  />
                  {searchError && (
                    <span
                      role="alert"
                      className="mt-1 text-[var(--text-on-critical)]"
                    >
                      {searchError}
                    </span>
                  )}
                </label>
                <FilterField id="filter-vendor" label="Vendor">
                  <MultiSelect
                    id="filter-vendor"
                    label="Vendor"
                    className={FIELD_CLASS}
                    allLabel={allLabel()}
                    selected={vendors}
                    options={VENDORS.map((v) => ({
                      value: v,
                      label: v,
                      count: facets?.vendor[v] ?? (facets ? 0 : undefined),
                    }))}
                    onToggle={(value) => {
                      toggleMulti("vendor", value);
                    }}
                    onClear={() => {
                      updateFilters({ vendor: null });
                    }}
                  />
                </FilterField>

                <FilterField id="filter-source" label="Source">
                  <MultiSelect
                    id="filter-source"
                    label="Source"
                    className={FIELD_CLASS}
                    allLabel={allLabel()}
                    selected={sourceProviders}
                    options={SOURCE_PROVIDERS.map((s) => ({
                      value: s.value,
                      label: s.label,
                      count:
                        facets?.source_provider[s.value] ??
                        (facets ? 0 : undefined),
                    }))}
                    onToggle={(value) => {
                      toggleMulti("source_provider", value);
                    }}
                    onClear={() => {
                      updateFilters({ source_provider: null });
                    }}
                  />
                </FilterField>

                <FilterField id="filter-site" label="Site">
                  <MultiSelect
                    id="filter-site"
                    label="Site"
                    className={FIELD_CLASS}
                    allLabel={allLabel("All sites")}
                    selected={siteIds}
                    options={sites.map((site) => ({
                      value: site.value,
                      label: site.label,
                      count:
                        facets?.site_id[site.value] ?? (facets ? 0 : undefined),
                    }))}
                    onToggle={(value) => {
                      toggleMulti("site_id", value);
                    }}
                    onClear={() => {
                      updateFilters({ site_id: null });
                    }}
                  />
                </FilterField>

                <FilterField id="filter-classification" label="Classification">
                  <MultiSelect
                    id="filter-classification"
                    label="Classification"
                    className={FIELD_CLASS}
                    allLabel={allLabel()}
                    selected={installationTypes}
                    options={INSTALLATION_TYPES.map((t) => ({
                      value: t,
                      label: t,
                      count:
                        facets?.installation_type[t] ??
                        (facets ? 0 : undefined),
                    }))}
                    onToggle={(value) => {
                      toggleMulti("installation_type", value);
                    }}
                    onClear={() => {
                      updateFilters({ installation_type: null });
                    }}
                  />
                </FilterField>

                <FilterField id="filter-installation" label="Installation">
                  <MultiSelect
                    id="filter-installation"
                    label="Installation"
                    className={FIELD_CLASS}
                    allLabel={allLabel()}
                    selected={openshiftStates}
                    options={OPENSHIFT_STATES.map((s) => ({
                      value: s.value,
                      label: s.label,
                      count:
                        facets?.openshift_state[s.value] ??
                        (facets ? 0 : undefined),
                    }))}
                    onToggle={(value) => {
                      toggleMulti("openshift_state", value);
                    }}
                    onClear={() => {
                      updateFilters({ openshift_state: null });
                    }}
                  />
                </FilterField>

                <FilterField id="filter-health" label="Health">
                  <MultiSelect
                    id="filter-health"
                    label="Health"
                    className={FIELD_CLASS}
                    allLabel={allLabel()}
                    selected={healthSeverities}
                    options={HEALTH_SEVERITIES.map((h) => ({
                      value: h,
                      label: h,
                      count: facets?.health[h] ?? (facets ? 0 : undefined),
                    }))}
                    onToggle={(value) => {
                      toggleMulti("health_overall", value);
                    }}
                    onClear={() => {
                      updateFilters({ health_overall: null });
                    }}
                  />
                </FilterField>
              </div>
              <div className="flex flex-wrap items-center gap-6">
                <div
                  role="radiogroup"
                  aria-label="Maintenance"
                  className="flex items-center gap-2 text-xs font-medium text-[var(--text-secondary)]"
                  title="A server in maintenance still counts under its own health; choose whether the list shows it"
                >
                  <span aria-hidden="true">Maintenance</span>
                  <div className="inline-flex overflow-hidden rounded-md border border-[var(--border-subtle)]">
                    {(
                      [
                        ["include", "Include", (inMaint ?? 0) + (notInMaint ?? 0)],
                        ["exclude", "Hide", notInMaint],
                        ["only", "Only", inMaint],
                      ] as const
                    ).map(([mode, label, n]) => (
                      <button
                        key={mode}
                        type="button"
                        role="radio"
                        aria-checked={maintenanceMode === mode}
                        onClick={() => {
                          setMaintenance(mode);
                        }}
                        className={`px-2.5 py-1 transition-colors duration-[var(--duration-fast)] ${
                          maintenanceMode === mode
                            ? "bg-[var(--tint-info)] text-[var(--text-primary)]"
                            : "bg-[var(--surface-raised)] hover:bg-[var(--surface-hover)]"
                        }`}
                      >
                        {label}
                        {facets ? ` (${n ?? 0})` : ""}
                      </button>
                    ))}
                  </div>
                </div>

                <label
                  className="flex items-center gap-2 text-xs font-medium text-[var(--text-secondary)]"
                  title="Not collected within the staleness window (INVENTORY_STALE_AFTER_SECONDS), or never"
                >
                  <input
                    type="checkbox"
                    checked={staleOnly}
                    onChange={(e) => {
                      updateFilters({
                        stale: e.target.checked ? "true" : null,
                      });
                    }}
                  />
                  Stale{staleOnly && facets ? ` (${facets.total})` : ""}
                </label>

                <label
                  className="flex items-center gap-2 text-xs font-medium text-[var(--text-secondary)]"
                  title="Shares its name with another server — a real platform bug (one machine, two documents) or two different machines with the same profile name"
                >
                  <input
                    type="checkbox"
                    checked={duplicateOnly}
                    onChange={(e) => {
                      updateFilters({
                        duplicate: e.target.checked ? "true" : null,
                      });
                    }}
                  />
                  Duplicate Name
                  {duplicateOnly && facets ? ` (${facets.total})` : ""}
                </label>

                <label
                  className="flex items-center gap-2 text-xs font-medium text-[var(--text-secondary)]"
                  title="Claimed by two OpenShift clusters at once — one physical server in two memberships"
                >
                  <input
                    type="checkbox"
                    checked={contestedOnly}
                    onChange={(e) => {
                      updateFilters({
                        contested: e.target.checked ? "true" : null,
                      });
                    }}
                  />
                  Duplicate Server
                  {contestedOnly && facets ? ` (${facets.total})` : ""}
                </label>

                <label
                  className="flex items-center gap-2 text-xs font-medium text-[var(--text-secondary)]"
                  title="Installed in OpenShift under a hostname the vendor manager no longer reports — a rename after install"
                >
                  <input
                    type="checkbox"
                    checked={nameMismatchOnly}
                    onChange={(e) => {
                      updateFilters({
                        name_mismatch: e.target.checked ? "true" : null,
                      });
                    }}
                  />
                  Name mismatch
                  {nameMismatchOnly && facets ? ` (${facets.total})` : ""}
                </label>

                <label
                  className="flex items-center gap-2 text-xs font-medium text-[var(--text-secondary)]"
                  title="In maintenance with “donor” in the reason — kept only for its parts"
                >
                  <input
                    type="checkbox"
                    checked={donorOnly}
                    onChange={(e) => {
                      updateFilters({
                        donor: e.target.checked ? "true" : null,
                      });
                    }}
                  />
                  Donor{donorOnly && facets ? ` (${facets.total})` : ""}
                </label>

                <button
                  type="button"
                  onClick={handleExportCsv}
                  disabled={matched.length === 0}
                  className="ml-auto rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-2.5 py-1.5 text-xs font-medium text-[var(--text-primary)] hover:border-[var(--border-strong)] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Export CSV
                </button>
              </div>
            </form>
            {activeFilters.length > 0 && (
              <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-[var(--text-secondary)]">
                <span>Filtered by:</span>
                {activeFilters.map((f) => (
                  <button
                    key={`${f.key}:${f.value ?? ""}`}
                    type="button"
                    onClick={() => {
                      if (f.value === undefined)
                        updateFilters({ [f.key]: null });
                      else toggleMulti(f.key, f.value);
                    }}
                    title="Remove this filter"
                    className="inline-flex items-center gap-1 rounded-full border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-2.5 py-1 hover:border-[var(--border-strong)]"
                  >
                    {f.label}
                    <span aria-hidden="true">×</span>
                  </button>
                ))}
                <button
                  type="button"
                  onClick={clearFilters}
                  className="text-[var(--color-status-info)] hover:underline"
                >
                  Clear filters
                </button>
              </div>
            )}
          </div>
          {isPending && (
            <p className="py-12 text-center text-sm text-[var(--text-muted)]">
              Loading servers…
            </p>
          )}

          {isError && (
            <p className="rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
              {error instanceof ApiError
                ? error.problem.detail
                : error instanceof Error
                  ? error.message
                  : "Failed to load servers."}
            </p>
          )}

          {!isPending && !isError && (
            <>
              <InventoryTable
                servers={servers}
                sortField={sortField}
                sortDesc={sortDesc}
                onSortChange={handleSortChange}
                {...(activeFilters.length > 0
                  ? {
                      emptyMessage: `No servers match: ${activeFilters.map((f) => f.label).join(", ")}.`,
                    }
                  : {})}
              />

              <div className="mt-4 flex items-center gap-3">
                <button
                  type="button"
                  onClick={() => {
                    goToPage(page - 1);
                  }}
                  disabled={page <= 1}
                  className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-3 py-1.5 text-sm text-[var(--text-primary)] transition-transform duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-40 disabled:active:scale-100"
                >
                  Previous
                </button>
                <span className="text-sm text-[var(--text-secondary)]">
                  Page {page} of {pageCount}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    goToPage(page + 1);
                  }}
                  disabled={page >= pageCount}
                  className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-3 py-1.5 text-sm text-[var(--text-primary)] transition-transform duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-40 disabled:active:scale-100"
                >
                  Next
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </main>
  );
}
