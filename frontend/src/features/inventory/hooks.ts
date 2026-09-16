import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import { disableMaintenance, enableMaintenance, listServerRows } from "@/api/servers";
import type { ServerRowsResponse } from "@/types/server";

/** The whole fleet as rows, re-polled every 30 s even on an unfocused wall
 * display. An unchanged fleet is a 304 the browser answers from its own
 * cache, and structural sharing then keeps every row's identity. */
export function useServerRowsQuery() {
  return useQuery({
    queryKey: queryKeys.servers.rows(),
    queryFn: listServerRows,
    refetchInterval: 30_000,
    refetchIntervalInBackground: true,
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
      // Patch, then refetch: under the Maintenance filter the row must
      // leave the list at once, not on the next poll.
      queryClient.setQueryData<ServerRowsResponse>(
        queryKeys.servers.rows(),
        (rows) =>
          rows && {
            ...rows,
            items: rows.items.map((row) =>
              row.id === server.id ? { ...row, maintenance: server.maintenance } : row,
            ),
          },
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.servers.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.sites.all });
    },
  });
}
