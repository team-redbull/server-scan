import { useState } from "react";
import type { UseInfiniteQueryResult } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import { EventTimeline } from "@/features/events/EventTimeline";
import type { AuditEventListResponse } from "@/types/events";

type Props = {
  query: UseInfiniteQueryResult<InfiniteData<AuditEventListResponse>, Error>;
  showServer?: boolean;
};

const PAGER_BUTTON =
  "rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-3 py-1.5 text-sm text-[var(--text-primary)] hover:border-[var(--border-strong)] disabled:cursor-not-allowed disabled:opacity-40";

/** Loading / error / empty / Previous-Next pager around an `EventTimeline`. */
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
  const [index, setIndex] = useState(0);
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
  const events = (data.pages[index] ?? data.pages[0])?.items ?? [];
  const first = data.pages[0]?.page;
  const pageCount = first?.total
    ? Math.max(1, Math.ceil(first.total / first.page_size))
    : data.pages.length + (hasNextPage ? 1 : 0);
  const canNext = index + 1 < data.pages.length || hasNextPage;
  if (data.pages[0]?.items.length === 0)
    return <p className="text-[var(--text-muted)]">No events recorded.</p>;
  return (
    <EventTimeline
      events={events}
      {...(showServer ? { showServer } : {})}
      footer={
        <div className="mt-3 flex items-center gap-3">
          <button
            type="button"
            disabled={index <= 0}
            onClick={() => {
              setIndex(index - 1);
            }}
            className={PAGER_BUTTON}
          >
            Previous
          </button>
          <span className="text-sm text-[var(--text-secondary)]">
            Page {index + 1} of {pageCount}
          </span>
          <button
            type="button"
            disabled={!canNext || isFetchingNextPage}
            onClick={() => {
              if (index + 1 < data.pages.length) setIndex(index + 1);
              else
                void fetchNextPage().then(() => {
                  setIndex(index + 1);
                });
            }}
            className={PAGER_BUTTON}
          >
            {isFetchingNextPage ? "Loading…" : "Next"}
          </button>
        </div>
      }
    />
  );
}
