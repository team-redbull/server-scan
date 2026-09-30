import type { UseInfiniteQueryResult } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import { EventTimeline } from "@/features/events/EventTimeline";
import type { AuditEventListResponse } from "@/types/events";

type Props = {
  query: UseInfiniteQueryResult<InfiniteData<AuditEventListResponse>, Error>;
  showServer?: boolean;
};

/** Loading / error / empty / "Load more" around an `EventTimeline`. */
export function EventList({ query, showServer }: Props) {
  const {
    data,
    isPending,
    isError,
    error,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
  } = query;
  if (isPending)
    return <p className="text-[var(--text-muted)]">Loading history…</p>;
  if (isError) {
    return (
      <p
        role="alert"
        className="rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300"
      >
        {error instanceof ApiError
          ? error.problem.detail
          : error.message || "Failed to load history."}
      </p>
    );
  }
  const events = data.pages.flatMap((p) => p.items);
  if (events.length === 0)
    return <p className="text-[var(--text-muted)]">No events recorded.</p>;
  return (
    <EventTimeline
      events={events}
      {...(showServer ? { showServer } : {})}
      footer={
        hasNextPage && (
          <button
            type="button"
            disabled={isFetchingNextPage}
            onClick={() => {
              void fetchNextPage();
            }}
            className="mt-3 rounded-md border border-[var(--border-subtle)] px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:opacity-50"
          >
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        )
      }
    />
  );
}
