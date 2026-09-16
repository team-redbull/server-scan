import { useMemo } from "react";
import { useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { SORTABLE_FIELDS } from "@/features/inventory/sorting";
import type { SortableField } from "@/features/inventory/sorting";
import { InventoryTable } from "@/features/inventory/InventoryTable";
import { siteOptions, SOURCE_PROVIDERS, VENDORS } from "@/api/sites";
import { useServerRowsQuery } from "@/features/inventory/hooks";
import {
  facetCounts,
  filterRows,
  paginate,
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
const FIELD_CLASS =
  "mt-1 w-full min-w-0 rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-2.5 py-1.5 text-sm text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)]";

const DEFAULT_SORT: SortableField = "name";
const PAGE_SIZE = 50;

function isSortableField(value: string): value is SortableField {
  return (SORTABLE_FIELDS as readonly string[]).includes(value);
}

/** The inventory table. All filter and pagination state lives in the URL,
 * so refresh and back both land where the user was — a project requirement.
 * The fleet arrives whole and is filtered, sorted and paged here (ADR-0033). */
export function InventoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();

  const sites = siteOptions(useSitesQuery().data?.items);

  const searchInput = searchParams.get("search") ?? "";
  const debouncedSearch = useDebouncedValue(searchInput, 300);

  const vendor = searchParams.get("vendor") ?? "";
  const siteId = searchParams.get("site_id") ?? "";
  const installationType = searchParams.get("installation_type") ?? "";
  const openshiftState = searchParams.get("openshift_state") ?? "";
  const healthOverall = searchParams.get("health_overall") ?? "";
  const sourceProvider = searchParams.get("source_provider") ?? "";
  const maintenanceOnly = searchParams.get("maintenance") === "true";
  const staleOnly = searchParams.get("stale") === "true";
  const duplicateOnly = searchParams.get("duplicate") === "true";
  const sortParam = searchParams.get("sort") ?? "";
  const sortField: SortableField = isSortableField(sortParam)
    ? sortParam
    : DEFAULT_SORT;
  const sortDesc = searchParams.get("sort_desc") === "true";
  const pageParam = Number(searchParams.get("page") ?? "1");

  const filters: RowFilters = useMemo(() => {
    // Built incrementally: `exactOptionalPropertyTypes` forbids assigning
    // `undefined` to an optional property outright.
    const next: RowFilters = {};
    if (debouncedSearch) next.search = debouncedSearch;
    if (vendor) next.vendor = vendor;
    if (siteId) next.site_id = siteId;
    if (sourceProvider) next.source_provider = sourceProvider;
    if (installationType) next.installation_type = installationType;
    if (openshiftState) next.openshift_state = openshiftState;
    if (healthOverall) next.health = healthOverall;
    if (maintenanceOnly) next.maintenance = true;
    if (staleOnly) next.stale = true;
    if (duplicateOnly) next.duplicate = true;
    return next;
  }, [
    debouncedSearch,
    vendor,
    siteId,
    sourceProvider,
    installationType,
    openshiftState,
    healthOverall,
    maintenanceOnly,
    staleOnly,
    duplicateOnly,
  ]);

  const { data, isPending, isError, error } = useServerRowsQuery();
  const matched = useMemo(
    () => sortRows(filterRows(data?.items ?? [], filters), sortField, sortDesc),
    [data, filters, sortField, sortDesc],
  );
  const facets = useMemo(
    () => (data ? facetCounts(matched) : undefined),
    [data, matched],
  );
  const {
    items: servers,
    page,
    pageCount,
  } = paginate(matched, pageParam, PAGE_SIZE);

  /** Append a filter option's match count to its label — "(0)" included, so
   * an empty option is visibly empty. Silent only when this dimension is
   * already filtered (every other option would read as zero when it is
   * really unknown). */
  function withCount(
    label: string,
    counts: Record<string, number> | undefined,
    value: string,
    filtered: boolean,
  ): string {
    if (filtered || !counts) return label;
    return `${label} (${counts[value] ?? 0})`;
  }

  /** Apply a filter patch to the URL and go back to page 1. Built from the
   * live location, not the hook's `prev`: that is the last *render's* URL,
   * so two changes under ~100 ms apart would both start from it and the
   * second silently drop the first. `replace: true` keeps keystrokes out of
   * browser history; `flushSync` keeps the navigation out of a transition,
   * or a controlled checkbox snaps back until the deferred render lands. */
  function updateFilters(patch: Record<string, string | null>) {
    setSearchParams(
      () => {
        const next = new URLSearchParams(window.location.search);
        for (const [key, value] of Object.entries(patch)) {
          if (value === null || value === "") {
            next.delete(key);
          } else {
            next.set(key, value);
          }
        }
        next.delete("page");
        return next;
      },
      { replace: true, flushSync: true },
    );
  }

  function handleSortChange(field: SortableField, desc: boolean) {
    updateFilters({ sort: field, sort_desc: desc ? "true" : null });
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
  const activeFilters: { key: string; label: string }[] = [];
  if (debouncedSearch)
    activeFilters.push({ key: "search", label: `Search "${debouncedSearch}"` });
  if (vendor) activeFilters.push({ key: "vendor", label: `Vendor ${vendor}` });
  if (siteId) {
    activeFilters.push({
      key: "site_id",
      label: `Site ${sites.find((s) => s.value === siteId)?.label ?? siteId}`,
    });
  }
  if (sourceProvider) {
    activeFilters.push({
      key: "source_provider",
      label: `Source ${SOURCE_PROVIDERS.find((s) => s.value === sourceProvider)?.label ?? sourceProvider}`,
    });
  }
  if (installationType)
    activeFilters.push({
      key: "installation_type",
      label: `Classification ${installationType}`,
    });
  if (openshiftState) {
    activeFilters.push({
      key: "openshift_state",
      label: `Installation ${
        OPENSHIFT_STATES.find((s) => s.value === openshiftState)?.label ??
        openshiftState
      }`,
    });
  }
  if (healthOverall)
    activeFilters.push({
      key: "health_overall",
      label: `Health ${healthOverall}`,
    });
  if (maintenanceOnly)
    activeFilters.push({ key: "maintenance", label: "Maintenance" });
  if (staleOnly) activeFilters.push({ key: "stale", label: "Stale" });
  if (duplicateOnly)
    activeFilters.push({ key: "duplicate", label: "Duplicate" });

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
    });
  }

  return (
    <main className="mx-auto max-w-7xl px-8 py-8">
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

      <form
        className="mt-6 space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
        }}
      >
        {/* Two fixed rows: the fields share the first and shrink to fit, so a
            wide count can never wrap Health under Search or move the toggles. */}
        <div className="flex items-end gap-3">
          <label className="flex min-w-0 flex-[1.4] flex-col text-xs font-medium text-[var(--text-secondary)]">
            Search
            <input
              type="text"
              value={searchInput}
              onChange={(e) => {
                updateFilters({ search: e.target.value });
              }}
              placeholder="Name, serial, tag, BMC…"
              className={FIELD_CLASS}
            />
          </label>

          <div className="flex min-w-0 flex-1 flex-col text-xs font-medium text-[var(--text-secondary)]">
            <label htmlFor="filter-vendor">Vendor</label>
            <select
              id="filter-vendor"
              value={vendor}
              onChange={(e) => {
                updateFilters({ vendor: e.target.value });
              }}
              className={FIELD_CLASS}
            >
              <option value="">All{facets ? ` (${facets.total})` : ""}</option>
              {VENDORS.map((v) => (
                <option key={v} value={v}>
                  {withCount(v, facets?.vendor, v, vendor !== "")}
                </option>
              ))}
            </select>
          </div>

          <div className="flex min-w-0 flex-1 flex-col text-xs font-medium text-[var(--text-secondary)]">
            <label htmlFor="filter-source">Source</label>
            <select
              id="filter-source"
              value={sourceProvider}
              onChange={(e) => {
                updateFilters({ source_provider: e.target.value });
              }}
              className={FIELD_CLASS}
            >
              <option value="">All{facets ? ` (${facets.total})` : ""}</option>
              {SOURCE_PROVIDERS.map((s) => (
                <option key={s.value} value={s.value}>
                  {withCount(
                    s.label,
                    facets?.source_provider,
                    s.value,
                    sourceProvider !== "",
                  )}
                </option>
              ))}
            </select>
          </div>

          <div className="flex min-w-0 flex-1 flex-col text-xs font-medium text-[var(--text-secondary)]">
            <label htmlFor="filter-site">Site</label>
            <select
              id="filter-site"
              value={siteId}
              onChange={(e) => {
                updateFilters({ site_id: e.target.value });
              }}
              className={FIELD_CLASS}
            >
              <option value="">
                All sites{facets ? ` (${facets.total})` : ""}
              </option>
              {sites.map((site) => (
                <option key={site.value} value={site.value}>
                  {withCount(
                    site.label,
                    facets?.site_id,
                    site.value,
                    siteId !== "",
                  )}
                </option>
              ))}
            </select>
          </div>

          <div className="flex min-w-0 flex-1 flex-col text-xs font-medium text-[var(--text-secondary)]">
            <label htmlFor="filter-classification">Classification</label>
            <select
              id="filter-classification"
              value={installationType}
              onChange={(e) => {
                updateFilters({ installation_type: e.target.value });
              }}
              className={FIELD_CLASS}
            >
              <option value="">All{facets ? ` (${facets.total})` : ""}</option>
              {INSTALLATION_TYPES.map((t) => (
                <option key={t} value={t}>
                  {withCount(
                    t,
                    facets?.installation_type,
                    t,
                    installationType !== "",
                  )}
                </option>
              ))}
            </select>
          </div>

          <div className="flex min-w-0 flex-1 flex-col text-xs font-medium text-[var(--text-secondary)]">
            <label htmlFor="filter-installation">Installation</label>
            <select
              id="filter-installation"
              value={openshiftState}
              onChange={(e) => {
                updateFilters({ openshift_state: e.target.value });
              }}
              className={FIELD_CLASS}
            >
              <option value="">All{facets ? ` (${facets.total})` : ""}</option>
              {OPENSHIFT_STATES.map((s) => (
                <option key={s.value} value={s.value}>
                  {withCount(
                    s.label,
                    facets?.openshift_state,
                    s.value,
                    openshiftState !== "",
                  )}
                </option>
              ))}
            </select>
          </div>

          <div className="flex min-w-0 flex-1 flex-col text-xs font-medium text-[var(--text-secondary)]">
            <label htmlFor="filter-health">Health</label>
            <select
              id="filter-health"
              value={healthOverall}
              onChange={(e) => {
                updateFilters({ health_overall: e.target.value });
              }}
              className={FIELD_CLASS}
            >
              <option value="">All{facets ? ` (${facets.total})` : ""}</option>
              {HEALTH_SEVERITIES.map((h) => (
                <option key={h} value={h}>
                  {withCount(h, facets?.health, h, healthOverall !== "")}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-6">
          <label className="flex items-center gap-2 text-xs font-medium text-[var(--text-secondary)]">
            <input
              type="checkbox"
              checked={maintenanceOnly}
              onChange={(e) => {
                updateFilters({
                  maintenance: e.target.checked ? "true" : null,
                });
              }}
            />
            Maintenance
            {maintenanceOnly && facets ? ` (${facets.total})` : ""}
          </label>

          <label
            className="flex items-center gap-2 text-xs font-medium text-[var(--text-secondary)]"
            title="Not collected within the staleness window (INVENTORY_STALE_AFTER_SECONDS), or never"
          >
            <input
              type="checkbox"
              checked={staleOnly}
              onChange={(e) => {
                updateFilters({ stale: e.target.checked ? "true" : null });
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
                updateFilters({ duplicate: e.target.checked ? "true" : null });
              }}
            />
            Duplicate{duplicateOnly && facets ? ` (${facets.total})` : ""}
          </label>
        </div>
      </form>

      {activeFilters.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-[var(--text-secondary)]">
          <span>Filtered by:</span>
          {activeFilters.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => {
                updateFilters({ [f.key]: null });
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

      <div className="mt-4">
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
    </main>
  );
}
