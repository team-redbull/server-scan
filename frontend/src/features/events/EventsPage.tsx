import { useMemo } from "react";
import { useSearchParams } from "react-router";

import { EventList } from "@/features/events/EventList";
import { WRITTEN_EVENT_TYPES } from "@/features/events/describeEvent";
import {
  filtersToParams,
  normalizeEventType,
  RANGE_PRESETS,
} from "@/features/events/filters";
import {
  useEventActorsQuery,
  useEventsInfiniteQuery,
} from "@/features/events/hooks";
import { actorLabel } from "@/lib/actor";
import { parseIsraelInput } from "@/lib/datetime";
import { useDebouncedValue } from "@/lib/useDebouncedValue";

const FIELD_CLASS =
  "mt-1 w-full min-w-0 rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-2.5 py-1.5 text-sm text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)]";
const DATE_HINT = "DD/MM/YYYY HH:MM";

/** `/events`: the platform-wide audit trail, filterable, newest first. */
export function EventsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const server = searchParams.get("server_name") ?? "";
  const eventType = normalizeEventType(searchParams.get("event_type") ?? "");
  const actorId = searchParams.get("actor_id") ?? "";
  const range = searchParams.get("range") ?? "";
  const from = searchParams.get("from") ?? "";
  const to = searchParams.get("to") ?? "";
  const debServer = useDebouncedValue(server, 300);
  const debFrom = useDebouncedValue(from, 300);
  const debTo = useDebouncedValue(to, 300);

  // A preset's `since` is fixed when the preset is picked, so the query key is stable.
  const params = useMemo(
    () =>
      filtersToParams({
        server: debServer,
        eventType,
        actorId,
        range,
        from: debFrom,
        to: debTo,
      }),
    [debServer, eventType, actorId, range, debFrom, debTo],
  );
  const query = useEventsInfiniteQuery(params);
  const actors = useEventActorsQuery().data?.items ?? [];

  function setParam(key: string, value: string) {
    setSearchParams(
      () => {
        const next = new URLSearchParams(window.location.search);
        if (value) next.set(key, value);
        else next.delete(key);
        return next;
      },
      { replace: true, flushSync: true },
    );
  }

  const badDate = (text: string) =>
    text.trim() !== "" && !parseIsraelInput(text);

  return (
    <main className="mx-auto max-w-5xl p-8">
      <h1 className="text-2xl font-semibold">Events</h1>
      <p className="mt-1 text-sm text-[var(--text-muted)]">
        The audit trail, newest first. Times are Israel time.
      </p>
      <div className="mt-4 flex flex-wrap items-end gap-3 text-xs font-medium text-[var(--text-secondary)]">
        <div className="flex min-w-48 flex-1 flex-col">
          <label htmlFor="events-type">Event type</label>
          <select
            id="events-type"
            value={eventType}
            onChange={(e) => {
              setParam("event_type", e.target.value);
            }}
            className={FIELD_CLASS}
          >
            <option value="">All</option>
            {WRITTEN_EVENT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
        <div className="flex min-w-48 flex-1 flex-col">
          <label htmlFor="events-server">Server</label>
          <input
            id="events-server"
            type="text"
            value={server}
            placeholder="Server name"
            onChange={(e) => {
              setParam("server_name", e.target.value);
            }}
            className={FIELD_CLASS}
          />
        </div>
        <div className="flex min-w-48 flex-1 flex-col">
          <label htmlFor="events-actor">User</label>
          <select
            id="events-actor"
            value={actorId}
            onChange={(e) => {
              setParam("actor_id", e.target.value);
            }}
            className={FIELD_CLASS}
          >
            <option value="">All</option>
            {actors.map((a) => (
              <option key={a.id} value={a.id}>
                {actorLabel(a).label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex min-w-48 flex-1 flex-col">
          <label htmlFor="events-range">Time range</label>
          <select
            id="events-range"
            value={range}
            onChange={(e) => {
              setParam("range", e.target.value);
            }}
            className={FIELD_CLASS}
          >
            <option value="">Any time</option>
            {RANGE_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
            <option value="custom">Custom</option>
          </select>
        </div>
        {range === "custom" && (
          <>
            {(
              [
                ["events-from", "From", "from", from],
                ["events-to", "To (exclusive)", "to", to],
              ] as const
            ).map(([id, label, key, value]) => (
              <div key={id} className="flex min-w-44 flex-1 flex-col">
                <label htmlFor={id}>{label} (Israel time)</label>
                <input
                  id={id}
                  type="text"
                  value={value}
                  placeholder={DATE_HINT}
                  aria-invalid={badDate(value)}
                  onChange={(e) => {
                    setParam(key, e.target.value);
                  }}
                  className={FIELD_CLASS}
                />
                {badDate(value) && (
                  <span className="mt-1 text-[var(--text-muted)]">
                    Use {DATE_HINT}
                  </span>
                )}
              </div>
            ))}
          </>
        )}
      </div>
      <div className="mt-6">
        <EventList query={query} showServer />
      </div>
    </main>
  );
}
