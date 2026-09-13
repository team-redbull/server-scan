import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import {
  disableMaintenance,
  enableMaintenance,
  getServerFacets,
  listServers,
} from "@/api/servers";
import type { ServerListParams } from "@/api/servers";
import type { ServerListResponse } from "@/types/server";

/** Server list query. `keepPreviousData` keeps the current rows on screen
 * while the next page is in flight. */
export function useServersQuery(params: ServerListParams) {
  return useQuery({
    queryKey: queryKeys.servers.list(params),
    queryFn: () => listServers(params),
    placeholderData: keepPreviousData,
  });
}

/** Per-option counts for the current filter set. */
export function useServerFacetsQuery(params: ServerListParams) {
  return useQuery({
    queryKey: queryKeys.servers.facets(params),
    queryFn: () => getServerFacets(params),
    placeholderData: keepPreviousData,
  });
}

/**
 * Toggle one server's maintenance mode — the app's only maintenance write
 * path. Row-agnostic: the id is a mutation variable, since a hook cannot
 * be called per row.
 *
 * Returns:
 *   The mutation, taking `{ id, enable, reason? }` where `enable` is the
 *   state to move the server *to*.
 */
export function useToggleMaintenanceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enable, reason }: { id: string; enable: boolean; reason?: string }) =>
      enable ? enableMaintenance(id, reason ? { reason } : {}) : disableMaintenance(id),
    onSuccess: (server) => {
      queryClient.setQueryData(queryKeys.servers.detail(server.id), server);
      // Patch, then refetch: under `?maintenance=true` the row must leave
      // the list. Safe because the server clears its list cache (ADR-0028).
      queryClient.setQueriesData<ServerListResponse>(
        { queryKey: queryKeys.servers.lists() },
        (page) =>
          page && {
            ...page,
            items: page.items.map((row) =>
              row.id === server.id ? { ...row, maintenance: server.maintenance } : row,
            ),
          },
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.servers.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.sites.all });
    },
  });
}
