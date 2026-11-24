import type { KeyboardEvent } from "react";
import { useSearchParams } from "react-router";

import { DIAGRAMS } from "@/features/architecture/diagrams";
import { HowItWorks } from "@/features/architecture/HowItWorks";

export const HOW_IT_WORKS_SLUG = "how-it-works";
const DEFAULT_SLUG = HOW_IT_WORKS_SLUG;

const PANEL_ID = "architecture-panel";
const tabId = (slug: string) => `architecture-tab-${slug}`;
const TAB_BASE =
  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)]";
const TAB_ON = "bg-[var(--tint-info)] text-[var(--text-on-info)]";
const TAB_OFF =
  "bg-[var(--surface-raised)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]";

/** How the platform is actually built, one interactive diagram per
 * collector plus the full flow — generated ahead of time (archify) from
 * the ADRs and provider source, not fetched from an API. Each diagram is
 * its own pan/zoom/search viewer; this page is only the picker around it. */
export function ArchitecturePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("diagram") ?? DEFAULT_SLUG;
  const active = DIAGRAMS.find((d) => d.slug === requested);

  function select(slug: string) {
    if (
      slug === (active?.slug ?? HOW_IT_WORKS_SLUG) &&
      searchParams.get("diagram") === slug
    )
      return;
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set("diagram", slug);
      return next;
    });
  }

  const slugs = [HOW_IT_WORKS_SLUG, ...DIAGRAMS.map((d) => d.slug)];
  const activeSlug = active?.slug ?? HOW_IT_WORKS_SLUG;

  function onTabKeyDown(event: KeyboardEvent<HTMLElement>) {
    const focused = (event.target as HTMLElement).closest('[role="tab"]')?.id;
    const at = Math.max(
      0,
      slugs.findIndex((slug) => tabId(slug) === focused),
    );
    const target =
      event.key === "ArrowRight"
        ? (at + 1) % slugs.length
        : event.key === "ArrowLeft"
          ? (at - 1 + slugs.length) % slugs.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? slugs.length - 1
              : -1;
    if (target < 0) return;
    event.preventDefault();
    const slug = slugs[target]!;
    select(slug);
    document.getElementById(tabId(slug))?.focus();
  }

  return (
    <main className="mx-auto max-w-7xl px-8 py-8">
      <h1 className="text-xl font-semibold tracking-tight text-[var(--text-primary)]">
        Architecture
      </h1>

      <div
        className="mt-6 flex flex-wrap gap-2"
        role="tablist"
        aria-label="Diagram"
        onKeyDown={onTabKeyDown}
      >
        {[{ slug: HOW_IT_WORKS_SLUG, label: "How it works" }, ...DIAGRAMS].map(
          (tab) => {
            const isActive = tab.slug === activeSlug;
            return (
              <button
                key={tab.slug}
                id={tabId(tab.slug)}
                type="button"
                role="tab"
                aria-selected={isActive}
                aria-controls={PANEL_ID}
                tabIndex={isActive ? 0 : -1}
                onClick={() => {
                  select(tab.slug);
                }}
                className={`${TAB_BASE} ${isActive ? TAB_ON : TAB_OFF}`}
              >
                {tab.label}
              </button>
            );
          },
        )}
      </div>

      <div role="tabpanel" id={PANEL_ID} aria-labelledby={tabId(activeSlug)}>
        {active === undefined ? (
          <HowItWorks />
        ) : (
          <>
            <p className="mt-3 text-sm text-[var(--text-secondary)]">
              {active.description}
            </p>

            <iframe
              key={active.slug}
              title={`${active.label} architecture diagram`}
              src={`/architecture/${active.slug}.html?theme=dark`}
              className="mt-4 h-[85vh] w-full rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)]"
            />
          </>
        )}
      </div>
    </main>
  );
}
