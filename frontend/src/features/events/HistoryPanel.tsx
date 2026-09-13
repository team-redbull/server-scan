import { useQueries } from "@tanstack/react-query";
import { Fragment } from "react";

import { ApiError } from "@/api/client";
import { listEvents } from "@/api/events";
import { queryKeys } from "@/api/queryKeys";
import { formatTimestamp } from "@/lib/datetime";
import type { AuditEventResponse } from "@/types/events";

interface HistoryPanelProps {
  /** One request per type — `GET /events` has no multi-value filter. */
  eventTypes: string[];
  /** The `data` key carrying the entity id ("rule_id", "policy_id"). */
  idField: string;
  entityId: string;
}

function formatValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.length > 0 ? value.join(", ") : "—";
  }
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

/**
 * Audit-trail timeline for one rule/policy. `GET /events` cannot filter on
 * `data.rule_id`/`policy_id`, so this fetches a page per `event_type` and
 * filters client-side — deliberate at current audit-log volumes.
 */
export function HistoryPanel({ eventTypes, idField, entityId }: HistoryPanelProps) {
  const results = useQueries({
    queries: eventTypes.map((eventType) => ({
      queryKey: queryKeys.events.list({ event_type: eventType, page_size: 200 }),
      queryFn: () => listEvents({ event_type: eventType, page_size: 200 }),
      // No mutation invalidates the events cache, so every mount refetches.
      staleTime: 0,
    })),
  });

  const isPending = results.some((r) => r.isPending);
  const errored = results.find((r) => r.isError);

  const events: AuditEventResponse[] = results
    .flatMap((r) => r.data?.items ?? [])
    .filter((event) => event.data[idField] === entityId)
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());

  return (
    <section className="mt-8">
      <h2 className="text-sm font-medium uppercase tracking-wide text-gray-500">History</h2>

      {isPending && <p className="mt-2 text-gray-500">Loading history…</p>}

      {errored && (
        <p className="mt-2 rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
          {errored.error instanceof ApiError
            ? errored.error.problem.detail
            : errored.error instanceof Error
              ? errored.error.message
              : "Failed to load history."}
        </p>
      )}

      {!isPending && !errored && events.length === 0 && (
        <p className="mt-2 text-gray-500">No history recorded yet.</p>
      )}

      {!isPending && !errored && events.length > 0 && (
        <ul className="mt-2 space-y-2">
          {events.map((event) => (
            <li
              key={event.id}
              className="rounded border border-gray-200 p-3 text-sm dark:border-gray-700"
            >
              <div className="flex items-center justify-between gap-4">
                <span className="font-medium">{event.event_type}</span>
                <span className="shrink-0 text-xs text-gray-500">
                  {formatTimestamp(event.created_at)}
                </span>
              </div>
              <p className="mt-1 text-xs text-gray-500">
                {event.actor.display ?? event.actor.id} ({event.actor.type})
              </p>
              {Object.keys(event.data).filter((k) => k !== idField).length > 0 && (
                <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-2 gap-y-1 text-xs text-gray-500">
                  {Object.entries(event.data)
                    .filter(([key]) => key !== idField)
                    .map(([key, value]) => (
                      <Fragment key={key}>
                        <dt className="font-medium">{key}</dt>
                        <dd>{formatValue(value)}</dd>
                      </Fragment>
                    ))}
                </dl>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
