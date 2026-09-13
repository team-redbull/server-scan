import type { KeyboardEvent, MouseEvent, ReactNode } from "react";
import { Link, useNavigate } from "react-router";

import { UNASSIGNED_SITE_ID, vendorLabel } from "@/api/sites";
import type { Breakdown, FleetSummary, SiteStats } from "@/api/sites";
import { SEVERITY_GLYPH } from "@/components/severity";
import { useSitesQuery } from "@/features/sites/hooks";
import type { HealthSeverity } from "@/types/server";

/**
 * The landing page: a fleet-wide row (everything, UPI, hosted cluster)
 * above one card per site, each summarising what is in it.
 *
 * This is the entry point rather than the server list because at ~10,000
 * servers a flat list has no answer to "is anything wrong?" — you would
 * have to sort and scan. Cards answer it in one glance, and each one is a
 * link that pre-filters the list, so drilling in is one click and never
 * requires touching a filter control.
 */

/** What one card renders: a name, the counts behind it, and where it
 * drills into. Every card on this page is the same component — the only
 * thing that differs between "the whole fleet", "the UPI fleet" and "Tel
 * Aviv" is which slice of the response was summed and which filter the
 * link carries. */
interface CardSpec {
  key: string;
  name: string;
  subtitle: string;
  to: string;
  stats: Breakdown;
}

/**
 * The fleet-wide cards: everything, each installation type, then what
 * OpenShift reports actually holding the fleet.
 *
 * `unassigned` is included in all of them — those servers are in the
 * fleet whatever their hostname says. The numbers come straight off the
 * backend's own `fleet` summary (`app.api.v1.sites._pivot`, folded from
 * the same aggregation rows the per-site cards are), not summed here —
 * so this page can never disagree with a second consumer of the same
 * endpoint.
 *
 * Args:
 *   fleet: the fleet-wide summary as returned by `GET /api/v1/sites`.
 *
 * Returns:
 *   CardSpec[]: the top row, in fixed order.
 */
function fleetCards(fleet: FleetSummary): CardSpec[] {
  return [
    {
      key: "__all__",
      name: "Across all sites",
      subtitle: "servers, every site",
      to: "/servers",
      stats: fleet,
    },
    {
      key: "UPI",
      name: "UPI",
      subtitle: "servers, every site",
      to: "/servers?installation_type=UPI",
      stats: fleet.by_installation_type.UPI,
    },
    {
      key: "HOSTED_CLUSTER",
      name: "Hosted cluster",
      subtitle: "servers, every site",
      to: "/servers?installation_type=HOSTED_CLUSTER",
      stats: fleet.by_installation_type.HOSTED_CLUSTER,
    },
    {
      key: "MCE",
      name: "MCE",
      subtitle: "servers, every site",
      to: "/servers?installation_type=MCE",
      stats: fleet.by_installation_type.MCE,
    },
    // What a cluster reports holding, not what a name claims — ADR-0024.
    {
      key: "AVAILABLE",
      name: "Available",
      subtitle: "no cluster holds these",
      to: "/servers?openshift_state=AVAILABLE",
      stats: fleet.by_openshift_state.AVAILABLE,
    },
    {
      key: "INSTALLED",
      name: "Installed",
      subtitle: "in use by a cluster",
      to: "/servers?openshift_state=INSTALLED",
      stats: fleet.by_openshift_state.INSTALLED,
    },
  ];
}

/**
 * One card per configured site, in the order the backend returned them.
 *
 * Args:
 *   items: the per-site records as returned by `GET /api/v1/sites`.
 *
 * Returns:
 *   CardSpec[]: the per-site row.
 */
function siteCards(items: SiteStats[]): CardSpec[] {
  // Unassigned is dropped when empty, unlike a configured site, which
  // still renders at zero: "no hostname failed to parse" is the expected
  // state, and a permanent empty card for it is noise.
  return items
    .filter((site) => site.site_id !== UNASSIGNED_SITE_ID || site.total > 0)
    .map((site) => ({
      key: site.site_id,
      name: site.name,
      subtitle:
        site.site_id === UNASSIGNED_SITE_ID ? "no site in hostname" : "servers",
      to: `/servers?site_id=${site.site_id}`,
      stats: site,
    }));
}

