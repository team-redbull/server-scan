import { useSearchParams } from "react-router";

import { DIAGRAMS } from "@/features/architecture/diagrams";

const DEFAULT_SLUG = DIAGRAMS[0]!.slug;

/** How the platform is actually built, one interactive diagram per
 * collector plus the full flow — generated ahead of time (archify) from
 * the ADRs and provider source, not fetched from an API. Each diagram is
 * its own pan/zoom/search viewer; this page is only the picker around it. */
export function ArchitecturePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("diagram") ?? DEFAULT_SLUG;
  const active = DIAGRAMS.find((d) => d.slug === requested) ?? DIAGRAMS[0]!;

  function select(slug: string) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set("diagram", slug);
      return next;
    });
  }

  return (
    <main className="mx-auto max-w-7xl px-8 py-8">
      <h1 className="text-xl font-semibold tracking-tight text-[var(--text-primary)]">
        Architecture
      </h1>
      <p className="mt-1 text-sm text-[var(--text-secondary)]">
        How data moves through the platform — pan, zoom, search and trace each
        diagram; every fact is sourced from an ADR or the provider it names.
      </p>

      <div className="mt-6 flex flex-wrap gap-2" role="tablist" aria-label="Diagram">
        {DIAGRAMS.map((diagram) => {
          const isActive = diagram.slug === active.slug;
          return (
            <button
              key={diagram.slug}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => {
                select(diagram.slug);
              }}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)] ${
                isActive
                  ? "bg-[var(--tint-info)] text-[var(--text-on-info)]"
                  : "bg-[var(--surface-raised)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              }`}
            >
              {diagram.label}
            </button>
          );
        })}
      </div>

      <p className="mt-3 text-sm text-[var(--text-secondary)]">{active.description}</p>

      <iframe
        key={active.slug}
        title={`${active.label} architecture diagram`}
        src={`/architecture/${active.slug}.html?theme=dark`}
        className="mt-4 h-[85vh] w-full rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)]"
      />
    </main>
  );
}
