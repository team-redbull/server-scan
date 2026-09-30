import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { listEventActors, listEvents, listServerEvents } from "@/api/events";
import type { EventListParams } from "@/api/events";
import { queryKeys } from "@/api/queryKeys";

const PAGE_SIZE = 50;

/** Cursor-paged `GET /events`; pages are flattened by the caller. */
export function useEventsInfiniteQuery(
  params: Omit<EventListParams, "cursor" | "page_size">,
) {
  return useInfiniteQuery({
    queryKey: [...queryKeys.events.list(params), "infinite"],
    queryFn: ({ pageParam }) =>
      listEvents({
        ...params,
        page_size: PAGE_SIZE,
        ...(pageParam ? { cursor: pageParam } : {}),
      }),
    initialPageParam: "",
    getNextPageParam: (last) =>
      last.page.has_more ? (last.page.next_cursor ?? undefined) : undefined,
  });
}

/** Cursor-paged `GET /servers/{id}/events`. */
export function useServerEventsInfiniteQuery(serverId: string) {
  return useInfiniteQuery({
    queryKey: [
      ...queryKeys.events.list({ server_id: serverId }),
      "server-infinite",
    ],
    queryFn: ({ pageParam }) =>
      listServerEvents(serverId, {
        page_size: PAGE_SIZE,
        ...(pageParam ? { cursor: pageParam } : {}),
      }),
    initialPageParam: "",
    getNextPageParam: (last) =>
      last.page.has_more ? (last.page.next_cursor ?? undefined) : undefined,
    enabled: serverId.length > 0,
  });
}

/** The User filter's options; the list changes only when someone first acts. */
export function useEventActorsQuery() {
  return useQuery({
    queryKey: queryKeys.events.actors(),
    queryFn: listEventActors,
    staleTime: 60_000,
  });
}