/** Bar widths are proportional to the card's own total, not to the
 * largest card — each one answers "what is the mix HERE", and
 * normalising across cards would make a small site's mix unreadable. */
function VendorBar({ stats }: { stats: Breakdown }) {
  if (stats.total === 0) {
    return null;
  }
  return (
    <div className="mt-4 space-y-1.5">
      {stats.by_vendor.map((entry) => {
        const percent = Math.round((entry.count / stats.total) * 100);
        return (
          <div key={entry.vendor} className="flex items-center gap-2.5 text-xs">
            <span className="w-16 shrink-0 text-[var(--text-secondary)]">
              {vendorLabel(entry.vendor)}
            </span>
            <span
              className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--surface-sunken)]"
              aria-hidden="true"
            >
              <span
                className="block h-full rounded-full bg-[var(--border-strong)]"
                style={{ width: `${percent}%` }}
              />
            </span>
            <span className="tabular w-9 shrink-0 text-right text-[var(--text-secondary)]">
              {entry.count}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** A card's link filters to the site/installation-type alone; this adds
 * `health_overall` on top, so "3 critical" goes straight to those 3
 * servers instead of to all of them. */
function withHealthFilter(to: string, severity: HealthSeverity): string {
  return `${to}${to.includes("?") ? "&" : "?"}health_overall=${severity}`;
}

/** A count that drills into a filtered list, nested inside the card's own
 * `<Link>` — real anchors cannot nest, so this is a `role="link"` span
 * that navigates itself and stops the click from also firing the card's
 * outer link. */
function CountLink({ to, className, children }: { to: string; className: string; children: ReactNode }) {
  const navigate = useNavigate();
  function go(e: MouseEvent | KeyboardEvent) {
    e.preventDefault();
    e.stopPropagation();
    void navigate(to);
  }
  return (
    <span
      role="link"
      tabIndex={0}
      className={className}
      onClick={go}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") go(e);
      }}
    >
      {children}
    </span>
  );
}

/** `emphasis` marks the fleet-wide row. It is a different kind of thing
 * from a site — a stronger border says so without a second card design. */
function SiteCard({ card, emphasis }: { card: CardSpec; emphasis?: boolean }) {
  const { stats } = card;
  const critical = stats.by_health.CRITICAL;
  const major = stats.by_health.MAJOR;
  const warning = stats.by_health.WARNING;
  const unknown = stats.by_health.UNKNOWN;
  const info = stats.by_health.INFO;

  return (
    <Link
      to={card.to}
      className={`group block rounded-[var(--radius-card)] border bg-[var(--surface-raised)] p-5 transition-[border-color,transform] duration-[var(--duration-fast)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] active:scale-[0.995] ${
        emphasis
          ? "border-[var(--border-strong)]"
          : "border-[var(--border-subtle)]"
      }`}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold tracking-tight text-[var(--text-primary)]">
          {card.name}
        </h3>
        <span className="tabular text-2xl font-semibold text-[var(--text-primary)]">
          {stats.total.toLocaleString()}
        </span>
      </div>

      <p className="mt-0.5 text-xs text-[var(--text-muted)]">{card.subtitle}</p>

      {/* Only surface counts that mean "look at this". A row of zeroes
       * across five cards is noise that trains people to skip the card. */}
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        {critical > 0 && (
          <CountLink
            to={withHealthFilter(card.to, "CRITICAL")}
            className="inline-flex cursor-pointer items-center gap-1.5 font-medium text-[var(--text-on-critical)] underline-offset-2 hover:underline"
          >
            <span aria-hidden="true">{SEVERITY_GLYPH.CRITICAL}</span>
            <span className="tabular">{critical}</span> critical
          </CountLink>
        )}
        {major > 0 && (
          <CountLink
            to={withHealthFilter(card.to, "MAJOR")}
            className="inline-flex cursor-pointer items-center gap-1.5 font-medium text-[var(--text-on-major)] underline-offset-2 hover:underline"
          >
            <span aria-hidden="true">{SEVERITY_GLYPH.MAJOR}</span>
            <span className="tabular">{major}</span> major
          </CountLink>
        )}
        {warning > 0 && (
          <CountLink
            to={withHealthFilter(card.to, "WARNING")}
            className="inline-flex cursor-pointer items-center gap-1.5 font-medium text-[var(--text-on-warning)] underline-offset-2 hover:underline"
          >
            <span aria-hidden="true">{SEVERITY_GLYPH.WARNING}</span>
            <span className="tabular">{warning}</span> warning
          </CountLink>
        )}
        {stats.in_maintenance > 0 && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-maintenance)]">
            <span aria-hidden="true">⏸</span>
            <span className="tabular">{stats.in_maintenance}</span> in
            maintenance
          </span>
        )}
        {unknown > 0 && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-unknown)]">
            <span aria-hidden="true">{SEVERITY_GLYPH.UNKNOWN}</span>
            <span className="tabular">{unknown}</span> unknown
          </span>
        )}
        {info > 0 && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-info)]">
            <span aria-hidden="true">{SEVERITY_GLYPH.INFO}</span>
            <span className="tabular">{info}</span> info
          </span>
        )}
        {/* Only a site every server in which was actually evaluated
         * HEALTHY earns this — never a byproduct of critical/warning
         * both being zero, which is also true of a site nothing has
         * evaluated yet. */}
        {stats.total > 0 && stats.by_health.HEALTHY === stats.total && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-healthy)]">
            <span aria-hidden="true">{SEVERITY_GLYPH.HEALTHY}</span> all healthy
          </span>
        )}
        {stats.total === 0 && (
          <span className="text-[var(--text-muted)]">empty</span>
        )}
      </div>

      <VendorBar stats={stats} />
    </Link>
  );
}

