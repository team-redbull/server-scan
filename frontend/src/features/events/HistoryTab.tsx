import { EventList } from "@/features/events/EventList";
import { useServerEventsInfiniteQuery } from "@/features/events/hooks";

/** The server detail page's History tab: its audit trail, newest first. */
export function HistoryTab({ serverId }: { serverId: string }) {
  return <EventList query={useServerEventsInfiniteQuery(serverId)} />;
}