function SkeletonCard() {
  return (
    <div
      className="h-[184px] rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--surface-raised)]"
      aria-hidden="true"
    />
  );
}

function SectionHeading({ children }: { children: string }) {
  return (
    <h2 className="mb-3 text-xs font-medium tracking-wide text-[var(--text-muted)] uppercase">
      {children}
    </h2>
  );
}

export function SitesOverviewPage() {
  const { data, isPending, isError, error } = useSitesQuery();

  const fleet = data ? fleetCards(data.fleet) : [];
  const sites = data ? siteCards(data.items) : [];
  const fleetTotal = fleet[0]?.stats.total ?? 0;

  return (
    <main className="mx-auto max-w-7xl px-8 py-8">
      <header className="mb-6">
        <h1 className="text-xl font-semibold tracking-tight text-[var(--text-primary)]">
          Sites
        </h1>
        <p className="mt-1 text-sm text-[var(--text-secondary)]">
          {isPending ? (
            "Loading fleet…"
          ) : (
            <>
              <span className="tabular font-medium text-[var(--text-primary)]">
                {fleetTotal.toLocaleString()}
              </span>{" "}
              servers across{" "}
              {data?.items.filter((s) => s.site_id !== UNASSIGNED_SITE_ID)
                .length ?? 0}{" "}
              sites
            </>
          )}
        </p>
      </header>

      {isError && (
        <p
          role="alert"
          className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--tint-critical)] px-4 py-3 text-sm text-[var(--text-on-critical)]"
        >
          Could not load sites:{" "}
          {error instanceof Error ? error.message : "unknown error"}
        </p>
      )}

      <section className="mb-8">
        <SectionHeading>Fleet</SectionHeading>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {isPending
            ? // Fixed-height placeholders matching the real card, so the
              // grid does not reflow when data lands.
              [0, 1, 2].map((i) => <SkeletonCard key={i} />)
            : fleet.map((card) => (
                <SiteCard key={card.key} card={card} emphasis />
              ))}
        </div>
      </section>

      <section>
        <SectionHeading>Sites</SectionHeading>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {isPending
            ? [0, 1, 2, 3, 4, 5].map((i) => <SkeletonCard key={i} />)
            : sites.map((card) => <SiteCard key={card.key} card={card} />)}
        </div>
      </section>
    </main>
  );
}
